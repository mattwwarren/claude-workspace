"""Disposition of a session whose worker already emitted its result (#2699, #2704).

cw's responsibility ends at the result a worker records with ``cw result emit``,
never at CI, review or merge. A session that already holds such a result is
finished: its queue row is routed from that result and the session completes,
instead of a second result being synthesized from git that the first-writer-wins
door refuses on every tick.

:func:`route_emitted_result` is the one routing primitive. It routes through
``_apply_sentinel_to_task`` and returns an :class:`EmittedResultDisposition`
(or None to fall through). Two callers share it and differ only where their
situations genuinely differ:

- ``cw spawn close`` (``cw.cli.spawn._route_staged_emit_result``, #2458) passes
  ``complete_unrouted=False`` -- an already-consumed result is cancelled, not
  completed, because a RUNNING row still bound to the session is the one guard
  against ``revert_completed_silent_tasks`` re-dispatching it (#317) -- and
  ``page=None`` -- an operator close does not page a stage mismatch, since the
  page's recovery text is ``cw spawn close`` itself.
- :func:`dispose_emitted_result`, the dead-process local harvest's routing half
  (:func:`handle_emitted_result`), passes ``complete_unrouted=True`` -- a dead
  worker will never produce another leg, so a consumed result completes the
  session -- and a page context, because it pages a permanent stage mismatch.

:func:`stamp_harvest_completed` is the shared completion tail the harvest's two
completion paths both end in.

Write ordering: the queue row is persisted inside ``_apply_sentinel_to_task``,
while the session's COMPLETED stamp only reaches disk with the caller's single
``save_state``. A crash in between is safe because a successful advance clears
the row's ``session_id``: the next tick finds the session still ACTIVE with an
unconsumed result, the lookup no longer matches, and the session completes
without advancing the row a second time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.models import CompletionReason, SessionStatus
from cw.reconcile._shared import (
    _apply_sentinel_to_task,
    _sentinel_partial_route_consumed,
    holds_staged_emit_result,
    stage_refusal_page,
)
from cw.reconcile.harvest_usage import stamp_backend_usage_unavailable
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate, StageRefusalPage


class EmittedResultDisposition(NamedTuple):
    """What the caller owes a session that already emitted its result.

    ``complete`` is True when the session is finished and must be stamped
    COMPLETED. Otherwise ``page`` is the stage-refusal page owed for a permanent
    refusal of the emitted result (the harvest appends it, and the latch it
    stamps stops the session being re-offered every tick, #2490).
    """

    complete: bool
    page: StageRefusalPage | None = None


class RefusalPageContext(NamedTuple):
    """What a stage-mismatch page needs: the row's lane and the worker's description."""

    lane: str
    subject: str


def route_emitted_result(
    session: Session,
    *,
    ticket_id: str | None,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> EmittedResultDisposition | None:
    """Route *session*'s staged emitted result to its row; None to fall through.

    Scoped to EMIT_CLI: a terminal result from another authority is left alone.
    *ticket_id* is the row-resolution input, chosen by the caller. A staged
    result routed to its row -- routed, raced to a terminal row, or landed FAILED
    -- completes the session. *complete_unrouted* decides the two no-route cases
    that are a caller policy: an already-consumed result, and no ticket to route
    to. *page* decides a stage mismatch: a page context owes the stage-refusal
    page (``complete=False``), None falls through. Any other refusal -- a row
    that moved on to a non-terminal state, or a staged dict that does not
    reconstruct -- returns None.

    Takes no lock. ``_apply_sentinel_to_task`` takes ``dev_queue_lock`` itself,
    inside the caller's ``sessions_lock`` (ADR-0019).
    """
    if not holds_staged_emit_result(session, include_consumed=True):
        return None
    unrouted = EmittedResultDisposition(complete=True) if complete_unrouted else None
    if _sentinel_partial_route_consumed(session):
        return unrouted
    staged = reconstruct_staged_sentinel(session.last_result)
    if staged is None:
        return None
    if ticket_id is None:
        return unrouted
    outcome = _apply_sentinel_to_task(ticket_id, session, staged)
    if outcome.routed or outcome.task_already_terminal or outcome.landed_terminal:
        return EmittedResultDisposition(complete=True)
    pg = (
        None
        if page is None
        else stage_refusal_page(
            session,
            outcome,
            ticket_id=ticket_id,
            lane=page.lane,
            sentinel=staged,
            subject=page.subject,
            live=False,
        )
    )
    return None if pg is None else EmittedResultDisposition(complete=False, page=pg)


def dispose_emitted_result(
    session: Session, candidate: ReapCandidate, *, subject: str
) -> EmittedResultDisposition | None:
    """Harvest policy: route *session*'s own emitted result, or None to fall through.

    A dead worker will never produce another leg, so an already-consumed result
    and a missing ticket both complete the session. A stage mismatch is permanent
    for a dead worker, so it returns the page for the harvest to emit (and skips
    git synthesis, which would only repeat the refusal). See
    :func:`route_emitted_result`.
    """
    return route_emitted_result(
        session,
        ticket_id=candidate.ticket_id,
        complete_unrouted=True,
        page=RefusalPageContext(candidate.lane, subject),
    )


def stamp_harvest_completed(
    session: Session,
    candidate: ReapCandidate,
    *,
    now: datetime,
    harvested_ticket_ids: list[str],
    pending_events: list[dict[str, object]],
) -> None:
    """Mark a dead LOCAL session COMPLETED and queue its SESSION_COMPLETED payload.

    Also stamps the opencode/local unavailable-usage marker (#3063): this is
    where the session's usage is first assigned at completion, for both
    completion paths. A pure in-memory assignment from data already in hand.
    """
    stamp_backend_usage_unavailable(session, candidate.ticket_id)
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
