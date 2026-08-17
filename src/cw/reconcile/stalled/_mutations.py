"""Act-phase state and dev-queue mutations for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: only the
COMPLETE_FOREIGN_RESULT disposition remains. These helpers write session
state in place (the caller owns the ``save_state`` flush) and apply dev-queue
status transitions under ``dev_queue_lock``. See GitHub #185, #552, #1470,
ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import (
    _extract_pr_url,
    _hold_aware_disposition,
    _result_blocker_reason,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.models import (
    CompletionReason,
    QueueItemStatus,
    SessionStatus,
)
from cw.reconcile._shared import _foreign_result_target_queue_status

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult, BlockedResult
    from cw.models import Session, TicketTask
    from cw.reconcile._shared import ReapCandidate


def _apply_stalled_state_mutations(
    session_by_id: dict[str, Session],
    *,
    now: datetime,
    foreign_result_candidates: list[ReapCandidate],
) -> None:
    """Apply in-place session-state mutations for foreign-result completions.

    COMPLETE_FOREIGN_RESULT (#1470) completes directly from the session's own
    already-recorded ``last_result`` -- no door arbitration needed, since the
    result being completed from IS the session's own record (there is nothing
    to refuse), and no ``cost_usd`` backfill (a foreign result was never
    captured through this session's own run). ``save_state`` is left to the
    caller's flush.
    """
    for candidate in foreign_result_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL


def _apply_foreign_result_queue_mutation(
    task: TicketTask,
    validated: AutoDevResult | BlockedResult,
) -> None:
    """Route a RUNNING task off a validated COMPLETE_FOREIGN_RESULT result.

    Mirrors ``concierge._route_park_marker_poison_task``'s foreign-result arm;
    does NOT clear ``task.session_id`` (kept for operator traceability).
    """
    # Deferred, not module-top: cw.dispatch's package __init__ imports
    # cw.reconcile (loop.py/gating.py/lanes.py), so a top-level import of any
    # cw.dispatch submodule here is a real circular import at package-init
    # time. Same shape as the #698 reconcile._shared -> cw.dispatch precedent
    # and tasks.py's deferred cw.dispatch.routing import. See #1750.
    from cw.dispatch.productivity import extract_claim_evidence, is_unproductive

    dumped = validated.model_dump(mode="json")
    blocker_reason = _result_blocker_reason(validated)
    transition_task_status(
        task,
        _foreign_result_target_queue_status(validated),
        disposition=_hold_aware_disposition(validated.status, blocker_reason),
        pr_url=_extract_pr_url(dumped),
        # #1750: classify off the real sentinel, reusing the `dumped` payload
        # computed above. A BlockedResult carries no commits/review keys, so
        # extract_claim_evidence naturally reads it as zero evidence via its
        # plain .get() defaults — no separate branch needed for that union arm.
        unproductive=is_unproductive(extract_claim_evidence(dumped)),
    )


def _apply_stalled_queue_mutations(
    foreign_result_candidates: list[ReapCandidate],
) -> None:
    """Apply dev-queue status changes for foreign-result completions.

    Acquires ``dev_queue_lock`` for the read+write window; writes only when at
    least one task changed.
    """
    foreign_result_by_ticket: dict[str, AutoDevResult | BlockedResult] = {
        c.ticket_id: c.routed_sentinel
        for c in foreign_result_candidates
        if c.ticket_id and c.routed_sentinel is not None
    }
    if not foreign_result_by_ticket:
        return
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in foreign_result_by_ticket:
                _apply_foreign_result_queue_mutation(
                    task, foreign_result_by_ticket[task.ticket_id]
                )
                changed = True
        if changed:
            save_dev_queue(store)
