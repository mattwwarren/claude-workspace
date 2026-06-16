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

The active tracker is resolved from ``.claude/project-config.yaml``
(``tracking.primary.system``); absent/unrecognized falls back to ``linear``.
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


def _resolve_tracker(repo_root: Path) -> str:
    """Resolve ``tracking.primary.system`` from .claude/project-config.yaml.

    Defaults to the legacy ``linear`` behavior when the file is absent or the
    value is missing/unrecognized. Uses a minimal line scan (the file format is
    controlled and has a single ``system:`` key) so this standalone script has
    no hard PyYAML dependency.
    """
    path = repo_root / ".claude" / "project-config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT_TRACKER
    match = re.search(r"^\s*system:\s*(\S+)", text, re.MULTILINE)
    if match and match.group(1) in _RECOGNIZED_TRACKERS:
        return match.group(1)
    return _DEFAULT_TRACKER


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
    try:
        data = json.loads(proc.stdout)
        checks = data.get("checks", [])
        failed_core = [
            c
            for c in checks
            if c.get("name") in _BACKEND_CORE_CHECKS and not c.get("ok", True)
        ]
        hard_failed = bool(failed_core)
        if hard_failed:
            hard_detail = "; ".join(
                f"{c.get('name', '?')}: {c.get('detail', 'failed')}"
                for c in failed_core
            )
        is_clean = bool(data.get("clean", True))
    except (json.JSONDecodeError, AttributeError, TypeError):
        hard_failed = proc.returncode != 0
        is_clean = proc.returncode == 0
        if hard_failed:
            hard_detail = output or "cw doctor exited non-zero with no output"
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
    # The script lives at <repo>/.claude/skills/cw-smoke-test/scripts/preflight.py
    # — climb four parents to land on the repo root.
    return Path(__file__).resolve().parents[4]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight checks for /cw-smoke-test."
    )
    parser.add_argument(
        "--ticket-id", required=True, help="GitHub ticket number (no '#')."
    )
    parser.add_argument(
        "--repo",
        default="mattwwarren/claude-workspace",
        help="GitHub repo in OWNER/NAME form (default: mattwwarren/claude-workspace).",
    )
    parser.add_argument(
        "--client",
        default="claude-workspace",
        help="cw client name for the dev-queue lookup.",
    )
    args = parser.parse_args()
    ticket = args.ticket_id.lstrip("#")
    repo_root = _resolve_repo_root()
    tracker = _resolve_tracker(repo_root)

    checks: list[dict[str, Any]] = []
    checks.append(_check_agents(repo_root))
    hard_doctor, soft_doctor = _check_cw_doctor()
    checks.append(hard_doctor)
    checks.append(soft_doctor)
    checks.append(_check_ticket_open(ticket, args.repo, tracker))
    checks.append(_check_no_open_pr(ticket, args.repo, tracker))
    checks.append(_check_not_queued(ticket, args.client))

    hard_failed = any(
        check["severity"] == "hard" and not check["passed"] for check in checks
    )
    report = {
        "ok": not hard_failed,
        "ticket_id": ticket,
        "client": args.client,
        "repo": args.repo,
        "tracker": tracker,
        "checks": checks,
    }
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")
    return 1 if hard_failed else 0


if __name__ == "__main__":
    sys.exit(main())
