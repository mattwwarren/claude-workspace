"""The review-debt audit events (#3041, R16; ``docs/events.md``).

Each goes through :func:`cw.events.record_event` with the queue ticket as the
``correlation_id``, and is called only outside any lock. Two write modes:

- Filing-stage events (``filed``, ``deduped``, ``filing_failed``, ``emitted``)
  are best-effort. They describe a ticket or payload that already exists, so a
  failed write is logged at WARNING and never aborts filing (RFC 0017 D-7;
  Adopted Assumption 11).
- ``verify_failed`` is a per-run audit record. A ``record`` run that fails
  verification mutates nothing: the row and artifact are untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.models.review_debt_record import FILED_MODE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_debt_filing._models import FilingContext

_log = logging.getLogger(__name__)


class AuditWarning(NamedTuple):
    """A filing-stage audit event that could not be persisted."""

    event_type: OrchestratorEventType
    detail: str


_EVENT_WRITE_ERRORS = (CwError, OSError, TypeError, ValueError)


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
    """Strict audit write: a failure raises, for the pre-mutation verify gate."""
    try:
        record_event(event_type, payload=dict(payload), correlation_id=ticket_id)
    except _EVENT_WRITE_ERRORS as exc:
        msg = f"Could not record {event_type} for {ticket_id}: {exc}"
        _log.error(msg)
        raise CwError(msg) from exc


def _record_best_effort(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> AuditWarning | None:
    """Filing-stage audit write: a failure is logged, never raised.

    The ticket (or payload file) it describes already exists by the time this
    runs, so raising here would drop the record that must persist (RFC 0017
    D-7; Adopted Assumption 11).
    """
    try:
        record_event(event_type, payload=dict(payload), correlation_id=ticket_id)
    except _EVENT_WRITE_ERRORS as exc:
        _log.warning("Could not record %s for %s: %s", event_type, ticket_id, exc)
        return AuditWarning(event_type, str(exc))
    return None


def emit_filed(
    ctx: FilingContext, key: str, tracker_id: str, url: str
) -> AuditWarning | None:
    """``review.debt_filed``: cw created the ticket for *key*."""
    payload = {**_common(ctx, key, tracker_id), "url": url, "mode": FILED_MODE}
    return _record_best_effort(
        OrchestratorEventType.REVIEW_DEBT_FILED, payload, ctx.ticket_id
    )


def emit_deduped(
    ctx: FilingContext, key: str, tracker_id: str, via: str
) -> AuditWarning | None:
    """``review.debt_deduped``: a ticket for *key* already exists."""
    payload = {**_common(ctx, key, tracker_id), "via": via}
    return _record_best_effort(
        OrchestratorEventType.REVIEW_DEBT_DEDUPED, payload, ctx.ticket_id
    )


def emit_filing_failed(
    ctx: FilingContext, key: str, failure: str, detail: str
) -> AuditWarning | None:
    """``review.debt_filing_failed``: *key* stayed unticketed."""
    payload = {**_common(ctx, key, None), "failure": failure, "detail": detail}
    return _record_best_effort(
        OrchestratorEventType.REVIEW_DEBT_FILING_FAILED, payload, ctx.ticket_id
    )


def emit_emitted(
    ctx: FilingContext, key: str, path: str, cause: str
) -> AuditWarning | None:
    """``review.debt_emitted``: a payload file for *key* was newly written."""
    payload = {**_common(ctx, key, None), "path": path, "cause": cause}
    return _record_best_effort(
        OrchestratorEventType.REVIEW_DEBT_EMITTED, payload, ctx.ticket_id
    )


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
