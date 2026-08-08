"""Detect-phase classification for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: the idle-watchdog
budget, confirm-before-reap counter, git-salvage, revert, and park
dispositions are gone -- transcript quietness never dispositions a session.
What remains is the unrouted-sentinel check (#578): a session whose
transcript already carries an emitted sentinel that ``signal_stop`` never
routed is routed forward, which is positive evidence of completion, not a
timeout. Every function here is read-only: zero writes to state, queue, or
event bus. See GitHub #105, #121, #552, #578, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import DEFAULT_LANE, SessionOrigin
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, OrchestratorConfig, Session, TicketTask


def _detect_idle_candidate_for_session(
    session: Session,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task: TicketTask | None,
    ticket_id: str | None,
) -> ReapCandidate | None:
    """Return a ROUTE_EMITTED_SENTINEL candidate for an unrouted sentinel, or None.

    An emitted sentinel is positive evidence the worker completed; the
    ``sentinel_unrouted_check_seconds`` threshold (300 s) is only a re-check
    delay before parsing the transcript, not a disposition timer -- a session
    with no sentinel is never dispositioned here regardless of elapsed time.
    Guard: ``last_result is None`` means signal_stop never ran — prevents
    double-routing. Constructive, not a reap. See GitHub #578.
    """
    elapsed = (now - session.started_at).total_seconds()
    unrouted_check = config.sentinel_unrouted_check_seconds
    if session.last_result is not None or elapsed < unrouted_check:
        return None
    routed = _parse_any_sentinel_from_transcript(session)
    if routed is None:
        return None
    _routed_result, _csid = routed
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=ticket_id,
        routed_sentinel=_routed_result,
        salvage_csid=_csid,
        elapsed_seconds=elapsed,
        lane=task.lane if task else DEFAULT_LANE,
        client=session.client,
    )


def _detect_idle_candidates(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Pure classification phase for live DAEMON sessions with unrouted sentinels.

    Returns a list of ReapCandidate objects (ROUTE_EMITTED_SENTINEL only).
    Makes zero writes to state, queue, or event bus. See GitHub #552, #578,
    ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if _has_terminal_sentinel(session):
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        candidate = _detect_idle_candidate_for_session(
            session,
            now=now,
            config=config,
            task=task,
            ticket_id=ticket_id,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates
