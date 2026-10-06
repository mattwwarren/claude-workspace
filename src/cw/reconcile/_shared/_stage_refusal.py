"""Stage-refusal page, payload and page-then-latch emit (#2490, #2513).

When the shared staged-advance guard refuses a session's result on a stage
mismatch, the result is dropped and the row is left unchanged. The local
harvest (a dead LOCAL process) and the idle, stalled and phantom sweeps each
latch that refusal so it is not re-offered every tick; this module owns the
one ``session.needs_attention`` page they all emit first, so the refusal is
never silent. Imports ``_constants`` and ``_sentinels``.

The page is at-least-once: the latch is stamped only after the page write
succeeded, so a failed write leaves the session un-latched and the next tick
pages again. A page is a leaf-rank event append, safe under ``sessions_lock``;
nothing here runs a subprocess or fires a push notification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult
from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.reconcile._shared._constants import _LOGGER_NAME
from cw.reconcile._shared._sentinels import stamp_stage_refusal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.auto_dev_result import BlockedResult
    from cw.models import Session, Stage
    from cw.reconcile._shared._routing import SentinelRouteOutcome

_log = logging.getLogger(_LOGGER_NAME)

# SESSION_NEEDS_ATTENTION ``paused_status`` for a refused result whose worker
# is gone: a dead LOCAL process (#2490) or an exited phantom worker (#2513).
SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON = "sentinel_stage_mismatch_dead_session"
# ... and for one whose worker is still in the daemon roster (idle and stalled
# sweeps, #2513): it may yet report a matching-stage result.
SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON = "sentinel_stage_mismatch_live_session"

_DEAD_TAIL = (
    "To discard the dead session and rerun the row's current stage:"
    " cw spawn close --confirmed-dead --requeue {session_id}"
)
_LIVE_TAIL = (
    "The worker is still running and may yet report a result for the row's"
    " current stage. If it is wedged, stop it and rerun the row's current"
    " stage: cw spawn close --requeue {session_id}"
)


@dataclass(frozen=True)
class StageRefusalPage:
    """A refused session awaiting its page and then its latch.

    ``stamp`` is the latch written once the page landed: the shared
    :func:`stamp_stage_refusal` by default, or the idle sweep's single-key
    variant.
    """

    session: Session
    payload: dict[str, object]
    stamp: Callable[[Session], None] = stamp_stage_refusal


def _reported_result(sentinel: AutoDevResult | BlockedResult) -> str:
    """What the worker reported: status, stage, blocker reason and its own hint.

    A ``BlockedResult`` has no ``stage_reached``, so only its status is named.
    """
    reported = f"{sentinel.status}"
    if isinstance(sentinel, AutoDevResult):
        reported += f" at {sentinel.stage_reached}"
    if sentinel.blocker is not None:
        reported += f" ({sentinel.blocker.reason})"
        if sentinel.blocker.recovery_hint:
            reported += f"; worker's recovery hint: {sentinel.blocker.recovery_hint}"
    return reported


def _attention_payload(
    session: Session,
    *,
    ticket_id: str,
    lane: str,
    sentinel: AutoDevResult | BlockedResult,
    row_stage: Stage,
    subject: str,
    live: bool,
) -> dict[str, object]:
    """Canonical 9-field SESSION_NEEDS_ATTENTION payload for a refused result.

    ``breadcrumbs`` names what the worker reported, the row's live stage that
    refused it and the exact recovery command, so the operator can tell a
    dropped terminal result from a stale replay without opening the log.

    A dead worker's command is ``cw spawn close --confirmed-dead --requeue``:
    the close cancels the RUNNING row that owns the session and ``--requeue``
    moves it back to PENDING at its current stage (``cw dev-queue requeue``
    alone would refuse a RUNNING row). A live worker's page never says
    ``--confirmed-dead`` or "dead": the worker is still in the roster.
    """
    tail = _LIVE_TAIL if live else _DEAD_TAIL
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": ticket_id,
        "claude_session_id": session.claude_session_id,
        "paused_status": (
            SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON
            if live
            else SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON
        ),
        "breadcrumbs": (
            f"{subject} reported {_reported_result(sentinel)}, refused by the"
            f" staged-advance guard: the row is at stage {row_stage}."
            " The result was NOT applied; the row is unchanged. "
            + tail.format(session_id=session.id)
        ),
        "crashed": False,
        "lane": lane,
    }


def stage_refusal_page(
    session: Session,
    outcome: SentinelRouteOutcome | None,
    *,
    ticket_id: str | None,
    lane: str,
    sentinel: AutoDevResult | BlockedResult,
    subject: str,
    live: bool,
    stamp: Callable[[Session], None] = stamp_stage_refusal,
) -> StageRefusalPage | None:
    """The page owed for a stage-guard refusal of *session*'s result, or None.

    Only a stage mismatch (``outcome.stage_refused``) owes a page; any other
    ``routed=False`` cause does not. The row stage named is
    ``outcome.refused_stage``, read under ``dev_queue_lock`` when it refused,
    not a per-pass snapshot. *subject* names the worker in the breadcrumbs
    (e.g. ``"dead opencode process"``, ``"live worker"``); *live* picks the
    paused_status and recovery wording. Nothing is latched here -- see
    :func:`emit_stage_refusal_pages`.
    """
    if (
        outcome is None
        or not outcome.stage_refused
        or outcome.refused_stage is None
        or ticket_id is None
    ):
        return None
    return StageRefusalPage(
        session,
        _attention_payload(
            session,
            ticket_id=ticket_id,
            lane=lane,
            sentinel=sentinel,
            row_stage=outcome.refused_stage,
            subject=subject,
            live=live,
        ),
        stamp,
    )


def emit_stage_refusal_pages(pages: Sequence[StageRefusalPage]) -> bool:
    """Page each refused session, then latch only the sessions whose page landed.

    At-least-once, never at-most-once: a session is latched (which makes
    detection skip it from then on) only AFTER its ``session.needs_attention``
    write succeeded. A failed write leaves the session un-latched, so the next
    tick re-detects it and pages again -- a duplicate page is acceptable,
    silence is not (#2490). Each page is its own ``try`` so one failing write
    never cancels the others. The latch is an in-memory stamp: the caller's
    ``save_state`` makes it durable, so a crash between a page and that save
    also just repeats the page. Returns True when at least one session was
    latched.
    """
    latched = False
    for page in pages:
        try:
            record_event(
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                page.payload,
                correlation_id=str(page.payload["ticket_id"]),
            )
        except OSError:
            _log.warning(
                "stage_mismatch_page_failed: session=%s; left un-latched,"
                " will re-page next tick",
                page.session.id,
                exc_info=True,
            )
            continue
        page.stamp(page.session)
        latched = True
    return latched


def page_and_latch_stage_refusal(
    session: Session,
    outcome: SentinelRouteOutcome | None,
    *,
    ticket_id: str | None,
    lane: str,
    sentinel: AutoDevResult | BlockedResult,
    subject: str,
    live: bool,
    stamp: Callable[[Session], None] = stamp_stage_refusal,
) -> bool:
    """Page a sweep's stage refusal, then latch it; returns whether *session* changed.

    For the idle, stalled and phantom sweeps, which page inline before their
    caller's ``save_state``. A ``routed=False`` that is not a stage mismatch
    (e.g. a PENDING row still carrying this session id) owes no page and is
    latched silently, as before #2513. A stage mismatch is latched only if its
    page landed; a failed page changes nothing and returns False.
    """
    page = stage_refusal_page(
        session,
        outcome,
        ticket_id=ticket_id,
        lane=lane,
        sentinel=sentinel,
        subject=subject,
        live=live,
        stamp=stamp,
    )
    if page is None:
        stamp(session)
        return True
    return emit_stage_refusal_pages([page])
