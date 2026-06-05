"""cw doctor preflight — report environment health in one place.

When the environment is missing required binaries or the state file is
corrupted, every cw command fails with a cryptic error. `cw doctor` is
the one place to find out *what* is wrong before starting a session.

Returns structured results so the CLI can format them and tests can
assert on specific checks.
"""

from __future__ import annotations

import json
import os
import subprocess as _sp
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from cw import __version__
from cw.cmux import get_backend_adapter
from cw.config import (
    clients_file,
    load_clients,
    load_state,
    orchestrator_config_file,
    save_state,
    state_file,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import _ROSTER_PATH
from cw.reconcile import SPAWN_GRACE_SECONDS, reconcile, ticket_id_for_session
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.cmux import MultiplexerAdapter
    from cw.models import CwState, DevQueueStore, Session


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

# Number of consecutive FRESHNESS_GATE ticks required to declare a loop stall.
_LOOP_STALL_CONSECUTIVE_TICKS = 3

# Lookback window (days) for timed_out-merged detection.
_TIMED_OUT_MERGED_LOOKBACK_DAYS = 7

# GitHub PR state string for a merged PR.
_GH_PR_STATE_MERGED = "MERGED"

# Seconds of worktree inactivity (no non-.git file modified) before a pane
# showing an idle shell is considered wedged.
WEDGE_IDLE_MTIME_SECONDS = 300

# Shell command names that indicate a pane is idle (back at prompt).
_SHELL_COMMANDS: frozenset[str] = frozenset({"bash", "zsh", "fish", "sh", "dash"})


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
    """Verify each session's worktree_path exists. Read-only, warn-only."""
    if state is None:
        return []
    wt_paths: list[tuple[str, Path]] = [
        (s.id, s.worktree_path) for s in state.sessions if s.worktree_path is not None
    ]
    total_checked = len(wt_paths)
    results: list[CheckResult] = []
    for session_id, wt in wt_paths:
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


def _check_wedge_pane_idle(
    state: CwState,
    _queue: DevQueueStore,
    adapter: MultiplexerAdapter,
) -> list[WedgeFinding]:
    """Detect panes showing idle shell with no recent worktree file activity.

    Fail-open: if inspect_pane returns {}, skip — missing info must not
    trigger false-positive findings.
    """
    findings: list[WedgeFinding] = []
    live_statuses = {SessionStatus.ACTIVE, SessionStatus.IDLE}
    for session in state.sessions:
        if session.status not in live_statuses:
            continue
        if session.surface_ref is None:
            continue
        pane_info = adapter.inspect_pane(session.surface_ref)
        if not pane_info:
            continue
        if pane_info.get("cmd") not in _SHELL_COMMANDS:
            continue
        worktree = session.worktree_path
        if worktree is None or not worktree.is_dir():
            continue
        cutoff = datetime.now(UTC).timestamp() - WEDGE_IDLE_MTIME_SECONDS
        recent = False
        for dirpath, dirnames, filenames in os.walk(worktree):
            # Exclude .git from mtime scan — git operations update files there
            # even when no real work is happening.
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_mtime > cutoff:
                        recent = True
                        break
                except OSError:
                    continue
            if recent:
                break
        if recent:
            continue
        findings.append(
            WedgeFinding(
                wedge_class="wedge/pane-idle-but-active",
                session_id=session.id,
                ticket_id=ticket_id_for_session(session.name),
                recipe=(
                    "Session pane shows idle shell prompt with no recent worktree"
                    " activity. Run: cw doctor --reap to synthesize completed event"
                    " and revert queue task."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


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
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.worktree_path is None:
            continue
        # Branch resolution: prefer session branch, fallback to auto-dev/<ticket>
        branch: str | None = None
        if task.session_id is not None:
            session = session_by_id.get(task.session_id)
            if session is not None:
                branch = session.branch
        if not branch:
            branch = f"auto-dev/{task.ticket_id}"
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


def _reap_wedge_findings(
    findings: list[WedgeFinding],
    state: CwState,
    adapter: MultiplexerAdapter,
) -> None:
    """Apply mutations for actionable wedge classes.

    Class-1 (pane-idle-but-active): mark session COMPLETED, close pane,
    revert queue task to PENDING.
    Class-2 (task-running-no-session): revert queue task to PENDING.
    Class-3 (task-running-completed-session): revert queue task to PENDING.
    Class-4 (repo-ahead-of-queue): advisory only — no mutations.
    """
    session_by_id = {s.id: s for s in state.sessions}
    now = datetime.now(UTC)

    # Collect IDs for class-1 sessions and ticket IDs for queue revert (classes 1-3).
    class1_session_ids: set[str] = set()
    for f in findings:
        if f.wedge_class != "wedge/pane-idle-but-active" or f.session_id is None:
            continue
        if f.session_id in session_by_id:
            class1_session_ids.add(f.session_id)

    revert_ticket_ids: set[str] = set()
    for f in findings:
        if f.wedge_class == "wedge/repo-ahead-of-queue":
            continue
        if f.ticket_id:
            revert_ticket_ids.add(f.ticket_id)

    # Hold queue lock across BOTH state writes — mutations happen inside the
    # lock so no reader sees sessions.json=COMPLETED while dev_queue.json=RUNNING.
    if class1_session_ids or revert_ticket_ids:
        with dev_queue_lock():
            if class1_session_ids:
                for sid in class1_session_ids:
                    session = session_by_id[sid]
                    session.status = SessionStatus.COMPLETED
                    session.completed_reason = CompletionReason.NORMAL
                    session.completed_at = now
                save_state(state)
            if revert_ticket_ids:
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

    # Side effects AFTER both writes succeed.
    for sid in class1_session_ids:
        session = session_by_id[sid]
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "crashed": False,  # FIX 4: not a crash
                "ticket_id": ticket_id_for_session(session.name),
            },
        )
        if session.surface_ref is not None:
            adapter.close(session.surface_ref)


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


def _timed_out_merged_result(
    session: Session,
    prs: list[dict[str, Any]],
    branch: str,
) -> CheckResult | None:
    """Return a warn CheckResult if session's PR is MERGED, else None."""
    if prs and prs[0].get("state") == _GH_PR_STATE_MERGED:
        return CheckResult(
            f"timed_out-merged/{session.id}",
            ok=True,
            warn=True,
            detail=(
                f"session {session.id} is TIMED_OUT but PR for {branch}"
                " is MERGED — see #315."
            ),
        )
    return None


def _check_timed_out_merged(state: CwState) -> list[CheckResult]:
    """Detect TIMED_OUT sessions whose PR has since merged.

    Scans TIMED_OUT DAEMON sessions whose completed_at falls within
    _TIMED_OUT_MERGED_LOOKBACK_DAYS, infers their branch name, and queries
    ``gh pr list`` to see whether the PR is MERGED. Emits a warn=True result
    per session when a merged PR is found.
    """
    cutoff = datetime.now(UTC) - timedelta(days=_TIMED_OUT_MERGED_LOOKBACK_DAYS)
    results: list[CheckResult] = []
    gh_missing = False

    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin != SessionOrigin.DAEMON:
            continue
        if session.completed_at is None or session.completed_at < cutoff:
            continue

        branch = session.branch
        if branch is None:
            ticket_id = ticket_id_for_session(session.name)
            branch = f"auto-dev/{ticket_id}" if ticket_id is not None else None
        if branch is None:
            continue

        prs, missing = _gh_pr_states(branch)
        if missing and not gh_missing:
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

        result = _timed_out_merged_result(session, prs, branch)
        if result is not None:
            results.append(result)

    return results


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
    report.checks.append(_check_daemon_reachable())
    report.checks.extend(_check_loop_health())
    report.checks.extend(_check_workspace_paths())
    report.checks.extend(_check_worktree_paths_sessions(link_state))

    if link_state is not None:
        report.checks.extend(_check_timed_out_merged(link_state))
        # Wedge checks: load queue once, run all four checks.
        queue = load_dev_queue()
        adapter = get_backend_adapter()
        report.wedge_findings.extend(_check_wedge_pane_idle(link_state, queue, adapter))
        report.wedge_findings.extend(
            _check_wedge_task_running_no_session(link_state, queue)
        )
        report.wedge_findings.extend(
            _check_wedge_task_running_completed_session(link_state, queue)
        )
        report.wedge_findings.extend(_check_wedge_repo_ahead(link_state, queue))
        if reap and report.wedge_findings:
            _reap_wedge_findings(report.wedge_findings, link_state, adapter)

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
    parts = first_token.split(".")
    if len(parts) < _VERSION_PARTS:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"could not parse version: {version_line}",
        )
    try:
        parsed = tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
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
