"""Act-phase orchestration for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: the reap-policy routing
gate, wall-clock revert, retry-cap park, finalize-blocked park, and liveness
veto are gone -- elapsed time never dispositions a session. The act phase
handles COMPLETE_FOREIGN_RESULT candidates and, since #2426,
ROUTE_EMITTED_SENTINEL candidates from the detect phase.
See GitHub #185, #552, #1470, #2426, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.reconcile._shared import ProposedAction
from cw.reconcile.stalled._events import (
    _emit_stalled_foreign_result_events,
    _emit_stalled_routed_events,
)
from cw.reconcile.stalled._mutations import (
    _apply_stalled_queue_mutations,
    _apply_stalled_routed_mutations,
    _apply_stalled_state_mutations,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState
    from cw.reconcile._shared import ReapCandidate


def _act_on_stalled_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> None:
    """Act phase for headless DAEMON sessions carrying a foreign result.

    Consumes COMPLETE_FOREIGN_RESULT candidates from ``_detect_stalled_
    candidates``: completes the session from its own already-recorded
    ``last_result``, routes the owning task, and emits the completion event.
    Since #2426, also consumes ROUTE_EMITTED_SENTINEL candidates (a validated
    foreign ``INTERMEDIATE_ADVANCE_STATUSES`` result): routes the sentinel
    through the shared stage-aware authority instead of completing the task
    outright. Constructive by construction -- no reap-policy routing is
    needed because nothing here destroys in-flight work.
    """
    if not candidates:
        return
    foreign_result_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.COMPLETE_FOREIGN_RESULT
    ]
    routed_sentinel_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]
    if not foreign_result_candidates and not routed_sentinel_candidates:
        return
    session_by_id = {s.id: s for s in state.sessions}
    if foreign_result_candidates:
        _apply_stalled_state_mutations(
            session_by_id,
            now=now,
            foreign_result_candidates=foreign_result_candidates,
        )
    accepted_routed_candidates: list[ReapCandidate] = []
    if routed_sentinel_candidates:
        accepted_routed_candidates = _apply_stalled_routed_mutations(
            session_by_id,
            routed_sentinel_candidates,
            now=now,
        )
    save_state(state)
    if foreign_result_candidates:
        _apply_stalled_queue_mutations(foreign_result_candidates)
        _emit_stalled_foreign_result_events(session_by_id, foreign_result_candidates)
    if accepted_routed_candidates:
        _emit_stalled_routed_events(session_by_id, accepted_routed_candidates)
