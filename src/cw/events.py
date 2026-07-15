"""Orchestrator event bus: append-only inbox with cursor-based consumption."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from cw.atomic import atomic_write_text
from cw.config import events_dir
from cw.exceptions import CwError
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


def inbox_path() -> Path:
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
        inbox = inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a") as f:
            f.write(event.model_dump_json() + "\n")
    return event


class PruneResult(BaseModel):
    """Outcome of a :func:`prune_events` call."""

    archived_count: int
    deleted_count: int
    archive_path: str | None
    kept_count: int


def _archive_path_for_today() -> Path:
    """Return today's inbox archive path: ``events/inbox.<YYYY-MM-DD>.jsonl``."""
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    return events_dir() / f"inbox.{date_str}.jsonl"


def _partition_events_by_before(
    events: list[OrchestratorEvent], before: datetime
) -> tuple[list[OrchestratorEvent], list[OrchestratorEvent]]:
    """Split *events* into (kept, pruned) by created_at against *before*."""
    kept = [e for e in events if e.created_at >= before]
    pruned = [e for e in events if e.created_at < before]
    return kept, pruned


def _partition_events_by_keep(
    events: list[OrchestratorEvent], keep: int
) -> tuple[list[OrchestratorEvent], list[OrchestratorEvent]]:
    """Split *events* into (kept, pruned), keeping the newest *keep* events."""
    if keep <= 0:
        return [], list(events)
    if keep >= len(events):
        return list(events), []
    split = len(events) - keep
    return events[split:], events[:split]


def prune_events(
    *,
    before: datetime | None = None,
    keep: int | None = None,
    archive: bool = True,
) -> PruneResult:
    """Truncate the inbox by age or count, archiving (default) or deleting the rest.

    Exactly one of *before* or *keep* must be given: *before* prunes every
    event with ``created_at`` earlier than the cutoff; *keep* retains the
    newest *keep* events and prunes the rest.

    When *archive* is True (default), pruned events are appended to
    ``events/inbox.<YYYY-MM-DD>.jsonl`` (plain JSONL, no compression) before
    being dropped from the inbox. When False, pruned events are discarded
    without being written anywhere.

    The read, rewrite, and (optional) archive-append all happen under a
    single acquisition of the inbox lock — ``_inbox_lock`` is not reentrant,
    so releasing and reacquiring it mid-call would self-deadlock.

    Deliberately does not call :func:`record_event`: doing so would require
    a second, nested ``_inbox_lock()`` acquisition, which self-deadlocks
    (see above). No audit event is emitted for a prune.

    Args:
        before: Prune events with ``created_at`` earlier than this.
        keep: Retain only the newest N events; prune the rest.
        archive: When True, append pruned events to the daily archive file
            before dropping them. When False, discard them outright.

    Returns:
        A :class:`PruneResult` describing what happened.

    Raises:
        CwError: If both or neither of *before*/*keep* are given, or if *keep*
            is negative.
    """
    if (before is None) == (keep is None):
        msg = "prune_events: exactly one of 'before' or 'keep' must be given."
        raise CwError(msg)
    if keep is not None and keep < 0:
        msg = "prune_events: 'keep' must be non-negative."
        raise CwError(msg)

    with _inbox_lock():
        inbox = inbox_path()
        raw_text = inbox.read_text() if inbox.exists() else ""
        if not raw_text:
            return PruneResult(
                archived_count=0, deleted_count=0, archive_path=None, kept_count=0
            )

        events = _parse_lines(raw_text.splitlines())
        if before is not None:
            kept_events, pruned_events = _partition_events_by_before(events, before)
        elif keep is not None:
            kept_events, pruned_events = _partition_events_by_keep(events, keep)
        else:  # pragma: no cover - unreachable, guarded by validation above
            msg = "prune_events: exactly one of 'before' or 'keep' must be given."
            raise CwError(msg)

        new_text = "".join(e.model_dump_json() + "\n" for e in kept_events)
        atomic_write_text(inbox, new_text)

        archived_count = 0
        deleted_count = 0
        archive_path: str | None = None
        if pruned_events:
            if archive:
                path = _archive_path_for_today()
                path.parent.mkdir(parents=True, exist_ok=True)
                # Why: this append is not atomic with the inbox rewrite above.
                # A crash between the two would drop the pruned events from
                # both files. Accepted: the archive is a best-effort audit
                # copy, not the durable source of truth (inbox.jsonl is), and
                # atomicity here would require a second temp-file+rename step
                # for marginal benefit on an operator-invoked, non-hot-path
                # command.
                with path.open("a") as f:
                    for ev in pruned_events:
                        f.write(ev.model_dump_json() + "\n")
                archived_count = len(pruned_events)
                archive_path = str(path)
            else:
                deleted_count = len(pruned_events)

        return PruneResult(
            archived_count=archived_count,
            deleted_count=deleted_count,
            archive_path=archive_path,
            kept_count=len(kept_events),
        )


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
    inbox = inbox_path()
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
    client_names: frozenset[str] | None = None,
) -> bool:
    """Return True if *event* passes the timestamp, type, and client filters."""
    if since_ts is not None and event.created_at < since_ts:
        return False
    if event_types is not None and event.type not in event_types:
        return False
    return not (
        client_names is not None and event.payload.get("client") not in client_names
    )


def _parse_lines(lines: list[str]) -> list[OrchestratorEvent]:
    """Parse JSONL lines into a list of events.

    Tolerates a malformed trailing line (torn write): if the last non-empty
    line fails JSON parsing, it is skipped with a warning.  Interior corrupt
    lines re-raise so callers see real corruption.

    Also tolerates a well-formed line whose ``type`` is not a member of the
    in-memory ``OrchestratorEventType`` enum (issue #1210): a newer producer
    may emit an event type an older, long-running consumer's process does not
    yet know about.  Such lines are skipped and summarized in a single
    warning per call.  Any other validation failure -- including an unknown
    type combined with a second, unrelated bad field -- still raises, so
    genuine interior corruption stays loud (issue #393's contract).
    """
    # Precompute last non-empty index so trailing blank lines don't cause the
    # torn-write check to misfire on an interior corrupt line.
    last_nonempty_idx = max(
        (j for j, line in enumerate(lines) if line.strip()), default=-1
    )
    results: list[OrchestratorEvent] = []
    unknown_types: set[str] = set()
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
        try:
            results.append(OrchestratorEvent.model_validate(raw))
        except ValidationError as exc:
            errors = exc.errors()
            if all(err["type"] == "enum" and err["loc"] == ("type",) for err in errors):
                unknown_types.add(raw.get("type", "<missing>"))
                continue
            raise
    if unknown_types:
        logger.warning(
            "skipping %d event(s) with unknown type: %s",
            len(unknown_types),
            ", ".join(sorted(unknown_types)),
        )
    return results


def read_events(
    consumer: str | None = None,
    *,
    since_cursor: str | None = None,
    since_ts: datetime | None = None,
    event_types: list[OrchestratorEventType] | None = None,
    client_names: frozenset[str] | None = None,
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
        client_names: If set, only return events whose payload.client is in this set.
        limit: Maximum number of events to return.

    Returns:
        List of matching events in ascending (chronological) order.
    """
    # Resolve cursor: explicit arg beats consumer persisted cursor
    cursor = since_cursor
    if cursor is None and consumer is not None:
        cursor = load_cursor(consumer)

    inbox = inbox_path()
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

        if not _event_matches(
            event,
            since_ts=since_ts,
            event_types=event_types,
            client_names=client_names,
        ):
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
            if _event_matches(
                event,
                since_ts=since_ts,
                event_types=event_types,
                client_names=client_names,
            )
        ]

    if limit is not None:
        events = events[:limit]

    return events


_FOLLOW_POLL_INTERVAL: float = 0.05  # 50ms — satisfies ≤100ms acceptance criterion


def _poll_inbox_growth(last_size: int | None) -> tuple[bool, int]:
    """Change-detection guard for the inbox-follow polls.

    Compares the current inbox byte size against the bytes-consumed offset from
    the previous poll (*last_size*) and returns ``(should_read, current_size)``.
    The caller always adopts the returned size as its new baseline; *should_read*
    gates the cursor-based ``read_events`` call. This helper never reads or
    replays — cursor semantics stay entirely in ``read_events``.

    Decision table:
      * first poll (``last_size is None``) -> read
      * size grew                           -> read
      * size unchanged                      -> no-op
      * size shrank                         -> warn + reset baseline, no read

    The shrink branch is a live path, not dead code: ``prune_events`` (#856)
    legitimately truncates and rewrites the inbox, so size is no longer
    monotonic in production. When a shrink is observed, this guard warns and
    resets the baseline rather than replaying from 0, which avoids the
    cursor-not-found replay path in ``read_events``.
    """
    inbox = inbox_path()
    current_size: int = 0
    if inbox.exists():
        try:
            current_size = inbox.stat().st_size
        except OSError:
            current_size = 0

    if last_size is not None and current_size < last_size:
        logger.warning(
            "inbox size decreased (%d -> %d); resetting change-detection baseline",
            last_size,
            current_size,
        )
        return False, current_size

    return (last_size is None or current_size != last_size), current_size


def _event_matches_wait(
    event: OrchestratorEvent,
    *,
    correlation_id: str | None,
    session_id: str | None,
    client: str | None,
) -> bool:
    """Return True if event passes correlation_id, session_id, and client filters."""
    if correlation_id is not None and event.correlation_id != correlation_id:
        return False
    if session_id is not None and event.payload.get("session_id") != session_id:
        return False
    return client is None or event.payload.get("client") == client


def wait_for_event(
    *,
    event_types: list[OrchestratorEventType] | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    client: str | None = None,
    timeout: float = 3600.0,
    follow: bool = False,
    poll_interval: float = _FOLLOW_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield matching events from the inbox, blocking until they arrive.

    Reads from the beginning of the inbox so events recorded before the
    call started are also matched.  Polls the inbox every *poll_interval*
    seconds, using the shared ``_poll_inbox_growth`` change-detection guard
    to skip the read when the inbox byte size is unchanged.

    In default mode (follow=False) exits after the first match.  With
    follow=True streams all matches until timeout.

    Args:
        event_types: Filter by event type(s).
        correlation_id: Filter by event.correlation_id.
        session_id: Filter by payload["session_id"].
        client: Filter by payload["client"].
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
        should_read, last_size = _poll_inbox_growth(last_size)
        if should_read:
            new_events = read_events(
                since_cursor=last_cursor,
                event_types=event_types,
            )
            for ev in new_events:
                # Advance cursor past all type-matched events, not just
                # correlation/session/client matches, so they are not re-scanned.
                last_cursor = ev.id
                if _event_matches_wait(
                    ev,
                    correlation_id=correlation_id,
                    session_id=session_id,
                    client=client,
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
    client_names: frozenset[str] | None = None,
    poll_interval: float = _FOLLOW_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield new events as they arrive, polling the inbox for changes.

    Uses the shared ``_poll_inbox_growth`` guard as a cheap change-detection
    check over the inbox byte size; calls ``read_events`` only when the size
    grows.  A size decrease (defensive, append-only inbox) warns and resets the
    baseline without reading.  Does not hold the inbox lock during sleep.  Does
    not advance any consumer cursor — that remains one-shot only.

    Exits when the caller sends a ``GeneratorExit`` (or raises
    ``KeyboardInterrupt`` / ``BrokenPipeError`` in the iterating loop).
    """
    last_cursor = since_cursor
    last_size: int | None = None

    while True:
        should_read, last_size = _poll_inbox_growth(last_size)
        if should_read:
            new_events = read_events(
                since_cursor=last_cursor,
                since_ts=since_ts,
                event_types=event_types,
                client_names=client_names,
            )
            for ev in new_events:
                yield ev
                last_cursor = ev.id

        time.sleep(poll_interval)
