"""Detect-phase classification for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: the idle-watchdog
budget, confirm-before-reap counter, git-salvage, revert, and park
dispositions are gone -- transcript quietness never dispositions a session.
What remains is the unrouted-sentinel check (#578): a session whose
transcript already carries an emitted sentinel that ``signal_stop`` never
routed is routed forward, which is positive evidence of completion, not a
timeout. Since #2458 the same check also covers a live session holding a
staged ``cw result emit`` result the Stop hook never routed. Every function
here is read-only: zero writes to state, queue, or event bus. See GitHub
#105, #121, #552, #578, #2458, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import DEFAULT_LANE, LastResultSource, SessionOrigin
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    holds_staged_emit_result,
    ticket_id_for_session,
)
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, OrchestratorConfig, Session, TicketTask


def _staged_emit_result_refused(session: Session) -> bool:
    """Whether a prior tick already refused this staged result (#1149).

    The phantom sweep's ``already_refused`` check
    (``phantom._detect._detect_phantom_candidates``), applied verbatim to the
    idle sweep's staged-result producer. A refusal flag merged INTO a staged
    ``last_result`` (as the phantom and stalled sweeps stamp it) leaves it
    terminal-shaped, so without this check it would be reconstructed and
    re-refused on every tick, forever.
    """
    last_result = session.last_result
    return isinstance(last_result, dict) and (
        last_result.get(_PAUSED_STATUS_KEY) == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        or last_result.get(_SENTINEL_ADVANCE_REFUSED_KEY) is True
    )


def _staged_emit_candidate(
    session: Session,
    *,
    ticket_id: str | None,
    lane: str,
    elapsed: float,
) -> ReapCandidate | None:
    """Route a live session's staged ``cw result emit`` result forward (#2458).

    The idle sweep's port of the phantom sweep's dead-session staged-result
    producer: the Stop hook is otherwise the only authority that can route an
    emit_cli result, and a worker whose async-completion wakeup is dropped
    upstream (#1889) never fires another Stop. Returns ``None`` when the
    result was already refused or reconstructs into neither arm of the
    ``AutoDevResult``/``BlockedResult`` union -- there is nothing to route.
    """
    if _staged_emit_result_refused(session):
        return None
    staged = reconstruct_staged_sentinel(session.last_result)
    if staged is None:
        return None
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=ticket_id,
        routed_sentinel=staged,
        result_source=LastResultSource.EMIT_CLI,
        # May legitimately be None: this producer reads session state, not a
        # transcript, so there is no csid to derive. _resolve_routed_sentinel
        # tolerates it.
        salvage_csid=session.claude_session_id,
        elapsed_seconds=elapsed,
        lane=lane,
        client=session.client,
    )


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
    delay before routing, not a disposition timer -- a session with no
    sentinel is never dispositioned here regardless of elapsed time.

    Two producers share that delay. A staged ``cw result emit`` result
    (:func:`~cw.reconcile._shared.holds_staged_emit_result`) is routed off
    ``last_result`` itself (#2458). Otherwise the guard ``last_result is
    None`` means signal_stop never ran -- prevents double-routing -- and the
    transcript is re-parsed. Constructive, not a reap. See GitHub #578, #2458.
    """
    elapsed = (now - session.started_at).total_seconds()
    if elapsed < config.sentinel_unrouted_check_seconds:
        return None
    lane = task.lane if task else DEFAULT_LANE
    if holds_staged_emit_result(session):
        return _staged_emit_candidate(
            session, ticket_id=ticket_id, lane=lane, elapsed=elapsed
        )
    if session.last_result is not None:
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
        lane=lane,
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
    A session holding a terminal ``last_result`` is skipped unless that
    result is a staged ``cw result emit`` one (#2458) -- the sweep is the
    backstop authority for those when the Stop hook never routes them.
    Makes zero writes to state, queue, or event bus. See GitHub #552, #578,
    #2458, ADR-0006.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if _has_terminal_sentinel(session) and not holds_staged_emit_result(session):
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
