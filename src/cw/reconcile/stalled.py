"""Stalled-headless-session detection and act phases for reconcile.

A stalled headless DAEMON session is one past its wall-clock budget that
produced no further Stop-hook firings. See GitHub #185, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    CompletionReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _GH_CHECK_BLOCKED_REASON,
    _LIVE_STATUSES,
    _NEEDS_SALVAGE_REASON,
    _PHANTOM_REAP_MERGED_REASON,
    _SALVAGE_SKIP_REASON,
    _SILENTLY_IDLE_REASON,
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _cleanup_timed_out_worktree,
    _is_headless,
    _queue_status_for_salvaged,
    resolve_headless_budget,
    resolve_reap_policy,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, TicketTask


def _detect_stalled_candidates(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Pure classification phase for stalled headless DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. See GitHub #552, ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if not _is_headless(session):
            continue
        # Park-marker check: sessions already parked by the idle watchdog.
        # Detect returns SKIP_PARKED candidate; act emits the skip event.
        if isinstance(session.last_result, dict) and session.last_result.get(
            "paused_status"
        ) in (_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON):
            actual_paused_status = session.last_result.get("paused_status")
            ticket_id = ticket_id_for_session(session.name)
            # Stamp lane for SKIP_PARKED too so act phase has a consistent candidate.
            skip_task = task_by_ticket.get(ticket_id) if ticket_id else None
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SKIP_PARKED,
                    ticket_id=ticket_id,
                    paused_status=str(actual_paused_status)
                    if actual_paused_status
                    else None,
                    lane=skip_task.lane if skip_task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_headless_budget(task, session, config)
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget:
            continue
        # Try terminal-sentinel salvage before declaring timeout.
        salvage = _shared.salvage_terminal_result(session)
        if salvage is not None:
            result, claude_session_id = salvage
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    elapsed_seconds=elapsed,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.REVERT_TASK,
                ticket_id=ticket_id,
                elapsed_seconds=elapsed,
                reap_reason=ReapReason.WALL_CLOCK_BUDGET,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
        )
    return candidates


def _act_on_stalled_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Act phase for stalled headless sessions: apply all mutations.

    Consumes ReapCandidate objects from _detect_stalled_candidates.
    Mirrors the side-effect logic in revert_stalled_headless_sessions.
    Returns (reverted_ticket_ids, merged_completed_ticket_ids).
    reverted_ticket_ids contains ticket IDs reverted to PENDING.
    merged_completed_ticket_ids contains ticket IDs completed because their
    PR was already merged (from merged_ticket_ids pre-pass; GitHub #637).

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), REVERT_TASK candidates are
    routed to BLOCKED_ON_USER instead of triggering stop/remove.  Non-REVERT
    candidates (SALVAGE_*, SKIP_PARKED) are unaffected and pass through.
    Per-lane resolution: each REVERT_TASK candidate's effective policy is
    resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return [], []

    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each REVERT_TASK candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.REVERT_TASK:
            if c.ticket_id and (
                c.ticket_id in merged_ticket_ids or c.ticket_id in gh_blocked_ticket_ids
            ):
                auto_candidates.append(c)
                continue
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(signal_mutations, clear_session_id=set())
    candidates = auto_candidates
    if not candidates:
        return [], []

    # Separate by action for batch processing.
    skip_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SKIP_PARKED
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    all_revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]

    # Split REVERT_TASK candidates by world-state check results (GitHub #637).
    # merged_ticket_ids / gh_blocked_ticket_ids come from a pre-pass in
    # reconcile() that runs BEFORE sessions_lock, so no gh subprocess executes
    # here. Candidates with no ticket_id fall through to the normal revert path.
    merged_revert_candidates = [
        c
        for c in all_revert_candidates
        if c.ticket_id and c.ticket_id in merged_ticket_ids
    ]
    gh_blocked_revert_candidates = [
        c
        for c in all_revert_candidates
        if c.ticket_id and c.ticket_id in gh_blocked_ticket_ids
    ]
    revert_candidates = [
        c
        for c in all_revert_candidates
        if c not in merged_revert_candidates and c not in gh_blocked_revert_candidates
    ]

    # SKIP_PARKED: emit event only, no state/queue change.
    for candidate in skip_candidates:
        record_event(
            OrchestratorEventType.SESSION_SALVAGE_SKIPPED,
            {
                "session_id": candidate.session_id,
                "ticket_id": candidate.ticket_id,
                "reason": _SALVAGE_SKIP_REASON,
                "paused_status": candidate.paused_status,
            },
            correlation_id=candidate.ticket_id,
        )

    if not salvage_candidates and not all_revert_candidates:
        return [], []

    # Apply state mutations for salvage, merged-complete, and revert.
    session_by_id = {s.id: s for s in state.sessions}

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )

    # Merged-complete: PR already shipped; mark session COMPLETED, not TIMED_OUT.
    for candidate in merged_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET

    # GH-blocked: can't verify PR status; terminate session so it is not
    # re-detected as a stalled candidate on subsequent ticks.
    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET

    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET

    save_state(state)

    timed_out_ticket_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    merged_tids = {c.ticket_id for c in merged_revert_candidates if c.ticket_id}
    gh_blocked_tids = {c.ticket_id for c in gh_blocked_revert_candidates if c.ticket_id}
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    salvaged_result_by_ticket = {
        c.ticket_id: c.salvage_result
        for c in salvage_candidates
        if c.ticket_id and c.salvage_result
    }
    reverted: list[str] = []
    merged_completed: list[str] = []
    if (
        timed_out_ticket_ids
        or merged_tids
        or gh_blocked_tids
        or salvaged_ticket_ids_set
    ):
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in timed_out_ticket_ids:
                    task.status = QueueItemStatus.PENDING
                    task.session_id = None
                    reverted.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in merged_tids:
                    task.status = QueueItemStatus.COMPLETED
                    task.session_id = None
                    merged_completed.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in gh_blocked_tids:
                    task.status = QueueItemStatus.BLOCKED_ON_USER
                    task.session_id = None
                    changed = True
                elif task.ticket_id in salvaged_ticket_ids_set:
                    result = salvaged_result_by_ticket[task.ticket_id]
                    task.status = _queue_status_for_salvaged(result)
                    changed = True
            if changed:
                save_dev_queue(store)

    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "elapsed_seconds": candidate.elapsed_seconds,
            "last_assistant_message_excerpt": "",
        }
        record_event(OrchestratorEventType.SESSION_TIMED_OUT, payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)

    # Why: no _cleanup_timed_out_worktree for merged — the PR shipped, so the
    # worktree content is already in main; pruning it is not our responsibility.
    for candidate in merged_revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "crashed": False,
                "salvaged": False,
                "reason": _PHANTOM_REAP_MERGED_REASON,
            },
            correlation_id=candidate.ticket_id,
        )

    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _GH_CHECK_BLOCKED_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
            },
            correlation_id=candidate.ticket_id,
        )

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)

    return reverted, merged_completed


def revert_stalled_headless_sessions(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Transition stalled headless DAEMON sessions past budget to TIMED_OUT.

    Passive backstop complementing signal_stop's Stop-hook-driven check.
    signal_stop can only fire at Claude turn boundaries; a session whose agent
    stalled mid-turn (classifier denial, OOM, long subagent chain) produces no
    further Stop firings and would sit ACTIVE forever without this sweep.

    Runs unconditionally before the outage guard so a transient backend hiccup
    does not delay enforcement of the wall-clock budget. The sweep is purely
    time-based; surface liveness is irrelevant.

    Loads the dev queue once (read-only, no lock) for per-ticket budget lookups.
    The existing dev_queue_lock block for the revert step (below) still guards
    the read-write window.

    Calls save_state(state) when any sessions are transitioned — callers must
    not assume state is unchanged on return. On the phantom-handling path in
    reconcile(), save_state is called again later; this double-save is benign
    because save_state is idempotent over identical content.

    Returns the list of ticket IDs whose TicketTask was reverted to PENDING.
    Tickets whose PR is already merged complete instead of reverting (#637).
    Tickets whose gh availability check fails go to BLOCKED_ON_USER (#637).
    See GitHub issue #185, #265.
    """
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_stalled_candidates(
        state, now=now, config=config, task_by_ticket=task_by_ticket
    )
    # Discard merged_completed_ids — this public wrapper's callers expect list[str]
    # (reverted ticket IDs only). merged completions are surfaced through the
    # ReconcileReport.completed_ticket_ids path inside _reconcile_locked (GitHub #637).
    reverted, _merged_completed = _act_on_stalled_candidates(
        state, candidates, now=now, config=config
    )
    return reverted
