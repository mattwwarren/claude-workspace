#!/usr/bin/env python3
"""Pre-flight checks for /cw-smoke-test.

Verifies the environment is healthy enough to dispatch ``/auto-dev`` against
a single ticket. Emits one JSON object on stdout with ``ok`` (bool) and
``checks`` (list of ``{name, passed, severity, detail}``). Exits 0 when no
``severity=hard`` check failed (soft warnings still report ``ok=true``);
exits 1 when at least one hard check failed.

Each check distinguishes ``severity``:
- ``hard``  — must pass; failure aborts the smoke test.
- ``soft``  — warning only; surfaces in the report but does not abort.

Checks performed:
- ``agents_present``        (hard)  plan-reviewer + plan-soundness-reviewer
                                   exist under one of ``~/.claude/agents/``
                                   or the repo-local ``.claude/agents/``.
- ``cw_backend_healthy``    (hard)  backend-core ``cw doctor`` checks pass
                                   (sessions.json, dev_queue.json, clients.yaml,
                                   claude-version, daemon-reachable). Non-core
                                   failures (project-config, linkage, workspace)
                                   do not block.
- ``cw_doctor_clean``       (soft)  ``cw doctor`` reports zero issues.
- ``ticket_open``           (hard)  ``gh issue view <id>`` returns
                                   ``state=OPEN`` (github-issues tracker).
                                   Soft-skipped for non-github trackers, whose
                                   ticket store is unreachable from a script.
- ``no_open_pr_for_ticket`` (hard)  github-issues: ``gh pr list --search``
                                   finds no in-flight PR referencing the
                                   ticket. Other trackers: keyed off the
                                   ``auto-dev/<id>`` branch head instead.

The tracker and the GitHub ``owner/repo`` slug are both resolved from
``--client``'s repo root (via ``clients.yaml``, the same way ``cw doctor``
resolves a client's repo) rather than the script's own on-disk location:
- ``client_repo_resolved`` (hard) ``--client`` names an entry in
                                  ``clients.yaml`` (or ``clients.yaml``
                                  doesn't exist at all, single-tenant mode).
                                  A populated ``clients.yaml`` missing the
                                  named client fails loudly instead of
                                  silently falling back to a default repo.
- ``repo_resolved``        (hard) a GitHub ``owner/repo`` slug was derived
                                  from the resolved repo root's ``origin``
                                  remote, or supplied explicitly via
                                  ``--repo``. Only skipped when a slug was
                                  resolved.

Tracker resolution reads ``.claude/project-config.yaml``
(``tracking.primary.system``) from that same client repo root;
absent/unrecognized falls back to ``linear``.
- ``not_already_queued``    (hard)  the ticket is not already RUNNING or
                                   PENDING in ``cw dev-queue status``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# sys.path bootstrap: must run before any `cw` import so this standalone script
# works under bare python3 too (not just `uv run`), matching the sibling
# skill scripts (cw-followup/scripts/parse_sentinel.py,
# cw-validate-result/scripts/validate_sentinel.py).
#
# Why: this cannot be extracted to a shared module — it must run BEFORE any cw
# import, so there is no shared cw path yet to import it from. Each standalone
# script that imports cw carries its own copy. Do not deduplicate.


def _bootstrap_sys_path() -> None:
    """Add <repo>/src to sys.path so ``cw`` is importable under bare python3.

    This script's documented invocation (``cw-smoke-test/SKILL.md``) runs via
    ``uv run --project "$(git rev-parse --show-toplevel)" python ...`` --
    inside the repo venv, where ``cw`` is editable-installed and its compiled
    deps (e.g. ``pydantic_core``) are ABI-matched to that interpreter. Under a
    bare ``/usr/bin/python3`` this bootstrap does NOT make ``cw`` importable:
    the failure is missing dependencies, not ``sys.path`` reachability
    (``import pydantic`` fails there too, #1598).
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            src = str(parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            return
    msg = (
        f"Could not locate pyproject.toml walking up from {__file__} — bootstrap failed"
    )
    raise RuntimeError(msg)


_bootstrap_sys_path()

from cw.config import clients_file, load_clients
from cw.exceptions import CwError
from cw.pr_hydrate import _resolve_repo_slug as _cw_resolve_repo_slug
from cw.tracker import resolve_tracker as _cw_resolve_tracker
from cw.worktree import _git_dir

_GLOBAL_AGENTS = Path.home() / ".claude" / "agents"
_REQUIRED_AGENTS = ("plan-reviewer.md", "plan-soundness-reviewer.md")
_DEV_QUEUE_BLOCKING_STATES = {"PENDING", "RUNNING", "CLAIMED"}

# Check names from ``cw doctor --json`` that indicate the backend core is broken.
# Failures in other checks (project-config, workspace, linkage) do not block the
# smoke test — the backend can still dispatch even when tracker config is missing.
_BACKEND_CORE_CHECKS = frozenset(
    {
        "sessions.json",
        "dev_queue.json",
        "clients.yaml",
        "claude-version",
        "daemon-reachable",
    }
)

# Tracker resolution: honor .claude/project-config.yaml rather than assuming gh.
_RECOGNIZED_TRACKERS = ("github-issues", "linear")
_DEFAULT_TRACKER = "linear"  # legacy default per auto-dev-intake.md
_AUTO_DEV_BRANCH_PREFIX = "auto-dev/"


class _ClientRepoUnresolvedError(Exception):
    """Raised when --client names a client whose repo cannot be resolved (#2158)."""


def _resolve_tracker(repo_root: Path) -> str:
    """Resolve ``tracking.primary.system`` from *repo_root*'s project-config.yaml.

    Delegates to ``cw.tracker.resolve_tracker`` (the same resolution
    ``spawn.py``/``session.py``/``cw.doctor`` share) and applies this script's
    own allowlist/default on top: defaults to the legacy ``linear`` behavior
    when the file is absent or the value is missing/unrecognized.
    """
    system = _cw_resolve_tracker(repo_root)
    return system if system in _RECOGNIZED_TRACKERS else _DEFAULT_TRACKER


def _resolve_client_repo_root(client: str) -> Path:
    """Map --client to a filesystem repo root via clients.yaml (#2158).

    Mirrors ``cw doctor``'s ``project-config/<client>`` check
    (``src/cw/doctor/config_checks.py``): ``repo_path`` wins for worktree-mode
    clients, ``workspace_path`` otherwise (``cw.worktree._git_dir``).

    No ``clients.yaml`` at all is single-tenant mode, not a failure — falls
    back to :func:`_resolve_repo_root` (the script's own location), exactly as
    ``cw.reconcile.tasks._client_cwd``/``_is_dangling_client`` distinguish
    "absent" from "populated but missing this client" elsewhere in cw. A
    ``clients.yaml`` that *exists* but has no entry for *client* — including
    one that defines no clients at all — is drift: raise loudly rather than
    silently falling back to a default repo. Branch on file existence
    (:func:`cw.config.clients_file`), not on whether ``load_clients()``
    returned an empty dict — both "absent" and "present but empty" produce
    ``{}``, and conflating them would silently re-introduce the fallback this
    ticket removes (operator round-2 resolution, #2158).

    A ``clients.yaml`` that fails to load or validate (malformed YAML, a
    client entry that fails ``ClientConfig`` schema validation, or an I/O
    failure reading the file) is the same "cannot resolve" outcome as a
    missing entry, not a crash: :func:`load_clients` can raise
    ``CwError``/``ConfigValidationError`` (invalid client name or schema), a
    raw ``yaml.YAMLError`` (unparseable YAML), or — from its own
    ``path.read_text()`` — ``OSError`` (e.g. permission denied) or
    ``UnicodeDecodeError`` (non-UTF-8 file). None of those are specific to
    *this* client, so they are wrapped into the same
    :class:`_ClientRepoUnresolvedError` main() already turns into a structured
    ``client_repo_resolved`` failed check (#2158).
    """
    try:
        clients = load_clients()
    except (CwError, yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        msg = f"failed to load clients.yaml: {exc}"
        raise _ClientRepoUnresolvedError(msg) from exc
    if not clients_file().exists():
        return _resolve_repo_root()
    cfg = clients.get(client)
    if cfg is None:
        msg = (
            f"--client {client!r} has no entry in clients.yaml; refusing to "
            "fall back to a default repo (GitHub #2158) — add it to "
            "clients.yaml"
        )
        raise _ClientRepoUnresolvedError(msg)
    return _git_dir(cfg)


def _resolve_repo_slug(repo_root: Path) -> str | None:
    """Resolve *repo_root*'s ``origin`` remote to a GitHub ``owner/repo`` slug.

    Thin wrapper over ``cw.pr_hydrate._resolve_repo_slug`` (kept local for
    mockability). Fail-open: returns ``None`` on any unresolvable case.
    """
    return _cw_resolve_repo_slug(repo_root)


def _check_agents(repo_root: Path) -> dict[str, Any]:
    local_agents = repo_root / ".claude" / "agents"
    missing: list[str] = []
    locations: list[str] = []
    for name in _REQUIRED_AGENTS:
        found_in: list[str] = []
        if (_GLOBAL_AGENTS / name).is_file():
            found_in.append(str(_GLOBAL_AGENTS))
        if (local_agents / name).is_file():
            found_in.append(str(local_agents))
        if not found_in:
            missing.append(name)
        else:
            locations.append(f"{name}@{found_in[0]}")
    if missing:
        detail = (
            f"missing: {', '.join(missing)} "
            f"(looked in {_GLOBAL_AGENTS}, {local_agents})"
        )
        return {
            "name": "agents_present",
            "passed": False,
            "severity": "hard",
            "detail": detail,
        }
    return {
        "name": "agents_present",
        "passed": True,
        "severity": "hard",
        "detail": "; ".join(locations),
    }


def _check_cw_doctor() -> tuple[dict[str, Any], dict[str, Any]]:
    cw = shutil.which("cw")
    if cw is None:
        hard = {
            "name": "cw_backend_healthy",
            "passed": False,
            "severity": "hard",
            "detail": "cw binary not on PATH",
        }
        soft = {
            "name": "cw_doctor_clean",
            "passed": False,
            "severity": "soft",
            "detail": "skipped — cw not on PATH",
        }
        return hard, soft
    proc = subprocess.run(
        [cw, "doctor", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    hard_failed = False
    is_clean = True
    hard_detail = "backend reachable, config parseable"
    # rc=2 means Click rejected --json as an unknown option (stale cw build).
    # Degrade gracefully rather than hard-blocking — a stale build that otherwise
    # works is not a backend failure.
    if proc.returncode == 2:
        hard = {
            "name": "cw_backend_healthy",
            "passed": True,
            "severity": "hard",
            "detail": (
                "cw doctor --json unsupported (stale cw build); "
                "assumed healthy — run install-cw to upgrade"
            ),
        }
        soft = {
            "name": "cw_doctor_clean",
            "passed": False,
            "severity": "soft",
            "detail": "cannot confirm clean (cw doctor --json unsupported)",
        }
        return hard, soft
    try:
        data = json.loads(proc.stdout)
        checks = data.get("checks", [])
        failed_core = [
            c
            for c in checks
            if isinstance(c, dict)
            and c.get("name") in _BACKEND_CORE_CHECKS
            and not c.get("ok", True)
        ]
        hard_failed = bool(failed_core)
        if hard_failed:
            hard_detail = "; ".join(
                f"{c.get('name', '?')}: {c.get('detail', 'failed')}"
                for c in failed_core
            )
        is_clean = bool(data.get("clean", False))
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        hard_failed = True
        is_clean = False
        hard_detail = f"cw doctor --json output unparseable: {exc}"
    hard = {
        "name": "cw_backend_healthy",
        "passed": not hard_failed,
        "severity": "hard",
        "detail": hard_detail,
    }
    soft = {
        "name": "cw_doctor_clean",
        "passed": is_clean,
        "severity": "soft",
        "detail": "no issues" if is_clean else f"cw doctor reported issues:\n{output}",
    }
    return hard, soft


def _check_ticket_open(ticket: str, repo: str, tracker: str) -> dict[str, Any]:
    if tracker != "github-issues":
        # The gh-issue existence probe assumes a GitHub issue number; a Linear
        # id (GEN-403) would make `gh issue view` fail. Ticket existence on a
        # non-github tracker needs that tracker's MCP, unreachable from a
        # script — degrade to a soft, non-blocking skip.
        return {
            "name": "ticket_open",
            "passed": True,
            "severity": "soft",
            "detail": (
                f"skipped — {tracker} ticket existence is not verifiable from"
                " preflight (needs the tracker MCP, unreachable from a script)"
            ),
        }
    gh = shutil.which("gh")
    if gh is None:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": "gh CLI not on PATH",
        }
    proc = subprocess.run(
        [gh, "issue", "view", ticket, "-R", repo, "--json", "state,title,number"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": (
                f"gh issue view failed: {proc.stderr.strip() or proc.stdout.strip()}"
            ),
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": f"gh output not valid JSON: {exc}",
        }
    state = payload.get("state", "UNKNOWN")
    title = payload.get("title", "")
    return {
        "name": "ticket_open",
        "passed": state == "OPEN",
        "severity": "hard",
        "detail": f"#{payload.get('number')} state={state} title={title!r}",
    }


def _check_no_open_pr(ticket: str, repo: str, tracker: str) -> dict[str, Any]:
    gh = shutil.which("gh")
    if gh is None:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": "gh CLI not on PATH",
        }
    # PRs live on GitHub regardless of tracker. For github-issues, search by the
    # issue reference in title/body. For any other tracker, the issue number is
    # not a GitHub one, so key off the deterministic auto-dev branch instead.
    if tracker == "github-issues":
        search = f"#{ticket} in:title,body is:pr is:open"
        argv = [gh, "pr", "list", "-R", repo, "--search", search,
                "--json", "number,title,url"]  # fmt: skip
    else:
        branch = f"{_AUTO_DEV_BRANCH_PREFIX}{ticket}"
        argv = [gh, "pr", "list", "-R", repo, "--head", branch, "--state",
                "open", "--json", "number,title,url"]  # fmt: skip
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": (
                f"gh pr list failed: {proc.stderr.strip() or proc.stdout.strip()}"
            ),
        }
    try:
        prs = json.loads(proc.stdout) or []
    except json.JSONDecodeError as exc:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": f"gh output not valid JSON: {exc}",
        }
    if tracker == "github-issues":
        # gh's free-text search is fuzzy — filter to PRs whose title actually
        # references the ticket (avoids false-positives from unrelated PRs that
        # mention the number in passing).
        pattern = re.compile(rf"(^|\D){re.escape(ticket)}(\D|$)")
        matching = [pr for pr in prs if pattern.search(pr.get("title", ""))]
    else:
        # --head is an exact branch match — any returned PR is this ticket's.
        matching = list(prs)
    if not matching:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": True,
            "severity": "hard",
            "detail": "no open PR references the ticket",
        }
    summary = ", ".join(f"#{pr['number']} {pr['title']}" for pr in matching)
    return {
        "name": "no_open_pr_for_ticket",
        "passed": False,
        "severity": "hard",
        "detail": f"open PRs reference ticket: {summary}",
    }


def _check_not_queued(ticket: str, client: str) -> dict[str, Any]:
    cw = shutil.which("cw")
    if cw is None:
        return {
            "name": "not_already_queued",
            "passed": False,
            "severity": "hard",
            "detail": "cw binary not on PATH",
        }
    proc = subprocess.run(
        [cw, "dev-queue", "status", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Older builds may not support --json. Fall back to a plain run and
        # grep for the ticket; the smoke-test is best-effort here.
        proc = subprocess.run(
            [cw, "dev-queue", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "name": "not_already_queued",
                "passed": False,
                "severity": "hard",
                "detail": (
                    f"cw dev-queue status failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                ),
            }
        # Plain-text fallback: look for the ticket id alongside a blocking state.
        text = proc.stdout
        for state in _DEV_QUEUE_BLOCKING_STATES:
            if state in text and ticket in text:
                return {
                    "name": "not_already_queued",
                    "passed": False,
                    "severity": "hard",
                    "detail": (
                        f"ticket {ticket} appears with state {state} "
                        f"in dev-queue status output"
                    ),
                }
        return {
            "name": "not_already_queued",
            "passed": True,
            "severity": "hard",
            "detail": "ticket not seen in dev-queue status",
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "not_already_queued",
            "passed": False,
            "severity": "hard",
            "detail": f"cw dev-queue status JSON parse failed: {exc}",
        }
    by_client = payload if isinstance(payload, dict) else {}
    entries = (
        by_client.get(client, []) if isinstance(by_client.get(client), list) else []
    )
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("ticket_id")) == ticket
            and entry.get("state") in _DEV_QUEUE_BLOCKING_STATES
        ):
            return {
                "name": "not_already_queued",
                "passed": False,
                "severity": "hard",
                "detail": (
                    f"ticket {ticket} is {entry.get('state')} "
                    f"in dev-queue for client {client}"
                ),
            }
    return {
        "name": "not_already_queued",
        "passed": True,
        "severity": "hard",
        "detail": f"ticket {ticket} not queued or running for client {client}",
    }


def _resolve_repo_root() -> Path:
    """Single-tenant fallback: the script's own repo root.

    Used only from :func:`_resolve_client_repo_root` when ``clients.yaml``
    doesn't exist at all (#2158) — not the general-purpose repo-root
    resolution it used to be. The script lives at
    ``<repo>/.claude/skills/cw-smoke-test/scripts/preflight.py`` — climb four
    parents to land on the repo root.
    """
    return Path(__file__).resolve().parents[4]


def _emit(report: dict[str, Any], rc: int) -> int:
    """Write *report* as one JSON line to stdout and return *rc*.

    Single emission point for main()'s two report shapes (hard-fail-early on
    an unresolvable client, and the full end-of-run report) so a future field
    added to one is not silently missed in the other (#2158).
    """
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight checks for /cw-smoke-test."
    )
    parser.add_argument(
        "--ticket-id", required=True, help="GitHub ticket number (no '#')."
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "GitHub repo in OWNER/NAME form. Explicit override — when "
            "omitted, derived from --client's resolved repo (its git "
            "remote 'origin')."
        ),
    )
    parser.add_argument(
        "--client",
        default="claude-workspace",
        help=(
            "cw client name — resolves the repo root/tracker (via "
            "clients.yaml) as well as the dev-queue lookup."
        ),
    )
    args = parser.parse_args()
    ticket = args.ticket_id.lstrip("#")

    try:
        repo_root = _resolve_client_repo_root(args.client)
    except _ClientRepoUnresolvedError as exc:
        report = {
            "ok": False,
            "ticket_id": ticket,
            "client": args.client,
            "repo": args.repo,
            "repo_root": None,
            "tracker": None,
            "checks": [
                {
                    "name": "client_repo_resolved",
                    "passed": False,
                    "severity": "hard",
                    "detail": str(exc),
                }
            ],
        }
        return _emit(report, 1)

    tracker = _resolve_tracker(repo_root)
    repo = args.repo or _resolve_repo_slug(repo_root)

    checks: list[dict[str, Any]] = [_check_agents(repo_root)]
    hard_doctor, soft_doctor = _check_cw_doctor()
    checks.append(hard_doctor)
    checks.append(soft_doctor)
    if repo is None:
        checks.append(
            {
                "name": "repo_resolved",
                "passed": False,
                "severity": "hard",
                "detail": (
                    f"could not resolve a github owner/repo slug from "
                    f"{repo_root}'s origin remote; pass --repo OWNER/NAME "
                    "explicitly"
                ),
            }
        )
    else:
        checks.append(_check_ticket_open(ticket, repo, tracker))
        checks.append(_check_no_open_pr(ticket, repo, tracker))
    checks.append(_check_not_queued(ticket, args.client))

    hard_failed = any(
        check["severity"] == "hard" and not check["passed"] for check in checks
    )
    report = {
        "ok": not hard_failed,
        "ticket_id": ticket,
        "client": args.client,
        "repo": repo,
        "repo_root": str(repo_root),
        "tracker": tracker,
        "checks": checks,
    }
    return _emit(report, 1 if hard_failed else 0)


if __name__ == "__main__":
    sys.exit(main())
