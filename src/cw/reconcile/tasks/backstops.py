"""Dev-queue task-revert backstops for reconcile (#2631 package split).

Recovers RUNNING TicketTasks whose owning session is already terminal
(TIMED_OUT or DAEMON-COMPLETED). See GitHub #421, #488, #637.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.queue_rows import (
    _is_backstop_exempt,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _DIRTY_WORKTREE_REASON,
    ticket_id_for_session,
)
from cw.reconcile.dirty_checks import DIRTY_CHECK_LOOKAHEAD_SECONDS, partition_dirty

if TYPE_CHECKING:
    from cw.models import CwState, Session
    from cw.reconcile.dirty_checks import DirtyChecks
# Grace window after Session.completed_at during which revert_timed_out_tasks/
# revert_completed_silent_tasks leave a RUNNING task untouched, giving
# consume_completed_sessions()/apply_staged_decision() (dispatch/loop.py,
# dispatch/routing/__init__.py) a chance to route the row using the
# session's own already-classified last_result before this generic,
# last_result-blind worktree-dirty check can pre-empt it with the coarser
# "dirty_worktree" disposition (#2355). ~2x the default tick_interval_seconds
# (30s) — long enough to survive one normal dispatch-loop tick plus a slow
# verdict-comment post, short enough that a genuinely orphaned dirty session
# is still caught within a minute. Hardcoded, not an OrchestratorConfig
# field, mirroring _LIVE_WRITER_RESCAN_BACKOFF_SECONDS immediately above.
_COMPLETION_ROUTING_GRACE_SECONDS = 60


def _sessions_past_completion_grace(
    sessions: list[Session], now: datetime | None = None
) -> list[Session]:
    """Filter out sessions still inside their post-completion routing grace.

    A None completed_at gets no grace (fail-safe: never suppress
    indefinitely, mirrors _collect_timed_out_merged_candidates's guard).
    *now* defaults to the current time (the dirty-check pre-pass passes a
    lookahead, #2548).
    """
    now = now or datetime.now(UTC)
    return [
        s
        for s in sessions
        if s.completed_at is None
        or (now - s.completed_at).total_seconds() >= _COMPLETION_ROUTING_GRACE_SECONDS
    ]


def _revert_running_tasks_for_sessions(
    session_ids: set[str],
    dirty_session_reasons: dict[str, str] | None = None,
    sessions_by_id: dict[str, Session] | None = None,
) -> list[str]:
    """Revert RUNNING TicketTasks whose ``session_id`` is in *session_ids*.

    Shared helper for the per-status revert wrappers. Acquires
    ``dev_queue_lock`` for the read+write window; writes only when at least
    one task was reverted. Returns the list of reverted ticket IDs (PENDING
    only — BLOCKED_ON_USER tickets are excluded from the return value so they
    do not enter ReconcileReport.reverted_ticket_ids).

    When *dirty_session_reasons* is provided, tasks whose session id is a key
    in the dict are routed to BLOCKED_ON_USER instead of PENDING to preserve
    in-flight worktree state for operator inspection (GitHub issue #421); the
    mapped reason string is folded into the breadcrumb (GitHub #2118).

    When *sessions_by_id* is provided, ``record_event(SESSION_NEEDS_ATTENTION)``
    and ``fire_push_notification`` are called **after** ``dev_queue_lock`` releases
    for each task routed to BLOCKED_ON_USER.  Firing after the status write is the
    edge-trigger: subsequent ticks find the task already BLOCKED_ON_USER (not RUNNING),
    so they skip it and both calls fire exactly once per dirty episode (#763).

    # Why: record_event and fire_push_notification run after dev_queue_lock
    # releases, by choice: ADR-0019 permits emitting under it or after it.
    # Sessions to notify are collected inside the lock, emitted after it releases.

    # Why: dirtiness is captured before sessions_lock, in reconcile()'s
    # lockless pre-pass (#2548), at most DIRTY_CHECK_MAX_AGE_SECONDS before this
    # write; the orphaned claude --bg process may still be alive and could write
    # to the worktree in between (TOCTOU). An in-bound clean hit can route the
    # row to PENDING; no act here removes the worktree. Tradeoff: block > clobber
    # (accepted, #2671).
    """
    if not session_ids:
        return []

    dirty = dirty_session_reasons or {}
    reverted: list[str] = []
    changed = False
    notify_sessions: list[Session] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            if _is_backstop_exempt(task):
                # #2324: the mid-turn usage-limit act closed this session and
                # has not yet transitioned its row; it resumes and finishes
                # that, uncharged. #2204: a row mid-fix-loop handoff belongs
                # to cw.reconcile.fix_dispatch for the whole handoff. Either
                # way, reverting it here would charge an attempt it should
                # never cost.
                continue
            session = sessions_by_id.get(task.session_id) if sessions_by_id else None
            recovery_disposition = (
                session.recovery_disposition if session is not None else None
            )
            if task.session_id in dirty or recovery_disposition is not None:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=recovery_disposition or "dirty_worktree",
                )
                if sessions_by_id and task.session_id in sessions_by_id:
                    notify_sessions.append(sessions_by_id[task.session_id])
            else:
                transition_task_status(task, QueueItemStatus.PENDING)
                reverted.append(task.ticket_id)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    # Fire notifications after dev_queue_lock releases (ADR-0019 permits either).
    for session in notify_sessions:
        reason = session.recovery_reason or dirty.get(session.id, "dirty_worktree")
        breadcrumbs = (
            f"{session.worktree_path}: {reason}" if session.worktree_path else reason
        )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id_for_session(session.name),
                "claude_session_id": session.claude_session_id,
                "paused_status": (
                    session.recovery_disposition or _DIRTY_WORKTREE_REASON
                ),
                "breadcrumbs": breadcrumbs,
                "crashed": False,
                "lane": session.lane,
            },
        )
        _deps.fire_push_notification(session.name, session.client)
    return reverted


def backstop_targets(
    state: CwState,
    tasks: list[TicketTask],
    status: SessionStatus,
    *,
    now: datetime | None = None,
) -> tuple[list[Session], set[str]]:
    """The DAEMON sessions in *status* past the completion grace, and which of
    them own a RUNNING, non-backstop-exempt row (the ones the backstop acts on).

    Shared by the in-lock revert and the lockless dirty-check capture (#2548),
    so the two select the same sessions.
    """
    targets = _sessions_past_completion_grace(
        [
            s
            for s in state.sessions
            if s.status == status and s.origin is SessionOrigin.DAEMON
        ],
        now,
    )
    target_ids = {s.id for s in targets}
    running_ids = {
        sid
        for t in tasks
        if (sid := t.session_id) is not None
        and sid in target_ids
        and t.status == QueueItemStatus.RUNNING
        and not _is_backstop_exempt(t)
    }
    return targets, running_ids


def capture_backstop_dirty_checks(
    state: CwState,
    tasks: list[TicketTask],
    *,
    now: datetime,
    checks: DirtyChecks,
) -> None:
    """Lockless pre-pass: capture the dirty checks the backstops will read.

    Runs before ``sessions_lock`` (#2548). Captures the TIMED_OUT and DAEMON
    COMPLETED sessions with a RUNNING row, with the completion grace shifted
    by ``DIRTY_CHECK_LOOKAHEAD_SECONDS`` so a session crossing it before the
    lock is captured rather than deferred.
    """
    lookahead = now + timedelta(seconds=DIRTY_CHECK_LOOKAHEAD_SECONDS)
    sessions: list[Session] = []
    for status in (SessionStatus.TIMED_OUT, SessionStatus.COMPLETED):
        targets, running_ids = backstop_targets(state, tasks, status, now=lookahead)
        sessions += [s for s in targets if s.id in running_ids]
    checks.capture_all(sessions)


def _revert_terminal_backstop(
    status: SessionStatus, dirty_checks: DirtyChecks | None
) -> list[str]:
    """Revert or park the RUNNING rows of DAEMON sessions in *status*.

    Caller must hold ``sessions_lock`` (all call sites are inside
    ``_reconcile_locked``); the reap_reason stamp below relies on it.

    Sessions with dirty worktrees are routed to BLOCKED_ON_USER instead of
    PENDING, and a SESSION_NEEDS_ATTENTION event is emitted for operator
    inspection (GitHub issue #421). Dirtiness is read from *dirty_checks*,
    captured before the lock (#2548); a session with no usable capture is
    deferred: its row stays RUNNING and bound, its session unstamped, and the
    next tick retries.

    Sets reap_reason=COMPLETED_BACKSTOP only on sessions whose RUNNING
    dev-queue task is actually being reverted, so the queue-events server
    can emit queue.session_reaped (#380) without false events on the happy
    path (sessions whose task already completed normally are not stamped).
    """
    state = load_state()
    # Pre-read the dev queue (no lock) to identify which sessions have a
    # RUNNING task that will actually be reverted. Only those are dirty-checked
    # and stamped. Why: this read is outside dev_queue_lock, so a task could
    # flip from RUNNING between here and the locked revert below -- TOCTOU
    # accepted; worst case is a missed or early event, no data loss. A row that
    # turned RUNNING after this read is not in the set, so it waits a tick.
    targets, running_ids = backstop_targets(state, load_dev_queue().tasks, status)
    sessions_by_id = {s.id: s for s in targets}
    dirty, deferred = partition_dirty(
        dirty_checks, [s for s in targets if s.id in running_ids]
    )
    ready = running_ids - deferred
    # Why: stamp in place + save_state, NOT mutate_state — the caller
    # already holds sessions_lock, and the lock is a per-open-fd flock,
    # so re-acquiring it here self-deadlocks (#387 gate hang).
    state_changed = False
    for s in targets:
        if s.reap_reason is None and s.id in ready:
            s.reap_reason = ReapReason.COMPLETED_BACKSTOP
            state_changed = True
    if state_changed:
        save_state(state)
    return _revert_running_tasks_for_sessions(ready, dirty, sessions_by_id)


def revert_timed_out_tasks(dirty_checks: DirtyChecks | None = None) -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is TIMED_OUT.

    Called during :func:`reconcile` as a backstop for the case where
    ``signal_stop`` crashed after writing TIMED_OUT status but before
    reverting the dev-queue task. Returns the list of ticket IDs reverted.
    See :func:`_revert_terminal_backstop`.
    """
    return _revert_terminal_backstop(SessionStatus.TIMED_OUT, dirty_checks)


def revert_completed_silent_tasks(
    dirty_checks: DirtyChecks | None = None,
) -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is DAEMON COMPLETED.

    Called during :func:`reconcile` as a backstop for sessions that completed
    without reverting their dev-queue task (e.g. the session wrote COMPLETED
    status but the dispatch consumer had not yet processed it). Returns the
    list of ticket IDs reverted. See :func:`_revert_terminal_backstop`.
    """
    return _revert_terminal_backstop(SessionStatus.COMPLETED, dirty_checks)
