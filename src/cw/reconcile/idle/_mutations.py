"""Act-phase session-state and dev-queue mutations for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: only the
ROUTE_EMITTED_SENTINEL mutation remains. ``save_state`` itself is left to the
caller in ``core``. See GitHub #105, #121, #552, #578, #1031, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import (
    CompletionReason,
    LastResultSource,
    SessionStatus,
)
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _apply_sentinel_to_task,
    _resolve_routed_sentinel,
)
from cw.result import emit_result_on

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


def _apply_idle_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> tuple[list[ReapCandidate], bool]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for alive-idle workers (#1031).

    Shares ``phantom._apply_phantom_routed_mutations``'s routing shape: both
    route the emitted advance sentinel through the shared staged-advance
    authority (``_apply_sentinel_to_task`` -> ``apply_staged_decision``) via the
    shared ``_resolve_routed_sentinel`` guard (GitHub #1762), then mark the
    session COMPLETED/NORMAL -- but only when the route was accepted.

    Not a byte-for-byte mirror, despite the older wording here: unlike phantom's
    post-#1762 ``reconstruct_staged_sentinel`` producer, every candidate
    ``_detect_idle_candidate_for_session`` builds carries a paired non-``None``
    ``salvage_csid`` (both halves come out of the same
    ``_parse_any_sentinel_from_transcript`` tuple). The csid half of the old
    duplicated guard was therefore inert here; it is dropped rather than
    preserved, because only phantom's genuinely ``None``-tolerant case ever
    depended on it.

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident), the session must NOT be completed here --
    ``_detect_idle_candidates`` only builds these candidates when the surface
    is still reported alive by the daemon, so an unconditional completion
    would tear down a live surface, not just orphan a task row.

    GitHub #2140: a ``not routed`` outcome can also mean
    ``task_already_terminal`` -- the dev-queue task was raced to a genuinely
    terminal status by a concurrent caller before this lookup ran, not a
    stage-mismatch refusal. That case now routes through the door
    (``emit_result_on``) and completes the session instead of falling into
    the stage-mismatch refusal-marker branch, which would otherwise orphan
    this session forever (mirrors the Stop-hook's #1692 carve-out).

    Returns ``(accepted, state_mutated)``. ``accepted`` is only the candidates
    actually routed, so the caller's downstream event emission fires solely for
    those. ``state_mutated`` is True when any session state changed here --
    including a refusal-marker stamp with no accepted candidate -- so the caller
    persists the stamp even on a pure-refusal tick (the marker would otherwise
    be lost and the candidate re-fire forever, GitHub #1149).
    """
    accepted: list[ReapCandidate] = []
    state_mutated = False
    for candidate in routed_sentinel_candidates:
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
        if not routed and not task_already_terminal:
            # #1149: a stage-mismatch refusal (earlier-stage replay / unresolvable
            # position) leaves the task untouched. Stamp a paused_status-only
            # marker so the next tick's `session.last_result is None` unrouted-check
            # gate (_detect_idle_candidate_for_session) stops re-proposing this same
            # doomed candidate forever. No "status" key -> _has_terminal_sentinel
            # stays False.
            session.last_result = {
                _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            }
            state_mutated = True
            continue
        if not routed and task_already_terminal:
            # #2140: another authority already landed this ticket's task
            # genuinely terminal before this call's own lookup ran. Route the
            # completion through the door instead of the raw assignment below
            # (that block is reached only when routed is True) so a foreign
            # authority's already-door-written result is never clobbered.
            emit_outcome = emit_result_on(
                session,
                candidate.routed_sentinel.model_dump(mode="json"),
                source=LastResultSource.SALVAGE_TRANSCRIPT,
            )
            if emit_outcome.refused:
                # emit_result_on() leaves `session` byte-identical on refusal --
                # nothing new for this tick to persist.
                continue
            session.status = SessionStatus.COMPLETED
            session.completed_at = now
            session.completed_reason = CompletionReason.NORMAL
            session.claude_session_id = candidate.salvage_csid
            accepted.append(candidate)
            state_mutated = True
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = routed_sentinel.model_dump(mode="json")
        # #1762: guarded for the same reason as phantom's copy -- the shared
        # guard no longer proves salvage_csid is non-None, and blanking the id
        # the transcript lookups key off would be a silent regression.
        if candidate.salvage_csid is not None:
            session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
        state_mutated = True
    return accepted, state_mutated
