"""Tests for cw.events — orchestrator event bus."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from click.testing import CliRunner
from freezegun import freeze_time
from pydantic import ValidationError

from cw.cli import main
from cw.events import (
    PruneResult,
    advance_cursor,
    init_cursor_at_end,
    prune_events,
    read_events,
    tail_events_follow,
    wait_for_event,
)
from cw.events import record_event as events_record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Model / enum tests
# ---------------------------------------------------------------------------

# `tmp_events_dir` lives in tests/conftest.py (promoted from here; see #1002)
# so both this file and tests/test_cw_operator_events.py can use it.


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


def test_cli_event_record_session_needs_attention(tmp_events_dir: Path) -> None:
    """cw event record session.needs_attention persists with a round-tripped payload.

    Regression pin (#952): the WARN emitted by auto-dev-intake on a comments
    fetch failure relies on session.needs_attention being an accepted event type
    for `cw event record`. The allowlist is enum-derived, so this asserts the
    contract stays intact.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "record",
            "session.needs_attention",
            "--payload",
            '{"reason": "comments_fetch_failed", "ticket_id": "952", '
            '"session_id": "s1"}',
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded event:" in result.output

    inbox = tmp_events_dir / "inbox.jsonl"
    assert inbox.exists()
    data = json.loads(inbox.read_text().strip())
    assert data["type"] == "session.needs_attention"
    assert data["payload"]["reason"] == "comments_fetch_failed"
    assert data["payload"]["ticket_id"] == "952"
    assert data["payload"]["session_id"] == "s1"


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
    """Fresh --since <consumer> starts at now; subsequent calls see only new events."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    # First tail with fresh cursor: history should NOT be replayed
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--since", "daemon"])
    assert result.exit_code == 0, result.output
    assert ev1.id not in result.output
    assert ev2.id not in result.output

    # New event added after cursor initialized
    ev3 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 3})

    # Second tail: cursor was at ev2, should only get ev3
    result2 = runner.invoke(main, ["event", "tail", "--since", "daemon"])
    assert result2.exit_code == 0, result2.output
    assert ev3.id in result2.output
    assert ev1.id not in result2.output
    assert ev2.id not in result2.output


# ---------------------------------------------------------------------------
# Stage event tests (issue #173)
# ---------------------------------------------------------------------------


def test_record_event_stage_entered_persists(tmp_events_dir: Path) -> None:
    """record_event persists a STAGE_ENTERED event with its full payload."""
    payload = {
        "session_id": "abc12345",
        "ticket_id": "173",
        "stage": "s2_impl_started",
        "prev_stage": "s1_plan_reviewed",
        "started_at": "2026-05-23T13:01:42Z",
    }
    events_record_event(OrchestratorEventType.STAGE_ENTERED, payload)

    events = read_events()
    assert len(events) == 1
    assert events[0].type is OrchestratorEventType.STAGE_ENTERED
    assert events[0].payload == payload


def test_record_event_stage_errored_persists(tmp_events_dir: Path) -> None:
    """record_event persists a STAGE_ERRORED event including error_kind."""
    payload = {
        "session_id": "abc12345",
        "ticket_id": "173",
        "stage": "s2_impl_started",
        "started_at": "2026-05-23T13:01:42Z",
        "error_kind": "agent_block",
    }
    events_record_event(OrchestratorEventType.STAGE_ERRORED, payload)

    events = read_events()
    assert len(events) == 1
    assert events[0].type is OrchestratorEventType.STAGE_ERRORED
    assert events[0].payload == payload


def test_read_events_type_filter_stage_entered(tmp_events_dir: Path) -> None:
    """read_events(event_types=[STAGE_ENTERED]) returns only stage_entered events."""
    ev_stage = events_record_event(
        OrchestratorEventType.STAGE_ENTERED,
        {
            "session_id": "abc",
            "ticket_id": "173",
            "stage": "s2_impl_started",
            "started_at": "2026-05-23T13:01:42Z",
        },
    )
    events_record_event(OrchestratorEventType.PR_MERGED, {"pr": 1})
    events_record_event(
        OrchestratorEventType.STAGE_ERRORED,
        {
            "session_id": "abc",
            "ticket_id": "173",
            "stage": "s2_impl_started",
            "started_at": "2026-05-23T13:01:45Z",
            "error_kind": "agent_block",
        },
    )

    result = read_events(event_types=[OrchestratorEventType.STAGE_ENTERED])
    assert len(result) == 1
    assert result[0].id == ev_stage.id


def test_cli_event_record_stage_entered_works(tmp_events_dir: Path) -> None:
    """cw event record stage.entered persists a STAGE_ENTERED event to disk."""
    payload_json = (
        '{"session_id":"x","ticket_id":"173",'
        '"stage":"s2_impl_started","started_at":"2026-05-23T13:01:42Z"}'
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "record", "stage.entered", "--payload", payload_json],
    )
    assert result.exit_code == 0, result.output

    events = read_events()
    assert len(events) == 1
    assert events[0].type is OrchestratorEventType.STAGE_ENTERED
    assert events[0].payload["stage"] == "s2_impl_started"


def test_cli_event_tail_type_filter_stage_entered(tmp_events_dir: Path) -> None:
    """cw event tail --type stage.entered filters out non-stage events."""
    events_record_event(
        OrchestratorEventType.STAGE_ENTERED,
        {
            "session_id": "abc",
            "ticket_id": "173",
            "stage": "s2_impl_started",
            "started_at": "2026-05-23T13:01:42Z",
        },
    )
    events_record_event(OrchestratorEventType.PR_MERGED, {"pr": 99})

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "tail", "--type", "stage.entered"],
    )
    assert result.exit_code == 0, result.output
    assert "stage.entered" in result.output
    assert "pr.merged" not in result.output


# ---------------------------------------------------------------------------
# Robustness: cursor-not-found fallback + torn-read guard (issue #393)
# ---------------------------------------------------------------------------


def test_read_events_cursor_not_found_replays_from_start(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """cursor pointing at nonexistent event id replays all events from the start."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev3 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    # Persist a cursor pointing at a nonexistent event id
    nonexistent_id = "deadbeef-0000-1111-2222-333333333333"
    advance_cursor("stale_consumer", nonexistent_id)

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = read_events(consumer="stale_consumer")

    assert len(result) == 3
    assert result[0].id == ev1.id
    assert result[1].id == ev2.id
    assert result[2].id == ev3.id
    assert any(nonexistent_id in record.message for record in caplog.records)


def test_read_events_torn_final_line_skipped(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed trailing line (torn write) is skipped with a warning."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    # Append partial JSON with NO trailing newline (simulate torn write)
    inbox = tmp_events_dir / "inbox.jsonl"
    with inbox.open("a") as f:
        f.write('{"type": "pr.registered", "incomplete"')  # no trailing newline

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = read_events()

    assert len(result) == 2
    assert result[0].id == ev1.id
    assert result[1].id == ev2.id
    assert any("malformed" in record.message for record in caplog.records)


def test_read_events_torn_interior_line_raises(tmp_events_dir: Path) -> None:
    """Interior corrupt line (with trailing newline) raises JSONDecodeError."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    # Append invalid JSON with trailing newline — interior (not last line)
    inbox = tmp_events_dir / "inbox.jsonl"
    with inbox.open("a") as f:
        f.write('{"type": "bad", "broken"\n')  # trailing newline = interior

    events_record_event(OrchestratorEventType.PR_MERGED, {"n": 3})

    with pytest.raises(json.JSONDecodeError):
        read_events()


def test_read_events_normal_cursor_semantics_unchanged(tmp_events_dir: Path) -> None:
    """Normal cursor semantics: only events after the cursor are returned."""
    ev_a = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev_b = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev_c = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    advance_cursor("normal_consumer", ev_b.id)
    result = read_events(consumer="normal_consumer")

    assert len(result) == 1
    assert result[0].id == ev_c.id
    ids = {e.id for e in result}
    assert ev_a.id not in ids
    assert ev_b.id not in ids


# ---------------------------------------------------------------------------
# Robustness: forward-compatible unknown event type (issue #1210)
# ---------------------------------------------------------------------------


def _append_raw_event(inbox: Path, **overrides: object) -> None:
    """Hand-append a full valid-shape event dict, with *overrides* applied.

    Mirrors the hand-append pattern used by the issue #393 torn-line tests,
    but writes a complete (well-formed JSON, full-schema) event line so only
    the fields in *overrides* are deliberately broken.
    """
    event: dict[str, object] = {
        "id": uuid4().hex[:16],
        "type": "pr.registered",
        "payload": {},
        "correlation_id": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    event.update(overrides)
    with inbox.open("a") as f:
        f.write(json.dumps(event) + "\n")


def test_read_events_unknown_type_line_skipped_with_warning(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A line with an unrecognized ``type`` is skipped, not raised, with a warning."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type")

    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = read_events()

    assert [e.id for e in result] == [ev1.id, ev2.id]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert "future.event.type" in warning_records[0].message


def test_read_events_multiple_unknown_types_single_summary_warning(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Multiple unknown-type lines are all skipped but summarized in one warning."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type")
    _append_raw_event(inbox, type="another.new.type")

    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = read_events()

    assert [e.id for e in result] == [ev1.id, ev2.id]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert "future.event.type" in warning_records[0].message
    assert "another.new.type" in warning_records[0].message


def test_read_events_known_type_other_validation_failure_raises(
    tmp_events_dir: Path,
) -> None:
    """A known type combined with an unrelated bad field still raises."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(
        inbox, type=OrchestratorEventType.PR_REGISTERED.value, created_at="not-a-date"
    )

    with pytest.raises(ValidationError):
        read_events()


def test_read_events_unknown_type_plus_other_bad_field_raises(
    tmp_events_dir: Path,
) -> None:
    """An unknown type PLUS a second bad field still raises (uses all(), not any())."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type", created_at="not-a-date")

    with pytest.raises(ValidationError):
        read_events()


def test_read_events_unknown_type_interspersed_with_cursor(
    tmp_events_dir: Path,
) -> None:
    """An unknown-type line does not disrupt normal cursor-based filtering."""
    ev_a = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev_b = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type")

    ev_c = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    advance_cursor("unknown_type_consumer", ev_b.id)
    result = read_events(consumer="unknown_type_consumer")

    assert [e.id for e in result] == [ev_c.id]
    ids = {e.id for e in result}
    assert ev_a.id not in ids
    assert ev_b.id not in ids


def test_prune_events_skips_unknown_type_line(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """prune_events tolerates an unknown-type line among the events it prunes."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type")

    ev_last = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = prune_events(keep=1)

    assert result.archived_count == 3
    assert result.kept_count == 1
    assert result.archive_path is not None
    remaining = read_events()
    assert [e.id for e in remaining] == [ev_last.id]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert "future.event.type" in warning_records[0].message


# ---------------------------------------------------------------------------
# Bug fixes: --json empty output + fresh --since cursor (issue #738)
# ---------------------------------------------------------------------------


def test_cli_event_tail_empty_json(tmp_events_dir: Path) -> None:
    """cw event tail --json with no events emits '[]', not 'No events.'"""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--json"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "[]"
    assert "No events." not in result.output


def test_cli_event_tail_since_fresh_cursor_starts_at_now(tmp_events_dir: Path) -> None:
    """Fresh --since <consumer>: pre-seeded history is NOT replayed."""
    # Pre-seed history before consumer ever runs
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    runner = CliRunner()
    # First call: fresh cursor — should return 0 events (no history replay)
    result = runner.invoke(main, ["event", "tail", "--since", "freshconsumer"])
    assert result.exit_code == 0, result.output
    assert ev1.id not in result.output
    assert ev2.id not in result.output

    # New event recorded after cursor initialized
    ev3 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 3})

    # Second call: only new event should appear
    result2 = runner.invoke(main, ["event", "tail", "--since", "freshconsumer"])
    assert result2.exit_code == 0, result2.output
    assert ev3.id in result2.output
    assert ev1.id not in result2.output
    assert ev2.id not in result2.output


def test_cli_event_tail_since_fresh_cursor_empty_inbox(tmp_events_dir: Path) -> None:
    """Fresh --since cursor on empty inbox returns 'No events.' (not JSON)."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--since", "emptyconsumer"])
    assert result.exit_code == 0, result.output
    assert "No events." in result.output


def test_init_cursor_at_end_fresh_consumer(tmp_events_dir: Path) -> None:
    """init_cursor_at_end returns True and sets cursor to last event id."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev3 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 3})

    result = init_cursor_at_end("freshconsumer")

    assert result is True
    cursor_file = tmp_events_dir / "cursors" / "freshconsumer.json"
    assert cursor_file.exists()
    data = json.loads(cursor_file.read_text())
    assert data["cursor"] == ev3.id
    # ev1, ev2 ids should not match the cursor
    assert data["cursor"] != ev1.id
    assert data["cursor"] != ev2.id


def test_init_cursor_at_end_existing_consumer_no_op(tmp_events_dir: Path) -> None:
    """init_cursor_at_end is a no-op (returns False) when cursor file already exists."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    # Pre-create cursor at ev1 (not the latest event)
    advance_cursor("existingconsumer", ev1.id)

    result = init_cursor_at_end("existingconsumer")

    assert result is False
    cursor_file = tmp_events_dir / "cursors" / "existingconsumer.json"
    data = json.loads(cursor_file.read_text())
    # Cursor must still point at ev1, not advanced to ev2
    assert data["cursor"] == ev1.id


def test_init_cursor_at_end_empty_inbox(tmp_events_dir: Path) -> None:
    """init_cursor_at_end returns False and creates no cursor when inbox is empty."""
    result = init_cursor_at_end("noconsumer")

    assert result is False
    cursor_file = tmp_events_dir / "cursors" / "noconsumer.json"
    assert not cursor_file.exists()


def test_init_cursor_at_end_unparseable_inbox(tmp_events_dir: Path) -> None:
    """init_cursor_at_end returns False when inbox has content that fails parsing."""
    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text("not-valid-json\n")

    result = init_cursor_at_end("anyconsumer")

    assert result is False
    cursor_file = tmp_events_dir / "cursors" / "anyconsumer.json"
    assert not cursor_file.exists()


def test_cli_event_tail_invalid_since_value(tmp_events_dir: Path) -> None:
    """cw event tail --since with an unparseable value exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--since", "2025-99-99T00:00:00"])
    assert result.exit_code != 0
    assert "Cannot parse --since value" in result.output


def test_cli_event_tail_since_naive_timestamp(tmp_events_dir: Path) -> None:
    """cw event tail --since with a tz-naive ISO timestamp is accepted (UTC assumed)."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--since", "2000-01-01T00:00:00"])
    assert result.exit_code == 0, result.output


def test_cli_event_tail_invalid_type_filter(tmp_events_dir: Path) -> None:
    """cw event tail --type with an unknown event type exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--type", "not.a.real.type"])
    assert result.exit_code != 0
    assert "Unknown event type" in result.output


# ---------------------------------------------------------------------------
# --follow streaming mode (issue #206)
# ---------------------------------------------------------------------------


def test_cli_event_tail_follow_exits_on_keyboard_interrupt(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow exits with code 130 on immediate KeyboardInterrupt."""

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"])
    assert result.exit_code == 130


def test_cli_event_tail_follow_emits_preexisting_event(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow emits pre-existing events, then exits."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    def raise_on_first_sleep(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_on_first_sleep)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"])
    assert result.exit_code == 130
    assert ev1.id in result.output


def test_cli_event_tail_follow_streams_new_events(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow streams events written after the command starts."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2_holder: list[OrchestratorEvent] = []
    printed_ids: list[str] = []

    # Why: no public seam observes "was this event actually streamed"; a
    # pass-through spy on the private _print_event is the only observable
    # streaming point. Tolerates the tail_events_follow _poll_inbox_growth
    # change-detection guard (#954), which can lag a just-completed append by
    # one poll iteration.
    from cw.cli import queues

    original_print_event = queues._print_event

    def spy_print_event(ev: OrchestratorEvent, *, as_json: bool) -> None:
        printed_ids.append(ev.id)
        original_print_event(ev, as_json=as_json)

    monkeypatch.setattr(queues, "_print_event", spy_print_event)

    max_poll_iterations = 100  # safety cap: bound the poll-until loop
    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})
            ev2_holder.append(ev2)
            return
        if ev2_holder[0].id in printed_ids:
            raise KeyboardInterrupt
        if call_count - 1 >= max_poll_iterations:
            msg = (
                f"ev2 {ev2_holder[0].id} not streamed after "
                f"{max_poll_iterations} polls; printed={printed_ids}"
            )
            raise AssertionError(msg)
        # else: no-op — let the follow loop re-poll until st_size catches up

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"], catch_exceptions=False)
    assert result.exit_code == 130
    assert ev1.id in result.output
    assert len(ev2_holder) == 1
    assert ev2_holder[0].id in printed_ids
    assert ev2_holder[0].id in result.output


def test_cli_event_tail_follow_with_type_filter(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow --type filters events in the stream."""
    ev_reg = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev_ci = events_record_event(OrchestratorEventType.PR_CI_FAILED, {"n": 2})

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--follow", "--type", "pr.ci_failed"]
    )
    assert result.exit_code == 130
    assert ev_ci.id in result.output
    assert ev_reg.id not in result.output


def test_cli_event_tail_follow_with_json_flag(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow --json outputs valid JSON per line."""
    events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {"ticket_id": "T-1"})

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow", "--json"])
    assert result.exit_code == 130
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines, "expected at least one output line"
    data = json.loads(lines[0])
    assert data["type"] == "ticket.enqueued"
    assert data["payload"]["ticket_id"] == "T-1"


def test_cli_event_tail_follow_with_since_ts(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow --since <timestamp> skips events before the cutoff."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    with freeze_time(base):
        ev_old = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    with freeze_time(base + timedelta(hours=2)):
        ev_new = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    cutoff = base + timedelta(hours=1)
    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--follow", "--since", cutoff.isoformat()]
    )
    assert result.exit_code == 130
    assert ev_new.id in result.output
    assert ev_old.id not in result.output


def test_cli_event_tail_follow_does_not_advance_consumer_cursor(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow --since <consumer> does not advance the cursor."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})
    advance_cursor("followconsumer", ev1.id)

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--follow", "--since", "followconsumer"]
    )
    assert result.exit_code == 130
    assert ev2.id in result.output

    # Cursor must still point at ev1
    cursor_file = tmp_events_dir / "cursors" / "followconsumer.json"
    data = json.loads(cursor_file.read_text())
    assert data["cursor"] == ev1.id


def test_cli_event_tail_follow_combined_since_consumer(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow + --since <consumer> respects cursor AND type filter on initial drain."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})
    # ev3 is after the cursor but has a non-matching type — must be suppressed
    ev3 = events_record_event(OrchestratorEventType.PR_CI_FAILED, {"n": 3})
    advance_cursor("combinedconsumer", ev1.id)

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "tail",
            "--follow",
            "--since",
            "combinedconsumer",
            "--type",
            "pr.merged",
        ],
    )
    assert result.exit_code == 130
    assert ev1.id not in result.output  # before cursor
    assert ev2.id in result.output  # after cursor, matching type
    assert ev3.id not in result.output  # after cursor, non-matching type — SUPPRESSED

    # Cursor must NOT be advanced
    cursor_file = tmp_events_dir / "cursors" / "combinedconsumer.json"
    data = json.loads(cursor_file.read_text())
    assert data["cursor"] == ev1.id


def test_cli_event_tail_follow_consumer_cursor_miss_warns_and_replays(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow + --since <unknown-consumer> warns on stderr and replays from start."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--follow", "--since", "unknownconsumer"]
    )
    assert result.exit_code == 130
    expected_warning = (
        "Warning: consumer cursor 'unknownconsumer' not found; replaying from start"
    )
    assert expected_warning in result.output
    assert ev1.id in result.output


def test_cli_event_tail_follow_exits_on_broken_pipe(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow exits cleanly (code 0) on BrokenPipeError."""

    def broken_pipe_gen(**kwargs: object) -> Generator[OrchestratorEvent]:
        raise BrokenPipeError
        yield  # pragma: no cover

    monkeypatch.setattr("cw.cli.queues.tail_events_follow", broken_pipe_gen)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"])
    assert result.exit_code == 0


def test_cli_event_tail_follow_returns_on_exhausted_stream(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event tail --follow exits cleanly when the stream generator is exhausted."""

    def empty_gen(**kwargs: object) -> Generator[OrchestratorEvent]:
        return
        yield  # pragma: no cover

    monkeypatch.setattr("cw.cli.queues.tail_events_follow", empty_gen)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# tail_events_follow — _poll_inbox_growth change-detection guard (issue #954)
# ---------------------------------------------------------------------------


def test_poll_inbox_growth_stat_oserror_treated_as_size_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat() OSError on an existing inbox is treated as size 0 (reads)."""
    from cw.events import _poll_inbox_growth

    class _RaisingPath:
        def exists(self) -> bool:
            return True

        def stat(self) -> object:
            raise OSError

    monkeypatch.setattr("cw.events.inbox_path", _RaisingPath)

    should_read, size = _poll_inbox_growth(None)

    assert size == 0
    assert should_read is True


def test_tail_events_follow_size_decrease_warns_and_continues(
    tmp_events_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive shrink branch: inbox size decrease warns and the loop continues.

    The inbox is append-only in production, so this exercises the defensive
    dead branch of the shared _poll_inbox_growth guard: it must warn, reset the
    baseline, skip the read for that poll, and keep polling (no crash).
    """
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    inbox = tmp_events_dir / "inbox.jsonl"

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Truncate the inbox to force current_size < last_size on next poll.
            inbox.write_text("")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    gen = tail_events_follow(
        since_cursor=None,
        since_ts=None,
        event_types=None,
    )
    with (
        caplog.at_level(logging.WARNING, logger="cw.events"),
        pytest.raises(KeyboardInterrupt),
    ):
        list(gen)

    assert any("inbox size decreased" in record.message for record in caplog.records)


def test_tail_events_follow_absent_inbox_first_poll_reads(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First poll with an absent inbox reads regardless (size 0) without crashing."""
    inbox = tmp_events_dir / "inbox.jsonl"
    assert not inbox.exists()

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    gen = tail_events_follow(
        since_cursor=None,
        since_ts=None,
        event_types=None,
    )
    with pytest.raises(KeyboardInterrupt):
        list(gen)

    # First-pass-reads-regardless invariant: the guard read once, then slept.
    assert call_count == 1


def test_tail_events_follow_empty_inbox_no_events_no_crash(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-empty inbox yields no events and does not crash."""
    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text("")

    yielded: list[OrchestratorEvent] = []

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    gen = tail_events_follow(
        since_cursor=None,
        since_ts=None,
        event_types=None,
    )
    with pytest.raises(KeyboardInterrupt):
        yielded.extend(gen)

    assert yielded == []


def test_wait_for_event_detects_appended_event_via_shared_guard(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait_for_event detects an event appended after the first poll via the guard.

    Deterministic counterpart to test_wait_for_event_blocks_then_matches (which
    uses a real thread): the shared _poll_inbox_growth guard must see the size
    grow on the second poll and drive the read that surfaces the match.
    """
    appended: list[OrchestratorEvent] = []

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        if not appended:
            appended.append(
                events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})
            )

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        timeout=5.0,
    )
    result = next(gen)

    assert appended
    assert result.id == appended[0].id


def test_ticket_needs_sync_event_type_serializes(tmp_events_dir: Path) -> None:
    """ticket.needs_sync OrchestratorEvent round-trips through JSON serialisation."""
    event = OrchestratorEvent(
        type=OrchestratorEventType.TICKET_NEEDS_SYNC,
        payload={"ticket_id": "CW-99", "client": "test-client"},
    )
    dumped = event.model_dump_json()
    restored = OrchestratorEvent.model_validate_json(dumped)
    assert restored.type == OrchestratorEventType.TICKET_NEEDS_SYNC
    assert restored.payload["ticket_id"] == "CW-99"
    assert restored.payload["client"] == "test-client"


# ---------------------------------------------------------------------------
# wait_for_event — unit tests (issue #275)
# ---------------------------------------------------------------------------


def test_wait_for_event_matches_preexisting_event(tmp_events_dir: Path) -> None:
    """wait_for_event yields an event already in the inbox before it is called."""
    ev = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})
    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        timeout=5.0,
    )
    result = next(gen)
    assert result.id == ev.id


def test_wait_for_event_blocks_then_matches(tmp_events_dir: Path) -> None:
    """wait_for_event blocks until a matching event is written by another thread."""
    matched: list[OrchestratorEvent] = []

    def writer() -> None:
        time.sleep(0.05)
        events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    for ev in wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        timeout=5.0,
    ):
        matched.append(ev)
        break

    t.join(timeout=2.0)
    assert len(matched) == 1
    assert matched[0].type == OrchestratorEventType.SESSION_COMPLETED


def test_wait_for_event_timeout_raises(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait_for_event raises TimeoutError when no matching event arrives."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="No matching event"):
        for _ in wait_for_event(
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
            timeout=0.01,
        ):
            pass


def test_wait_for_event_type_filter(tmp_events_dir: Path) -> None:
    """wait_for_event skips events that don't match event_types."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev_match = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 2})

    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        timeout=1.0,
    )
    result = next(gen)
    assert result.id == ev_match.id


def test_wait_for_event_correlation_id_filter(tmp_events_dir: Path) -> None:
    """wait_for_event filters by correlation_id."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {}, correlation_id="other"
    )
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {}, correlation_id="target-123"
    )

    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        correlation_id="target-123",
        timeout=1.0,
    )
    result = next(gen)
    assert result.id == ev_match.id


def test_wait_for_event_session_id_filter(tmp_events_dir: Path) -> None:
    """wait_for_event filters by payload session_id."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {"session_id": "other-sess"}
    )
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {"session_id": "target-sess"}
    )

    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        session_id="target-sess",
        timeout=1.0,
    )
    result = next(gen)
    assert result.id == ev_match.id


def test_wait_for_event_follow_yields_multiple(tmp_events_dir: Path) -> None:
    """wait_for_event follow=True yields all pre-existing matches then returns."""
    ev1 = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 2})

    results = list(
        wait_for_event(
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
            timeout=0.01,
            poll_interval=0.001,
            follow=True,
        )
    )
    assert len(results) == 2
    assert results[0].id == ev1.id
    assert results[1].id == ev2.id


def test_wait_for_event_follow_timeout_after_match_returns(
    tmp_events_dir: Path,
) -> None:
    """follow=True: expiring timeout after a match returns cleanly (no TimeoutError)."""
    events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})

    results = list(
        wait_for_event(
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
            timeout=0.01,
            poll_interval=0.001,
            follow=True,
        )
    )
    assert len(results) == 1


def test_wait_for_event_no_filter_matches_all_types(tmp_events_dir: Path) -> None:
    """wait_for_event with no event_types filter matches any event type."""
    ev = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    result = next(wait_for_event(timeout=1.0))
    assert result.id == ev.id


# ---------------------------------------------------------------------------
# CLI: cw event wait (issue #275)
# ---------------------------------------------------------------------------


def test_cli_event_wait_exits_on_match(tmp_events_dir: Path) -> None:
    """cw event wait exits 0 when a matching event is found."""
    events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "session.completed", "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["type"] == "session.completed"


def test_cli_event_wait_timeout_exits_nonzero(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event wait exits non-zero when timeout expires with no match."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "session.completed", "--timeout", "0.01"],
    )
    assert result.exit_code != 0
    assert "No matching event" in result.output


def test_cli_event_wait_type_filter(tmp_events_dir: Path) -> None:
    """cw event wait respects --type filter."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {})
    ev_match = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {})
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "session.completed", "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["id"] == ev_match.id


def test_cli_event_wait_ticket_filter(tmp_events_dir: Path) -> None:
    """cw event wait --ticket filters by correlation_id."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {}, correlation_id="other"
    )
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {}, correlation_id="ticket-99"
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "wait",
            "--type",
            "session.completed",
            "--ticket",
            "ticket-99",
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["id"] == ev_match.id


def test_cli_event_wait_session_filter(tmp_events_dir: Path) -> None:
    """cw event wait --session filters by payload session_id."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"session_id": "other-sess"},
    )
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"session_id": "target-sess"},
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "wait",
            "--type",
            "session.completed",
            "--session",
            "target-sess",
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["id"] == ev_match.id


def test_cli_event_wait_follow_exits_on_keyboard_interrupt(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event wait --follow exits 130 on KeyboardInterrupt."""
    events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"n": 1})

    def raise_interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_interrupt)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "session.completed", "--follow"],
    )
    assert result.exit_code == 130


def test_cli_event_wait_invalid_type(tmp_events_dir: Path) -> None:
    """cw event wait with unknown --type exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "not.a.real.type"],
    )
    assert result.exit_code != 0
    assert "Unknown event type" in result.output


def test_cli_event_wait_outputs_json(tmp_events_dir: Path) -> None:
    """cw event wait outputs valid JSON per match."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"session_id": "abc", "result": "done"},
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "wait", "--type", "session.completed", "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["type"] == "session.completed"
    assert data["payload"]["result"] == "done"


def test_cli_event_wait_exits_cleanly_on_broken_pipe(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cw event wait exits 0 on BrokenPipeError (downstream reader closed pipe)."""

    def broken_pipe_gen(**kwargs: object) -> Generator[OrchestratorEvent]:
        raise BrokenPipeError
        yield  # pragma: no cover

    monkeypatch.setattr("cw.cli.queues.wait_for_event", broken_pipe_gen)
    runner = CliRunner()
    result = runner.invoke(main, ["event", "wait"])
    assert result.exit_code == 0


def test_cli_event_wait_client_filter(tmp_events_dir: Path) -> None:
    """cw event wait --client filters by payload client field."""
    events_record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"client": "other-client"},
    )
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"client": "claude-workspace"},
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "wait",
            "--type",
            "session.completed",
            "--client",
            "claude-workspace",
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["id"] == ev_match.id


def test_wait_for_event_client_filter(tmp_events_dir: Path) -> None:
    """wait_for_event filters by payload client field."""
    events_record_event(OrchestratorEventType.SESSION_COMPLETED, {"client": "other"})
    ev_match = events_record_event(
        OrchestratorEventType.SESSION_COMPLETED, {"client": "claude-workspace"}
    )

    gen = wait_for_event(
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
        client="claude-workspace",
        timeout=1.0,
    )
    result = next(gen)
    assert result.id == ev_match.id


def test_cli_event_wait_type_comma_separated(tmp_events_dir: Path) -> None:
    """cw event wait --type accepts comma-separated values (ticket AC1 syntax)."""
    ev = events_record_event(OrchestratorEventType.SESSION_COMPLETED, {})
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "wait",
            "--type",
            "session.completed,session.timed_out",
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["id"] == ev.id


# ---------------------------------------------------------------------------
# --client filter (issue #783)
# ---------------------------------------------------------------------------


def test_cli_event_tail_client_filter(tmp_events_dir: Path) -> None:
    """--client filters events by payload.client field."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", "alpha"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "beta" not in result.output  # "beta" has non-hex 't', safe in hex event IDs


def test_cli_event_tail_client_filter_multiple(tmp_events_dir: Path) -> None:
    """--client can be repeated to include events from multiple clients."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "gamma", "session_id": "ccc"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--client", "alpha", "--client", "beta"]
    )
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "bbb" in result.output
    assert "gamma" not in result.output


def test_cli_event_tail_client_filter_no_match(tmp_events_dir: Path) -> None:
    """--client with no matching events outputs 'No events.'"""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", "gamma"])
    assert result.exit_code == 0, result.output
    assert "No events." in result.output


def test_cli_event_tail_client_filter_events_without_client_field(
    tmp_events_dir: Path,
) -> None:
    """Events with no payload.client field are excluded when --client is used."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", "alpha"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "pr.registered" not in result.output


def test_cli_event_tail_client_filter_comma_separated(tmp_events_dir: Path) -> None:
    """--client a,b is equivalent to --client a --client b."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "gamma", "session_id": "ccc"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", "alpha,beta"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "bbb" in result.output
    # "gamma" has non-hex 'g'/'m', safe against hex event ID false positives
    assert "gamma" not in result.output


# ---------------------------------------------------------------------------
# --dedup-terminal (issue #783)
# ---------------------------------------------------------------------------


def test_cli_event_tail_dedup_terminal_collapses_same_session(
    tmp_events_dir: Path,
) -> None:
    """--dedup-terminal keeps only first occurrence of (type, session) pair."""
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {"session_id": "s1"},
            correlation_id="T-1",
        )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--dedup-terminal"])
    assert result.exit_code == 0, result.output
    assert result.output.count("session.timed_out") == 1


def test_cli_event_tail_dedup_terminal_different_sessions_not_collapsed(
    tmp_events_dir: Path,
) -> None:
    """--dedup-terminal keeps one event per unique session."""
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"session_id": "s1"},
        correlation_id="T-1",
    )
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"session_id": "s2"},
        correlation_id="T-2",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--dedup-terminal"])
    assert result.exit_code == 0, result.output
    assert result.output.count("session.timed_out") == 2


def test_cli_event_tail_dedup_terminal_non_terminal_events_not_deduped(
    tmp_events_dir: Path,
) -> None:
    """--dedup-terminal does not collapse non-terminal events."""
    for _ in range(3):
        events_record_event(OrchestratorEventType.DISPATCH_TICK, {"n": 1})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--dedup-terminal"])
    assert result.exit_code == 0, result.output
    assert result.output.count("dispatch.tick") == 3


def test_cli_event_tail_dedup_terminal_paused_status_field_in_key(
    tmp_events_dir: Path,
) -> None:
    """--dedup-terminal uses paused_status: different conditions kept separate."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"session_id": "s1", "paused_status": "dirty_worktree"},
        correlation_id="T-1",
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"session_id": "s1", "paused_status": "dirty_worktree"},
        correlation_id="T-1",
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"session_id": "s1", "paused_status": "silently_idle"},
        correlation_id="T-1",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--dedup-terminal"])
    assert result.exit_code == 0, result.output
    assert result.output.count("session.needs_attention") == 2


def test_cli_event_tail_follow_dedup_terminal(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--follow --dedup-terminal suppresses repeated terminal events in stream."""
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"session_id": "s1"},
        correlation_id="T-1",
    )
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"session_id": "s1"},
        correlation_id="T-1",
    )

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow", "--dedup-terminal"])
    assert result.exit_code == 130
    assert result.output.count("session.timed_out") == 1


def test_cli_event_tail_follow_client_filter(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--follow --client filters events by payload.client in the stream."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow", "--client", "alpha"])
    assert result.exit_code == 130
    assert "aaa" in result.output
    assert "beta" not in result.output  # "beta" has non-hex 't', safe in hex event IDs


def test_cli_event_tail_client_and_dedup_terminal_compose(
    tmp_events_dir: Path,
) -> None:
    """--client and --dedup-terminal compose: client filter applied before dedup."""
    # alpha has repeated terminal events; beta has one
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {"session_id": "s1", "client": "alpha"},
        )
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"session_id": "s2", "client": "beta"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--client", "alpha", "--dedup-terminal"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("session.timed_out") == 1
    assert "s2" not in result.output


def test_cli_event_tail_client_filter_with_json(tmp_events_dir: Path) -> None:
    """--client --json outputs valid JSON containing only matching client events."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", "alpha", "--json"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line]
    assert len(lines) == 1
    import json

    parsed = json.loads(lines[0])
    assert parsed["payload"]["client"] == "alpha"
    assert parsed["payload"]["session_id"] == "aaa"


def test_cli_event_tail_client_filter_with_type(tmp_events_dir: Path) -> None:
    """--client and --type compose: only events matching both filters are returned."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"client": "alpha", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "session_id": "bbb"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--client", "alpha", "--type", "session.spawned"]
    )
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "session.timed_out" not in result.output
    assert "beta" not in result.output  # "beta" has non-hex 't', safe in hex event IDs


def test_cli_event_tail_client_comma_only_no_events(tmp_events_dir: Path) -> None:
    """--client ',' (comma-only) normalizes to None and returns all events."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "session_id": "aaa"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--client", ","])
    assert result.exit_code == 0, result.output
    # Comma-only produces empty frozenset normalized to None — no filter applied
    assert "aaa" in result.output


# ---------------------------------------------------------------------------
# prune_events (issue #856)
# ---------------------------------------------------------------------------


def test_prune_events_keep_n_archives_by_default(tmp_events_dir: Path) -> None:
    """prune_events(keep=N) archives the oldest events beyond N by default."""
    for i in range(5):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    result = prune_events(keep=2)

    assert result.archived_count == 3
    assert result.deleted_count == 0
    assert result.kept_count == 2
    assert result.archive_path is not None
    archive_lines = [
        ln for ln in Path(result.archive_path).read_text().splitlines() if ln.strip()
    ]
    assert len(archive_lines) == 3


def test_prune_events_before_ts_archives_by_default(tmp_events_dir: Path) -> None:
    """prune_events(before=ts) archives events older than ts by default."""
    base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    with freeze_time(base):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    with freeze_time(base + timedelta(days=1)):
        ev_new = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    cutoff = base + timedelta(hours=12)
    result = prune_events(before=cutoff)

    assert result.archived_count == 1
    assert result.kept_count == 1
    assert result.archive_path is not None
    remaining = read_events()
    assert [e.id for e in remaining] == [ev_new.id]


def test_prune_events_delete_flag_discards_without_archiving(
    tmp_events_dir: Path,
) -> None:
    """archive=False discards pruned events without writing an archive file."""
    for i in range(4):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    result = prune_events(keep=1, archive=False)

    assert result.archived_count == 0
    assert result.deleted_count == 3
    assert result.kept_count == 1
    assert result.archive_path is None
    assert not list(tmp_events_dir.glob("inbox.*.jsonl"))


def test_prune_events_returns_counts(tmp_events_dir: Path) -> None:
    """prune_events returns a PruneResult with archived/deleted/archive_path/kept."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    result = prune_events(keep=1)

    assert isinstance(result, PruneResult)
    assert result.archived_count == 2
    assert result.deleted_count == 0
    assert result.kept_count == 1
    assert result.archive_path is not None


def test_prune_events_mutually_exclusive_raises(tmp_events_dir: Path) -> None:
    """Passing both before and keep raises CwError."""
    with pytest.raises(CwError):
        prune_events(before=datetime.now(UTC), keep=1)


def test_prune_events_neither_given_raises(tmp_events_dir: Path) -> None:
    """Passing neither before nor keep raises CwError."""
    with pytest.raises(CwError):
        prune_events()


def test_prune_events_negative_keep_raises(tmp_events_dir: Path) -> None:
    """A negative keep raises CwError directly from prune_events, not just the CLI."""
    with pytest.raises(CwError):
        prune_events(keep=-1)


def test_prune_events_holds_inbox_lock(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prune_events acquires the inbox lock exactly once (no release+reacquire)."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    from cw import events as events_module

    original_lock = events_module._inbox_lock
    call_count = 0

    @contextlib.contextmanager
    def counting_lock() -> Generator[None]:
        nonlocal call_count
        call_count += 1
        with original_lock():
            yield

    monkeypatch.setattr(events_module, "_inbox_lock", counting_lock)

    result = prune_events(keep=1)

    assert call_count == 1
    assert result.kept_count == 1


def test_prune_events_kept_events_remain_readable(tmp_events_dir: Path) -> None:
    """Events remaining after a prune are still readable via read_events."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    ev2 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})
    ev3 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 3})

    prune_events(keep=2)

    remaining = read_events()
    assert [e.id for e in remaining] == [ev2.id, ev3.id]


def test_prune_events_archive_file_appends_across_multiple_prunes(
    tmp_events_dir: Path,
) -> None:
    """Multiple prune_events calls on the same day append to the same archive file."""
    with freeze_time("2026-07-07 12:00:00", tz_offset=0):
        for i in range(3):
            events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
        result1 = prune_events(keep=1)
        assert result1.archived_count == 2
        archive_path = result1.archive_path
        assert archive_path is not None

        for i in range(3, 5):
            events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
        result2 = prune_events(keep=1)
        assert result2.archived_count == 2
        assert result2.archive_path == archive_path

    lines = [ln for ln in Path(archive_path).read_text().splitlines() if ln.strip()]
    assert len(lines) == 4


def test_prune_events_keep_zero_archives_all(tmp_events_dir: Path) -> None:
    """keep=0 archives every event in the inbox."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    result = prune_events(keep=0)

    assert result.archived_count == 3
    assert result.kept_count == 0
    remaining = read_events()
    assert remaining == []


def test_prune_events_empty_inbox_returns_zero_counts(tmp_events_dir: Path) -> None:
    """prune_events on an empty/absent inbox returns all-zero counts."""
    result = prune_events(keep=5)

    assert result.archived_count == 0
    assert result.deleted_count == 0
    assert result.kept_count == 0
    assert result.archive_path is None


def test_prune_events_keep_greater_than_total_keeps_all(
    tmp_events_dir: Path,
) -> None:
    """keep >= total event count prunes nothing."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    result = prune_events(keep=100)

    assert result.archived_count == 0
    assert result.deleted_count == 0
    assert result.kept_count == 3
    assert result.archive_path is None
    assert len(read_events()) == 3


# ---------------------------------------------------------------------------
# CLI: cw event prune (issue #856)
# ---------------------------------------------------------------------------


def test_cli_event_prune_keep_basic(tmp_events_dir: Path) -> None:
    """cw event prune --keep N truncates the inbox to the newest N events."""
    for i in range(4):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--keep", "1"])
    assert result.exit_code == 0, result.output
    remaining = read_events()
    assert len(remaining) == 1


def test_cli_event_prune_before_basic(tmp_events_dir: Path) -> None:
    """cw event prune --before <iso> removes events older than the cutoff."""
    base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    with freeze_time(base):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    with freeze_time(base + timedelta(days=1)):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 2})

    cutoff = (base + timedelta(hours=12)).isoformat()
    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--before", cutoff])
    assert result.exit_code == 0, result.output
    remaining = read_events()
    assert len(remaining) == 1


def test_cli_event_prune_both_flags_errors(tmp_events_dir: Path) -> None:
    """--before and --keep together is a CwError."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "prune", "--keep", "1", "--before", "2026-01-01T00:00:00"]
    )
    assert result.exit_code != 0


def test_cli_event_prune_neither_flag_errors(tmp_events_dir: Path) -> None:
    """Neither --before nor --keep is a CwError."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune"])
    assert result.exit_code != 0


def test_cli_event_prune_keep_json_output(tmp_events_dir: Path) -> None:
    """--json emits exactly the PruneResult schema."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--keep", "1", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert set(data.keys()) == {
        "archived_count",
        "deleted_count",
        "archive_path",
        "kept_count",
    }
    assert data["archived_count"] == 2
    assert data["kept_count"] == 1


def test_cli_event_prune_delete_flag_json_archive_path_null(
    tmp_events_dir: Path,
) -> None:
    """--delete --json reports archive_path: null and populates deleted_count."""
    for i in range(3):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "prune", "--keep", "1", "--delete", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert data["archive_path"] is None
    assert data["deleted_count"] == 2
    assert data["archived_count"] == 0


def test_cli_event_prune_invalid_keep_negative_errors(tmp_events_dir: Path) -> None:
    """A negative --keep value is rejected."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--keep", "-1"])
    assert result.exit_code != 0


def test_cli_event_prune_invalid_before_errors(tmp_events_dir: Path) -> None:
    """An unparseable --before value is rejected with a CwError."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--before", "not-a-timestamp"])
    assert result.exit_code != 0
    assert "Cannot parse --before value" in result.output


def test_cli_event_prune_before_naive_timestamp(tmp_events_dir: Path) -> None:
    """A tz-naive --before ISO timestamp is accepted (UTC assumed) with a warning."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    runner = CliRunner()
    result = runner.invoke(main, ["event", "prune", "--before", "2000-01-01T00:00:00"])
    assert result.exit_code == 0, result.output
    assert "no timezone; assuming UTC" in result.output
