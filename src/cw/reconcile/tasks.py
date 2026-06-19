"""Dev-queue task-revert backstops for reconcile.

Recovers RUNNING TicketTasks whose owning session is already terminal
(TIMED_OUT or DAEMON-COMPLETED), and auto-completes PENDING tasks whose
TIMED_OUT session's PR merged. See GitHub #421, #488, #637.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.config import load_clients, load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.gh import TIMED_OUT_MERGED_LOOKBACK_DAYS
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _DIRTY_WORKTREE_REASON,
    _TIMED_OUT_MERGED_REASON,
    feature_branch_key,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session


def _revert_running_tasks_for_sessions(
    session_ids: set[str],
    dirty_session_ids: set[str] | None = None,
    sessions_by_id: dict[str, Session] | None = None,
) -> list[str]:
    """Revert RUNNING TicketTasks whose ``session_id`` is in *session_ids*.

    Shared helper for the per-status revert wrappers. Acquires
    ``dev_queue_lock`` for the read+write window; writes only when at least
    one task was reverted. Returns the list of reverted ticket IDs (PENDING
    only — BLOCKED_ON_USER tickets are excluded from the return value so they
    do not enter ReconcileReport.reverted_ticket_ids).

    When *dirty_session_ids* is provided, tasks whose session_id is in the
    set are routed to BLOCKED_ON_USER instead of PENDING to preserve in-flight
    worktree state for operator inspection (GitHub issue #421).

    When *sessions_by_id* is provided, ``fire_push_notification`` is called
    for each task routed to BLOCKED_ON_USER. Firing here (rather than in
    ``_build_dirty_session_ids_and_notify``) is the edge-trigger: subsequent
    ticks find the task already BLOCKED_ON_USER (not RUNNING), so they skip it
    and the push fires exactly once per dirty episode (#763).

    # Why: dirtiness is checked before dev_queue_lock is acquired (in the
    # callers revert_timed_out_tasks / revert_completed_silent_tasks), but the
    # orphaned claude --bg process may still be alive and could write to the
    # worktree between that check and the BLOCKED_ON_USER write below (TOCTOU).
    # The accepted tradeoff is block > clobber — narrow the window, accept the race.
    """
    if not session_ids:
        return []

    dirty = dirty_session_ids or set()
    reverted: list[str] = []
    changed = False
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            if task.session_id in dirty:
                task.status = QueueItemStatus.BLOCKED_ON_USER
                if sessions_by_id and task.session_id in sessions_by_id:
                    session = sessions_by_id[task.session_id]
                    _deps.fire_push_notification(session.name, session.client)
            else:
                task.status = QueueItemStatus.PENDING
                reverted.append(task.ticket_id)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    return reverted


def _collect_timed_out_merged_candidates(
    sessions: list[Session],
    task_by_ticket: dict[str, TicketTask],
    cutoff: datetime,
) -> list[tuple[Session, str]]:
    """Cheap pre-gh filter for ``complete_timed_out_merged_tasks`` (Phase 1).

    Returns (session, ticket_id) pairs for TIMED_OUT DAEMON sessions inside the
    lookback window whose dev-queue task is still PENDING. Makes zero gh calls.
    """
    # session.branch is None for all DAEMON sessions (spawn.py never sets it).
    candidates: list[tuple[Session, str]] = []
    for session in sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        # Guard: completed_at may be None in legacy state files.
        if session.completed_at is None:
            continue
        if session.completed_at < cutoff:
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        # Idempotency gate: only PENDING tasks are safe to auto-complete.
        # RUNNING means a new session already picked it up; terminal means done.
        task = task_by_ticket.get(ticket_id)
        if task is None or task.status != QueueItemStatus.PENDING:
            continue
        candidates.append((session, ticket_id))
    return candidates


def _filter_merged_candidates(
    candidates: list[tuple[Session, str]],
    clients: dict[str, ClientConfig],
) -> list[tuple[Session, str]]:
    """One gh call per candidate to keep only merged-PR tickets (Phase 2).

    Runs outside any lock. Stops scanning if the gh binary is absent; skips
    candidates with transient gh errors or unmerged PRs.

    *clients* is used to resolve each session's
    :attr:`ClientConfig.feature_branch_prefix` so the branch key matches what
    the staged pipeline provisions (GitHub #728).
    """
    to_complete: list[tuple[Session, str]] = []
    for session, ticket_id in candidates:
        branch = feature_branch_key(session.client, ticket_id, clients)
        merged, gh_available = _deps.pr_is_merged_for_ticket(ticket_id, branch=branch)
        if not gh_available:
            # gh binary absent — skip all remaining candidates.
            break
        if merged is None:
            # Transient error — skip this session only.
            continue
        if merged:
            to_complete.append((session, ticket_id))
        # merged is False → leave PENDING.
    return to_complete


def complete_timed_out_merged_tasks() -> list[str]:
    """Upgrade PENDING TicketTasks to COMPLETED when their PR merged.

    Targets TIMED_OUT DAEMON sessions in the lookback window whose PR merged.

    Post-pass over TIMED_OUT DAEMON sessions in the lookback window. For each
    whose dev-queue task is still PENDING and whose linked PR is MERGED (via
    issue-linkage), upgrades the task to COMPLETED and emits SESSION_COMPLETED
    with reason="timed_out_merged".

    Called from reconcile() AFTER sessions_lock is released — no gh subprocess
    runs under the session lock (liveness requirement, #485 SHOULD_FIX 4).

    Returns the list of ticket IDs auto-completed.
    """
    state = load_state()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)

    # Build a cheap lookup: ticket_id → task (for PENDING filter before gh call).
    task_by_ticket: dict[str, TicketTask] = {
        t.ticket_id: t for t in load_dev_queue().tasks
    }

    # Phase 1: Cheap filters before any gh subprocess call.
    candidates = _collect_timed_out_merged_candidates(
        state.sessions, task_by_ticket, cutoff
    )
    if not candidates:
        return []

    # Load clients once for branch-key resolution (feature_branch_prefix SSOT, #728).
    clients = load_clients()

    # Phase 2: One gh call per surviving candidate (outside any lock).
    to_complete = _filter_merged_candidates(candidates, clients)
    if not to_complete:
        return []

    # Phase 3: Acquire only dev_queue_lock for the PENDING→COMPLETED write.
    completed_ids: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for _, ticket_id in to_complete:
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.PENDING
                ):
                    task.status = QueueItemStatus.COMPLETED
                    completed_ids.append(ticket_id)
                    changed = True
                    break
        if changed:
            save_dev_queue(store)

    # Phase 4: Emit decision-trace events after the lock releases.
    for session, ticket_id in to_complete:
        if ticket_id in completed_ids:
            record_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": session.id,
                    "session_name": session.name,
                    "client": session.client,
                    "ticket_id": ticket_id,
                    "claude_session_id": session.claude_session_id,
                    "crashed": False,
                    "salvaged": True,
                    "reason": _TIMED_OUT_MERGED_REASON,
                },
                correlation_id=ticket_id,
            )

    return completed_ids


def _build_dirty_session_ids_and_notify(
    sessions: list[Session],
) -> set[str]:
    """Identify sessions with dirty worktrees, emit SESSION_NEEDS_ATTENTION.

    Called before acquiring dev_queue_lock so that dirtiness is assessed
    outside the lock window (see TOCTOU note in _revert_running_tasks_for_sessions).

    Returns the set of session IDs whose worktrees have unsaved work.
    Does NOT write session.last_result — this is a queue-level guard, not a
    park-marker update (to avoid interfering with the existing park-marker logic).

    Note: ``fire_push_notification`` is intentionally NOT called here.
    The push fires inside ``_revert_running_tasks_for_sessions`` only when a
    RUNNING task is actually routed to BLOCKED_ON_USER, so it fires at most
    once per dirty episode rather than once per tick (#763).
    """
    dirty_session_ids: set[str] = set()
    for session in sessions:
        if not _shared.worktree_dirty_by_path(session.client, session.worktree_path):
            continue
        dirty_session_ids.add(session.id)
        ticket_id = ticket_id_for_session(session.name)
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _DIRTY_WORKTREE_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
            },
        )
    return dirty_session_ids


def revert_timed_out_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is TIMED_OUT.

    Called during :func:`reconcile` as a backstop for the case where
    ``signal_stop`` crashed after writing TIMED_OUT status but before
    reverting the dev-queue task. Returns the list of ticket IDs reverted.

    Caller must hold ``sessions_lock`` (all call sites are inside
    ``_reconcile_locked``); the reap_reason stamp below relies on it.

    Sessions with dirty worktrees are routed to BLOCKED_ON_USER instead of
    PENDING, and a SESSION_NEEDS_ATTENTION event is emitted for operator
    inspection (GitHub issue #421).

    Sets reap_reason=COMPLETED_BACKSTOP only on sessions whose RUNNING
    dev-queue task is actually being reverted, so the queue-events server
    can emit queue.session_reaped (#380) without false events on the happy
    path (sessions whose task already completed normally are not stamped).
    """
    state = load_state()
    target_sessions = [
        s
        for s in state.sessions
        if s.status == SessionStatus.TIMED_OUT and s.origin is SessionOrigin.DAEMON
    ]
    session_ids = {s.id for s in target_sessions}
    # Pre-read the dev queue (no lock) to identify which sessions have a
    # RUNNING task that will actually be reverted.  Only those sessions get
    # the COMPLETED_BACKSTOP stamp so we avoid emitting false reap events for
    # sessions whose task already completed normally via the happy path.
    # Why: this read is outside dev_queue_lock, so a task could flip from
    # RUNNING to another status between here and the locked revert below —
    # TOCTOU accepted (same pattern as the dirty-check in
    # _revert_running_tasks_for_sessions); worst case is a missed or early
    # event, no data loss.
    store = load_dev_queue()
    backstop_session_ids = {
        t.session_id
        for t in store.tasks
        if t.status == QueueItemStatus.RUNNING and t.session_id in session_ids
    }
    # Why: stamp in place + save_state, NOT mutate_state — the caller
    # already holds sessions_lock, and the lock is a per-open-fd flock,
    # so re-acquiring it here self-deadlocks (#387 gate hang).
    state_changed = False
    for s in target_sessions:
        if s.reap_reason is None and s.id in backstop_session_ids:
            s.reap_reason = ReapReason.COMPLETED_BACKSTOP
            state_changed = True
    if state_changed:
        save_state(state)
    # Compute dirtiness BEFORE acquiring dev_queue_lock (see TOCTOU note in
    # _revert_running_tasks_for_sessions docstring).
    sessions_by_id = {s.id: s for s in target_sessions}
    dirty_session_ids = _build_dirty_session_ids_and_notify(target_sessions)
    return _revert_running_tasks_for_sessions(
        session_ids, dirty_session_ids, sessions_by_id
    )


def revert_completed_silent_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is DAEMON COMPLETED.

    Called during :func:`reconcile` as a backstop for sessions that completed
    without reverting their dev-queue task (e.g. the session wrote COMPLETED
    status but the dispatch consumer had not yet processed it). Returns the
    list of ticket IDs reverted.

    Caller must hold ``sessions_lock`` (all call sites are inside
    ``_reconcile_locked``); the reap_reason stamp below relies on it.

    Sessions with dirty worktrees are routed to BLOCKED_ON_USER instead of
    PENDING, and a SESSION_NEEDS_ATTENTION event is emitted for operator
    inspection (GitHub issue #421).

    Sets reap_reason=COMPLETED_BACKSTOP only on sessions whose RUNNING
    dev-queue task is actually being reverted, so the queue-events server
    can emit queue.session_reaped (#380) without false events on the happy
    path (sessions whose task already completed normally are not stamped).
    """
    state = load_state()
    target_sessions = [
        s
        for s in state.sessions
        if s.status == SessionStatus.COMPLETED and s.origin is SessionOrigin.DAEMON
    ]
    session_ids = {s.id for s in target_sessions}
    # Pre-read the dev queue (no lock) to identify which sessions have a
    # RUNNING task that will actually be reverted.  Only those sessions get
    # the COMPLETED_BACKSTOP stamp so we avoid emitting false reap events for
    # sessions whose task already completed normally via the happy path.
    # Why: this read is outside dev_queue_lock, so a task could flip from
    # RUNNING to another status between here and the locked revert below —
    # TOCTOU accepted (same pattern as the dirty-check in
    # _revert_running_tasks_for_sessions); worst case is a missed or early
    # event, no data loss.
    store = load_dev_queue()
    backstop_session_ids = {
        t.session_id
        for t in store.tasks
        if t.status == QueueItemStatus.RUNNING and t.session_id in session_ids
    }
    # Why: stamp in place + save_state, NOT mutate_state — the caller
    # already holds sessions_lock, and the lock is a per-open-fd flock,
    # so re-acquiring it here self-deadlocks (#387 gate hang).
    state_changed = False
    for s in target_sessions:
        if s.reap_reason is None and s.id in backstop_session_ids:
            s.reap_reason = ReapReason.COMPLETED_BACKSTOP
            state_changed = True
    if state_changed:
        save_state(state)
    # Compute dirtiness BEFORE acquiring dev_queue_lock (see TOCTOU note in
    # _revert_running_tasks_for_sessions docstring).
    sessions_by_id = {s.id: s for s in target_sessions}
    dirty_session_ids = _build_dirty_session_ids_and_notify(target_sessions)
    return _revert_running_tasks_for_sessions(
        session_ids, dirty_session_ids, sessions_by_id
    )
