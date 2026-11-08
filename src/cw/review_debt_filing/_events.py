"""The five review-debt audit events (#3041, R16; ``docs/events.md``).

Each goes through :func:`cw.events.record_event` with the queue ticket as the
``correlation_id``, and is called only outside any lock. A failed write is
logged at WARNING and never aborts filing (plan Adopted Assumption 11): the
ticket already exists, so the row record, not the event, is what must land.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_debt_filing._models import FilingContext

_log = logging.getLogger(__name__)


def _common(ctx: FilingContext, key: str, tracker_id: str | None) -> dict[str, object]:
    return {
        "ticket_id": ctx.ticket_id,
        "client": ctx.client,
        "branch": ctx.branch,
        "key": key,
        "tracker": ctx.tracker,
        "tracker_id": tracker_id,
    }


def _record(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> None:
    try:
        record_event(event_type, payload=dict(payload), correlation_id=ticket_id)
    except OSError as exc:
        _log.warning("could not record %s for %s: %s", event_type, ticket_id, exc)


def emit_filed(ctx: FilingContext, key: str, tracker_id: str, url: str) -> None:
    """``review.debt_filed``: cw created the ticket for *key*."""
    payload = {**_common(ctx, key, tracker_id), "url": url, "mode": "filed"}
    _record(OrchestratorEventType.REVIEW_DEBT_FILED, payload, ctx.ticket_id)


def emit_deduped(ctx: FilingContext, key: str, tracker_id: str, via: str) -> None:
    """``review.debt_deduped``: a ticket for *key* already exists."""
    payload = {**_common(ctx, key, tracker_id), "via": via}
    _record(OrchestratorEventType.REVIEW_DEBT_DEDUPED, payload, ctx.ticket_id)


def emit_filing_failed(ctx: FilingContext, key: str, failure: str, detail: str) -> None:
    """``review.debt_filing_failed``: *key* stayed unticketed."""
    payload = {**_common(ctx, key, None), "failure": failure, "detail": detail}
    _record(OrchestratorEventType.REVIEW_DEBT_FILING_FAILED, payload, ctx.ticket_id)


def emit_emitted(ctx: FilingContext, key: str, path: str, cause: str) -> None:
    """``review.debt_emitted``: a payload file for *key* was newly written."""
    payload = {**_common(ctx, key, None), "path": path, "cause": cause}
    _record(OrchestratorEventType.REVIEW_DEBT_EMITTED, payload, ctx.ticket_id)


def emit_verify_failed(
    ctx: FilingContext, failures: Sequence[tuple[str | None, str]]
) -> None:
    """``review.debt_verify_failed``: one ``record`` run's verification failed."""
    payload: dict[str, object] = {
        "ticket_id": ctx.ticket_id,
        "client": ctx.client,
        "branch": ctx.branch,
        "tracker": ctx.tracker,
        "count": len(failures),
        "failures": [{"key": key, "reason": reason} for key, reason in failures],
    }
    _record(OrchestratorEventType.REVIEW_DEBT_VERIFY_FAILED, payload, ctx.ticket_id)
