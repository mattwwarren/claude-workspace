"""Dev-queue task-revert backstops for reconcile.

Recovers RUNNING TicketTasks whose owning session is already terminal
(TIMED_OUT or DAEMON-COMPLETED), and auto-completes PENDING tasks whose
TIMED_OUT session's PR merged. See GitHub #421, #488, #637.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.config import load_clients, load_orchestrator_config, load_state, save_state
from cw.dev_queue import (
    _stamp_salvage_stage,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.gh import TIMED_OUT_MERGED_LOOKBACK_DAYS
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _DIRTY_WORKTREE_REASON,
    _NEVER_CLAIMED_COMPLETION_REASON,
    _TIMED_OUT_MERGED_REASON,
    feature_branch_key,
    ticket_id_for_session,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

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

    When *sessions_by_id* is provided, ``record_event(SESSION_NEEDS_ATTENTION)``
    and ``fire_push_notification`` are called **after** ``dev_queue_lock`` releases
    for each task routed to BLOCKED_ON_USER.  Firing after the status write is the
    edge-trigger: subsequent ticks find the task already BLOCKED_ON_USER (not RUNNING),
    so they skip it and both calls fire exactly once per dirty episode (#763).

    # Why: record_event and fire_push_notification are called outside dev_queue_lock
    # to preserve the lock-order invariant (record_event acquires _inbox_lock;
    # holding dev_queue_lock while acquiring _inbox_lock risks deadlock with any
    # concurrent process that acquires _inbox_lock first — see #765).
    # Sessions to notify are collected inside the lock, emitted after it releases.

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
    notify_sessions: list[Session] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            if task.session_id in dirty:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition="dirty_worktree",
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
    # Fire notifications after dev_queue_lock releases (lock-order invariant #765).
    for session in notify_sessions:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id_for_session(session.name),
                "claude_session_id": session.claude_session_id,
                "paused_status": _DIRTY_WORKTREE_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
                "lane": session.lane,
            },
        )
        _deps.fire_push_notification(session.name, session.client)
    return reverted


def _collect_timed_out_merged_candidates(
    sessions: list[Session],
    task_by_ticket: dict[tuple[str, str], TicketTask],
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
        task = task_by_ticket.get((session.client, ticket_id))
        if task is None or task.status != QueueItemStatus.PENDING:
            continue
        candidates.append((session, ticket_id))
    return candidates


def _is_never_claimed(task: TicketTask) -> bool:
    """True when *task* shows no evidence of ever attaching to a worker.

    Refuses when every attempt this row made died on the generic
    spawn-error path (dispatch/claim.py:793, stamp_backoff=True increments
    spawn_error_count in lockstep with attempts at claim.py:435), i.e.
    attempts == spawn_error_count. The original #1387 shape (attempts == 0,
    session_id is None) is a degenerate case of this: attempts == 0 forces
    spawn_error_count == 0 too (it can only increment after a claim already
    happened), so attempts == 0 == spawn_error_count always holds and
    #1387's refusal is preserved unchanged (R4).

    No legitimate completion can satisfy this: attempts increments only at
    claim time (dispatch/claim.py:250,278) and is never decremented on
    revert; session_id is stamped only on spawn and cleared on every
    revert-to-PENDING path. A task in this shape reaching this call site is
    definitionally a reconciler false-match (GitHub #1385, #1623), not a
    genuine completion.

    Known accepted gap (#1631, not closed here): a task whose every attempt
    failed via UsageLimitError has attempts > spawn_error_count (that revert
    path deliberately does not stamp spawn_error_count -- dispatch/claim.py:
    769, #868 fleet-wide backoff, see #1623 R7) and is therefore NOT refused
    by this predicate, even though it never attached to a worker either. It
    is structurally identical on the task record to a task that legitimately
    ran, shipped, and timed out inside its first stage (both have
    stage_high_water is None) apart from an attempts count that carries no
    threshold meaning -- closing it needs new durable state ("a session was
    successfully spawned at least once"), not a cleverer predicate.
    """
    return task.session_id is None and task.attempts == task.spawn_error_count


def _emit_never_claimed_refusals(refused: list[tuple[str, Session, str]]) -> None:
    """Emit SESSION_NEEDS_ATTENTION for each refused false-completion.

    Called after dev_queue_lock releases, mirroring this module's existing
    SESSION_COMPLETED emission (Phase 4) -- record_event acquires the
    events-inbox lock and this function's caller must not hold
    dev_queue_lock while doing so (see _revert_running_tasks_for_sessions
    for the same ordering rationale, #765).
    """
    for ticket_id, session, lane in refused:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "",
                "session_name": "",
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": None,
                "paused_status": _NEVER_CLAIMED_COMPLETION_REASON,
                "breadcrumbs": (
                    f"matched timed-out session {session.id!r} but the dev-queue "
                    "task was never claimed (session_id=None, every attempt "
                    "died on the spawn-error path) -- refusing false completion"
                ),
                "crashed": False,
                "lane": lane,
            },
            correlation_id=ticket_id,
        )


def _client_cwd(client_name: str, clients: dict[str, ClientConfig]) -> Path | None:
    """Resolve the gh cwd for *client_name*, or None if no config is found.

    None preserves today's ambient-CWD gh behavior for single-tenant
    deployments with no clients.yaml at all (see #1269 plan Adopted
    Assumptions and TestCompleteTimedOutMergedTasks's no-clients.yaml
    coverage). Callers must check :func:`_is_dangling_client` first and
    skip rather than call this when *clients* is populated but missing an
    entry for *client_name* -- that is config drift, not single-tenant mode,
    and an unscoped gh call there risks misattributing a same-numbered
    ticket from a different repo as merged (GitHub #1269).
    """
    client_cfg = clients.get(client_name)
    return _git_dir(client_cfg) if client_cfg is not None else None


def _is_dangling_client(client_name: str, clients: dict[str, ClientConfig]) -> bool:
    """True when *clients* is populated but has no entry for *client_name*.

    Distinguishes "no clients.yaml at all" (single-tenant, safe ambient-cwd
    fallback via :func:`_client_cwd`) from "client removed/renamed from an
    otherwise-populated clients.yaml" (drift), which must not fall back to
    an unscoped gh call (GitHub #1269).
    """
    return bool(clients) and client_name not in clients


def _filter_merged_candidates(
    candidates: list[tuple[Session, str]],
    clients: dict[str, ClientConfig],
) -> list[tuple[Session, str]]:
    """One gh call per candidate to keep only merged-PR tickets (Phase 2).

    Runs outside any lock. Stops scanning if the gh binary is absent; skips
    candidates with transient gh errors, unmerged PRs, or a dangling client
    (present in the candidate but absent from a populated *clients*, GitHub
    #1269 -- see :func:`_is_dangling_client`).

    *clients* is used to resolve each session's
    :attr:`ClientConfig.feature_branch_prefix` so the branch key matches what
    the staged pipeline provisions (GitHub #728).
    """
    to_complete: list[tuple[Session, str]] = []
    for session, ticket_id in candidates:
        if _is_dangling_client(session.client, clients):
            continue
        branch = feature_branch_key(session.client, ticket_id, clients)
        cwd = _client_cwd(session.client, clients)
        merged, gh_available = _deps.pr_is_merged_for_ticket(
            ticket_id, branch=branch, cwd=cwd
        )
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

    A matching PENDING row with no claim history (attempts == spawn_error_
    count, session_id is None -- every attempt died on the generic
    spawn-error path) is refused rather than completed. Such a row was
    never dispatched and its match is a reconciler false-match (GitHub
    #1385), not a genuine completion. It is left PENDING and a
    SESSION_NEEDS_ATTENTION event is emitted with
    paused_status=_NEVER_CLAIMED_COMPLETION_REASON (#1387 belt-and-braces
    guard, widened by #1623 to also cover attempts > 0 spawn-error-only
    histories).

    Called from reconcile() AFTER sessions_lock is released — no gh subprocess
    runs under the session lock (liveness requirement, #485 SHOULD_FIX 4).

    Returns the list of ticket IDs auto-completed.
    """
    state = load_state()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)

    # Build a cheap lookup: (client, ticket_id) → task (for PENDING filter
    # before gh call). Scoped by client so a TIMED_OUT session in one
    # client's queue cannot match a same-numbered ticket in another
    # client's queue (GitHub #1385, mirrors the #1054 fix in core.py).
    task_by_ticket: dict[tuple[str, str], TicketTask] = {
        (t.client, t.ticket_id): t for t in load_dev_queue().tasks
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
    refused: list[tuple[str, Session, str]] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for session, ticket_id in to_complete:
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.client == session.client
                    and task.status == QueueItemStatus.PENDING
                ):
                    if _is_never_claimed(task):
                        refused.append((ticket_id, session, task.lane))
                        break
                    # Why: PR URL is not retrieved here — a second gh call is
                    # disproportionate for this recovery path; disposition alone
                    # is enough for the operator to identify these as shipped.
                    transition_task_status(
                        task, QueueItemStatus.COMPLETED, disposition="shipped"
                    )
                    _stamp_salvage_stage(task)
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
    _emit_never_claimed_refusals(refused)

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

    Note: neither ``record_event(SESSION_NEEDS_ATTENTION)`` nor
    ``fire_push_notification`` is called here. Both fire inside
    ``_revert_running_tasks_for_sessions`` only when a RUNNING task is actually
    routed to BLOCKED_ON_USER, providing the edge-trigger: each fires at most
    once per dirty episode rather than once per tick (#763).
    """
    dirty_session_ids: set[str] = set()
    for session in sessions:
        if not _shared.worktree_dirty_by_path(session.client, session.worktree_path):
            continue
        dirty_session_ids.add(session.id)
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


def _resolve_task_policy(
    client: str,
    lane: str,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> ReapPolicy:
    """Resolve reap_policy for a task's client/lane without a ReapCandidate."""
    client_cfg = clients.get(client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == lane and lane_cfg.reap_policy is not None:
                return lane_cfg.reap_policy
    return config.reap_policy


_TERMINAL_SIBLING_STATUSES = frozenset(
    [QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED]
)


def _latest_terminal_at_index(
    store: DevQueueStore,
) -> dict[tuple[str, str], datetime]:
    """Return (client, ticket_id) → max(created_at) for all terminal rows."""
    index: dict[tuple[str, str], datetime] = {}
    for t in store.tasks:
        if t.status in _TERMINAL_SIBLING_STATUSES:
            key = (t.client, t.ticket_id)
            if key not in index or t.created_at > index[key]:
                index[key] = t.created_at
    return index


def park_terminal_sibling_tasks() -> list[str]:
    """Park PENDING tasks whose (client, ticket_id) has a terminal sibling.

    A "terminal sibling" is a COMPLETED or CANCELLED task for the same
    (client, ticket_id) across all lanes. Catches stale PENDING rows that
    keep churning after a ticket already reached a terminal state (GitHub #876).

    Per ADR-0006 signal-only posture:
    - ReapPolicy.SIGNAL_ONLY (default): PENDING → BLOCKED_ON_USER + emit
      SESSION_REAP_PROPOSED(reason="terminal_sibling").
    - ReapPolicy.AUTO: PENDING → CANCELLED.

    Does NOT require sessions_lock — operates on dev_queue only. Returns the
    list of ticket_ids that were parked or cancelled.

    Ordering guard: a PENDING task whose created_at is older than any of its
    terminal siblings is skipped. This prevents false-firing on doctor's
    _collapse_blocked_on_user_tasks, which creates CANCELLED rows newer than
    the PENDING it just reverted (oldest PENDING, newer CANCELLED pattern).
    A genuine stale PENDING from the enqueue-dedup gap is always NEWER than
    the original COMPLETED/CANCELLED row.

    # Why: event emission is after dev_queue_lock releases (lock-order
    # invariant #765 — record_event acquires _inbox_lock; holding dev_queue_lock
    # while acquiring _inbox_lock risks deadlock).
    """
    # Cheap pre-read outside the lock to fast-exit when no terminal rows exist.
    pre_store = load_dev_queue()
    if not any(t.status in _TERMINAL_SIBLING_STATUSES for t in pre_store.tasks):
        return []

    # Load policy config outside the lock (no lock-order constraint).
    orchestrator_config = load_orchestrator_config()
    clients = load_clients()

    parked_ids: list[str] = []
    # Snapshot fields needed for event emission after the lock.
    pending_events: list[tuple[str, str, str, str | None]] = []

    with dev_queue_lock():
        store = load_dev_queue()
        latest_terminal_at = _latest_terminal_at_index(store)
        if not latest_terminal_at:
            return []

        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.PENDING:
                continue
            key = (task.client, task.ticket_id)
            if key not in latest_terminal_at:
                continue
            # Ordering guard: skip PENDING tasks older than their terminal siblings.
            # Genuine stale PENDING (enqueue-dedup gap) is inserted AFTER the
            # terminal row — task.created_at >= terminal.created_at.
            # Doctor's collapse reverts the OLDEST task to PENDING and creates
            # newer CANCELLED rows — task.created_at < latest_terminal.created_at.
            if task.created_at < latest_terminal_at[key]:
                continue
            policy = _resolve_task_policy(
                task.client, task.lane, clients, orchestrator_config
            )
            orig_session_id = task.session_id
            if policy is ReapPolicy.AUTO:
                # Why: CANCELLED is in _RESET_DISPOSITION_STATUSES — disposition is
                # cleared regardless; reason is captured in SESSION_REAP_PROPOSED event.
                transition_task_status(task, QueueItemStatus.CANCELLED)
            else:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=ReapReason.TERMINAL_SIBLING.value,
                )
            task.session_id = None
            parked_ids.append(task.ticket_id)
            pending_events.append(
                (task.ticket_id, task.client, task.lane, orig_session_id)
            )
            changed = True
        if changed:
            save_dev_queue(store)

    # Emit events after dev_queue_lock releases (lock-order invariant #765).
    for ticket_id, client, lane, session_id in pending_events:
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            {
                "session_id": session_id,
                "session_name": None,
                "client": client,
                "ticket_id": ticket_id,
                "lane": lane,
                "proposed_action": "terminal_sibling",
                "reason": ReapReason.TERMINAL_SIBLING.value,
                "evidence": {},
            },
            correlation_id=ticket_id,
        )

    return parked_ids
