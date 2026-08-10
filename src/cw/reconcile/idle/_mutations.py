"""Act-phase session-state and dev-queue mutations for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: only the
ROUTE_EMITTED_SENTINEL mutation remains. ``save_state`` itself is left to the
caller in ``core``. See GitHub #105, #121, #552, #578, #1031, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import (
    CompletionReason,
    SessionStatus,
)
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _apply_sentinel_to_task,
)

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

    Mirrors ``phantom._apply_phantom_routed_mutations``: routes the emitted
    advance sentinel through the shared staged-advance authority
    (``_apply_sentinel_to_task`` -> ``apply_staged_decision``), then marks the
    session COMPLETED/NORMAL -- but only when the route was accepted.

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident), the session must NOT be completed here --
    ``_detect_idle_candidates`` only builds these candidates when the surface
    is still reported alive by the daemon, so an unconditional completion
    would tear down a live surface, not just orphan a task row.

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
            # stays False.
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
