"""Act-phase orchestration for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: the reap-policy routing
gate, idle-watchdog budget, confirm counter, git-salvage, revert, and park
dispositions are gone -- transcript quietness never dispositions a session.
The act phase handles only ROUTE_EMITTED_SENTINEL candidates from the detect
phase. See GitHub #105, #121, #552, #578, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.reconcile._shared import ProposedAction
from cw.reconcile.idle._events import _emit_idle_completion_events
from cw.reconcile.idle._mutations import _apply_idle_routed_mutations

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState
    from cw.reconcile._shared import ReapCandidate


def _act_on_idle_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> None:
    """Act phase for live DAEMON sessions with an emitted-but-unrouted sentinel.

    Consumes ROUTE_EMITTED_SENTINEL ReapCandidate objects from
    ``_detect_idle_candidates``: routes the sentinel through the shared
    staged-advance authority, completes the session on acceptance, and emits
    the completion event. A stage-mismatch refusal (#1031) stamps a
    paused-status marker instead so the doomed candidate stops re-firing.
    Constructive by construction -- no reap-policy routing is needed because
    nothing here destroys in-flight work.
    """
    if not candidates:
        return
    routed_sentinel_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]
    if not routed_sentinel_candidates:
        return
    session_by_id = {s.id: s for s in state.sessions}
    accepted, state_mutated = _apply_idle_routed_mutations(
        session_by_id, routed_sentinel_candidates, now=now
    )
    if state_mutated:
        save_state(state)
    _emit_idle_completion_events(session_by_id, accepted)
