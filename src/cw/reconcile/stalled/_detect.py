"""Detect-phase classification for the stalled-headless sweep.

Evidence-only since the process-kill-timeout removal: the wall-clock budget,
retry-cap, finalize-blocked, and liveness-veto dispositions are gone --
elapsed time never dispositions a session. This sweep produces
COMPLETE_FOREIGN_RESULT for a terminal foreign result and, since #2426,
ROUTE_EMITTED_SENTINEL for an ``INTERMEDIATE_ADVANCE_STATUSES`` foreign result
(``stage_complete``) -- a still-live worker's out-of-band ``cw result emit``
must advance the pipeline's stage, not land the ticket terminal-COMPLETED at
whatever stage it happened to be at (#2382 made this possible: the emit CLI is
write-only and never flips ``session.status``, so a genuinely still-running
worker can hold a terminal-shaped result while ``session.status`` stays
ACTIVE/IDLE). Every function here is read-only: zero writes to state, queue,
or event bus. Since #2435, a live session whose result was written by ``cw
result emit`` (``last_result_source == LastResultSource.EMIT_CLI``) is never
claimed by this sweep at all -- that worker's own Stop hook is still running
and is the routing authority for it. See GitHub #185, #552, #1470, #2382,
#2426, #2435, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES, AutoDevResult
from cw.models import DEFAULT_LANE, LastResultSource, SessionOrigin
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _is_headless,
    _validate_existing_result_for_routing,
    stage_refusal_latched,
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
    """Append a foreign-result candidate; return whether the guard fired.

    A live session whose ``last_result`` already carries a terminal sentinel
    from another authority (e.g. an out-of-band ``cw result emit``, which by
    contract never flips ``session.status``) is completed directly from that
    result. An unroutable/invalid foreign result still short-circuits (True
    with no disposition candidate) rather than being re-offered every tick --
    it is not this sweep's scope to disposition an unroutable foreign write,
    only to stop re-parsing the transcript for one. See GitHub #1470.

    #2426: a validated result whose status is an intermediate stage-advance
    claim (``INTERMEDIATE_ADVANCE_STATUSES``, today exactly ``stage_complete``)
    is reclassified as ``ROUTE_EMITTED_SENTINEL`` instead of
    ``COMPLETE_FOREIGN_RESULT`` -- it must advance the owning task's stage
    through the same stage-aware authority ``phantom._detect`` already uses
    (``_apply_sentinel_to_task`` / ``apply_staged_decision``), not land the
    ticket terminal-COMPLETED at its current stage. A session already latched
    ``already_refused`` by a prior tick's stage-mismatch refusal is never
    re-offered.

    #2435: a session whose ``last_result`` was itself written by ``cw result
    emit`` (``LastResultSource.EMIT_CLI``) is never offered here, even though
    the caller's loop already restricts candidates to ``_LIVE_STATUSES``. Such
    a session's own Stop hook (#536 emit precedence) is running right now and
    is the routing authority for that result -- it already handles stage
    advance, attention events, and fix-dispatch handoffs. Racing it from this
    sweep risks a second, conflicting ``_apply_sentinel_to_task`` call against
    the same task row.
    """
    if session.last_result_source == LastResultSource.EMIT_CLI:
        return False
    if not _has_terminal_sentinel(session):
        return False
    # #1149: without this latch a stale/earlier-stage ``stage_complete`` claim
    # would be re-offered as a candidate forever.
    if stage_refusal_latched(session):
        return True
    validated_foreign = _validate_existing_result_for_routing(session.last_result)
    if validated_foreign is not None:
        lane = task.lane if task else DEFAULT_LANE
        if (
            isinstance(validated_foreign, AutoDevResult)
            and validated_foreign.status in INTERMEDIATE_ADVANCE_STATUSES
        ):
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
                    ticket_id=ticket_id,
                    routed_sentinel=validated_foreign,
                    lane=lane,
                    client=session.client,
                )
            )
        else:
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.COMPLETE_FOREIGN_RESULT,
                    ticket_id=ticket_id,
                    routed_sentinel=validated_foreign,
                    lane=lane,
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

    Returns a list of ReapCandidate objects (COMPLETE_FOREIGN_RESULT or, since
    #2426, ROUTE_EMITTED_SENTINEL for an INTERMEDIATE_ADVANCE_STATUSES foreign
    result). Makes zero writes to state, queue, or event bus. Elapsed
    wall-clock time is deliberately never consulted -- a session is only
    dispositioned here on the positive evidence of an already-recorded
    terminal result. Since #2435, an EMIT_CLI-sourced result on a still-live
    session is withheld entirely (see ``_append_foreign_result_candidate``) --
    that worker's own Stop hook owns routing it. See GitHub #552, #1470,
    #2426, #2435, ADR-0006.
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
