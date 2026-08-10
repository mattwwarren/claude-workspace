"""Act-phase event emission and surface teardown for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: only the
COMPLETE_FOREIGN_RESULT emission remains. The surface stop here is cleanup of
a session whose work another authority already recorded as terminal -- it is
not a timer-driven kill. See GitHub #185, #552, #1470, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.reconcile import _deps
from cw.reconcile.dispositions import build_salvage_completion_payload

if TYPE_CHECKING:
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


def _emit_stalled_foreign_result_events(
    session_by_id: dict[str, Session],
    foreign_result_candidates: list[ReapCandidate],
) -> None:
    """Emit salvaged SESSION_COMPLETED + stop surface for foreign-result completions.

    #1470. The stop is evidence-driven: the session's own ``last_result``
    already carries a terminal sentinel, so the surface is done -- this is a
    completed session's teardown, not a timeout.
    """
    for candidate in foreign_result_candidates:
        if candidate.routed_sentinel is None:
            continue  # Invariant: COMPLETE_FOREIGN_RESULT always has routed_sentinel
        session = session_by_id[candidate.session_id]
        completed_payload = build_salvage_completion_payload(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
        )
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
