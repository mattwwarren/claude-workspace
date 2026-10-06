"""Act-phase event emission and surface teardown for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: only the
COMPLETE_FOREIGN_RESULT emission and, since #2426, ROUTE_EMITTED_SENTINEL
remain. The surface stop here is cleanup of a session whose work another
authority already recorded as terminal -- it is not a timer-driven kill. The
stop is queued on the caller's post-lock sink and runs after
``sessions_lock`` releases (#1232). See GitHub #185, #552, #1470, #2426,
ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.reconcile.deferred import defer_surface_stop
from cw.reconcile.dispositions import (
    build_salvage_completion_payload,
    emit_routed_sentinel_completion,
)

if TYPE_CHECKING:
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate
    from cw.reconcile.deferred import DeferredReconcileJobs


def _emit_stalled_foreign_result_events(
    session_by_id: dict[str, Session],
    foreign_result_candidates: list[ReapCandidate],
    *,
    deferred: DeferredReconcileJobs,
) -> None:
    """Emit salvaged SESSION_COMPLETED + queue surface stop for foreign results.

    #1470. The stop is evidence-driven: the session's own ``last_result``
    already carries a terminal sentinel, so the surface is done -- this is a
    completed session's teardown, not a timeout. It is queued on *deferred*
    after the emit (#1232).
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
            defer_surface_stop(deferred, session.surface_ref)


def _emit_stalled_routed_events(
    session_by_id: dict[str, Session],
    routed_candidates: list[ReapCandidate],
    *,
    deferred: DeferredReconcileJobs,
) -> None:
    """Emit salvaged SESSION_COMPLETED + queue surface stop for routed sentinels.

    #2426. Only for candidates ``_apply_stalled_routed_mutations`` actually
    accepted (routed) -- a stage-mismatch refusal must not fire this event or
    stop a still-live surface. The stop is queued on *deferred* (#1232).
    """
    for candidate in routed_candidates:
        if candidate.routed_sentinel is None:
            continue  # Invariant: ROUTE_EMITTED_SENTINEL always has routed_sentinel
        emit_routed_sentinel_completion(
            session_by_id[candidate.session_id],
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
            deferred=deferred,
        )
