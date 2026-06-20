"""Orchestrator event bus: append-only inbox with cursor-based consumption."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.atomic import atomic_write_text
from cw.config import events_dir
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


def _inbox_path() -> Path:
    """Return the path to the global event inbox JSONL file."""
    return events_dir() / "inbox.jsonl"


def _cursor_path(consumer: str) -> Path:
    """Return the cursor file path for a named consumer."""
    return events_dir() / "cursors" / f"{consumer}.json"


def _lock_path() -> Path:
    """Return the lock file path for the event inbox."""
    return events_dir() / ".inbox.lock"


@contextlib.contextmanager
def _inbox_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the event inbox."""
    events_dir().mkdir(parents=True, exist_ok=True)
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


def load_cursor(consumer: str) -> str | None:
    """Return the last-consumed event ID for *consumer*, or None if no cursor."""
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
    atomic_write_text(path, json.dumps(data))


def init_cursor_at_end(consumer: str) -> bool:
    """Initialize a fresh consumer cursor to the current end of the inbox.

    If the consumer already has a persisted cursor, this function is a no-op
    and returns False.  If the inbox is empty, no cursor is written and False
    is returned.  Otherwise, advances the cursor to the last event in the inbox
    and returns True.

    Use this before the first ``read_events(consumer=...)`` call when you want
    "new events only" semantics rather than replaying history.
    """
    if _cursor_path(consumer).exists():
        return False
    inbox = _inbox_path()
    with _inbox_lock():
        raw_text = inbox.read_text() if inbox.exists() else ""
    # Why: _inbox_lock is released before advance_cursor runs, so events appended
    # in that window will appear to be "before" the cursor and be skipped on the
    # first read.  The race is accepted: the consumer sees at-most-once semantics
    # on startup; any missed events are benign (follow-mode replays on size change).
    if not raw_text:
        return False
    parsed = _parse_lines(raw_text.splitlines())
    if not parsed:
        return False
    advance_cursor(consumer, parsed[-1].id)
    return True


def _event_matches(
    event: OrchestratorEvent,
    *,
    since_ts: datetime | None,
    event_types: list[OrchestratorEventType] | None,
) -> bool:
    """Return True if *event* passes the timestamp and type filters."""
    if since_ts is not None and event.created_at < since_ts:
        return False
    return event_types is None or event.type in event_types


def _parse_lines(lines: list[str]) -> list[OrchestratorEvent]:
    """Parse JSONL lines into a list of events.

    Tolerates a malformed trailing line (torn write): if the last non-empty
    line fails JSON parsing, it is skipped with a warning.  Interior corrupt
    lines re-raise so callers see real corruption.
    """
    # Precompute last non-empty index so trailing blank lines don't cause the
    # torn-write check to misfire on an interior corrupt line.
    last_nonempty_idx = max(
        (j for j, line in enumerate(lines) if line.strip()), default=-1
    )
    results: list[OrchestratorEvent] = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if i == last_nonempty_idx:
                # Tolerate malformed trailing line only (torn write)
                logger.warning("skipping malformed trailing line in inbox: %s", exc)
                continue
            raise
        results.append(OrchestratorEvent.model_validate(raw))
    return results


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

    At-least-once delivery contract: if a consumer's cursor is not found in the
    inbox (e.g. due to inbox rotation or compaction), all events are replayed
    from the beginning. Callers using a named consumer cursor must be idempotent.

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
        cursor = load_cursor(consumer)

    inbox = _inbox_path()
    with _inbox_lock():
        raw_text = inbox.read_text() if inbox.exists() else ""

    if not raw_text:
        return []

    lines = raw_text.splitlines()
    parsed = _parse_lines(lines)

    events: list[OrchestratorEvent] = []
    past_cursor = cursor is None  # If no cursor, start from beginning

    for event in parsed:
        # Cursor-based skip: advance past the cursor event, then include from there
        if not past_cursor:
            if event.id == cursor:
                past_cursor = True
            continue

        if not _event_matches(event, since_ts=since_ts, event_types=event_types):
            continue

        events.append(event)

    # Cursor-not-found fallback: replay from the beginning.
    # Why: callers are idempotent — orchestrate_retire guards on sess.status ==
    # COMPLETED, dispatch consumer skips non-RUNNING tasks, and event tail is
    # display-only — so replaying from the start is safe.
    if cursor is not None and not past_cursor:
        logger.warning("cursor %s not found in inbox; replaying from start", cursor)
        events = [
            event
            for event in parsed
            if _event_matches(event, since_ts=since_ts, event_types=event_types)
        ]

    if limit is not None:
        events = events[:limit]

    return events


_FOLLOW_POLL_INTERVAL: float = 0.05  # 50ms — satisfies ≤100ms acceptance criterion
_WAIT_POLL_INTERVAL: float = 0.05


def _event_matches_wait(
    event: OrchestratorEvent,
    *,
    correlation_id: str | None,
    session_id: str | None,
) -> bool:
    """Return True if event passes correlation_id and session_id filters."""
    if correlation_id is not None and event.correlation_id != correlation_id:
        return False
    return session_id is None or event.payload.get("session_id") == session_id


def wait_for_event(
    *,
    event_types: list[OrchestratorEventType] | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    timeout: float = 3600.0,
    follow: bool = False,
    poll_interval: float = _WAIT_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield matching events from the inbox, blocking until they arrive.

    Reads from the beginning of the inbox so events recorded before the
    call started are also matched.  Polls the inbox every *poll_interval*
    seconds for new content.

    In default mode (follow=False) exits after the first match.  With
    follow=True streams all matches until timeout.

    Args:
        event_types: Filter by event type(s).
        correlation_id: Filter by event.correlation_id.
        session_id: Filter by payload["session_id"].
        timeout: Seconds before raising TimeoutError.
        follow: If True, keep streaming after first match.
        poll_interval: Seconds between inbox polls.

    Raises:
        TimeoutError: If no match arrives within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    last_cursor: str | None = None
    last_size: int | None = None
    matched = False

    while True:
        inbox = _inbox_path()
        current_size: int = 0
        if inbox.exists():
            try:
                current_size = inbox.stat().st_size
            except OSError:
                current_size = 0

        if last_size is None or current_size != last_size:
            last_size = current_size
            new_events = read_events(
                since_cursor=last_cursor,
                event_types=event_types,
            )
            for ev in new_events:
                last_cursor = ev.id
                if _event_matches_wait(
                    ev,
                    correlation_id=correlation_id,
                    session_id=session_id,
                ):
                    matched = True
                    yield ev
                    if not follow:
                        return

        if time.monotonic() >= deadline:
            if not matched:
                msg = f"No matching event after {timeout:.0f}s"
                raise TimeoutError(msg)
            return

        time.sleep(poll_interval)


def tail_events_follow(
    *,
    since_cursor: str | None,
    since_ts: datetime | None,
    event_types: list[OrchestratorEventType] | None,
    poll_interval: float = _FOLLOW_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield new events as they arrive, polling the inbox for changes.

    Polls ``_inbox_path().stat().st_size`` as a cheap change-detection guard;
    calls ``read_events`` only when the size changes.  Does not hold the inbox
    lock during sleep.  Does not advance any consumer cursor — that remains
    one-shot only.

    Exits when the caller sends a ``GeneratorExit`` (or raises
    ``KeyboardInterrupt`` / ``BrokenPipeError`` in the iterating loop).
    """
    last_cursor = since_cursor
    last_size: int | None = None

    while True:
        inbox = _inbox_path()
        current_size: int = 0
        if inbox.exists():
            try:
                current_size = inbox.stat().st_size
            except OSError:
                current_size = 0

        if last_size is None or current_size != last_size:
            last_size = current_size
            new_events = read_events(
                since_cursor=last_cursor,
                since_ts=since_ts,
                event_types=event_types,
            )
            for ev in new_events:
                yield ev
                last_cursor = ev.id

        time.sleep(poll_interval)
