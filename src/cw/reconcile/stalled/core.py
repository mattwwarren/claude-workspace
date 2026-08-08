"""Act-phase orchestration for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: the reap-policy routing
gate, wall-clock revert, retry-cap park, finalize-blocked park, and liveness
veto are gone -- elapsed time never dispositions a session. The act phase
handles only COMPLETE_FOREIGN_RESULT candidates from the detect phase.
See GitHub #185, #552, #1470, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.reconcile._shared import ProposedAction
from cw.reconcile.stalled._events import _emit_stalled_foreign_result_events
from cw.reconcile.stalled._mutations import (
    _apply_stalled_queue_mutations,
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
    """Act phase for headless DAEMON sessions carrying a foreign terminal result.

    Consumes COMPLETE_FOREIGN_RESULT ReapCandidate objects from
    ``_detect_stalled_candidates``: completes the session from its own
    already-recorded ``last_result``, routes the owning task, and emits the
    completion event. Constructive by construction -- no reap-policy routing
    is needed because nothing here destroys in-flight work.
    """
    if not candidates:
        return
    foreign_result_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.COMPLETE_FOREIGN_RESULT
    ]
    if not foreign_result_candidates:
        return
    session_by_id = {s.id: s for s in state.sessions}
    _apply_stalled_state_mutations(
        session_by_id,
        now=now,
        foreign_result_candidates=foreign_result_candidates,
    )
    save_state(state)
    _apply_stalled_queue_mutations(foreign_result_candidates)
    _emit_stalled_foreign_result_events(session_by_id, foreign_result_candidates)
