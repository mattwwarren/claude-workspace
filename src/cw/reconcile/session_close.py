"""Shared history audit for explicit operator session closes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent
from cw.history import record_event as record_history_event

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cw.models import Session, SessionStatus

logger = logging.getLogger(__name__)


def record_operator_close(
    sess: Session,
    *,
    command: str,
    prior_status: SessionStatus,
    ticket_id: str,
    details: Mapping[str, str] | None = None,
    detail: str = "operator_close",
    record: Callable[[str, HistoryEvent], None] = record_history_event,
) -> None:
    """Write the durable history event for an explicit operator close."""
    metadata = {
        "actor": "operator",
        "command": command,
        "session_id": sess.id,
        "ticket_id": ticket_id,
        "surface_ref": sess.surface_ref or "",
        "prior_status": prior_status.value,
        "resulting_status": sess.status.value,
        **(details or {}),
    }
    event = HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client=sess.client,
        session_id=sess.id,
        session_name=sess.name,
        purpose=sess.purpose,
        detail=detail,
        metadata=metadata,
    )
    try:
        record(sess.client, event)
    except (OSError, ValueError) as exc:
        logger.exception(
            "operator_close_audit_failed: session_id=%s command=%s", sess.id, command
        )
        message = (
            f"Session {sess.id} was closed, but its audit record could not be"
            f" written: {exc}"
        )
        raise CwError(message) from exc
