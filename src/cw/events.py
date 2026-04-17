"""Orchestrator event bus: append-only inbox with cursor-based consumption."""

from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.config import EVENTS_DIR
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _inbox_path() -> Path:
    """Return the path to the global event inbox JSONL file."""
    return EVENTS_DIR / "inbox.jsonl"


def _cursor_path(consumer: str) -> Path:
    """Return the cursor file path for a named consumer."""
    return EVENTS_DIR / "cursors" / f"{consumer}.json"


def _lock_path() -> Path:
    """Return the lock file path for the event inbox."""
    return EVENTS_DIR / ".inbox.lock"


@contextlib.contextmanager
def _inbox_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the event inbox."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path()
    fd = lock.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def record_event(
    event_type: OrchestratorEventType,
    payload: dict[str, Any] | None = None,
    *,
    correlation_id: str | None = None,
) -> OrchestratorEvent:
    """Append a new event to the inbox and return it.

    Args:
        event_type: The orchestrator event type.
        payload: Arbitrary JSON-serialisable payload dict.
        correlation_id: Optional ID linking related events.

    Returns:
        The newly created and persisted event.
    """
    event = OrchestratorEvent(
        type=event_type,
        payload=payload or {},
        correlation_id=correlation_id,
    )
    with _inbox_lock():
        inbox = _inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a") as f:
            f.write(event.model_dump_json() + "\n")
    return event


def _load_cursor(consumer: str) -> str | None:
    """Load the last-consumed event ID for a consumer, or None if no cursor."""
    path = _cursor_path(consumer)
    if not path.exists():
        return None
    data: dict[str, str] = json.loads(path.read_text())
    return data.get("cursor")


def advance_cursor(consumer: str, event_id: str) -> None:
    """Persist a consumer's cursor to the given event ID.

    Args:
        consumer: Consumer name (used as filename stem).
        event_id: The event ID to advance the cursor to.
    """
    path = _cursor_path(consumer)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {
        "cursor": event_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(data))


def read_events(
    consumer: str | None = None,
    *,
    since_cursor: str | None = None,
    since_ts: datetime | None = None,
    event_types: list[OrchestratorEventType] | None = None,
    limit: int | None = None,
) -> list[OrchestratorEvent]:
    """Read events from the inbox, optionally filtered.

    When *consumer* is provided and no explicit *since_cursor* is given,
    the consumer's persisted cursor is used automatically.

    When *since_cursor* is provided (or derived from the consumer cursor),
    only events *after* the referenced event ID are returned.

    Args:
        consumer: Consumer name; used to load a persisted cursor when
            *since_cursor* is not explicitly set.
        since_cursor: Skip events up to and including this event ID.
        since_ts: Skip events with created_at before this timestamp.
        event_types: If set, only return events of these types.
        limit: Maximum number of events to return.

    Returns:
        List of matching events in ascending (chronological) order.
    """
    # Resolve cursor: explicit arg beats consumer persisted cursor
    cursor = since_cursor
    if cursor is None and consumer is not None:
        cursor = _load_cursor(consumer)

    inbox = _inbox_path()
    if not inbox.exists():
        return []

    events: list[OrchestratorEvent] = []
    past_cursor = cursor is None  # If no cursor, start from beginning

    for raw_line in inbox.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        raw = json.loads(stripped)
        event = OrchestratorEvent.model_validate(raw)

        # Cursor-based skip: advance past the cursor event, then include from there
        if not past_cursor:
            if event.id == cursor:
                past_cursor = True
            continue

        if since_ts is not None and event.created_at < since_ts:
            continue
        if event_types is not None and event.type not in event_types:
            continue

        events.append(event)

    if limit is not None:
        events = events[:limit]

    return events
