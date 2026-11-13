"""Tests for cw.reconcile.roster_skip_streak (#3335)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.reconcile.roster_skip_streak import (
    ROSTER_SKIP_ATTENTION_THRESHOLD,
    ROSTER_SKIP_STREAK_STATUS,
    settle_roster_skip_streak,
)
from cw.reconcile.roster_snapshot import RosterUnavailableError
from tests._reconcile_helpers import _failing_record_event
from tests._shared.daemon_fakes import _CANONICAL_ATTENTION_KEYS

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests._shared.daemon_fakes import CapturedEvent

_MODULE = "cw.reconcile.roster_skip_streak"


def _stale() -> RosterUnavailableError:
    return RosterUnavailableError("stale age=31.0s", reason="stale")


def _missing() -> RosterUnavailableError:
    return RosterUnavailableError("missing", reason="missing")


def _failed() -> RosterUnavailableError:
    return RosterUnavailableError("failed: TimeoutExpired", reason="failed")


def _skip(ticks: int) -> None:
    for _ in range(ticks):
        settle_roster_skip_streak(_stale())


@pytest.fixture
def events(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(_MODULE, OrchestratorEventType.SESSION_NEEDS_ATTENTION)


def test_threshold_is_the_named_constant() -> None:
    assert ROSTER_SKIP_ATTENTION_THRESHOLD == 3


def test_no_event_below_the_threshold(events: list[CapturedEvent]) -> None:
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD - 1)
    assert events == []


def test_event_fires_once_at_the_threshold_with_the_nine_canonical_keys(
    events: list[CapturedEvent],
) -> None:
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD - 1)
    settle_roster_skip_streak(_missing())

    [(_etype, payload, correlation_id)] = events
    assert set(payload) == _CANONICAL_ATTENTION_KEYS
    assert payload["paused_status"] == ROSTER_SKIP_STREAK_STATUS
    assert ROSTER_SKIP_STREAK_STATUS == "roster_skip_streak"
    assert payload["client"] == ""
    assert payload["session_name"] == ""
    assert payload["ticket_id"] is None
    assert payload["claude_session_id"] is None
    assert payload["lane"] is None
    assert payload["crashed"] is False
    assert str(payload["session_id"]).startswith("roster-skip:3@")
    breadcrumbs = str(payload["breadcrumbs"])
    assert breadcrumbs.startswith("3 consecutive ticks skipped")
    assert "last cause: missing" in breadcrumbs
    assert correlation_id is None


def test_silent_while_the_streak_persists(events: list[CapturedEvent]) -> None:
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD + 3)
    assert len(events) == 1


def test_a_usable_roster_tick_resets_and_the_next_episode_fires_again(
    events: list[CapturedEvent],
) -> None:
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD)
    settle_roster_skip_streak(None)
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD - 1)
    assert len(events) == 1

    settle_roster_skip_streak(_stale())

    assert len(events) == 2


def test_failed_read_tick_is_not_a_skip_and_resets(
    events: list[CapturedEvent],
) -> None:
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD - 1)
    settle_roster_skip_streak(_failed())
    _skip(ROSTER_SKIP_ATTENTION_THRESHOLD - 1)
    assert events == []


@pytest.mark.parametrize("error", [OSError, CwError])
def test_failed_emit_unlatches_and_retries_next_tick(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception],
) -> None:
    if error is OSError:
        failures = _failing_record_event(
            monkeypatch,
            target=f"{_MODULE}.record_event",
            event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            fail_for=lambda _payload: True,
        )
    else:
        failures = []

        def _raise(*_a: object, **_k: object) -> None:
            failures.append(1)
            msg = "inbox unavailable"
            raise CwError(msg)

        monkeypatch.setattr(f"{_MODULE}.record_event", _raise)

    with caplog.at_level(logging.WARNING, logger=_MODULE):
        _skip(ROSTER_SKIP_ATTENTION_THRESHOLD)
    assert failures == [1]
    assert any("retrying next tick" in r.getMessage() for r in caplog.records)

    _skip(1)

    assert failures == [1, 1]
