"""Pre-flight checks for dispatching one ticket (``cw preflight``, #2697).

Verifies the environment is healthy enough to dispatch ``/auto-dev`` against
a single ticket. :func:`run_preflight` returns a :class:`PreflightReport`
with ``ok`` (bool) and ``checks`` (a list of :class:`PreflightCheck`) plus the
process exit code: 0 when no ``severity=hard`` check failed (soft warnings
still report ``ok=true``), 1 when at least one hard check failed. The
``cw preflight`` command prints the report as one JSON line.

Each check distinguishes ``severity``:
- ``hard``  — must pass; failure aborts the dispatch.
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
                                   ticket store is unreachable from here.
- ``no_open_pr_for_ticket`` (hard)  github-issues: ``gh pr list --search``
                                   finds no in-flight PR referencing the
                                   ticket. Other trackers: keyed off the
                                   ``auto-dev/<id>`` branch head instead.
- ``not_already_queued``    (hard)  the ticket is not already RUNNING,
                                   PENDING or CLAIMED in ``cw dev-queue
                                   status``.

The tracker and the GitHub ``owner/repo`` slug are both resolved from the
client's repo root (via ``clients.yaml``, the same way ``cw doctor`` resolves
a client's repo):
- ``client_repo_resolved`` (hard) the client names an entry in
                                  ``clients.yaml`` (or ``clients.yaml``
                                  doesn't exist at all, single-tenant mode,
                                  where the repo root is the nearest ancestor
                                  of cwd holding ``.git``). A populated
                                  ``clients.yaml`` missing the named client
                                  fails loudly instead of silently falling
                                  back to a default repo.
- ``repo_resolved``        (hard) a GitHub ``owner/repo`` slug was derived
                                  from the resolved repo root's ``origin``
                                  remote, or supplied explicitly as *repo*.
                                  Only reported when no slug was resolved.

Tracker resolution reads ``.claude/project-config.yaml``
(``tracking.primary.system``) from that same client repo root;
absent/unrecognized falls back to ``linear``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal, TypedDict

import click
import yaml

from cw.config import clients_file, load_clients
from cw.exceptions import CwError
from cw.pr_hydrate import _resolve_repo_slug as _cw_resolve_repo_slug
from cw.tracker import resolve_tracker as _cw_resolve_tracker
from cw.worktree import _git_dir

__all__ = ["PreflightCheck", "PreflightReport", "run_preflight"]

CheckSeverity = Literal["hard", "soft"]


class PreflightCheck(TypedDict):
    """One check result in a :class:`PreflightReport`."""

    name: str
    passed: bool
    severity: CheckSeverity
    detail: str


class PreflightReport(TypedDict):
    """The JSON object ``cw preflight`` prints (keys unchanged from the script)."""

    ok: bool
    ticket_id: str
    client: str
    repo: str | None
    repo_root: str | None
    tracker: str | None
    checks: list[PreflightCheck]


_GLOBAL_AGENTS = Path.home() / ".claude" / "agents"
_REQUIRED_AGENTS = ("plan-reviewer.md", "plan-soundness-reviewer.md")
# Ordered, so the plain-text fallback reports the same state on every run.
_DEV_QUEUE_BLOCKING_STATES = ("PENDING", "RUNNING", "CLAIMED")

# Check names from ``cw doctor --json`` that indicate the backend core is broken.
# Failures in other checks (project-config, workspace, linkage) do not block
# dispatch — the backend can still dispatch even when tracker config is missing.
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
_GITHUB_TRACKER = "github-issues"
_AUTO_DEV_BRANCH_PREFIX = "auto-dev/"

_BACKEND_CHECK = "cw_backend_healthy"
_CLEAN_CHECK = "cw_doctor_clean"
_TICKET_CHECK = "ticket_open"
_PR_CHECK = "no_open_pr_for_ticket"
_QUEUE_CHECK = "not_already_queued"
_PR_FIELDS = "number,title,url"


class _ClientRepoUnresolvedError(Exception):
    """Raised when the client's repo cannot be resolved (#2158)."""


def _check(
    name: str, *, passed: bool, detail: str, severity: CheckSeverity = "hard"
) -> PreflightCheck:
    return {"name": name, "passed": passed, "severity": severity, "detail": detail}


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _proc_error(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stderr.strip() or proc.stdout.strip()


def _resolve_tracker(repo_root: Path) -> str:
    """Resolve ``tracking.primary.system`` from *repo_root*'s project-config.yaml.

    Delegates to ``cw.tracker.resolve_tracker`` (the same resolution
    ``spawn.py``/``session.py``/``cw.doctor`` share) and applies this module's
    own allowlist/default on top: defaults to the legacy ``linear`` behavior
    when the file is absent or the value is missing/unrecognized.
    """
    system = _cw_resolve_tracker(repo_root)
    return system if system in _RECOGNIZED_TRACKERS else _DEFAULT_TRACKER


def _resolve_repo_root() -> Path:
    """Single-tenant fallback: the nearest ancestor of cwd holding ``.git``.

    Used only from :func:`_resolve_client_repo_root` when ``clients.yaml``
    doesn't exist at all (#2158). Tests ``exists()`` rather than ``is_dir()``
    because in a linked worktree ``.git`` is a file. With no such ancestor,
    cwd itself. No subprocess.
    """
    cwd = Path.cwd()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return cwd


def _resolve_client_repo_root(client: str) -> Path:
    """Map *client* to a filesystem repo root via clients.yaml (#2158).

    Mirrors ``cw doctor``'s ``project-config/<client>`` check
    (``src/cw/doctor/config_checks.py``): ``repo_path`` wins for worktree-mode
    clients, ``workspace_path`` otherwise (``cw.worktree._git_dir``).

    No ``clients.yaml`` at all is single-tenant mode, not a failure — falls
    back to :func:`_resolve_repo_root`, exactly as
    ``cw.reconcile.tasks._client_cwd``/``_is_dangling_client`` distinguish
    "absent" from "populated but missing this client" elsewhere in cw. A
    ``clients.yaml`` that *exists* but has no entry for *client* — including
    one that defines no clients at all — is drift: raise loudly rather than
    silently falling back to a default repo. Branch on file existence
    (:func:`cw.config.clients_file`), not on whether ``load_clients()``
    returned an empty dict — both "absent" and "present but empty" produce
    ``{}``, and conflating them would silently re-introduce the fallback
    #2158 removed.

    A ``clients.yaml`` that fails to load or validate (malformed YAML, a
    client entry that fails ``ClientConfig`` schema validation, or an I/O
    failure reading the file) is the same "cannot resolve" outcome as a
    missing entry, not a crash: :func:`load_clients` can raise
    ``CwError``/``ConfigValidationError`` (invalid client name or schema), a
    raw ``yaml.YAMLError`` (unparseable YAML), or — from its own
    ``path.read_text()`` — ``OSError`` (e.g. permission denied) or
    ``UnicodeDecodeError`` (non-UTF-8 file). None of those are specific to
    *this* client, so they are wrapped into the same
    :class:`_ClientRepoUnresolvedError` :func:`run_preflight` turns into a
    structured ``client_repo_resolved`` failed check.
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


def _check_agents(repo_root: Path) -> PreflightCheck:
    local_agents = repo_root / ".claude" / "agents"
    missing: list[str] = []
    locations: list[str] = []
    for name in _REQUIRED_AGENTS:
        found_in = [
            str(directory)
            for directory in (_GLOBAL_AGENTS, local_agents)
            if (directory / name).is_file()
        ]
        if not found_in:
            missing.append(name)
        else:
            locations.append(f"{name}@{found_in[0]}")
    if missing:
        detail = (
            f"missing: {', '.join(missing)} "
            f"(looked in {_GLOBAL_AGENTS}, {local_agents})"
        )
        return _check("agents_present", passed=False, detail=detail)
    return _check("agents_present", passed=True, detail="; ".join(locations))


def _doctor_checks_from_output(
    proc: subprocess.CompletedProcess[str],
) -> tuple[PreflightCheck, PreflightCheck]:
    """Turn a ``cw doctor --json`` run into the (hard, soft) check pair."""
    output = (proc.stdout + proc.stderr).strip()
    hard_detail = "backend reachable, config parseable"
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
    clean_detail = "no issues" if is_clean else f"cw doctor reported issues:\n{output}"
    return (
        _check(_BACKEND_CHECK, passed=not hard_failed, detail=hard_detail),
        _check(_CLEAN_CHECK, passed=is_clean, detail=clean_detail, severity="soft"),
    )


def _check_cw_doctor() -> tuple[PreflightCheck, PreflightCheck]:
    cw = shutil.which("cw")
    if cw is None:
        return (
            _check(_BACKEND_CHECK, passed=False, detail="cw binary not on PATH"),
            _check(
                _CLEAN_CHECK,
                passed=False,
                detail="skipped — cw not on PATH",
                severity="soft",
            ),
        )
    proc = _run([cw, "doctor", "--json"])
    # Click's usage-error exit means --json was rejected as an unknown option
    # (stale cw build). Degrade gracefully rather than hard-blocking — a stale
    # build that otherwise works is not a backend failure.
    if proc.returncode == click.UsageError.exit_code:
        return (
            _check(
                _BACKEND_CHECK,
                passed=True,
                detail=(
                    "cw doctor --json unsupported (stale cw build); "
                    "assumed healthy — run install-cw to upgrade"
                ),
            ),
            _check(
                _CLEAN_CHECK,
                passed=False,
                detail="cannot confirm clean (cw doctor --json unsupported)",
                severity="soft",
            ),
        )
    return _doctor_checks_from_output(proc)


def _check_ticket_open(ticket: str, repo: str, tracker: str) -> PreflightCheck:
    if tracker != _GITHUB_TRACKER:
        # The gh-issue existence probe assumes a GitHub issue number; a Linear
        # id (GEN-403) would make `gh issue view` fail. Ticket existence on a
        # non-github tracker needs that tracker's MCP, unreachable from here —
        # degrade to a soft, non-blocking skip.
        return _check(
            _TICKET_CHECK,
            passed=True,
            severity="soft",
            detail=(
                f"skipped — {tracker} ticket existence is not verifiable from"
                " preflight (needs the tracker MCP, unreachable from a script)"
            ),
        )
    gh = shutil.which("gh")
    if gh is None:
        return _check(_TICKET_CHECK, passed=False, detail="gh CLI not on PATH")
    proc = _run(
        [gh, "issue", "view", ticket, "-R", repo, "--json", "state,title,number"]
    )
    if proc.returncode != 0:
        return _check(
            _TICKET_CHECK,
            passed=False,
            detail=f"gh issue view failed: {_proc_error(proc)}",
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _check(
            _TICKET_CHECK, passed=False, detail=f"gh output not valid JSON: {exc}"
        )
    state = payload.get("state", "UNKNOWN")
    title = payload.get("title", "")
    return _check(
        _TICKET_CHECK,
        passed=state == "OPEN",
        detail=f"#{payload.get('number')} state={state} title={title!r}",
    )


def _open_pr_argv(gh: str, ticket: str, repo: str, tracker: str) -> list[str]:
    """``gh pr list`` argv for the ticket's in-flight PRs.

    PRs live on GitHub regardless of tracker. For github-issues, search by the
    issue reference in title/body. For any other tracker, the issue number is
    not a GitHub one, so key off the deterministic auto-dev branch instead.
    """
    if tracker == _GITHUB_TRACKER:
        search = f"#{ticket} in:title,body is:pr is:open"
        return [gh, "pr", "list", "-R", repo, "--search", search, "--json", _PR_FIELDS]
    branch = f"{_AUTO_DEV_BRANCH_PREFIX}{ticket}"
    return [
        gh,
        "pr",
        "list",
        "-R",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        _PR_FIELDS,
    ]


def _check_no_open_pr(ticket: str, repo: str, tracker: str) -> PreflightCheck:
    gh = shutil.which("gh")
    if gh is None:
        return _check(_PR_CHECK, passed=False, detail="gh CLI not on PATH")
    proc = _run(_open_pr_argv(gh, ticket, repo, tracker))
    if proc.returncode != 0:
        return _check(
            _PR_CHECK, passed=False, detail=f"gh pr list failed: {_proc_error(proc)}"
        )
    try:
        prs = json.loads(proc.stdout) or []
    except json.JSONDecodeError as exc:
        return _check(
            _PR_CHECK, passed=False, detail=f"gh output not valid JSON: {exc}"
        )
    if tracker == _GITHUB_TRACKER:
        # gh's free-text search is fuzzy — filter to PRs whose title actually
        # references the ticket (avoids false-positives from unrelated PRs that
        # mention the number in passing).
        pattern = re.compile(rf"(^|\D){re.escape(ticket)}(\D|$)")
        matching = [pr for pr in prs if pattern.search(pr.get("title", ""))]
    else:
        # --head is an exact branch match — any returned PR is this ticket's.
        matching = list(prs)
    if not matching:
        return _check(_PR_CHECK, passed=True, detail="no open PR references the ticket")
    summary = ", ".join(f"#{pr['number']} {pr['title']}" for pr in matching)
    return _check(
        _PR_CHECK, passed=False, detail=f"open PRs reference ticket: {summary}"
    )


def _queue_result(*, passed: bool, detail: str) -> PreflightCheck:
    """Build the ``not_already_queued`` check."""
    return _check(_QUEUE_CHECK, passed=passed, detail=detail)


def _queued_from_text(ticket: str, stdout: str) -> PreflightCheck:
    """Plain-text fallback: the ticket id alongside a blocking state."""
    for state in _DEV_QUEUE_BLOCKING_STATES:
        if state in stdout and ticket in stdout:
            return _queue_result(
                passed=False,
                detail=(
                    f"ticket {ticket} appears with state {state} "
                    f"in dev-queue status output"
                ),
            )
    return _queue_result(passed=True, detail="ticket not seen in dev-queue status")


def _queued_from_json(ticket: str, client: str, stdout: str) -> PreflightCheck:
    """Look *ticket* up in *client*'s rows of ``cw dev-queue status --json``."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _queue_result(
            passed=False, detail=f"cw dev-queue status JSON parse failed: {exc}"
        )
    by_client = payload if isinstance(payload, dict) else {}
    rows = by_client.get(client)
    entries = rows if isinstance(rows, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("ticket_id")) == ticket
            and entry.get("state") in _DEV_QUEUE_BLOCKING_STATES
        ):
            return _queue_result(
                passed=False,
                detail=(
                    f"ticket {ticket} is {entry.get('state')} "
                    f"in dev-queue for client {client}"
                ),
            )
    return _queue_result(
        passed=True,
        detail=f"ticket {ticket} not queued or running for client {client}",
    )


def _check_not_queued(ticket: str, client: str) -> PreflightCheck:
    cw = shutil.which("cw")
    if cw is None:
        return _queue_result(passed=False, detail="cw binary not on PATH")
    proc = _run([cw, "dev-queue", "status", "--json"])
    if proc.returncode == 0:
        return _queued_from_json(ticket, client, proc.stdout)
    # Older builds may not support --json. Fall back to a plain run and
    # grep for the ticket; the check is best-effort here.
    proc = _run([cw, "dev-queue", "status"])
    if proc.returncode != 0:
        return _queue_result(
            passed=False, detail=f"cw dev-queue status failed: {_proc_error(proc)}"
        )
    return _queued_from_text(ticket, proc.stdout)


def _unresolved_client_report(
    ticket: str, client: str, repo: str | None, exc: _ClientRepoUnresolvedError
) -> PreflightReport:
    """The fail-early report when *client*'s repo root cannot be resolved."""
    return {
        "ok": False,
        "ticket_id": ticket,
        "client": client,
        "repo": repo,
        "repo_root": None,
        "tracker": None,
        "checks": [_check("client_repo_resolved", passed=False, detail=str(exc))],
    }


def _collect_checks(
    ticket: str, client: str, repo_root: Path, repo: str | None, tracker: str
) -> list[PreflightCheck]:
    """Run every check, in report order."""
    checks = [_check_agents(repo_root), *_check_cw_doctor()]
    if repo is None:
        checks.append(
            _check(
                "repo_resolved",
                passed=False,
                detail=(
                    f"could not resolve a github owner/repo slug from "
                    f"{repo_root}'s origin remote; pass --repo OWNER/NAME "
                    "explicitly"
                ),
            )
        )
    else:
        checks.append(_check_ticket_open(ticket, repo, tracker))
        checks.append(_check_no_open_pr(ticket, repo, tracker))
    checks.append(_check_not_queued(ticket, client))
    return checks


def run_preflight(
    ticket: str, client: str, repo: str | None
) -> tuple[PreflightReport, int]:
    """Run the pre-flight checks for *ticket* against *client*.

    *repo* (``OWNER/NAME``) overrides the slug derived from the client repo's
    ``origin`` remote. Returns the report and the exit code (1 when any hard
    check failed, else 0); never prints.
    """
    try:
        repo_root = _resolve_client_repo_root(client)
    except _ClientRepoUnresolvedError as exc:
        return _unresolved_client_report(ticket, client, repo, exc), 1

    tracker = _resolve_tracker(repo_root)
    resolved_repo = repo or _resolve_repo_slug(repo_root)
    checks = _collect_checks(ticket, client, repo_root, resolved_repo, tracker)
    hard_failed = any(
        check["severity"] == "hard" and not check["passed"] for check in checks
    )
    report: PreflightReport = {
        "ok": not hard_failed,
        "ticket_id": ticket,
        "client": client,
        "repo": resolved_repo,
        "repo_root": str(repo_root),
        "tracker": tracker,
        "checks": checks,
    }
    return report, 1 if hard_failed else 0
