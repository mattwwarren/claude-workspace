"""Tests for cw.history module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from freezegun import freeze_time

from cw.history import (
    EventType,
    HistoryEvent,
    append_event,
    load_history,
    record_event,
)
from cw.notify import set_notifications_enabled

if TYPE_CHECKING:
    from pathlib import Path


@freeze_time("2025-06-01 12:00:00", tz_offset=0)
def test_append_and_load(tmp_config_dir: Path) -> None:
    """Events appended via append_event are retrievable via load_history."""
    event = HistoryEvent(
        event_type=EventType.SESSION_STARTED,
        client="test-client",
        session_id="abc123",
        session_name="test-client/impl",
        purpose="impl",
    )
    append_event("test-client", event)

    events = load_history("test-client")
    assert len(events) == 1
    assert events[0].event_type == EventType.SESSION_STARTED
    assert events[0].session_name == "test-client/impl"
    assert events[0].client == "test-client"


def test_load_empty_client(tmp_config_dir: Path) -> None:
    """Loading history for a client with no events returns empty list."""
    events = load_history("nonexistent")
    assert events == []


def test_load_reverse_chronological(tmp_config_dir: Path) -> None:
    """Events are returned in reverse chronological order."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        event = HistoryEvent(
            timestamp=base + timedelta(minutes=i),
            event_type=EventType.SESSION_STARTED,
            client="test-client",
            session_id=f"id{i}",
            session_name=f"test-client/s{i}",
        )
        append_event("test-client", event)

    events = load_history("test-client")
    assert len(events) == 3
    assert events[0].session_id == "id2"
    assert events[1].session_id == "id1"
    assert events[2].session_id == "id0"


def test_load_with_limit(tmp_config_dir: Path) -> None:
    """Limit restricts number of returned events."""
    for i in range(5):
        event = HistoryEvent(
            timestamp=datetime(2025, 6, 1, 12, i, 0, tzinfo=UTC),
            event_type=EventType.SESSION_STARTED,
            client="test-client",
            session_id=f"id{i}",
        )
        append_event("test-client", event)

    events = load_history("test-client", limit=2)
    assert len(events) == 2
    assert events[0].session_id == "id4"


def test_load_with_since_filter(tmp_config_dir: Path) -> None:
    """Since filter excludes events before the threshold."""
    old = HistoryEvent(
        timestamp=datetime(2025, 5, 1, 12, 0, 0, tzinfo=UTC),
        event_type=EventType.SESSION_STARTED,
        client="test-client",
        session_id="old",
    )
    new = HistoryEvent(
        timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        event_type=EventType.SESSION_COMPLETED,
        client="test-client",
        session_id="new",
    )
    append_event("test-client", old)
    append_event("test-client", new)

    since = datetime(2025, 5, 15, 0, 0, 0, tzinfo=UTC)
    events = load_history("test-client", since=since)
    assert len(events) == 1
    assert events[0].session_id == "new"


def test_load_with_event_type_filter(tmp_config_dir: Path) -> None:
    """Event type filter restricts by type."""
    event_types = [
        EventType.SESSION_STARTED,
        EventType.SESSION_COMPLETED,
        EventType.SESSION_CRASHED,
    ]
    for etype in event_types:
        event = HistoryEvent(
            event_type=etype,
            client="test-client",
        )
        append_event("test-client", event)

    events = load_history("test-client", event_types=[EventType.SESSION_CRASHED])
    assert len(events) == 1
    assert events[0].event_type == EventType.SESSION_CRASHED


def test_record_event_appends(tmp_config_dir: Path) -> None:
    """record_event writes to JSONL and calls notification hook."""
    set_notifications_enabled(False)

    event = HistoryEvent(
        event_type=EventType.DAEMON_STARTED,
        client="test-client",
        purpose="debt",
    )
    record_event("test-client", event)

    events = load_history("test-client")
    assert len(events) == 1
    assert events[0].event_type == EventType.DAEMON_STARTED


def test_history_event_metadata(tmp_config_dir: Path) -> None:
    """Events store arbitrary metadata."""
    event = HistoryEvent(
        event_type=EventType.QUEUE_ITEM_ADDED,
        client="test-client",
        metadata={"queue_item_id": "abc123"},
    )
    append_event("test-client", event)

    events = load_history("test-client")
    assert events[0].metadata["queue_item_id"] == "abc123"


def test_multiple_clients_isolated(tmp_config_dir: Path) -> None:
    """Each client has its own history file."""
    for client in ["alpha", "beta"]:
        event = HistoryEvent(
            event_type=EventType.SESSION_STARTED,
            client=client,
            session_name=f"{client}/impl",
        )
        append_event(client, event)

    alpha_events = load_history("alpha")
    beta_events = load_history("beta")
    assert len(alpha_events) == 1
    assert len(beta_events) == 1
    assert alpha_events[0].session_name == "alpha/impl"
    assert beta_events[0].session_name == "beta/impl"
