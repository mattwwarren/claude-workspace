"""cw doctor preflight — report environment health in one place.

When the environment is missing required binaries or the state file is
corrupted, every cw command fails with a cryptic error. `cw doctor` is
the one place to find out *what* is wrong before starting a session.

Returns structured results so the CLI can format them and tests can
assert on specific checks.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import shutil
import subprocess as _sp
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from cw import __version__
from cw.config import (
    clients_file,
    load_clients,
    load_state,
    orchestrator_config_file,
    save_state,
    sessions_lock,
    state_file,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.gh import TIMED_OUT_MERGED_LOOKBACK_DAYS, pr_is_merged_for_ticket
from cw.models import (
    CompletionReason,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import _ROSTER_PATH, get_native_daemon_client
from cw.reconcile import SPAWN_GRACE_SECONDS, reconcile, ticket_id_for_session
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session, TicketTask


@dataclass(frozen=True)
class CheckResult:
    """One preflight check and whether it passed."""

    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass(frozen=True)
class WedgeFinding:
    """A detected wedge condition with an actionable recipe."""

    wedge_class: str
    session_id: str | None
    ticket_id: str | None
    recipe: str
    state_file: str


@dataclass
class DoctorReport:
    """Aggregated output from :func:`run_doctor`."""

    version: str
    checks: list[CheckResult] = field(default_factory=list)
    wedge_findings: list[WedgeFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def clean(self) -> bool:
        """True only when every check is both ok and not warned."""
        return all(c.ok and not c.warn for c in self.checks)


# Path to Claude Code user settings — read for the disclaimer-acceptance flag.
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Minimum supported Claude Code version for native-daemon dispatch.
_MIN_CLAUDE_VERSION = (2, 1, 139)

# Number of components (major.minor.patch) required in a version string.
_VERSION_PARTS = 3

# Check name for the installed-vs-source cw version drift detector.
_CW_VERSION_CHECK_NAME = "cw-version"

# Reinstall command surfaced in warnings when the installed cw is stale.
_CW_REINSTALL_CMD = "uv tool install --reinstall claude-workspace"

# Package name used for importlib.metadata lookups.
_CW_PACKAGE_NAME = "claude-workspace"

# Number of consecutive FRESHNESS_GATE ticks required to declare a loop stall.
_LOOP_STALL_CONSECUTIVE_TICKS = 3

# Session statuses that represent an expected terminal lifecycle end-state.
# A missing worktree on a terminal session is normal (cleaned after merge);
# only non-terminal sessions with missing worktrees indicate a potential fault.
_TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.TIMED_OUT}
)


# Tracker systems cw recognizes in .claude/project-config.yaml. Anything else
# is a config error: the headless worker would silently fall back to its
# built-in default (Linear MCP) and stall on OAuth (see #675 / project-config).
_RECOGNIZED_TRACKERS: frozenset[str] = frozenset({"github-issues", "linear"})

# Repo-relative path to the per-client tracker config the auto-dev skills read.
_PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"


def _gh_on_path() -> bool:
    """True when the ``gh`` binary is resolvable on PATH (testable seam)."""
    return shutil.which("gh") is not None


def _tracker_system(raw: object) -> object:
    """Extract ``tracking.primary.system`` from parsed YAML, or None if absent."""
    if not isinstance(raw, dict):
        return None
    tracking = raw.get("tracking")
    if not isinstance(tracking, dict):
        return None
    primary = tracking.get("primary")
    if not isinstance(primary, dict):
        return None
    return primary.get("system")


def _tracker_prereq_result(name: str, system: object, path: Path) -> CheckResult:
    """Build the CheckResult for a recognized tracker's prerequisite probe."""
    if system == "github-issues":
        if _gh_on_path():
            return CheckResult(name, ok=True, detail=f"github-issues ({path})")
        return CheckResult(
            name,
            ok=True,
            warn=True,
            detail="github-issues tracker but `gh` is not on PATH",
        )
    # linear: cw cannot deterministically probe the Linear MCP from here, so
    # surface it informationally rather than fail.
    return CheckResult(
        name,
        ok=True,
        detail=f"linear tracker ({path}); requires Linear MCP reachable in worker",
    )


def _check_project_configs(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Validate each client's ``.claude/project-config.yaml`` tracker config.

    Per client, resolves the repo root (``repo_path`` when worktree-based, else
    ``workspace_path``), reads ``.claude/project-config.yaml``, and checks that
    ``tracking.primary.system`` is a recognized tracker whose prerequisites are
    present. An absent file warns (github-issues is the documented default);
    an unrecognized system or a parse failure is a hard failure.
    """
    results: list[CheckResult] = []
    for client_name, client in clients.items():
        root = client.repo_path or client.workspace_path
        path = root / _PROJECT_CONFIG_RELPATH
        name = f"project-config/{client_name}"
        if not path.exists():
            results.append(
                CheckResult(
                    name,
                    ok=True,
                    warn=True,
                    detail=(
                        f"no project-config.yaml at {path}; headless workers"
                        " fall back to the legacy Linear MCP default and can"
                        " stall on OAuth — pin tracking.primary.system"
                        " (github-issues or linear)"
                    ),
                )
            )
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            results.append(CheckResult(name, ok=False, detail=f"parse failed: {exc}"))
            continue
        system = _tracker_system(raw)
        if system not in _RECOGNIZED_TRACKERS:
            results.append(
                CheckResult(
                    name,
                    ok=False,
                    detail=(
                        f"tracking.primary.system={system!r} is not recognized"
                        f" (expected one of {sorted(_RECOGNIZED_TRACKERS)})"
                    ),
                )
            )
            continue
        results.append(_tracker_prereq_result(name, system, path))
    return results


def _check_config_file() -> CheckResult:
    """Verify the clients.yaml exists or that no clients is acceptable."""
    path = clients_file()
    if not path.exists():
        return CheckResult(
            "clients.yaml",
            ok=True,
            detail=f"not yet created at {path} (run `cw init`)",
        )
    try:
        load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError) as exc:
        return CheckResult("clients.yaml", ok=False, detail=f"parse failed: {exc}")
    return CheckResult("clients.yaml", ok=True, detail=str(path))


def _check_orchestrator_config() -> CheckResult:
    path = orchestrator_config_file()
    if not path.exists():
        return CheckResult(
            "orchestrator.yaml",
            ok=True,
            detail=f"not yet created at {path} (will be generated on first use)",
        )
    return CheckResult("orchestrator.yaml", ok=True, detail=str(path))


def _check_state_file() -> tuple[CheckResult, CwState | None]:
    """Verify sessions.json parses, returning the loaded state for downstream consumers.

    Returning the parsed state avoids a second ``load_state()`` call in
    ``run_doctor``: linkage checks reuse the same parsed object. On parse
    failure the second tuple element is ``None`` and downstream checks that
    need state should skip themselves; the failure is already visible via
    the returned ``CheckResult``.
    """
    path = state_file()
    try:
        state = load_state()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        return (
            CheckResult("sessions.json", ok=False, detail=f"load failed: {exc}"),
            None,
        )
    return CheckResult("sessions.json", ok=True, detail=str(path)), state


def _check_dev_queue() -> CheckResult:
    try:
        load_dev_queue()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        return CheckResult("dev_queue.json", ok=False, detail=f"load failed: {exc}")
    return CheckResult("dev_queue.json", ok=True, detail="parseable")


def _check_linkage(state: CwState) -> list[CheckResult]:
    """Detect parent/worker linkage drift in session state.

    Returns one :class:`CheckResult` per drift type:

    * ``linkage/dangling-worker`` — an orchestrator's ``worker_session_ids``
      references a session ID absent from state.
    * ``linkage/dangling-parent`` — a worker's ``parent_session_id`` points at
      a session absent from state.
    * ``linkage/asymmetric`` — one side knows about the link but the other
      side doesn't (forward-only or reverse-only reference).

    All three results are always returned; each is ``ok=True`` when no drift
    of that type is detected.
    """
    # Index built once: O(1) membership (via .keys()) and lookup throughout.
    session_by_id: dict[str, Session] = {s.id: s for s in state.sessions}

    # --- dangling-worker: orchestrator.worker_session_ids → missing session ---
    dangling_worker_msgs: list[str] = [
        f"orchestrator {sess.id!r} references missing worker {wid!r}"
        " — remove the stale ID from worker_session_ids"
        for sess in state.sessions
        for wid in sess.worker_session_ids
        if wid not in session_by_id
    ]

    if dangling_worker_msgs:
        dw_detail = "; ".join(dangling_worker_msgs)
        dw_result = CheckResult("linkage/dangling-worker", ok=False, detail=dw_detail)
    else:
        dw_result = CheckResult("linkage/dangling-worker", ok=True, detail="")

    # --- dangling-parent: worker.parent_session_id → missing session ---
    dangling_parent_msgs: list[str] = [
        f"worker {sess.id!r} references missing parent {sess.parent_session_id!r}"
        " — clear parent_session_id or restore the parent session"
        for sess in state.sessions
        if sess.parent_session_id is not None
        and sess.parent_session_id not in session_by_id
    ]

    if dangling_parent_msgs:
        dp_detail = "; ".join(dangling_parent_msgs)
        dp_result = CheckResult("linkage/dangling-parent", ok=False, detail=dp_detail)
    else:
        dp_result = CheckResult("linkage/dangling-parent", ok=True, detail="")

    # --- asymmetric: one side of the link is missing ---
    # Build a map: parent_id → {set of worker IDs that claim it as parent}
    claimed_by: dict[str, set[str]] = {}
    for sess in state.sessions:
        parent_id = sess.parent_session_id
        if parent_id is not None and parent_id in session_by_id:
            claimed_by.setdefault(parent_id, set()).add(sess.id)

    # Forward check: orchestrator lists a worker, but worker doesn't claim it back.
    # Workers already caught as dangling are skipped (wid not in session_by_id).
    fwd_msgs: list[str] = []
    for sess in state.sessions:
        for wid in sess.worker_session_ids:
            worker = session_by_id.get(wid)
            if worker is None or worker.parent_session_id == sess.id:
                continue
            fwd_msgs.append(
                f"orchestrator {sess.id!r} lists worker {wid!r},"
                f" but worker's parent_session_id is {worker.parent_session_id!r}"
                " — update parent_session_id on the worker"
            )

    # Reverse check: worker claims this session as parent, but session
    # doesn't list the worker in its worker_session_ids.
    rev_msgs: list[str] = [
        f"worker {wid!r} claims parent {sess.id!r},"
        f" but {sess.id!r}'s worker_session_ids does not include it"
        " — add the worker ID to worker_session_ids"
        for sess in state.sessions
        for wid in claimed_by.get(sess.id, set())
        if wid not in sess.worker_session_ids
    ]

    asym_msgs = fwd_msgs + rev_msgs

    if asym_msgs:
        asym_detail = "; ".join(asym_msgs)
        asym_result = CheckResult("linkage/asymmetric", ok=False, detail=asym_detail)
    else:
        asym_result = CheckResult("linkage/asymmetric", ok=True, detail="")

    return [dw_result, dp_result, asym_result]


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        reconcile_report = reconcile()
    except CwError as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"reconcile failed: {exc}",
        )
    reaped = len(reconcile_report.phantom_session_ids)
    reverted = len(reconcile_report.reverted_ticket_ids)
    if reaped == 0 and reverted == 0:
        return CheckResult("reconciliation", ok=True, detail="no phantoms")
    return CheckResult(
        "reconciliation",
        ok=True,
        detail=(
            f"reaped {reaped} session(s), reverted {reverted} ticket(s); "
            f"ids: {reconcile_report.phantom_session_ids}"
        ),
    )


def _check_workspace_paths() -> list[CheckResult]:
    """Verify each client's effective git directory exists."""
    try:
        clients = load_clients()
    except Exception:  # noqa: BLE001
        return []  # _check_config_file() already surfaces parse errors
    results = []
    for name, client in clients.items():
        git_dir = _git_dir(client)
        if not git_dir.exists():
            results.append(
                CheckResult(
                    f"workspace/{name}",
                    ok=False,
                    detail=f"path does not exist: {git_dir}",
                )
            )
    return results


def _check_worktree_paths_sessions(
    state: CwState | None = None,
) -> list[CheckResult]:
    """Verify each non-terminal session's worktree_path exists. Read-only, warn-only.

    Terminal sessions (COMPLETED, TIMED_OUT) have their worktrees cleaned up
    as part of normal lifecycle — a missing path is expected, not a fault.
    Only non-terminal sessions with a missing worktree path emit a warn.
    """
    if state is None:
        return []
    wt_paths: list[tuple[str, Path, SessionStatus]] = [
        (s.id, s.worktree_path, s.status)
        for s in state.sessions
        if s.worktree_path is not None
    ]
    total_checked = len(wt_paths)
    results: list[CheckResult] = []
    for session_id, wt, status in wt_paths:
        if status in _TERMINAL_SESSION_STATUSES:
            continue
        if not wt.exists():
            results.append(
                CheckResult(
                    f"worktree/{session_id}",
                    ok=True,
                    warn=True,
                    detail=f"path does not exist: {wt}",
                )
            )
    missing_count = len(results)
    results.append(
        CheckResult(
            "worktree/summary",
            ok=True,
            warn=False,
            detail=(
                f"{total_checked} sessions checked, {missing_count} missing worktrees"
            ),
        )
    )
    return results


# ---------------------------------------------------------------------------
# Wedge detection helpers
# ---------------------------------------------------------------------------


def _check_wedge_task_running_no_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks with no associated live session.

    Skips tasks within SPAWN_GRACE_SECONDS of creation — newly spawned
    tasks have not yet registered their session_id.
    """
    findings: list[WedgeFinding] = []
    _live = {SessionStatus.ACTIVE, SessionStatus.IDLE}
    live_session_ids = {s.id for s in state.sessions if s.status in _live}
    now = datetime.now(UTC)
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.session_id is None:
            created = task.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).total_seconds() < SPAWN_GRACE_SECONDS:
                continue
            findings.append(
                WedgeFinding(
                    wedge_class="wedge/task-running-no-session",
                    session_id=None,
                    ticket_id=task.ticket_id,
                    recipe=(
                        "Queue task RUNNING with no matching session. "
                        "Run: cw doctor --reap to revert task to PENDING."
                    ),
                    state_file=str(state_file()),
                )
            )
        elif task.session_id not in live_session_ids:
            # session_id set but points to a non-live session (missing from
            # state, TIMED_OUT, etc.).
            # _check_wedge_task_running_completed_session handles COMPLETED.
            # BACKGROUNDED is excluded: the session is intentionally paused
            # and will resume — flagging it as a wedge is a false positive.
            session_by_id_local = {s.id: s for s in state.sessions}
            sess = session_by_id_local.get(task.session_id)
            _non_wedge = {SessionStatus.COMPLETED, SessionStatus.BACKGROUNDED}
            if sess is None or sess.status not in _non_wedge:
                findings.append(
                    WedgeFinding(
                        wedge_class="wedge/task-running-no-session",
                        session_id=task.session_id,
                        ticket_id=task.ticket_id,
                        recipe=(
                            "Queue task RUNNING with no matching session. "
                            "Run: cw doctor --reap to revert task to PENDING."
                        ),
                        state_file=str(state_file()),
                    )
                )
    return findings


def _check_wedge_task_running_completed_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks whose session is already COMPLETED."""
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING or task.session_id is None:
            continue
        session = session_by_id.get(task.session_id)
        if session is None or session.status != SessionStatus.COMPLETED:
            continue
        findings.append(
            WedgeFinding(
                wedge_class="wedge/task-running-completed-session",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "Queue task RUNNING but its session is already COMPLETED. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _resolve_wedge_branch(
    task: TicketTask,
    session_by_id: dict[str, Session],
    clients: dict[str, ClientConfig],
) -> str:
    """Branch for a wedge check: the session's branch, else the feature prefix.

    Falls back to ``<feature_branch_prefix>/<ticket>`` (``dev`` when the client
    is unknown), mirroring what the staged pipeline provisions and pushes (#712).
    """
    if task.session_id is not None:
        session = session_by_id.get(task.session_id)
        if session is not None and session.branch:
            return session.branch
    client = clients.get(task.client)
    prefix = client.feature_branch_prefix if client is not None else "dev"
    return f"{prefix}/{task.ticket_id}"


def _check_wedge_repo_ahead(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING tasks whose branch is pushed to remote but queue not updated.

    Uses ``git ls-remote`` to check if the branch exists on the remote and
    ``gh pr list`` to determine whether a PR is open. Advisory only — no
    automatic reap.
    """
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients and fall back to the default feature-branch prefix below
    # (mirrors the guard around load_clients in run_doctor).
    try:
        clients = load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.worktree_path is None:
            continue
        # Branch resolution: prefer session branch, fallback to the client's
        # configured feature branch (<feature_branch_prefix>/<ticket>, e.g.
        # dev/662 — what the staged pipeline provisions and pushes, #712).
        branch = _resolve_wedge_branch(task, session_by_id, clients)
        # Get remote URL from worktree
        try:
            remote_result = _sp.run(
                ["git", "-C", str(task.worktree_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_result.returncode != 0:
                continue
            remote_url = remote_result.stdout.strip()
        except OSError:
            continue
        # Check if branch exists on remote
        try:
            ls_result = _sp.run(
                ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if ls_result.returncode != 0 or not ls_result.stdout.strip():
                continue
        except (OSError, _sp.TimeoutExpired):
            continue
        # Check PR status via gh CLI
        recipe: str
        prs, _ = _gh_pr_states(branch)
        if not prs:
            recipe = (
                f"Branch {branch} is ahead of main with no open PR. "
                f"Suggested: cw spawn-complete {task.ticket_id} or open PR manually."
            )
        else:
            pr_state = prs[0].get("state", "OPEN")
            recipe = (
                f"Branch {branch} has {pr_state} PR but queue still RUNNING. "
                f"Suggested: cw spawn-complete {task.ticket_id} --status shipped."
            )
        findings.append(
            WedgeFinding(
                wedge_class="wedge/repo-ahead-of-queue",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=recipe,
                state_file=str(state_file()),
            )
        )
    return findings


def _reap_wedge_findings(findings: list[WedgeFinding]) -> None:
    """Apply mutations for actionable wedge classes.

    Class-2 (task-running-no-session): revert queue task to PENDING.
    Class-3 (task-running-completed-session): revert queue task to PENDING.
    Class-4 (repo-ahead-of-queue): advisory only — no mutations.

    The former class-1 (pane-idle-but-active) wedge was removed with the
    multiplexer substrate — under the native daemon there are no panes to
    inspect for an idle shell (see #504).
    """
    revert_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id and f.wedge_class != "wedge/repo-ahead-of-queue"
    }
    if not revert_ticket_ids:
        return

    with dev_queue_lock():
        queue = load_dev_queue()
        changed = False
        for task in queue.tasks:
            if (
                task.ticket_id in revert_ticket_ids
                and task.status == QueueItemStatus.RUNNING
            ):
                task.status = QueueItemStatus.PENDING
                task.session_id = None
                changed = True
        if changed:
            save_dev_queue(queue)


def _check_loop_health() -> list[CheckResult]:
    """Detect dispatch stalls: pending>0, running==0 across N consecutive ticks.

    Reads DISPATCH_TICK events from the last hour, groups by client, and checks
    whether the most recent _LOOP_STALL_CONSECUTIVE_TICKS ticks are all
    FRESHNESS_GATE with pending>0 and running==0. When a stall is detected for
    a client, emits a warn=True result suggesting ``cw dev-queue refresh-all``.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    events = read_events(
        event_types=[OrchestratorEventType.DISPATCH_TICK],
        since_ts=cutoff,
    )

    per_client: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        client = ev.payload.get("client", "")
        per_client.setdefault(client, []).append(ev.payload)

    results: list[CheckResult] = []
    for client, ticks in per_client.items():
        recent = ticks[-_LOOP_STALL_CONSECUTIVE_TICKS:]
        if len(recent) < _LOOP_STALL_CONSECUTIVE_TICKS:
            continue
        stalled = all(
            t.get("skip_reason") == DispatchSkipReason.FRESHNESS_GATE
            and int(t.get("pending", 0)) > 0
            and int(t.get("running", 0)) == 0
            and int(t.get("claimed", 0)) == 0
            for t in recent
        )
        if stalled:
            results.append(
                CheckResult(
                    f"loop-health/{client}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"dispatch stalled for {client} — main behind origin."
                        " Run `cw dev-queue refresh-all`."
                    ),
                )
            )

    if not results:
        results.append(
            CheckResult("loop-health", ok=True, warn=False, detail="no stall detected")
        )
    return results


def _gh_pr_states(branch: str) -> tuple[list[dict[str, Any]], bool]:
    """Return (pr_list, gh_missing) for the given branch.

    Returns ([], False) on empty result or non-zero exit.
    Returns ([], True) if gh binary is not found.
    Swallows OSError, ValueError, TimeoutExpired.
    """
    try:
        pr_result = _sp.run(
            ["gh", "pr", "list", "--head", branch, "--json", "state", "--limit", "1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        prs: list[dict[str, Any]] = (
            json.loads(pr_result.stdout) if pr_result.returncode == 0 else []
        )
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError, _sp.TimeoutExpired):
        return [], False
    else:
        return prs, False


def _check_timed_out_merged(state: CwState) -> list[CheckResult]:
    """Detect TIMED_OUT sessions whose linked PR has since merged.

    Scans TIMED_OUT DAEMON sessions whose completed_at falls within
    TIMED_OUT_MERGED_LOOKBACK_DAYS, extracts the ticket id from the
    session name, and uses ``gh issue view`` + ``gh pr view`` to
    determine whether a linked PR is MERGED. Emits a warn=True result
    per session when a merged PR is found.
    """
    cutoff = datetime.now(UTC) - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)
    results: list[CheckResult] = []
    gh_missing = False

    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin != SessionOrigin.DAEMON:
            continue
        if session.completed_at is None or session.completed_at < cutoff:
            continue

        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue

        merged, gh_available = pr_is_merged_for_ticket(ticket_id)
        if not gh_available and not gh_missing:
            results.append(
                CheckResult(
                    "timed_out-merged",
                    ok=True,
                    warn=True,
                    detail="gh unavailable; skipping timed_out-merged check",
                )
            )
            gh_missing = True
            continue

        if merged is True:
            results.append(
                CheckResult(
                    f"timed_out-merged/{session.id}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"session {session.id} is TIMED_OUT but linked PR"
                        f" for ticket {ticket_id} is MERGED — see #315."
                    ),
                )
            )

    return results


def _reap_session_by_selector(
    selector: str,
    *,
    authority: str = "operator",
    lane: str | None = None,
    proposed_action: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Reap a single session by exact short id or exact session name.

    Bypasses ``reap_policy`` — targeted reap is always authorized by the operator.

    Called directly from the CLI ``doctor --reap <SESSION>`` targeted path.
    Does NOT go through ``run_doctor`` to avoid changing its return type.
    Uses the same write primitives as the normal reconcile act phase.

    Returns True when the session was found (even if already terminal).
    Returns False when no session matches *selector*.
    """
    with sessions_lock():
        state = load_state()
        target = next(
            (s for s in state.sessions if selector in (s.id, s.name)),
            None,
        )
        if target is None:
            return False
        if target.status not in (
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
            SessionStatus.BACKGROUNDED,
        ):
            # Already terminal — idempotent.
            return True
        now = datetime.now(UTC)
        target.status = SessionStatus.COMPLETED
        target.completed_at = now
        target.completed_reason = CompletionReason.USER
        save_state(state)

    # Stop daemon surface after releasing sessions_lock.
    if target.surface_ref is not None:
        with contextlib.suppress(Exception):
            get_native_daemon_client().stop(target.surface_ref)

    # Revert owning TicketTask to PENDING — separate lock per established pattern.
    ticket_id = ticket_id_for_session(target.name)
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.PENDING
                    task.session_id = None
                    save_dev_queue(store)
                    break

    # Emit audit event after all locks released. record_event uses _inbox_lock
    # (separate file lock — no deadlock risk). Covers both automated 4c consumer
    # and manual cw doctor --reap so propose→authorize→act is fully traceable.
    record_event(
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
        payload={
            "session_id": target.id,
            "session_name": target.name,
            "client": target.client,
            "ticket_id": ticket_id,
            "lane": lane,
            "authority": authority,
            "proposed_action": proposed_action,
            "mutations": [
                "session_status_completed",
                "daemon_stopped",
                "task_reverted_to_pending",
            ],
        },
        correlation_id=correlation_id,
    )
    return True


def run_doctor(*, reap: bool = False) -> DoctorReport:
    """Run every preflight check and return a populated report.

    When *reap* is True, also run state reconciliation and append a
    ``reconciliation`` check summarising the number of reaped sessions and
    reverted tickets. Also runs wedge checks and applies reap recipes.

    Linkage drift checks (parent/worker reference integrity) are always run,
    independent of the *reap* flag.
    """
    report = DoctorReport(version=__version__)
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    # Per-client tracker config. A broken clients.yaml is already surfaced by
    # _check_config_file; degrade to no clients rather than crash the run.
    try:
        _clients = load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        _clients = {}
    report.checks.extend(_check_project_configs(_clients))
    state_check, link_state = _check_state_file()
    report.checks.append(state_check)
    report.checks.append(_check_dev_queue())

    # Linkage checks reuse the state already loaded by _check_state_file.
    # If state failed to load, state_check is ok=False and the user sees the
    # underlying problem; skipping linkage is correct (cascading from a
    # failed parse would just spam noise).
    if link_state is not None:
        report.checks.extend(_check_linkage(link_state))

    report.checks.append(_check_bypass_disclaimer())
    report.checks.append(_check_claude_version())
    report.checks.append(_check_cw_version())
    report.checks.append(_check_daemon_reachable())
    report.checks.extend(_check_loop_health())
    report.checks.extend(_check_workspace_paths())
    report.checks.extend(_check_worktree_paths_sessions(link_state))

    if link_state is not None:
        report.checks.extend(_check_timed_out_merged(link_state))
        # Wedge checks: load queue once, run all three checks.
        queue = load_dev_queue()
        report.wedge_findings.extend(
            _check_wedge_task_running_no_session(link_state, queue)
        )
        report.wedge_findings.extend(
            _check_wedge_task_running_completed_session(link_state, queue)
        )
        report.wedge_findings.extend(_check_wedge_repo_ahead(link_state, queue))
        if reap and report.wedge_findings:
            _reap_wedge_findings(report.wedge_findings)

    if reap:
        report.checks.append(_check_reconcile())
    return report


def _check_bypass_disclaimer() -> CheckResult:
    """Check whether the user has accepted the bypass-permissions disclaimer."""
    try:
        raw = _CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"settings.json not found at {_CLAUDE_SETTINGS_PATH}",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"could not parse settings.json: {exc}",
        )
    if data.get("skipDangerousModePermissionPrompt"):
        return CheckResult("bypass-disclaimer", ok=True, warn=False, detail="accepted")
    return CheckResult(
        "bypass-disclaimer",
        ok=True,
        warn=True,
        detail=(
            "skipDangerousModePermissionPrompt not set"
            " — run `claude --dangerously-skip-permissions` once interactively"
        ),
    )


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a 'X.Y.Z' version string into a comparable int tuple.

    Returns an empty tuple when the string is absent, too short, or
    non-numeric — callers treat an empty return as "unparseable".
    """
    parts = v.split(".")
    if len(parts) < _VERSION_PARTS:
        return ()
    try:
        return tuple(int(p) for p in parts[:_VERSION_PARTS])
    except ValueError:
        return ()


def _check_claude_version() -> CheckResult:
    """Check that the claude binary is reachable and return its version.

    Returns ok=True, warn=True when the binary ran but exited non-zero, or when
    the version string cannot be parsed, or when the version is below the floor
    required for native-daemon dispatch.
    """
    try:
        proc = _sp.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult("claude-version", ok=False, detail="claude binary not found")
    except _sp.TimeoutExpired:
        return CheckResult(
            "claude-version", ok=False, detail="claude --version timed out (10s)"
        )

    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""

    if proc.returncode != 0:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"claude --version exited {proc.returncode}: {version_line}",
        )

    # Parse the leading X.Y.Z token from the version line.
    first_token = version_line.split()[0] if version_line else ""
    parsed = _parse_version(first_token)
    if not parsed:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"could not parse version: {version_line}",
        )

    if parsed < _MIN_CLAUDE_VERSION:
        min_str = ".".join(str(x) for x in _MIN_CLAUDE_VERSION)
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=(
                f"{version_line} — upgrade to >= {min_str} for native-daemon dispatch"
            ),
        )

    return CheckResult("claude-version", ok=True, detail=version_line)


def _resolve_cw_source_path() -> Path | CheckResult:
    """Resolve the local source dir for the installed cw, or a skip CheckResult.

    Returns the source :class:`Path` for an editable/local install. For a
    registry/PyPI install (no package metadata, no/foreign ``direct_url.json``)
    returns an ``ok=True, warn=False`` skip :class:`CheckResult` that the
    caller propagates unchanged.
    """
    try:
        dist = importlib.metadata.distribution(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    try:
        direct_url: dict[str, object] = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="malformed direct_url.json; skipping source check",
        )

    url = direct_url.get("url", "")
    if not isinstance(url, str) or not url.startswith("file://"):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    return Path(urllib.parse.urlparse(url).path)


def _check_cw_version() -> CheckResult:
    """Check whether the installed cw matches the source repo's pyproject.toml version.

    Silent-skips (ok=True, warn=False) for registry/PyPI installs and when
    package metadata is absent — source-version comparison only makes sense
    for local installs. Warns (ok=True, warn=True) when installed is behind
    source or when the source path is stale/unreadable.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return source_path

    if not source_path.exists():
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"source path {source_path} no longer exists"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    pyproject_path = source_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
        source_version_str: str = pyproject["project"]["version"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not read source version from {pyproject_path}",
        )

    installed_version_str = importlib.metadata.version(_CW_PACKAGE_NAME)

    installed_ver = _parse_version(installed_version_str)
    source_ver = _parse_version(source_version_str)

    if not installed_ver or not source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"could not compare versions:"
                f" installed={installed_version_str} source={source_version_str}"
            ),
        )

    if installed_ver < source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"installed {installed_version_str} < source {source_version_str}"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    return CheckResult(
        _CW_VERSION_CHECK_NAME,
        ok=True,
        warn=False,
        detail=f"installed {installed_version_str} matches source",
    )


def _check_daemon_reachable() -> CheckResult:
    """Check whether the Claude native daemon's roster reports a running supervisor."""
    try:
        raw = _ROSTER_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"roster.json not found at {_ROSTER_PATH} — daemon not started?",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"could not parse roster.json: {exc}",
        )
    pid = data.get("supervisorPid", 0)
    if isinstance(pid, int) and pid > 0:
        return CheckResult(
            "daemon-reachable", ok=True, warn=False, detail=f"supervisorPid={pid}"
        )
    return CheckResult(
        "daemon-reachable",
        ok=True,
        warn=True,
        detail="supervisorPid absent or zero — daemon may not be running",
    )


def format_report(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as a human-readable block."""
    lines = [f"cw {report.version}"]
    for check in report.checks:
        if check.warn:
            mark = "WARN"
        elif check.ok:
            mark = "OK"
        else:
            mark = "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
    if report.wedge_findings:
        lines.append("")
        lines.append("wedge findings:")
        for wf in report.wedge_findings:
            lines.append(f"  [{wf.wedge_class}] ticket={wf.ticket_id}")
            lines.append(f"    {wf.recipe}")
    lines.append("")
    if not report.ok:
        footer = "status: problems detected"
    elif report.clean:
        footer = "status: healthy"
    else:
        footer = "status: healthy — advisory warnings"
    lines.append(footer)
    return "\n".join(lines)


def format_report_json(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as JSON."""
    return json.dumps(
        {
            "version": 1,
            "ok": report.ok,
            "clean": report.clean,
            "checks": [
                {"name": c.name, "ok": c.ok, "warn": c.warn, "detail": c.detail}
                for c in report.checks
            ],
            "wedge_findings": [
                {
                    "wedge_class": f.wedge_class,
                    "session_id": f.session_id,
                    "ticket_id": f.ticket_id,
                    "recipe": f.recipe,
                    "state_file": f.state_file,
                }
                for f in report.wedge_findings
            ],
        },
        indent=2,
    )
