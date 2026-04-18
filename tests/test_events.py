"""Tests for cw.events — orchestrator event bus."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.events import advance_cursor, read_events
from cw.events import record_event as events_record_event
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.events.EVENTS_DIR (and cw.config.EVENTS_DIR) to tmp_path."""
    events_dir = tmp_path / ".local" / "share" / "cw" / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.events.EVENTS_DIR", events_dir)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    return events_dir


# ---------------------------------------------------------------------------
# Model / enum tests
# ---------------------------------------------------------------------------


def test_all_orchestrator_event_types_round_trip() -> None:
    """Every OrchestratorEventType value survives a Pydantic model round-trip."""
    for etype in OrchestratorEventType:
        event = OrchestratorEvent(type=etype)
        dumped = event.model_dump_json()
        restored = OrchestratorEvent.model_validate_json(dumped)
        assert restored.type == etype
        assert restored.id == event.id


def test_orchestrator_event_defaults() -> None:
    """OrchestratorEvent auto-generates id, created_at, and defaults payload."""
    event = OrchestratorEvent(type=OrchestratorEventType.PR_REGISTERED)
    assert len(event.id) == 16
    assert event.payload == {}
    assert event.correlation_id is None
    assert event.consumed_at is None
    assert event.created_at is not None


def test_orchestrator_event_with_payload() -> None:
    """OrchestratorEvent stores arbitrary payload."""
    payload = {"pr": 42, "repo": "example/repo"}
    event = OrchestratorEvent(
        type=OrchestratorEventType.PR_REGISTERED,
        payload=payload,
        correlation_id="corr-abc",
    )
    assert event.payload == payload
    assert event.correlation_id == "corr-abc"


# ---------------------------------------------------------------------------
# record_event tests
# ---------------------------------------------------------------------------


@freeze_time("2025-06-01 12:00:00", tz_offset=0)
def test_record_event_persists_to_jsonl(tmp_events_dir: Path) -> None:
    """record_event writes a line to inbox.jsonl."""
    event = events_record_event(
        OrchestratorEventType.PR_REGISTERED,
        {"pr": 1},
    )
    inbox = tmp_events_dir / "inbox.jsonl"
    assert inbox.exists()
    lines = [ln for ln in inbox.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["id"] == event.id
    assert data["type"] == "pr.registered"
    assert data["payload"] == {"pr": 1}


def test_record_event_appends_multiple(tmp_events_dir: Path) -> None:
    """Multiple record_event calls produce multiple lines in inbox.jsonl."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_CI_FAILED, {"run": i})
    inbox = tmp_events_dir / "inbox.jsonl"
    lines = [ln for ln in inbox.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3


def test_record_event_returns_event(tmp_events_dir: Path) -> None:
    """record_event returns the OrchestratorEvent that was persisted."""
    ev = events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session": "abc"},
        correlation_id="corr-1",
    )
    assert isinstance(ev, OrchestratorEvent)
    assert ev.correlation_id == "corr-1"
    assert ev.payload["session"] == "abc"


# ---------------------------------------------------------------------------
# read_events tests
# ---------------------------------------------------------------------------


def test_read_events_returns_in_order(tmp_events_dir: Path) -> None:
    """read_events returns events in chronological insertion order."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    events = []
    for i in range(3):
        with freeze_time(base + timedelta(minutes=i)):
            ev = events_record_event(
                OrchestratorEventType.TICKET_ENQUEUED,
                {"i": i},
            )
            events.append(ev)

    result = read_events()
    assert len(result) == 3
    assert result[0].id == events[0].id
    assert result[2].id == events[2].id


def test_read_events_empty_inbox(tmp_events_dir: Path) -> None:
    """read_events returns empty list when inbox does not exist."""
    result = read_events()
    assert result == []


def test_read_events_since_cursor(tmp_events_dir: Path) -> None:
    """read_events with since_cursor skips up to and including the cursor event."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev3 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    result = read_events(since_cursor=ev1.id)
    assert len(result) == 2
    assert result[0].id == ev2.id
    assert result[1].id == ev3.id


def test_read_events_since_cursor_at_last(tmp_events_dir: Path) -> None:
    """Since_cursor equal to last event returns empty list."""
    ev = events_record_event(OrchestratorEventType.PR_MERGED, {})
    result = read_events(since_cursor=ev.id)
    assert result == []


def test_read_events_since_ts_filter(tmp_events_dir: Path) -> None:
    """read_events respects since_ts filter."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    with freeze_time(base):
        events_record_event(OrchestratorEventType.SESSION_SPAWNED, {"seq": 0})
    with freeze_time(base + timedelta(hours=2)):
        ev_new = events_record_event(
            OrchestratorEventType.SESSION_COMPLETED, {"seq": 1}
        )

    cutoff = base + timedelta(hours=1)
    result = read_events(since_ts=cutoff)
    assert len(result) == 1
    assert result[0].id == ev_new.id


def test_read_events_type_filter(tmp_events_dir: Path) -> None:
    """read_events filters by event_types."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {})
    ev_ci = events_record_event(OrchestratorEventType.PR_CI_FAILED, {})
    events_record_event(OrchestratorEventType.PR_MERGED, {})

    result = read_events(event_types=[OrchestratorEventType.PR_CI_FAILED])
    assert len(result) == 1
    assert result[0].id == ev_ci.id


def test_read_events_limit(tmp_events_dir: Path) -> None:
    """read_events respects limit."""
    for _ in range(5):
        events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {})
    result = read_events(limit=3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# advance_cursor / consumer cursor tests
# ---------------------------------------------------------------------------


def test_advance_cursor_creates_file(tmp_events_dir: Path) -> None:
    """advance_cursor creates a cursor JSON file."""
    advance_cursor("test-consumer", "abcdef1234567890")
    cursor_file = tmp_events_dir / "cursors" / "test-consumer.json"
    assert cursor_file.exists()
    data = json.loads(cursor_file.read_text())
    assert data["cursor"] == "abcdef1234567890"
    assert "updated_at" in data


def test_advance_cursor_overwrites_previous(tmp_events_dir: Path) -> None:
    """advance_cursor updates an existing cursor file."""
    advance_cursor("consumer", "first-id-123456789")
    advance_cursor("consumer", "second-id-12345678")
    cursor_file = tmp_events_dir / "cursors" / "consumer.json"
    data = json.loads(cursor_file.read_text())
    assert data["cursor"] == "second-id-12345678"


def test_read_events_with_consumer_cursor(tmp_events_dir: Path) -> None:
    """read_events(consumer=...) uses the persisted cursor."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev3 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    # Persist cursor at ev1
    advance_cursor("myapp", ev1.id)

    # Consumer reads from cursor: should get ev2, ev3
    result = read_events(consumer="myapp")
    assert len(result) == 2
    assert result[0].id == ev2.id
    assert result[1].id == ev3.id

    # Advance cursor to ev3 and confirm nothing new
    advance_cursor("myapp", ev3.id)
    result2 = read_events(consumer="myapp")
    assert result2 == []


def test_read_events_consumer_no_cursor(tmp_events_dir: Path) -> None:
    """read_events with a consumer that has no saved cursor returns all events."""
    ev1 = events_record_event(OrchestratorEventType.PR_MERGED, {})
    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {})

    result = read_events(consumer="brand-new-consumer")
    assert len(result) == 2
    assert result[0].id == ev1.id
    assert result[1].id == ev2.id


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_event_record_creates_event(tmp_events_dir: Path) -> None:
    """cw event record pr.registered persists event to inbox."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "record", "pr.registered", "--payload", '{"pr": 42}'],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded event:" in result.output
    assert "pr.registered" in result.output

    inbox = tmp_events_dir / "inbox.jsonl"
    assert inbox.exists()
    data = json.loads(inbox.read_text().strip())
    assert data["type"] == "pr.registered"
    assert data["payload"] == {"pr": 42}


def test_cli_event_record_with_correlation_id(tmp_events_dir: Path) -> None:
    """cw event record --correlation-id passes the correlation_id."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "record",
            "pr.merged",
            "--correlation-id",
            "corr-xyz",
        ],
    )
    assert result.exit_code == 0, result.output
    inbox = tmp_events_dir / "inbox.jsonl"
    data = json.loads(inbox.read_text().strip())
    assert data["correlation_id"] == "corr-xyz"


def test_cli_event_record_invalid_type(tmp_events_dir: Path) -> None:
    """cw event record with unknown type returns error."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "record", "not.a.type"],
    )
    assert result.exit_code != 0
    assert "Unknown event type" in result.output


def test_cli_event_record_invalid_payload(tmp_events_dir: Path) -> None:
    """cw event record with non-JSON payload returns error."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "record", "pr.registered", "--payload", "not-json"],
    )
    assert result.exit_code != 0
    assert "Invalid JSON payload" in result.output


def test_cli_event_tail_prints_events(tmp_events_dir: Path) -> None:
    """cw event tail shows recorded events."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})
    events_record_event(OrchestratorEventType.PR_MERGED, {"pr": 1})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert "pr.registered" in result.output
    assert "pr.merged" in result.output


def test_cli_event_tail_json_flag(tmp_events_dir: Path) -> None:
    """cw event tail --json outputs valid JSON per line."""
    events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {"ticket_id": "T-1"})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--json"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    data = json.loads(lines[0])
    assert data["type"] == "ticket.enqueued"
    assert data["payload"]["ticket_id"] == "T-1"


def test_cli_event_tail_type_filter(tmp_events_dir: Path) -> None:
    """cw event tail --type filters by event type."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {})
    events_record_event(OrchestratorEventType.PR_CI_FAILED, {})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--type", "pr.ci_failed"])
    assert result.exit_code == 0, result.output
    assert "pr.ci_failed" in result.output
    assert "pr.registered" not in result.output


def test_cli_event_tail_empty(tmp_events_dir: Path) -> None:
    """cw event tail with no events prints 'No events.'"""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert "No events." in result.output


def test_cli_event_tail_since_consumer_advances_cursor(tmp_events_dir: Path) -> None:
    """cw event tail --since <consumer> reads new events and advances cursor."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    # First tail: reads both events, advances cursor
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--since", "daemon"])
    assert result.exit_code == 0, result.output
    assert ev1.id in result.output
    assert ev2.id in result.output

    # New event added
    ev3 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 3})

    # Second tail: cursor was at ev2, should only get ev3
    result2 = runner.invoke(main, ["event", "tail", "--since", "daemon"])
    assert result2.exit_code == 0, result2.output
    assert ev3.id in result2.output
    assert ev1.id not in result2.output
    assert ev2.id not in result2.output
