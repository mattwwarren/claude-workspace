"""Act-phase state and dev-queue mutations for the stalled-headless sweep.

Extracted verbatim from the historical flat ``cw.reconcile.stalled`` module by
the #1484 package split. These helpers write session state in place (the
caller owns the ``save_state`` flush) and apply dev-queue status transitions
under ``dev_queue_lock``. See GitHub #185, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import (
    _derive_disposition,
    _extract_pr_url,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.models import (
    CompletionReason,
    QueueItemStatus,
    ReapReason,
    SessionStatus,
)
from cw.reconcile._shared import (
    _FINALIZE_BLOCKED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _foreign_result_target_queue_status,
    _queue_status_for_salvaged,
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
    salvage_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    finalize_blocked_candidates: list[ReapCandidate],
    reset_salvage_skip_candidates: list[ReapCandidate],
    foreign_result_candidates: list[ReapCandidate],
) -> list[ReapCandidate]:
    """Apply in-place session-state mutations for stalled dispositions.

    Mirrors the pattern from _apply_idle_state_mutations; save_state is left
    to the caller's combined flush.

    Returns the door-accepted salvage subset (RFC 0012 A3, #1459): any candidate
    whose ``_apply_salvaged_completion`` returned ``refused=True`` (first-writer-
    wins) is dropped, so the caller routes tickets / emits events off the
    accepted list rather than the raw ``salvage_candidates`` (which is built from
    the candidate list independently of what the door actually wrote).

    ``foreign_result_candidates`` (#1470) are completed unconditionally -- no
    door arbitration needed, since the result being completed from IS the
    session's own already-recorded ``last_result`` (there is nothing to
    refuse). Unlike ``_apply_salvaged_completion``'s own-session salvage path,
    this does not backfill ``session.cost_usd``: the more-directly-comparable
    precedent (``_apply_stalled_routed_mutations``/ROUTE_EMITTED_SENTINEL,
    below) does not set it either, and a foreign result was never captured
    through this session's own run, so there is no "recovered cost" to
    restore.
    """
    accepted_salvage: list[ReapCandidate] = []
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        outcome = _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )
        if outcome.refused:
            continue
        accepted_salvage.append(candidate)
    # Merged-complete: PR already shipped; mark session COMPLETED, not TIMED_OUT.
    for candidate in merged_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET
    # GH-blocked: can't verify PR status; terminate so not re-detected as stalled.
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
        # #1030: read the candidate's own reap_reason (branched at construction
        # in _resolve_wall_clock_candidate) instead of re-deriving it here from
        # usage_limit_detected — keeps this in sync with the SESSION_REAP_PROPOSED
        # audit event, which reads candidate.reap_reason before this apply phase
        # runs. Same pattern as the cap-park loop below.
        session.reap_reason = candidate.reap_reason
    # Cap exceeded: terminate and park BLOCKED_ON_USER (not re-queued to PENDING).
    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        # #1030: read the candidate's own reap_reason (branched at detect time,
        # including the new USAGE_LIMIT_CUTOFF case) instead of re-hardcoding
        # the pre-#1030 STALLED_RETRY_CAP_PARKED constant — the detect-phase
        # branch above would otherwise be inert.
        session.reap_reason = candidate.reap_reason
    # Finalize-blocked: work complete, PR not opened. Preserve worktree for rescue.
    # Write branch into last_result so rescue_finalize_blocked_sessions can find it.
    for candidate in finalize_blocked_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.FINALIZE_BLOCKED
        session.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": candidate.branch,
        }
    # Recovery: zero the salvage-skip latch (#974). Reset-eligibility was
    # already gated at detect time (session.consecutive_salvage_skips != 0),
    # so every candidate here is a real transition back to 0.
    for candidate in reset_salvage_skip_candidates:
        session_by_id[candidate.session_id].consecutive_salvage_skips = 0

    # COMPLETE_FOREIGN_RESULT (#1470): complete directly from the session's
    # own already-recorded last_result -- no door write (R1), no cost_usd
    # backfill (see docstring above).
    for candidate in foreign_result_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL

    return accepted_salvage


def _apply_stalled_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> list[ReapCandidate]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for wall-clock-expired advance workers.

    Path 1 backstop act phase (GitHub #1149). A direct structural mirror of
    ``idle._apply_idle_routed_mutations``: route the emitted advance sentinel
    through the shared staged-advance authority (``_apply_sentinel_to_task`` ->
    ``apply_staged_decision``), then mark the session COMPLETED/NORMAL only when
    the route was accepted. On a stage-mismatch refusal (``routed=False``) the
    session is NOT completed -- the candidate is dropped from the accepted list
    so the session falls through to the ordinary cap / wall-clock-revert path on
    the next tick rather than being torn down on a refusal. (Unlike idle.py, no
    paused_status marker is stamped: stalled.py's detect phase only builds a
    candidate for a same-/later-stage position, which routes successfully, so
    there is no earlier-stage refusal loop to break here.)

    ``_apply_sentinel_to_task`` acquires its own ``dev_queue_lock``; session
    state is flushed by the caller's ``save_state``. ``session_by_id`` is built
    from ``state.sessions`` by reference, so these mutations persist in the
    caller's existing ``save_state(state)`` call.
    """
    accepted: list[ReapCandidate] = []
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue  # Invariant: ROUTE_EMITTED_SENTINEL has routed_sentinel + csid
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, candidate.session_id, candidate.routed_sentinel
            )
            routed = outcome.routed
        if not routed:
            continue
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
    return accepted


def _apply_foreign_result_queue_mutation(
    task: TicketTask,
    validated: AutoDevResult | BlockedResult,
) -> None:
    """Route a RUNNING task off a validated COMPLETE_FOREIGN_RESULT result.

    Extracted from ``_apply_stalled_queue_mutations``'s per-task dispatch loop
    to keep that function under the branch/statement cap (#1470). Mirrors
    ``concierge._route_park_marker_poison_task``'s foreign-result arm; does
    NOT clear ``task.session_id``, matching the existing salvaged-ticket
    branch immediately below (session_id is kept for operator traceability).
    """
    dumped = validated.model_dump(mode="json")
    transition_task_status(
        task,
        _foreign_result_target_queue_status(validated),
        disposition=_derive_disposition(validated.status),
        pr_url=_extract_pr_url(dumped),
    )


def _apply_stalled_queue_mutations(
    revert_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
    foreign_result_candidates: list[ReapCandidate],
) -> tuple[list[str], list[str]]:
    """Apply dev-queue status changes for stalled-session dispositions.

    Acquires ``dev_queue_lock`` for the read+write window; writes only when at
    least one task changed. Returns (reverted_ticket_ids, merged_completed_ids).
    """
    timed_out_ticket_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    merged_tids = {c.ticket_id for c in merged_revert_candidates if c.ticket_id}
    gh_blocked_tids = {c.ticket_id for c in gh_blocked_revert_candidates if c.ticket_id}
    park_disposition_by_tid = {
        c.ticket_id: c.paused_status for c in park_candidates if c.ticket_id
    }
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    foreign_result_by_ticket: dict[str, AutoDevResult | BlockedResult] = {
        c.ticket_id: c.routed_sentinel
        for c in foreign_result_candidates
        if c.ticket_id and c.routed_sentinel is not None
    }
    reverted: list[str] = []
    merged_completed: list[str] = []
    if not (
        timed_out_ticket_ids
        or merged_tids
        or gh_blocked_tids
        or park_disposition_by_tid
        or salvaged_ticket_ids_set
        or foreign_result_by_ticket
    ):
        return reverted, merged_completed
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in timed_out_ticket_ids:
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                reverted.append(task.ticket_id)
                changed = True
            elif task.ticket_id in merged_tids:
                # Why: PR URL is not in hand here — not worth a second gh call.
                transition_task_status(
                    task, QueueItemStatus.COMPLETED, disposition="shipped"
                )
                task.session_id = None
                merged_completed.append(task.ticket_id)
                changed = True
            elif task.ticket_id in gh_blocked_tids:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=_GH_CHECK_BLOCKED_REASON,
                )
                task.session_id = None
                changed = True
            elif task.ticket_id in park_disposition_by_tid:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=park_disposition_by_tid[task.ticket_id],
                )
                task.session_id = None
                changed = True
            elif task.ticket_id in salvaged_ticket_ids_set:
                result = salvaged_result_by_ticket[task.ticket_id]
                last_result = result.model_dump(mode="json")
                transition_task_status(
                    task,
                    _queue_status_for_salvaged(result),
                    disposition=_derive_disposition(result.status),
                    pr_url=_extract_pr_url(last_result),
                )
                changed = True
            elif task.ticket_id in foreign_result_by_ticket:
                _apply_foreign_result_queue_mutation(
                    task, foreign_result_by_ticket[task.ticket_id]
                )
                changed = True
        if changed:
            save_dev_queue(store)
    return reverted, merged_completed


def _apply_finalize_blocked_queue_mutations(
    candidates: list[ReapCandidate],
) -> None:
    """Route RUNNING tasks for finalize-blocked sessions to BLOCKED_ON_USER.

    Separate from _apply_stalled_queue_mutations because finalize-blocked tasks
    are RUNNING at detection time (not yet reverted to PENDING by a prior tick).
    Under dev_queue_lock; no-op when the candidate set is empty. See GitHub #812.
    """
    ticket_ids = {c.ticket_id for c in candidates if c.ticket_id}
    if not ticket_ids:
        return
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.ticket_id not in ticket_ids:
                continue
            if task.status != QueueItemStatus.RUNNING:
                continue
            transition_task_status(
                task,
                QueueItemStatus.BLOCKED_ON_USER,
                disposition=_FINALIZE_BLOCKED_REASON,
            )
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
