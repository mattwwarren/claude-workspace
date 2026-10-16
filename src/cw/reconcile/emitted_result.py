"""Disposition of a dead LOCAL session whose worker already emitted its result (#2699).

cw's responsibility ends at the result a worker records with ``cw result emit``,
never at CI, review or merge. A dead-process session that already holds such a
result is finished: the dead-process local harvest (``cw.reconcile.local``)
routes that result to its queue row and completes the session instead of
synthesizing a second result from git that the first-writer-wins door refuses
on every tick.

:func:`dispose_emitted_result` is the routing half and
:func:`stamp_harvest_completed` the shared completion tail the harvest's two
completion paths both end in.

It routes through ``_apply_sentinel_to_task`` like ``cw spawn close``
(``cw.reconcile.session_close.route_staged_emit_result``, #2458), but is not
a copy of it: this path also completes on an already-consumed result and on a
blocked result that landed the row FAILED, because a dead worker will never
produce another leg, and it pages a permanent stage mismatch. Unifying the two
is tracked in #2704.

Write ordering: the queue row is persisted inside ``_apply_sentinel_to_task``,
while the session's COMPLETED stamp only reaches disk with the caller's single
``save_state``. A crash in between is safe because a successful advance clears
the row's ``session_id``: the next tick finds the session still ACTIVE with an
unconsumed result, the lookup no longer matches, and the session completes
without advancing the row a second time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.models import CompletionReason, LastResultSource, SessionStatus
from cw.reconcile._shared import (
    _apply_sentinel_to_task,
    _sentinel_partial_route_consumed,
    stage_refusal_page,
)
from cw.result import has_terminal_result, reconstruct_staged_sentinel

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate, StageRefusalPage


class EmittedResultDisposition(NamedTuple):
    """What the harvest owes a dead session that already emitted its result.

    ``complete`` is True when the session is finished and must be stamped
    COMPLETED. Otherwise ``page`` is the stage-refusal page owed for a permanent
    refusal of the emitted result (the harvest appends it, and the latch it
    stamps stops the session being re-offered every tick, #2490).
    """

    complete: bool
    page: StageRefusalPage | None = None


def _holds_emitted_result(session: Session) -> bool:
    """True when *session*'s own ``cw result emit`` result is terminal-shaped."""
    return session.last_result_source is LastResultSource.EMIT_CLI and (
        has_terminal_result(session.last_result)
    )


def dispose_emitted_result(
    session: Session, candidate: ReapCandidate, *, subject: str
) -> EmittedResultDisposition | None:
    """Route *session*'s own emitted result, or None to fall through to git synthesis.

    Scoped to EMIT_CLI: a terminal result from another authority keeps the
    door's refuse-and-leave behavior. A result a #2458 partial route already
    consumed is complete without re-routing, as is one with no ticket to route.
    A staged result is routed to its row: routed, raced to a terminal row, or
    landed FAILED all complete the session. A stage mismatch is permanent for a
    dead worker, so it returns the page for the harvest to emit (and skips git
    synthesis, which would only repeat the refusal). Any other refusal -- a row
    that moved on to a non-terminal state, or a staged dict that does not
    reconstruct -- returns None and keeps the previous behavior.
    """
    if not _holds_emitted_result(session):
        return None
    if _sentinel_partial_route_consumed(session):
        return EmittedResultDisposition(complete=True)
    staged = reconstruct_staged_sentinel(session.last_result)
    if staged is None:
        return None
    if candidate.ticket_id is None:
        return EmittedResultDisposition(complete=True)
    outcome = _apply_sentinel_to_task(candidate.ticket_id, session, staged)
    if outcome.routed or outcome.task_already_terminal or outcome.landed_terminal:
        return EmittedResultDisposition(complete=True)
    page = stage_refusal_page(
        session,
        outcome,
        ticket_id=candidate.ticket_id,
        lane=candidate.lane,
        sentinel=staged,
        subject=subject,
        live=False,
    )
    return None if page is None else EmittedResultDisposition(complete=False, page=page)


def stamp_harvest_completed(
    session: Session,
    candidate: ReapCandidate,
    *,
    now: datetime,
    harvested_ticket_ids: list[str],
    pending_events: list[dict[str, object]],
) -> None:
    """Mark a dead LOCAL session COMPLETED and queue its SESSION_COMPLETED payload."""
    if candidate.ticket_id:
        harvested_ticket_ids.append(candidate.ticket_id)
    session.status = SessionStatus.COMPLETED
    session.completed_reason = CompletionReason.NORMAL
    session.completed_at = now
    harvest_payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "crashed": False,
    }
    if candidate.ticket_id:
        harvest_payload["ticket_id"] = candidate.ticket_id
    pending_events.append(harvest_payload)


def handle_emitted_result(
    session: Session,
    candidate: ReapCandidate,
    *,
    subject: str,
    now: datetime,
    harvested_ticket_ids: list[str],
    pending_events: list[dict[str, object]],
    refusal_pages: list[StageRefusalPage],
) -> bool:
    """Apply :func:`dispose_emitted_result`; True when the candidate is dispositioned.

    True means the harvest is done with *candidate*: the session was completed,
    or a stage-refusal page was queued on *refusal_pages*. False means fall
    through to git synthesis.
    """
    disposition = dispose_emitted_result(session, candidate, subject=subject)
    if disposition is None:
        return False
    if disposition.complete:
        stamp_harvest_completed(
            session,
            candidate,
            now=now,
            harvested_ticket_ids=harvested_ticket_ids,
            pending_events=pending_events,
        )
    elif disposition.page is not None:
        refusal_pages.append(disposition.page)
    return True
