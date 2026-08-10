"""Act-phase lifecycle-event emission for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: only the
ROUTE_EMITTED_SENTINEL completion emission remains. Runs after the caller's
``save_state`` flush. See GitHub #105, #121, #552, #578, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.reconcile.dispositions import emit_routed_sentinel_completion

if TYPE_CHECKING:
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


def _emit_idle_completion_events(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
) -> None:
    """Emit SESSION_COMPLETED + stop surfaces for routed-sentinel completions.

    The surface stop inside ``emit_routed_sentinel_completion`` is
    evidence-driven: the session emitted a sentinel, so its work is recorded
    as done -- this is a completed session's teardown, not a timeout.
    """
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        emit_routed_sentinel_completion(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
        )
