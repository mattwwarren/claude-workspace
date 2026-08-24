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


def test_review_finding_voided_round_trips_and_is_cli_recordable(
    tmp_events_dir: Path,
) -> None:
    """#1814: the suppression audit event is a first-class bus member.

    ``cli.queues._VALID_EVENT_TYPES`` is derived from the enum, so there is no
    second list to update — this asserts that derivation rather than a copy.
    """
    from cw.cli.queues import _VALID_EVENT_TYPES

    ev = events_record_event(
        OrchestratorEventType.REVIEW_FINDING_VOIDED,
        {"file": "src/cw/foo.py", "severity": "MUST_FIX"},
        correlation_id="T-1814",
    )
    inbox = tmp_events_dir / "inbox.jsonl"
    data = json.loads(inbox.read_text().splitlines()[0])
    assert data["type"] == "review.finding_voided"
    assert data["correlation_id"] == "T-1814"
    restored = OrchestratorEvent.model_validate_json(ev.model_dump_json())
    assert restored.type is OrchestratorEventType.REVIEW_FINDING_VOIDED
    assert "review.finding_voided" in _VALID_EVENT_TYPES


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


def test_read_events_limit_returns_most_recent(tmp_events_dir: Path) -> None:
    """limit=N returns the N most recently recorded events, not the oldest N."""
    recorded = [
        events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {"i": i})
        for i in range(5)
    ]
    result = read_events(limit=3)
    assert [ev.id for ev in result] == [ev.id for ev in recorded[-3:]]


def test_read_events_limit_zero_returns_empty_list(tmp_events_dir: Path) -> None:
    """limit=0 returns [] rather than the list[-0:] full-list trap."""
    for _ in range(3):
        events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {})
    result = read_events(limit=0)
    assert result == []


def test_read_events_limit_composes_with_type_and_since_ts_filters(
    tmp_events_dir: Path,
) -> None:
    """limit bounds the already-filtered (type + since_ts) set, not the raw inbox."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    matching: list[OrchestratorEvent] = []
    with freeze_time(base):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 0})
    with freeze_time(base + timedelta(minutes=1)):
        matching.append(
            events_record_event(OrchestratorEventType.PR_CI_FAILED, {"n": 1})
        )
    with freeze_time(base + timedelta(minutes=2)):
        events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})
    with freeze_time(base + timedelta(minutes=3)):
        matching.append(
            events_record_event(OrchestratorEventType.PR_CI_FAILED, {"n": 3})
        )
    with freeze_time(base + timedelta(minutes=4)):
        matching.append(
            events_record_event(OrchestratorEventType.PR_CI_FAILED, {"n": 4})
        )

    cutoff = base + timedelta(seconds=30)
    result = read_events(
        since_ts=cutoff,
        event_types=[OrchestratorEventType.PR_CI_FAILED],
        limit=2,
    )
    assert [ev.id for ev in result] == [ev.id for ev in matching[-2:]]


def test_read_events_limit_exceeds_available_count_returns_all(
    tmp_events_dir: Path,
) -> None:
    """limit greater than the available count returns everything, in order."""
    recorded = [
        events_record_event(OrchestratorEventType.TICKET_ENQUEUED, {"i": i})
        for i in range(3)
    ]
    result = read_events(limit=100)
    assert [ev.id for ev in result] == [ev.id for ev in recorded]


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


def test_read_events_repeated_unknown_type_counts_events_not_types(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning's event count reflects skipped lines, not distinct type strings."""
    ev1 = events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})

    inbox = tmp_events_dir / "inbox.jsonl"
    _append_raw_event(inbox, type="future.event.type")
    _append_raw_event(inbox, type="future.event.type")
    _append_raw_event(inbox, type="future.event.type")

    ev2 = events_record_event(OrchestratorEventType.PR_MERGED, {"n": 2})

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        result = read_events()

    assert [e.id for e in result] == [ev1.id, ev2.id]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert "skipping 3 event(s)" in warning_records[0].message


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


def test_stat_inbox_absent_is_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent inbox is (0, 0) — a follower polls until it appears.

    Replaces the #954 _poll_inbox_growth guard test: that helper was superseded
    by _stat_inbox / _poll_follow_state in #1979, but the invariant it pinned
    still matters — an unreadable inbox must not crash a follower.
    """
    from cw.events import _stat_inbox

    class _MissingPath:
        def stat(self) -> object:
            raise FileNotFoundError

    monkeypatch.setattr("cw.events.inbox_path", _MissingPath)

    assert _stat_inbox() == (0, 0)


def test_stat_inbox_transient_failure_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed stat is None, NOT (0, 0) — it is not a replaced file.

    Collapsing the two would make a permissions blip or an NFS hiccup
    indistinguishable from a truncation: a false "inbox replaced" warning, a
    full re-resolve, and a zeroed identity that forces a second re-resolve on
    the next poll — that one silent, since a zero inode also defeats the
    first-poll warning guard.
    """
    from cw.events import _stat_inbox

    class _RaisingPath:
        def stat(self) -> object:
            raise PermissionError

    monkeypatch.setattr("cw.events.inbox_path", _RaisingPath)

    assert _stat_inbox() is None


def test_poll_follow_state_transient_stat_failure_changes_nothing(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient stat failure must not be mistaken for a replacement."""
    import cw.events as events_mod

    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    events, read = events_mod._resolve_follow_start(None)
    state = events_mod._FollowState(read.offset, read.size, read.ino, events[-1].id)

    monkeypatch.setattr(events_mod, "_stat_inbox", lambda: None)

    new_events, new_state = events_mod._poll_follow_state(state)

    assert new_events == []
    assert new_state == state  # identity preserved, not zeroed


def test_tail_events_follow_size_decrease_warns_and_continues(
    tmp_events_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive shrink branch: inbox size decrease warns and the loop continues.

    prune_events truncates and rewrites the inbox in production, so this is a
    live path. Truncation in place keeps the inode, so it is the `size <
    offset` half of the check (not the inode half) that must catch it: warn,
    re-resolve position, and keep polling without crashing.
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

    assert any(
        "inbox replaced or truncated" in record.message for record in caplog.records
    )


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


def test_cli_event_tail_dedup_terminal_legacy_producer_ignores_renotify_marker(
    tmp_events_dir: Path,
) -> None:
    """Widening _terminal_dedup_key to a 4-tuple doesn't change a legacy
    producer's dedup collapse (#1858) -- a payload with no renotify_marker
    key reads None uniformly via .get(), so all three occurrences still
    share one dedup key."""
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "lane:test-client/default@2026-08-01T12:00:00+00:00",
                "paused_status": "lane_circuit_paused",
            },
            correlation_id="T-1",
        )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--dedup-terminal"])
    assert result.exit_code == 0, result.output
    assert result.output.count("session.needs_attention") == 1


def test_cli_event_tail_follow_dedup_terminal_renotify_marker_survives(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--follow --dedup-terminal: two liveness renotify fires with the same
    session_id/paused_status but distinct renotify_marker values both
    survive as separate rows (#1858)."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": "s1",
            "paused_status": "session_unresponsive",
            "renotify_marker": "2026-08-01T12:00:00+00:00",
        },
        correlation_id="T-1",
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": "s1",
            "paused_status": "session_unresponsive",
            "renotify_marker": "2026-08-01T13:00:00+00:00",
        },
        correlation_id="T-1",
    )

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow", "--dedup-terminal"])
    assert result.exit_code == 130
    assert result.output.count("session.needs_attention") == 2


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
# --lane filter (issue #1331)
# ---------------------------------------------------------------------------


def test_cli_event_tail_lane_filter(tmp_events_dir: Path) -> None:
    """--lane filters events by payload.lane field."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "default", "session_id": "bbb"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--lane", "bugs"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "default" not in result.output


def test_cli_event_tail_lane_filter_multiple(tmp_events_dir: Path) -> None:
    """--lane can be repeated to include events from multiple lanes."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "default", "session_id": "bbb"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "infra", "session_id": "ccc"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--lane", "bugs", "--lane", "default"]
    )
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "bbb" in result.output
    assert "infra" not in result.output


def test_cli_event_tail_lane_filter_no_match(tmp_events_dir: Path) -> None:
    """--lane with no matching events outputs 'No events.'"""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--lane", "nonexistent"])
    assert result.exit_code == 0, result.output
    assert "No events." in result.output


def test_cli_event_tail_lane_filter_events_without_lane_field(
    tmp_events_dir: Path,
) -> None:
    """Events with no payload.lane field are excluded when --lane is used."""
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--lane", "bugs"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "pr.registered" not in result.output


def test_cli_event_tail_lane_filter_comma_separated(tmp_events_dir: Path) -> None:
    """--lane a,b is equivalent to --lane a --lane b."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "default", "session_id": "bbb"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "infra", "session_id": "ccc"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--lane", "bugs,default"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "bbb" in result.output
    assert "infra" not in result.output


def test_cli_event_tail_no_lane_filter_returns_all(tmp_events_dir: Path) -> None:
    """Without --lane, all events are returned unfiltered, including lane-less ones."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "pr.registered" in result.output


def test_cli_event_tail_lane_filter_with_type(tmp_events_dir: Path) -> None:
    """--lane and --type compose: only events matching both filters are returned."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_TIMED_OUT,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "default", "session_id": "bbb"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["event", "tail", "--lane", "bugs", "--type", "session.needs_attention"],
    )
    assert result.exit_code == 0, result.output
    assert "aaa" in result.output
    assert "session.timed_out" not in result.output
    assert "default" not in result.output


def test_cli_event_tail_follow_lane_filter(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--follow --lane filters events by payload.lane in the stream."""
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "bugs", "session_id": "aaa"},
    )
    events_record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {"lane": "default", "session_id": "bbb"},
    )

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow", "--lane", "bugs"])
    assert result.exit_code == 130
    assert "aaa" in result.output
    assert "default" not in result.output


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


# ---------------------------------------------------------------------------
# --limit/-n (issue #1694)
# ---------------------------------------------------------------------------


def test_cli_event_tail_limit_flag_returns_at_most_n_most_recent(
    tmp_events_dir: Path,
) -> None:
    """--limit N returns only the N most recently recorded matching events."""
    recorded = [
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
        for i in range(5)
    ]

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--limit", "2"])
    assert result.exit_code == 0, result.output
    for ev in recorded[-2:]:
        assert ev.id in result.output
    for ev in recorded[:3]:
        assert ev.id not in result.output


def test_cli_event_tail_limit_short_flag(tmp_events_dir: Path) -> None:
    """-n is the short form of --limit."""
    recorded = [
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
        for i in range(5)
    ]

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "-n", "2"])
    assert result.exit_code == 0, result.output
    for ev in recorded[-2:]:
        assert ev.id in result.output
    for ev in recorded[:3]:
        assert ev.id not in result.output


def test_cli_event_tail_limit_composes_with_since_type_client_lane(
    tmp_events_dir: Path,
) -> None:
    """--limit composes with --type/--client/--lane: filter-then-limit ordering."""
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "alpha", "lane": "l1", "session_id": "s0"},
    )
    matching = [
        events_record_event(
            OrchestratorEventType.SESSION_SPAWNED,
            {"client": "alpha", "lane": "l1", "session_id": f"s{i}"},
        )
        for i in range(1, 4)
    ]
    events_record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"client": "beta", "lane": "l1", "session_id": "sX"},
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "event",
            "tail",
            "--type",
            "session.spawned",
            "--client",
            "alpha",
            "--lane",
            "l1",
            "--limit",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    for ev in matching[-2:]:
        assert ev.id in result.output
    assert matching[0].id not in result.output


def test_cli_event_tail_limit_rejects_non_positive(tmp_events_dir: Path) -> None:
    """--limit 0 and --limit -1 both exit non-zero via CwError."""
    runner = CliRunner()
    result_zero = runner.invoke(main, ["event", "tail", "--limit", "0"])
    assert result_zero.exit_code != 0
    assert "--limit must be a positive integer" in result_zero.output

    result_negative = runner.invoke(main, ["event", "tail", "--limit", "-1"])
    assert result_negative.exit_code != 0
    assert "--limit must be a positive integer" in result_negative.output


def test_cli_event_tail_limit_incompatible_with_follow(tmp_events_dir: Path) -> None:
    """--limit with --follow exits non-zero via CwError mentioning unbounded follow."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--limit", "5", "--follow"])
    assert result.exit_code != 0
    assert "follow" in result.output.lower()
    assert "unbounded" in result.output.lower()


def test_cli_event_tail_json_limit_compose(tmp_events_dir: Path) -> None:
    """--json --limit returns exactly the N most recent events' ids, in order."""
    recorded = [
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
        for i in range(5)
    ]

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--json", "--limit", "2"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    ids = [json.loads(ln)["id"] for ln in lines]
    assert ids == [ev.id for ev in recorded[-2:]]


# ---------------------------------------------------------------------------
# Compact default (non-json) output format (issue #1694)
# ---------------------------------------------------------------------------


def _dispatch_tick_payload_with_marker(marker: str) -> dict[str, object]:
    """A dispatch.tick payload shaped like the real one (src/cw/dispatch/lanes.py).

    The *marker* is planted only inside the nested ``lanes``/``lane_occupants``
    structures (as a lane name), never in a scalar field, so a test can assert
    it is absent from compact (non-json) output without also matching a scalar.
    """
    return {
        "client": "acme",
        "claimed": 2,
        "pending": 1,
        "running": 3,
        "cap": 5,
        "skip_reason": "none",
        "lanes": {
            marker: {
                "claimed": 1,
                "running": 1,
                "blocked": 0,
                "signoff": 0,
                "pending": 0,
            },
        },
        "lane_occupants": {
            marker: [
                {"session_id": "s1", "ticket_id": "T-1"},
            ],
        },
        "occupied": 1,
        "host_running": 4,
        "host_budget": 10,
    }


def test_cli_event_tail_default_format_omits_nested_lane_maps(
    tmp_events_dir: Path,
) -> None:
    """Default (non-json) output drops dispatch.tick's nested lanes/lane_occupants."""
    marker = "lane-zebra-unique"
    events_record_event(
        OrchestratorEventType.DISPATCH_TICK,
        _dispatch_tick_payload_with_marker(marker),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert marker not in result.output
    assert "claimed=" in result.output
    assert "pending=" in result.output
    assert "running=" in result.output
    assert "skip_reason=" in result.output


def test_cli_event_tail_default_format_keeps_id_and_timestamp(
    tmp_events_dir: Path,
) -> None:
    """Regression guard: id and an ISO-ish timestamp still appear in default output."""
    ev = events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert ev.id in result.output
    assert ev.created_at.strftime("%Y-%m-%dT%H:%M:%S") in result.output


def test_cli_event_tail_default_format_keeps_short_list_fields(
    tmp_events_dir: Path,
) -> None:
    """A pr.ci_failed-shaped event's short scalar-list field survives compaction."""
    events_record_event(
        OrchestratorEventType.PR_CI_FAILED,
        {
            "pr": 42,
            "repo": "owner/repo",
            "failing_checks": ["lint", "type-check"],
        },
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail"])
    assert result.exit_code == 0, result.output
    assert "lint" in result.output
    assert "type-check" in result.output


def test_cli_event_tail_json_still_full_fidelity(tmp_events_dir: Path) -> None:
    """--json retains the full dispatch.tick payload verbatim, nested fields kept."""
    marker = "lane-zebra-unique"
    payload = _dispatch_tick_payload_with_marker(marker)
    events_record_event(OrchestratorEventType.DISPATCH_TICK, payload)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--json"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    data = json.loads(lines[0])
    assert data["payload"]["lanes"] == payload["lanes"]
    assert data["payload"]["lane_occupants"] == payload["lane_occupants"]


def test_cli_event_tail_follow_streams_compact_format_line_buffered(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--follow default output is also compact and still flushes per-event."""
    marker = "lane-zebra-unique"
    events_record_event(
        OrchestratorEventType.DISPATCH_TICK,
        _dispatch_tick_payload_with_marker(marker),
    )

    def raise_immediately(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", raise_immediately)

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--follow"])
    assert result.exit_code == 130
    assert marker not in result.output
    assert "claimed=" in result.output
    assert "dispatch.tick" in result.output


# ---------------------------------------------------------------------------
# --collapse-repeats (issue #1754)
# ---------------------------------------------------------------------------


def test_cli_event_tail_collapse_repeats_consecutive_run_collapses_to_one_line(
    tmp_events_dir: Path,
) -> None:
    """3 consecutive same-type/same-payload events collapse to one `x3` line."""
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1}
        )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert result.output.count("dispatch.tick") == 1
    assert "x3" in result.output


def test_cli_event_tail_collapse_repeats_run_interrupted_reopens(
    tmp_events_dir: Path,
) -> None:
    """A run broken by an unrelated event re-opens instead of merging across it."""
    for _ in range(2):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1}
        )
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})
    for _ in range(2):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1}
        )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert result.output.count("dispatch.tick x2") == 2
    assert "x4" not in result.output
    assert "pr.registered" in result.output


def test_cli_event_tail_collapse_repeats_differing_payload_does_not_collapse(
    tmp_events_dir: Path,
) -> None:
    """Consecutive same-type events with differing scalar payloads do not merge."""
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1})
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 2})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert "x2" not in result.output
    assert result.output.count("dispatch.tick") == 2


def test_cli_event_tail_collapse_repeats_ignores_non_salient_nested_fields(
    tmp_events_dir: Path,
) -> None:
    """Identical scalar fields but differing nested dict fields still collapse."""
    events_record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {"client": "acme", "n": 1, "lanes": {"l1": {"claimed": 1}}},
    )
    events_record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {"client": "acme", "n": 1, "lanes": {"l2": {"claimed": 2}}},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert result.output.count("dispatch.tick") == 1
    assert "x2" in result.output


def test_cli_event_tail_collapse_repeats_singleton_run_prints_normally(
    tmp_events_dir: Path,
) -> None:
    """A run of length 1 prints via the normal full per-event line, not `x1`."""
    ev = events_record_event(OrchestratorEventType.PR_REGISTERED, {"pr": 1})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert ev.id in result.output
    assert ev.created_at.strftime("%Y-%m-%dT%H:%M:%S") in result.output
    assert "x1" not in result.output


def test_cli_event_tail_collapse_repeats_line_format_matches_ticket_example(
    tmp_events_dir: Path,
) -> None:
    """Collapsed line matches `TYPE xN over Mm  k=v ...`, span computed in minutes."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        with freeze_time(base + timedelta(minutes=i)):
            events_record_event(
                OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1}
            )

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert "dispatch.tick x3 over 2m  client=acme n=1" in result.output


def test_cli_event_tail_collapse_repeats_json_unaffected(tmp_events_dir: Path) -> None:
    """--json --collapse-repeats is a no-op: one JSON line per original event."""
    recorded = [
        events_record_event(OrchestratorEventType.DISPATCH_TICK, {"client": "acme"})
        for _ in range(3)
    ]

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--json", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    ids = [json.loads(ln)["id"] for ln in lines]
    assert ids == [ev.id for ev in recorded]


def test_cli_event_tail_collapse_repeats_rejects_with_follow(
    tmp_events_dir: Path,
) -> None:
    """--collapse-repeats with --follow exits non-zero via CwError."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats", "--follow"])
    assert result.exit_code != 0
    assert "follow" in result.output.lower()
    assert "buffering" in result.output.lower() or "flush" in result.output.lower()


def test_cli_event_tail_collapse_repeats_composes_with_dedup_terminal(
    tmp_events_dir: Path,
) -> None:
    """--dedup-terminal runs first; --collapse-repeats then summarizes the rest."""
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.SESSION_TIMED_OUT, {"session_id": "s1"}
        )
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1}
        )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--dedup-terminal", "--collapse-repeats"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("session.timed_out") == 1
    assert result.output.count("dispatch.tick") == 1
    assert "x3" in result.output


def test_cli_event_tail_collapse_repeats_composes_with_limit(
    tmp_events_dir: Path,
) -> None:
    """--limit applies server-side before --collapse-repeats groups the window."""
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 1})
    for _ in range(4):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "acme", "n": 2}
        )

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--limit", "4", "--collapse-repeats"]
    )
    assert result.exit_code == 0, result.output
    assert "x4" in result.output
    assert "n=1" not in result.output


def test_cli_event_tail_collapse_repeats_composes_with_client_filter(
    tmp_events_dir: Path,
) -> None:
    """--client filters before --collapse-repeats groups the surviving events."""
    for _ in range(3):
        events_record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": "alpha", "n": 1}
        )
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {"client": "beta", "n": 1})

    runner = CliRunner()
    result = runner.invoke(
        main, ["event", "tail", "--client", "alpha", "--collapse-repeats"]
    )
    assert result.exit_code == 0, result.output
    assert "x3" in result.output
    assert "beta" not in result.output


def test_cli_event_tail_collapse_repeats_empty_events_no_crash(
    tmp_events_dir: Path,
) -> None:
    """No matching events + --collapse-repeats still prints the empty-path message."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert "No events." in result.output


def test_cli_event_tail_collapse_repeats_mixed_types_no_cross_type_grouping(
    tmp_events_dir: Path,
) -> None:
    """Type is part of the grouping key: never merges across types."""
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {})
    events_record_event(OrchestratorEventType.PR_REGISTERED, {})
    events_record_event(OrchestratorEventType.DISPATCH_TICK, {})

    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--collapse-repeats"])
    assert result.exit_code == 0, result.output
    assert "x2" not in result.output
    assert result.output.count("dispatch.tick") == 2
    assert result.output.count("pr.registered") == 1


def test_cli_event_tail_collapse_repeats_help_text_documents_flag(
    tmp_events_dir: Path,
) -> None:
    """`cw event tail --help` mentions --collapse-repeats."""
    runner = CliRunner()
    result = runner.invoke(main, ["event", "tail", "--help"])
    assert result.exit_code == 0, result.output
    assert "--collapse-repeats" in result.output


# ---------------------------------------------------------------------------
# Byte-offset follow reads (#1979)
# ---------------------------------------------------------------------------


def test_read_lines_after_offset_returns_only_appended(tmp_events_dir: Path) -> None:
    """Only bytes past the offset are read; the offset advances past them."""
    from cw.events import _read_lines_after_offset

    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text('{"a": 1}\n{"b": 2}\n')
    first_size = inbox.stat().st_size

    read = _read_lines_after_offset(0)
    assert read.lines == ['{"a": 1}', '{"b": 2}']
    assert read.offset == first_size

    inbox.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    read = _read_lines_after_offset(first_size)
    assert read.lines == ['{"c": 3}']
    assert read.offset == inbox.stat().st_size


def test_read_lines_after_offset_leaves_torn_trailing_line(
    tmp_events_dir: Path,
) -> None:
    """A partial final line is not consumed, and is picked up once completed.

    The old whole-file read relied on _parse_lines' malformed-trailing-line
    tolerance to survive a torn append, which *discarded* the event once the
    writer finished it. Trimming at the last newline instead means the torn
    line is simply not consumed yet.
    """
    from cw.events import _read_lines_after_offset

    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text('{"a": 1}\n{"b": 2')  # torn: no trailing newline

    read = _read_lines_after_offset(0)
    assert read.lines == ['{"a": 1}']
    assert read.offset == len('{"a": 1}\n')

    # Writer completes the line; the next poll picks it up whole.
    inbox.write_text('{"a": 1}\n{"b": 2}\n')
    read = _read_lines_after_offset(read.offset)
    assert read.lines == ['{"b": 2}']
    assert read.offset == inbox.stat().st_size


def test_read_lines_after_offset_no_newline_consumes_nothing(
    tmp_events_dir: Path,
) -> None:
    """A chunk with no newline at all advances nothing."""
    from cw.events import _read_lines_after_offset

    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text('{"partial"')

    read = _read_lines_after_offset(0)
    assert read.lines == []
    assert read.offset == 0


def test_read_lines_after_offset_absent_inbox(tmp_events_dir: Path) -> None:
    """An absent inbox returns no lines and leaves the offset untouched."""
    from cw.events import _read_lines_after_offset

    assert not (tmp_events_dir / "inbox.jsonl").exists()

    read = _read_lines_after_offset(0)

    assert read.lines == []
    assert read.offset == 0
    assert (read.size, read.ino) == (0, 0)


def _three_events() -> list[OrchestratorEvent]:
    """Three distinct events; _apply_cursor is pure, so no inbox is needed."""
    return [
        OrchestratorEvent(type=OrchestratorEventType.PR_REGISTERED) for _ in range(3)
    ]


def test_apply_cursor_slices_strictly_after_match() -> None:
    """The cursor event itself is excluded; everything after it is returned."""
    from cw.events import _apply_cursor

    events = _three_events()

    after, found = _apply_cursor(events, events[0].id)

    assert found is True
    assert [e.id for e in after] == [events[1].id, events[2].id]


def test_apply_cursor_at_last_event_returns_empty() -> None:
    """A caught-up cursor yields nothing, and is still 'found'."""
    from cw.events import _apply_cursor

    events = _three_events()

    after, found = _apply_cursor(events, events[-1].id)

    assert found is True
    assert after == []


def test_apply_cursor_missing_id_returns_all_and_false() -> None:
    """A pruned-away cursor returns everything, flagged so the caller can warn."""
    from cw.events import _apply_cursor

    events = _three_events()

    after, found = _apply_cursor(events, "nonexistent-id")

    assert found is False
    assert after == events


def test_apply_cursor_none_returns_all_and_true() -> None:
    """No cursor means start from the beginning — 'absent' is not 'missing'."""
    from cw.events import _apply_cursor

    events = _three_events()

    after, found = _apply_cursor(events, None)

    assert found is True
    assert after == events


def test_read_lines_after_offset_survives_multibyte_utf8(
    tmp_events_dir: Path,
) -> None:
    """Cutting at b"\n" never splits a multi-byte character.

    _read_lines_after_offset documents this as load-bearing (0x0A never appears
    as a UTF-8 continuation byte), so it needs a test with real non-ASCII
    payloads — event payloads carry client and ticket names.
    """
    from cw.events import _read_lines_after_offset

    inbox = tmp_events_dir / "inbox.jsonl"
    first = '{"client": "café-münchen", "note": "🔥 emoji"}'
    second = '{"client": "日本語", "note": "naïve"}'
    inbox.write_text(first + "\n")
    boundary = inbox.stat().st_size

    read = _read_lines_after_offset(0)
    assert read.lines == [first]
    assert read.offset == boundary

    inbox.write_text(first + "\n" + second + "\n")
    read = _read_lines_after_offset(boundary)
    assert read.lines == [second]
    assert read.offset == inbox.stat().st_size


def test_tail_events_follow_torn_append_settles_to_idle(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently torn trailing line must not pin the follower at 100% poll.

    The offset only advances past whole lines, so a torn append leaves bytes on
    disk that are correctly never consumed. Change detection therefore has to
    compare against the size last *seen*, not the offset last *consumed* — if
    it compared against the offset, size != offset would hold forever and every
    poll would re-read the torn tail, which is exactly the busy loop #1979
    exists to remove.
    """
    import cw.events as events_mod

    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": 1})
    inbox = tmp_events_dir / "inbox.jsonl"
    with inbox.open("a") as handle:  # writer dies mid-line and never returns
        handle.write('{"partial": ')

    reads: list[int] = []
    real_reader = events_mod._read_lines_after_offset

    def counting_reader(offset: int) -> object:
        reads.append(offset)
        return real_reader(offset)

    monkeypatch.setattr(events_mod, "_read_lines_after_offset", counting_reader)

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 5:
            raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    with pytest.raises(KeyboardInterrupt):
        list(tail_events_follow(since_cursor=None, since_ts=None, event_types=None))

    # Exactly one read: the startup read, which already observed the torn tail
    # and captured seen_size past it. Every later poll must be a pure stat()
    # no-op. Anything more means the torn tail is being re-read.
    assert reads == [0], f"torn tail re-read after startup: {reads}"


def test_tail_events_follow_reresolves_when_inbox_replaced(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inbox replaced and regrown past the old offset must not be seek-read.

    prune_events rewrites via atomic_write_text, so a rewrite is a new inode.
    Size arithmetic alone cannot catch a replace-then-regrow that lands above
    the old offset; seeking there would land mid-line and raise JSONDecodeError
    out of a daemon loop. The inode check routes it to a re-resolve instead.
    """
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": "pre"})

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Replace the inbox with a LARGER unrelated file: new inode, and a
            # size well past the follower's offset, so only the inode check can
            # notice. Long payloads guarantee the old offset lands mid-line.
            from cw.atomic import atomic_write_text

            replacement = "".join(
                OrchestratorEvent(
                    type=OrchestratorEventType.PR_REGISTERED,
                    payload={"n": f"replaced-{i}", "pad": "x" * 200},
                ).model_dump_json()
                + "\n"
                for i in range(5)
            )
            atomic_write_text(tmp_events_dir / "inbox.jsonl", replacement)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    yielded: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        yielded.extend(
            tail_events_follow(since_cursor=None, since_ts=None, event_types=None)
        )

    # No JSONDecodeError escaped, and the replacement's contents were delivered
    # whole rather than from a mid-line seek.
    payloads = [e.payload.get("n") for e in yielded]
    assert payloads[0] == "pre"
    assert [p for p in payloads if str(p).startswith("replaced-")] == [
        f"replaced-{i}" for i in range(5)
    ]


def test_wait_for_event_does_not_reread_history(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1979 perf contract for wait_for_event, the twin of the tail test.

    wait_for_event shares the offset path but had no bytes-read guard of its
    own, so a regression that reintroduced whole-inbox reads here would have
    gone undetected.
    """
    import cw.events as events_mod

    for i in range(50):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
    history_size = (tmp_events_dir / "inbox.jsonl").stat().st_size

    bytes_read: list[int] = []
    real_reader = events_mod._read_lines_after_offset

    def counting_reader(offset: int) -> object:
        read = real_reader(offset)
        bytes_read.append(read.offset - offset)
        return read

    monkeypatch.setattr(events_mod, "_read_lines_after_offset", counting_reader)

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            events_record_event(
                OrchestratorEventType.SESSION_COMPLETED, {"session_id": "s1"}
            )
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    matched: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        matched.extend(
            wait_for_event(
                event_types=[OrchestratorEventType.SESSION_COMPLETED],
                session_id="s1",
                follow=True,
            )
        )

    assert [e.payload.get("session_id") for e in matched] == ["s1"]
    assert bytes_read[0] == history_size
    assert 0 < bytes_read[1] < history_size


def test_wait_for_event_after_prune_delivers_unseen_survivors(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait_for_event must not lose a matching event to a concurrent prune.

    cw event wait is the scripting primitive for "tell me when X happens"; a
    dropped session.completed makes a caller hang to timeout and conclude the
    session never finished.
    """
    for i in range(5):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # The awaited event lands, then a prune drops the waiter's position
            # while KEEPING that event.
            events_record_event(
                OrchestratorEventType.SESSION_COMPLETED, {"session_id": "s1"}
            )
            prune_events(keep=1)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    matched: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        matched.extend(
            wait_for_event(
                event_types=[OrchestratorEventType.SESSION_COMPLETED],
                session_id="s1",
                follow=True,
            )
        )

    assert [e.payload.get("session_id") for e in matched] == ["s1"]


def test_tail_events_follow_does_not_reread_history(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1979 perf contract: polls read new bytes only, not the whole inbox.

    This is the regression guard for the defect itself. Before the byte-offset
    read, every poll re-read and re-parsed the entire inbox to answer "what is
    new?", so the bytes read per poll grew without bound with history.
    """
    import cw.events as events_mod

    for i in range(50):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})
    inbox = tmp_events_dir / "inbox.jsonl"
    history_size = inbox.stat().st_size

    bytes_read: list[int] = []
    real_reader = events_mod._read_lines_after_offset

    def counting_reader(offset: int) -> object:
        read = real_reader(offset)
        bytes_read.append(read.offset - offset)
        return read

    monkeypatch.setattr(events_mod, "_read_lines_after_offset", counting_reader)

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": "new"})
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    yielded: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        yielded.extend(
            tail_events_follow(since_cursor=None, since_ts=None, event_types=None)
        )

    # One startup read covering history, then a poll reading ONLY the new event.
    assert bytes_read[0] == history_size
    assert 0 < bytes_read[1] < history_size
    assert sum(bytes_read[1:]) < history_size
    assert [e.payload.get("n") for e in yielded][-1] == "new"


def test_tail_events_follow_streams_appends_without_replaying(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each appended event is yielded exactly once across successive polls.

    Behavioural-parity coverage, NOT a #1979 regression guard: verified to pass
    against the pre-#1979 whole-inbox implementation too. The plain-append case
    was never broken, so this pins that the offset rewrite did not break it —
    the guards that actually discriminate are
    test_tail_events_follow_does_not_reread_history (cost) and
    test_tail_events_follow_after_prune_delivers_unseen_survivors (delivery).
    """
    events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": "pre"})

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            events_record_event(
                OrchestratorEventType.PR_REGISTERED, {"n": f"new-{call_count}"}
            )
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    yielded: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        yielded.extend(
            tail_events_follow(since_cursor=None, since_ts=None, event_types=None)
        )

    assert [e.payload.get("n") for e in yielded] == ["pre", "new-1", "new-2", "new-3"]


def test_tail_events_follow_after_prune_delivers_unseen_survivors(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prune must not cost a follower events it had not yet seen (#1979).

    prune_events keeps a *suffix*. So if a follower's position is pruned away,
    every event at or before it went too, which makes the surviving events
    exactly the ones that follower has never seen. Skipping to the new EOF
    would silently drop them -- and because prune only ever removes a prefix,
    replaying the survivors cannot duplicate anything already delivered.
    """
    for i in range(5):
        events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": i})

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Append past the follower's position AND prune below it in the
            # same gap, so events it already streamed are gone from the inbox
            # while newer ones it has never seen remain. This is the only
            # shape that reaches the re-resolve path: prune keeps the newest
            # events, so a caught-up follower's position survives an ordinary
            # prune untouched.
            for j in range(5, 10):
                events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": j})
            prune_events(keep=2)  # keeps n=8, n=9; drops the follower's position
            return
        if call_count == 2:
            events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": "post"})
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    yielded: list[OrchestratorEvent] = []
    with pytest.raises(KeyboardInterrupt):
        yielded.extend(
            tail_events_follow(since_cursor=None, since_ts=None, event_types=None)
        )

    payloads = [e.payload.get("n") for e in yielded]
    # n=8 and n=9 are events prune deliberately KEPT and the follower had never
    # seen. They must arrive. n=5..7 are absent because prune genuinely
    # discarded them. Nothing is delivered twice.
    assert payloads == [0, 1, 2, 3, 4, 8, 9, "post"]
    assert len(payloads) == len(set(map(str, payloads)))


def test_tail_events_follow_inbox_created_midrun_logs_no_replacement(
    tmp_events_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An inbox appearing for the first time is not a "replacement".

    Every daemon started before the first record_event takes this path in
    normal operation: _stat_inbox reports (0, 0) for absent, so the first real
    inode is an inode *change*. The `if state.ino:` guard is what keeps that
    from logging a false "inbox replaced or truncated" on ordinary startup.
    Mutation-tested: without the guard this test fails.
    """
    assert not (tmp_events_dir / "inbox.jsonl").exists()

    call_count = 0

    def sleep_side_effect(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            events_record_event(OrchestratorEventType.PR_REGISTERED, {"n": "first"})
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep_side_effect)

    yielded: list[OrchestratorEvent] = []
    with (
        caplog.at_level(logging.WARNING, logger="cw.events"),
        pytest.raises(KeyboardInterrupt),
    ):
        yielded.extend(
            tail_events_follow(since_cursor=None, since_ts=None, event_types=None)
        )

    assert [e.payload.get("n") for e in yielded] == ["first"]
    assert not [
        r for r in caplog.records if "inbox replaced or truncated" in r.message
    ], "first sighting of the inbox logged as a replacement"


def test_resolve_follow_start_empty_inbox_logs_no_cursor_miss(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An inbox that parsed to nothing has lost no cursor, so it must not warn.

    "cursor not found; replaying from start" is an incident-shaped message. An
    empty inbox is not an incident, and nothing is being replayed — the
    returned list is empty either way. Mutation-tested: reverting the guard to
    a bare `if not found:` fails this test.
    """
    from cw.events import _resolve_follow_start

    (tmp_events_dir / "inbox.jsonl").write_text("")

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        events, read = _resolve_follow_start("some-cursor-that-does-not-exist")

    assert events == []
    assert read.offset == 0
    assert not [r for r in caplog.records if "cursor" in r.message], (
        "empty inbox warned about a missing cursor"
    )


def test_read_events_empty_inbox_logs_no_cursor_miss(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """read_events applies the same guard as its _apply_cursor sibling.

    Both callers of _apply_cursor must agree on the empty-parse edge case;
    read_events backs dispatch and consumer polling, so a spurious "replaying
    from start" there is a false alarm in a daemon's logs.
    """
    inbox = tmp_events_dir / "inbox.jsonl"
    inbox.write_text('{"torn": ')  # tolerated torn line -> parses to nothing

    with caplog.at_level(logging.WARNING, logger="cw.events"):
        events = read_events(since_cursor="some-cursor-that-does-not-exist")

    assert events == []
    assert not [r for r in caplog.records if "not found in inbox" in r.message], (
        "inbox that parsed to nothing warned about a missing cursor"
    )
