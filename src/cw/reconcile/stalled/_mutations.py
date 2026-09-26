"""Act-phase state and dev-queue mutations for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: only the
COMPLETE_FOREIGN_RESULT disposition and, since #2426, ROUTE_EMITTED_SENTINEL
remain. These helpers write session state in place (the caller owns the
``save_state`` flush) and apply dev-queue status transitions under
``dev_queue_lock``. See GitHub #185, #552, #1470, #2426, ADR-0006.
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
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _apply_sentinel_to_task,
    _foreign_result_target_queue_status,
    _resolve_routed_sentinel,
)

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


def _apply_stalled_routed_mutations(
    session_by_id: dict[str, Session],
    routed_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> list[ReapCandidate]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for a live worker's foreign sentinel.

    #2426.

    A third sibling of ``phantom._apply_phantom_routed_mutations`` and
    ``idle._apply_idle_routed_mutations`` -- neither is reused directly here.
    ``idle``'s version unconditionally overwrites ``session.last_result`` on a
    refusal (safe only because idle's precondition is ``last_result is None``
    going in); stalled's precondition is the opposite -- ``last_result`` is
    always already the emitted terminal dict -- so an unconditional overwrite
    would destroy it. ``phantom``'s version merges safely (matching stalled's
    precondition) but also stamps ``session.reap_reason =
    ReapReason.PHANTOM_SURFACE``, mislabeling a still-registered-live session
    as a phantom-surface teardown. This function reuses phantom's merge-safe
    refusal-stamp shape while omitting ``reap_reason`` (matching stalled's own
    ``COMPLETE_FOREIGN_RESULT`` convention, which never stamps ``reap_reason``
    either).

    Routes the emitted advance sentinel through the shared staged-advance
    authority (``_apply_sentinel_to_task`` -> ``apply_staged_decision``) so the
    task advances to its next stage, then marks the session COMPLETED/NORMAL
    -- but only when the route was accepted. A stage-mismatch refusal leaves
    the task untouched and the session live (not completed/torn down): the
    session is a still-registered-live headless worker, not a phantom, so
    refusing here must not orphan it.

    Returns only the candidates actually routed, so the caller's event
    emission fires solely for those.
    """
    accepted: list[ReapCandidate] = []
    for candidate in routed_candidates:
        routed_sentinel = _resolve_routed_sentinel(candidate)
        if routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        routed = True
        task_already_terminal = False
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, session, routed_sentinel, now=now
            )
            routed = outcome.routed
            task_already_terminal = outcome.task_already_terminal
        if not routed and task_already_terminal:
            # #2140-shape race: another authority already landed this
            # ticket's task genuinely terminal before this lookup ran. Unlike
            # phantom's/idle's identical-looking arm, stalled's own
            # precondition (this session was only a candidate because
            # ``_has_terminal_sentinel`` already found a terminal sentinel in
            # ``session.last_result``) guarantees ``emit_result_on`` would
            # always see ``has_terminal_result(...) == True`` and always
            # return ``refused=True`` -- that door is never open here, so
            # complete the session directly instead (#2426 fix-cycle-1).
            session.status = SessionStatus.COMPLETED
            session.completed_at = now
            session.completed_reason = CompletionReason.NORMAL
            session.last_result = routed_sentinel.model_dump(mode="json")
            if candidate.salvage_csid is not None:
                session.claude_session_id = candidate.salvage_csid
            accepted.append(candidate)
            continue
        if not routed:
            # #1149-shape stage-mismatch refusal: leave the task untouched and
            # merge (never clobber) the refusal flag into the pre-existing
            # last_result dict, matching phantom's merge-safe convention --
            # stalled's precondition guarantees last_result is always already
            # a dict here (it is the terminal sentinel that made this a
            # candidate in the first place).
            existing = session.last_result
            if isinstance(existing, dict):
                session.last_result = {
                    **existing,
                    _SENTINEL_ADVANCE_REFUSED_KEY: True,
                }
            else:
                session.last_result = {
                    _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
                }
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = routed_sentinel.model_dump(mode="json")
        if candidate.salvage_csid is not None:
            session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
    return accepted


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
