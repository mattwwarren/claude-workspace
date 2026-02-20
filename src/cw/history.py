"""Structured event history for session lifecycle tracking."""

from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cw.config import HISTORY_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class EventType(StrEnum):
    SESSION_STARTED = "session_started"
    SESSION_BACKGROUNDED = "session_backgrounded"
    SESSION_RESUMED = "session_resumed"
    SESSION_COMPLETED = "session_completed"
    SESSION_CRASHED = "session_crashed"
    SESSION_HANDOFF = "session_handoff"
    QUEUE_ITEM_ADDED = "queue_item_added"
    QUEUE_ITEM_COMPLETED = "queue_item_completed"
    QUEUE_ITEM_FAILED = "queue_item_failed"
    DAEMON_STARTED = "daemon_started"
    DAEMON_STOPPED = "daemon_stopped"


class HistoryEvent(BaseModel):
    """A single event in the session history log."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    client: str
    session_id: str | None = None
    session_name: str | None = None
    purpose: str | None = None
    detail: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


def _history_path(client: str) -> Path:
    """Return the JSONL history file path for a client."""
    return HISTORY_DIR / f"{client}.jsonl"


def _lock_path(client: str) -> Path:
    """Return the lock file path for a client's history."""
    return HISTORY_DIR / f".{client}.lock"


@contextlib.contextmanager
def _history_lock(client: str) -> Iterator[None]:
    """Acquire an exclusive file lock for a client's history."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(client)
    fd = lock.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def append_event(client: str, event: HistoryEvent) -> None:
    """Append a single event to the client's history JSONL file."""
    with _history_lock(client):
        path = _history_path(client)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(event.model_dump_json() + "\n")


def load_history(
    client: str,
    *,
    since: datetime | None = None,
    event_types: list[EventType] | None = None,
    limit: int | None = None,
) -> list[HistoryEvent]:
    """Load history events for a client, filtered and reverse-chronological.

    Reads and parses the entire JSONL file on each call. Acceptable for
    current scale (lifecycle events are infrequent, files stay small).
    If history files grow large, consider tail-based reading or an index.

    Args:
        client: Client name.
        since: Only return events after this timestamp.
        event_types: Only return events of these types.
        limit: Maximum number of events to return.
    """
    path = _history_path(client)
    if not path.exists():
        return []

    events: list[HistoryEvent] = []
    for raw_line in path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        raw = json.loads(stripped)
        event = HistoryEvent.model_validate(raw)

        if since and event.timestamp < since:
            continue
        if event_types and event.event_type not in event_types:
            continue
        events.append(event)

    # Reverse chronological
    events.sort(key=lambda e: e.timestamp, reverse=True)

    if limit:
        events = events[:limit]

    return events


def record_event(client: str, event: HistoryEvent) -> None:
    """Append an event and optionally fire a desktop notification.

    This is the main entry point for recording events. It appends the
    event to the JSONL history and fires a notification if enabled for
    the client.
    """
    append_event(client, event)

    # Check if notifications are enabled for this client
    from cw.config import load_clients
    from cw.notify import notify_event

    clients = load_clients()
    client_config = clients.get(client)
    if client_config and client_config.notifications:
        notify_event(event)
