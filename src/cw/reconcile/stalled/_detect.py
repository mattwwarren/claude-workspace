"""Detect-phase classification for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: the wall-clock budget,
retry-cap, finalize-blocked, and liveness-veto dispositions are gone --
elapsed time never dispositions a session. The only candidate this sweep
still produces is COMPLETE_FOREIGN_RESULT, which acts on a terminal result
some other authority already recorded on the session (e.g. an out-of-band
``cw result emit``). Every function here is read-only: zero writes to state,
queue, or event bus. See GitHub #185, #552, #1470, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import DEFAULT_LANE, SessionOrigin
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _is_headless,
    _validate_existing_result_for_routing,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from cw.models import CwState, Session, TicketTask


def _append_foreign_result_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
) -> bool:
    """Append a COMPLETE_FOREIGN_RESULT candidate; return whether the guard fired.

    A live session whose ``last_result`` already carries a terminal sentinel
    from another authority (e.g. an out-of-band ``cw result emit``, which by
    contract never flips ``session.status``) is completed directly from that
    result. An unroutable/invalid foreign result still short-circuits (True
    with no disposition candidate) rather than being re-offered every tick --
    it is not this sweep's scope to disposition an unroutable foreign write,
    only to stop re-parsing the transcript for one. See GitHub #1470.
    """
    if not _has_terminal_sentinel(session):
        return False
    validated_foreign = _validate_existing_result_for_routing(session.last_result)
    if validated_foreign is not None:
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.COMPLETE_FOREIGN_RESULT,
                ticket_id=ticket_id,
                routed_sentinel=validated_foreign,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
        )
    return True


def _detect_stalled_candidates(
    state: CwState,
    *,
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Pure classification phase for headless DAEMON sessions.

    Returns a list of ReapCandidate objects (COMPLETE_FOREIGN_RESULT only).
    Makes zero writes to state, queue, or event bus. Elapsed wall-clock time
    is deliberately never consulted -- a session is only dispositioned here on
    the positive evidence of an already-recorded terminal result.
    See GitHub #552, #1470, ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        # Only live, headless DAEMON sessions are eligible for this sweep.
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
        ):
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        _append_foreign_result_candidate(candidates, session, task, ticket_id)
    return candidates
