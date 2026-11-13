"""Tests for the advisory ``session-outcomes`` doctor check (#2840).

NEW file, 1:1 with ``src/cw/doctor/session_outcomes.py``. Fixture inventory:
autouse ``tmp_config_dir`` + ``_isolate_home``, ``freeze_time``, ``monkeypatch``
(patches ``cw.session_outcomes.read_events``), ``cw.events.record_event`` for
seeding, ``CwState`` for the non-``None`` state argument, and
``tests._shared.session_seeding._make_daemon_session`` for live/terminal
sessions in that state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from cw.doctor._shared import CheckResult
from cw.doctor.session_outcomes import _check_session_outcomes
from cw.events import record_event
from cw.models import CwState, OrchestratorEventType, SessionStatus
from tests._shared.session_seeding import _make_daemon_session

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _seed(etype: OrchestratorEventType, payload: dict[str, Any]) -> None:
    record_event(etype, payload)


def test_state_none_is_skipped_without_reading_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock(name="read_events")
    monkeypatch.setattr("cw.session_outcomes.read_events", spy)
    result = _check_session_outcomes(None)
    assert result == CheckResult(
        "session-outcomes",
        ok=True,
        detail="skipped: state did not load",
    )
    spy.assert_not_called()


def test_zero_spawns_reports_no_sessions() -> None:
    with freeze_time(NOW):
        result = _check_session_outcomes(CwState())
    assert result.ok is True
    assert result.warn is False
    assert "no worker sessions spawned" in result.detail


def test_seeded_inbox_reports_rate_and_command() -> None:
    with freeze_time(NOW):
        _seed(
            OrchestratorEventType.SESSION_SPAWNED,
            {"session_id": "s1", "ticket_id": "T-1", "client": "acme"},
        )
        _seed(
            OrchestratorEventType.SESSION_COMPLETED,
            {"session_id": "s1", "crashed": True},
        )
        result = _check_session_outcomes(CwState())
    assert result.name == "session-outcomes"
    assert result.ok is True
    assert result.warn is False
    assert "1 of 1 spawned without a result" in result.detail
    assert "largest cause crash" in result.detail
    assert "cw event session-outcomes --days 7" in result.detail


def test_high_rate_still_not_warned() -> None:
    """Decision D1: the rate never warns, even at the ticket's 37%-plus."""
    with freeze_time(NOW):
        for sid in ("s1", "s2", "s3"):
            _seed(
                OrchestratorEventType.SESSION_SPAWNED,
                {"session_id": sid, "ticket_id": f"T-{sid}", "client": "acme"},
            )
        _seed(
            OrchestratorEventType.SESSION_COMPLETED,
            {"session_id": "s1", "crashed": True},
        )
        result = _check_session_outcomes(CwState())
    assert result.ok is True
    assert result.warn is False


def test_spawn_older_than_seven_days_excluded() -> None:
    with freeze_time(NOW - timedelta(days=8)):
        _seed(
            OrchestratorEventType.SESSION_SPAWNED,
            {"session_id": "old", "ticket_id": "T-old", "client": "acme"},
        )
    with freeze_time(NOW):
        result = _check_session_outcomes(CwState())
    assert "no worker sessions spawned" in result.detail


@pytest.mark.parametrize("error", [OSError("gone"), ValueError("corrupt line")])
def test_unreadable_inbox_warns_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("cw.session_outcomes.read_events", _boom)
    result = _check_session_outcomes(CwState())
    assert result.ok is True
    assert result.warn is True
    assert type(error).__name__ in result.detail
    assert str(error) in result.detail
    assert "cw event session-outcomes" in result.detail


def test_unreadable_inbox_detail_names_the_failing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3123: an OSError's text carries the path, so the warning says which file."""
    error = OSError(2, "No such file or directory", "/x/inbox.jsonl")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("cw.session_outcomes.read_events", _boom)
    result = _check_session_outcomes(CwState())
    assert result.warn is True
    assert "/x/inbox.jsonl" in result.detail


def _seed_live_candidate() -> None:
    _seed(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session_id": "s1", "ticket_id": "T-1", "client": "acme"},
    )


def test_live_session_is_in_flight_and_has_no_largest_cause() -> None:
    """#3128: a non-terminal session in state is in flight, not a failure."""
    state = CwState(
        sessions=[_make_daemon_session(id="s1", status=SessionStatus.ACTIVE)]
    )
    with freeze_time(NOW):
        _seed_live_candidate()
        result = _check_session_outcomes(state)
    assert result.ok is True
    assert result.warn is False
    assert "1 of 1 spawned without a result" in result.detail
    assert "largest cause none" in result.detail
    assert "cw event session-outcomes --days 7" in result.detail


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(CwState(), id="empty-state"),
        pytest.param(
            CwState(
                sessions=[_make_daemon_session(id="s1", status=SessionStatus.COMPLETED)]
            ),
            id="completed-session",
        ),
    ],
)
def test_terminal_or_absent_session_is_not_in_flight(state: CwState) -> None:
    """#3128 wiring control: only a live state session makes the spawn in flight."""
    with freeze_time(NOW):
        _seed_live_candidate()
        result = _check_session_outcomes(state)
    assert result.warn is False
    assert "largest cause unclassified" in result.detail
