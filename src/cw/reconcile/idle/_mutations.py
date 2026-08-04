"""Act-phase session-state and dev-queue mutations for the silently-idle sweep.

Extracted verbatim from the historical flat ``cw.reconcile.idle`` module by
the package split. ``save_state`` itself is left to the caller in ``core``.
See GitHub #105, #121, #545, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import (
    _extract_pr_url,
    _hold_aware_disposition,
    _result_blocker_reason,
    _stamp_salvage_stage,
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
    _GH_CHECK_BLOCKED_REASON,
    _PAUSED_STATUS_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _SILENTLY_IDLE_REASON,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _queue_status_for_salvaged,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


def _apply_idle_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> tuple[list[ReapCandidate], bool]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for alive-idle workers (#1031).

    Mirrors ``phantom._apply_phantom_routed_mutations``: routes the emitted
    advance sentinel through the shared staged-advance authority
    (``_apply_sentinel_to_task`` -> ``apply_staged_decision``), then marks the
    session COMPLETED/NORMAL -- but only when the route was accepted.

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident), the session must NOT be completed here --
    unlike the phantom sweep, ``_detect_idle_candidates`` only builds these
    candidates when the surface is still reported alive by the daemon, so an
    unconditional completion would tear down a live surface, not just orphan
    a task row.

    Returns ``(accepted, state_mutated)``. ``accepted`` is only the candidates
    actually routed, so the caller's downstream event emission fires solely for
    those. ``state_mutated`` is True when any session state changed here --
    including a refusal-marker stamp with no accepted candidate -- so the caller
    persists the stamp even on a pure-refusal tick, whose ``accepted`` list is
    empty and would otherwise leave ``has_dispositions`` False and skip
    ``save_state`` (the marker would be lost and the candidate re-fire forever,
    GitHub #1149).
    """
    accepted: list[ReapCandidate] = []
    state_mutated = False
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue
        session = session_by_id[candidate.session_id]
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, session, candidate.routed_sentinel, now=now
            )
            routed = outcome.routed
        if not routed:
            # #1149: a stage-mismatch refusal (earlier-stage replay / unresolvable
            # position) leaves the task untouched. Stamp a paused_status-only
            # marker so the next tick's `session.last_result is None` unrouted-check
            # gate (_detect_idle_candidate_for_session) stops re-proposing this same
            # doomed candidate forever. No "status" key -> _has_terminal_sentinel
            # stays False and the ordinary idle-stall machinery still runs.
            session.last_result = {
                _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            }
            state_mutated = True
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
        state_mutated = True
    return accepted, state_mutated


def _apply_idle_state_mutations(
    session_by_id: dict[str, Session],
    *,
    now: datetime,
    counter_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
) -> tuple[bool, list[ReapCandidate]]:
    """Apply in-place session-state mutations for idle dispositions.

    Returns ``(counters_changed, accepted_salvage_candidates)``. ``counters_changed``
    is whether any counter-only update occurred (so the caller can decide to
    save_state even when there are no dispositions). ``accepted_salvage_candidates``
    is the salvage subset the door actually wrote (RFC 0012 A3, #1459) -- any
    candidate whose ``_apply_salvaged_completion`` returned ``refused=True`` is
    dropped, so the caller routes tickets / emits events off the accepted list,
    not the raw one (mirrors ``_apply_idle_routed_mutations``' filtered return one
    call above). save_state itself is left to the caller's combined flush.
    """
    # Counter-only updates: just update the counter and possibly save_state.
    counters_changed = False
    for candidate in counter_candidates:
        session = session_by_id[candidate.session_id]
        session.idle_observation_count = candidate.new_observation_count
        counters_changed = True

    # Salvage completions.
    accepted_salvage: list[ReapCandidate] = []
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        # RFC 0012 A3 (#1459): a door refusal (first-writer-wins) skips the
        # completion; drop the candidate so downstream ticket-routing / event
        # emission never fires for a session that was never actually completed.
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
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # GH-blocked: can't verify PR status; terminate so it is not re-detected.
    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # Recover (revert to PENDING for re-dispatch).
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # Park: flag-only (preserves #348 — no daemon stop, session stays ACTIVE).
    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        session.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
        session.reap_reason = ReapReason.RETRY_CAP_PARKED

    return counters_changed, accepted_salvage


def _apply_idle_queue_mutations(
    revert_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
) -> tuple[list[str], list[str]]:
    """Apply dev-queue status changes for idle dispositions.

    Acquires ``dev_queue_lock`` for the read+write window; writes only when at
    least one task changed. Returns (blocked_ticket_ids, merged_completed_ids).
    """
    recovered_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    # (client, ticket_id) pairs, not bare ticket_id -- merged_revert_candidates
    # can now include FINALIZE-stage / merged-first candidates (GitHub #1054),
    # and ticket_id strings are not globally unique across clients.
    merged_client_tids = {
        (c.client, c.ticket_id)
        for c in merged_revert_candidates
        if c.ticket_id and c.client
    }
    gh_blocked_tids = {c.ticket_id for c in gh_blocked_revert_candidates if c.ticket_id}
    park_disposition_by_tid = {
        c.ticket_id: c.paused_status for c in park_candidates if c.ticket_id
    }
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    blocked: list[str] = []
    merged_completed: list[str] = []
    if not (
        recovered_ids
        or merged_client_tids
        or gh_blocked_tids
        or park_disposition_by_tid
        or salvaged_ticket_ids_set
    ):
        return blocked, merged_completed
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in recovered_ids:
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                changed = True
            elif task.client and (task.client, task.ticket_id) in merged_client_tids:
                # Why: PR URL is not in hand here — not worth a second gh call.
                transition_task_status(
                    task, QueueItemStatus.COMPLETED, disposition="shipped"
                )
                _stamp_salvage_stage(task)
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
                blocked.append(task.ticket_id)
                changed = True
            elif task.ticket_id in salvaged_ticket_ids_set:
                result = salvaged_result_by_ticket[task.ticket_id]
                last_result = result.model_dump(mode="json")
                reason = _result_blocker_reason(result)
                transition_task_status(
                    task,
                    _queue_status_for_salvaged(result),
                    disposition=_hold_aware_disposition(result.status, reason),
                    pr_url=_extract_pr_url(last_result),
                )
                changed = True
        if changed:
            save_dev_queue(store)
    return blocked, merged_completed
