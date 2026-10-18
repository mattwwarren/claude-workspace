"""Lane circuit breaker: recorded cause, worktree counter, auto-resume (#2696).

1:1 test module of ``cw.dispatch.lane_circuit``. The breaker's original #875 /
#1630 behaviour stays covered in ``tests/test_dispatch.py``; this file covers
what #2696 added: the persisted pause cause and recent errors, the separate
worktree-error counter (R3), auto-resume with a doubling cool-down, the
half-open no-charge probe (R16) and quiet paging.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    load_effective_clients,
)
from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch import dispatch_tick
from cw.dispatch.lane_circuit import (
    _auto_resume_cooldown_minutes,
    _check_lane_circuit_paused,
    _handle_paused_lane,
    _maybe_auto_resume_lane,
    _maybe_notify_lane_starved,
    _record_lane_spawn_error,
    _reset_lane_spawn_errors,
)
from cw.events import read_events
from cw.exceptions import HookContextConflictError, StaleWorktreeError, WorktreeError
from cw.models import (
    ConcurrencyOverrides,
    DevQueueStore,
    DispatchSkipReason,
    LaneConcurrencyOverride,
    LaneConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._dispatch_lane_helpers import (
    _BREAKER_LANE_KEY,
    _lane_override,
    _RaisingNativeDaemon,
    _save_lane_override,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig
    from tests.conftest import CapturedEvent

_CLIENT = "test-client"
_LANE = LaneConfig(name="default")
_T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_DUE = _T0 + timedelta(minutes=16)


def _tripped(**updates: object) -> LaneConcurrencyOverride:
    """A circuit-breaker pause from ``_T0``, threshold-2 config, due at +15m."""
    base = LaneConcurrencyOverride(
        paused=True,
        paused_at=_T0,
        pause_source="circuit_breaker",
        pause_trigger="spawn_errors",
        consecutive_spawn_errors=2,
        recent_spawn_errors=["boom", "boom"],
        circuit_trip_streak=1,
        circuit_auto_resume_at=_T0 + timedelta(minutes=15),
    )
    return base.model_copy(update=updates)


def _pending_snapshot(ticket: str = "GEN-2696") -> DevQueueStore:
    return DevQueueStore(
        tasks=[
            TicketTask(ticket_id=ticket, client=_CLIENT, status=QueueItemStatus.PENDING)
        ]
    )


def _lane_events(
    consumer: str, event_type: OrchestratorEventType
) -> list[dict[str, object]]:
    return [e.payload for e in read_events(consumer=consumer, event_types=[event_type])]


# ---------------------------------------------------------------------------
# Recording errors
# ---------------------------------------------------------------------------


class TestRecordLaneSpawnError:
    def test_errors_appended_and_capped_at_five_oldest_dropped(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        config = breaker_config.model_copy(
            update={"lane_circuit_breaker_threshold": 100}
        )
        for i in range(7):
            _record_lane_spawn_error(_LANE, _CLIENT, f"err-{i}", config=config)

        assert _lane_override().recent_spawn_errors == [
            "err-2",
            "err-3",
            "err-4",
            "err-5",
            "err-6",
        ]

    def test_error_capped_at_500_chars_plus_ellipsis(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _record_lane_spawn_error(_LANE, _CLIENT, "x" * 900, config=breaker_config)

        (recorded,) = _lane_override().recent_spawn_errors
        assert recorded == "x" * 500 + "…"

    def test_multiline_error_collapsed(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _record_lane_spawn_error(
            _LANE, _CLIENT, "line one\n  line two\n", config=breaker_config
        )

        assert _lane_override().recent_spawn_errors == ["line one line two"]

    def test_event_recent_errors_are_the_redacted_list_last_error_is_raw(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )
        secret = "sk-" + "A" * 30
        _save_lane_override(LaneConcurrencyOverride(consecutive_spawn_errors=1))

        _record_lane_spawn_error(
            _LANE, _CLIENT, f"auth failed {secret}", config=breaker_config
        )

        persisted = _lane_override().recent_spawn_errors
        assert len(events) == 1
        payload = events[0][1]
        assert payload["recent_errors"] == persisted
        assert secret not in persisted[0]
        assert secret in str(payload["last_error"])

    @freeze_time(_T0)
    def test_trip_persists_the_cause(self, breaker_config: OrchestratorConfig) -> None:
        _save_lane_override(LaneConcurrencyOverride(consecutive_spawn_errors=1))

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.paused is True
        assert override.paused_at == _T0
        assert override.pause_source == "circuit_breaker"
        assert override.pause_trigger == "spawn_errors"
        assert override.circuit_trip_streak == 1
        assert override.circuit_half_open is False
        assert override.circuit_auto_resume_at == _T0 + timedelta(minutes=15)

    @freeze_time(datetime(2026, 10, 18, 12, 0, 0, 961607, tzinfo=UTC))
    def test_trip_stamps_are_whole_seconds(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(LaneConcurrencyOverride(consecutive_spawn_errors=1))

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.paused_at == _T0
        assert override.circuit_auto_resume_at == _T0 + timedelta(minutes=15)

    @freeze_time(_T0)
    def test_second_trip_doubles_the_cool_down(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(
            LaneConcurrencyOverride(consecutive_spawn_errors=1, circuit_trip_streak=1)
        )

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.circuit_trip_streak == 2
        assert override.circuit_auto_resume_at == _T0 + timedelta(minutes=30)

    @freeze_time(_T0)
    def test_cool_down_capped_at_max(self, breaker_config: OrchestratorConfig) -> None:
        _save_lane_override(
            LaneConcurrencyOverride(consecutive_spawn_errors=1, circuit_trip_streak=9)
        )

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        assert _lane_override().circuit_auto_resume_at == _T0 + timedelta(minutes=240)

    @freeze_time(_T0)
    def test_auto_resume_disabled_leaves_no_schedule(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )
        config = breaker_config.model_copy(
            update={"lane_circuit_auto_resume_minutes": 0}
        )
        _save_lane_override(LaneConcurrencyOverride(consecutive_spawn_errors=1))

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=config)

        assert _lane_override().paused is True
        assert _lane_override().circuit_auto_resume_at is None
        assert events[0][1]["auto_resume_at"] is None

    @freeze_time(_T0)
    def test_lane_paused_payload_carries_the_new_keys(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )
        _save_lane_override(LaneConcurrencyOverride(consecutive_spawn_errors=1))

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        assert events[0][1] == {
            "client": _CLIENT,
            "lane": "default",
            "source": "circuit_breaker",
            "consecutive_count": 2,
            "last_error": "boom",
            "trigger": "spawn_errors",
            "trip_streak": 1,
            "recent_errors": ["boom"],
            "auto_resume_at": "2026-10-18T12:15:00Z",
        }

    def test_below_threshold_records_without_tripping(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.consecutive_spawn_errors == 1
        assert override.paused is None
        assert override.pause_source is None
        assert events == []

    def test_any_recorded_error_clears_the_half_open_flag(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        """Decision 2: the probe bound holds across failure kinds."""
        _save_lane_override(
            LaneConcurrencyOverride(
                consecutive_spawn_errors=0,
                consecutive_worktree_errors=0,
                circuit_half_open=True,
            )
        )

        # A worktree error well below its 3x threshold: no trip, flag cleared.
        _record_lane_spawn_error(
            _LANE, _CLIENT, "wt", config=breaker_config, worktree_failure=True
        )

        override = _lane_override()
        assert override.paused is None
        assert override.circuit_half_open is False

    @pytest.mark.parametrize("held_source", ["operator", None])
    def test_trip_never_overwrites_an_operator_or_legacy_hold(
        self,
        held_source: str | None,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Decision 5: an operator (or legacy) hold stays sticky."""
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )
        _save_lane_override(
            LaneConcurrencyOverride(
                paused=True,
                paused_at=_T0,
                pause_source=held_source,
                consecutive_spawn_errors=1,
            )
        )

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.paused is True
        assert override.pause_source == held_source
        assert override.paused_at == _T0
        assert override.circuit_auto_resume_at is None
        assert override.circuit_trip_streak == 0
        assert override.consecutive_spawn_errors == 2
        assert override.recent_spawn_errors == ["boom"]
        assert events == []


# ---------------------------------------------------------------------------
# Worktree counter (R3)
# ---------------------------------------------------------------------------


class TestWorktreeCounter:
    def test_worktree_error_counts_separately_and_records(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _record_lane_spawn_error(
            _LANE, _CLIENT, "wt broke", config=breaker_config, worktree_failure=True
        )

        override = _lane_override()
        assert override.consecutive_worktree_errors == 1
        assert override.consecutive_spawn_errors == 0
        assert override.recent_spawn_errors == ["wt broke"]

    @freeze_time(_T0)
    def test_trips_at_three_times_the_threshold(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.LANE_PAUSED
        )
        for _ in range(5):
            _record_lane_spawn_error(
                _LANE, _CLIENT, "wt", config=breaker_config, worktree_failure=True
            )
        assert _lane_override().paused is None
        assert events == []

        _record_lane_spawn_error(
            _LANE, _CLIENT, "wt", config=breaker_config, worktree_failure=True
        )

        override = _lane_override()
        assert override.paused is True
        assert override.pause_trigger == "worktree_errors"
        assert override.consecutive_spawn_errors == 0
        assert len(events) == 1
        assert events[0][1]["trigger"] == "worktree_errors"
        assert events[0][1]["consecutive_count"] == 6

    def test_plain_error_does_not_reset_the_worktree_counter(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(LaneConcurrencyOverride(consecutive_worktree_errors=4))

        _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.consecutive_worktree_errors == 4
        assert override.consecutive_spawn_errors == 1

    def test_success_resets_both_counters(self) -> None:
        _save_lane_override(
            LaneConcurrencyOverride(
                consecutive_spawn_errors=1, consecutive_worktree_errors=4
            )
        )

        _reset_lane_spawn_errors(_LANE, _CLIENT)

        override = _lane_override()
        assert override.consecutive_spawn_errors == 0
        assert override.consecutive_worktree_errors == 0

    def test_worktree_error_tick_keeps_spawn_error_skip_and_charges_the_ticket(
        self,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-2696W1", client=_CLIENT))
        add_ticket(TicketTask(ticket_id="GEN-2696W2", client=_CLIENT))

        def _boom(*_args: object, **_kwargs: object) -> Path:
            msg = "git worktree add failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.dispatch.claim.spawn.create_worktree", _boom)

        dispatch_tick(breaker_config, native_daemon=FakeNativeDaemonClient())

        ticks = _lane_events("t-2696-wt-tick", OrchestratorEventType.DISPATCH_TICK)
        assert [t["skip_reason"] for t in ticks] == [DispatchSkipReason.SPAWN_ERROR]
        # One failure per tick: the lane loop still breaks after the first.
        assert _lane_override().consecutive_worktree_errors == 1
        charged = [t.unproductive_attempts for t in load_dev_queue().tasks]
        assert sorted(charged) == [0, 1]

    def test_worktree_tripped_lane_reports_circuit_paused(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        overrides = ConcurrencyOverrides(
            lanes={
                _BREAKER_LANE_KEY: LaneConcurrencyOverride(
                    paused=True, consecutive_worktree_errors=6
                )
            }
        )

        assert _check_lane_circuit_paused(
            _LANE,
            _pending_snapshot(),
            _CLIENT,
            overrides=overrides,
            config=breaker_config,
        )

    def test_worktree_tripped_lane_fires_the_starved_page(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        override = LaneConcurrencyOverride(paused=True, consecutive_worktree_errors=6)
        _save_lane_override(override)

        _handle_paused_lane(
            _LANE,
            _pending_snapshot(),
            _CLIENT,
            overrides=ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: override}),
            config=breaker_config,
        )

        assert len(pages) == 1


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestResetLaneSpawnErrors:
    def test_clears_breaker_progress_but_not_the_pause(self) -> None:
        _save_lane_override(_tripped(consecutive_worktree_errors=3))

        _reset_lane_spawn_errors(_LANE, _CLIENT)

        override = _lane_override()
        assert override.consecutive_spawn_errors == 0
        assert override.consecutive_worktree_errors == 0
        assert override.circuit_trip_streak == 0
        assert override.recent_spawn_errors == []
        assert override.circuit_half_open is False
        assert override.paused is True
        assert override.pause_source == "circuit_breaker"
        assert override.paused_at == _T0

    def test_steady_state_lane_is_not_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save_lane_override(LaneConcurrencyOverride())
        saves: list[ConcurrencyOverrides] = []
        monkeypatch.setattr(
            "cw.dispatch.lane_circuit._save_concurrency_overrides", saves.append
        )

        _reset_lane_spawn_errors(_LANE, _CLIENT)

        assert saves == []

    def test_half_open_lane_with_zero_counters_is_cleared(self) -> None:
        _save_lane_override(LaneConcurrencyOverride(circuit_half_open=True))

        _reset_lane_spawn_errors(_LANE, _CLIENT)

        assert _lane_override().circuit_half_open is False


# ---------------------------------------------------------------------------
# Auto-resume
# ---------------------------------------------------------------------------


class TestCooldown:
    @pytest.mark.parametrize(
        ("streak", "expected"),
        [(0, 15), (1, 15), (2, 30), (3, 60), (5, 240), (40, 240)],
    )
    def test_doubling_capped(self, streak: int, expected: int) -> None:
        assert (
            _auto_resume_cooldown_minutes(streak, base=15, max_minutes=240) == expected
        )


def _resume(
    config: OrchestratorConfig, overrides: ConcurrencyOverrides | None = None
) -> bool:
    return _maybe_auto_resume_lane(
        _LANE,
        _CLIENT,
        overrides=overrides if overrides is not None else _load_concurrency_overrides(),
        config=config,
    )


class TestAutoResume:
    def test_not_due_changes_nothing(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        events = capture_events("cw.dispatch.lane_circuit")
        _save_lane_override(_tripped())

        with freeze_time(_T0 + timedelta(minutes=5)):
            assert _resume(breaker_config) is False

        assert _lane_override() == _tripped()
        assert events == []

    def test_due_resumes_half_open(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events = capture_events("cw.dispatch.lane_circuit")
        _save_lane_override(_tripped(lane_starved_notify_next_eligible_at=_T0))

        with (
            freeze_time(_DUE),
            caplog.at_level(logging.INFO, logger="cw.dispatch"),
        ):
            assert _resume(breaker_config) is True

        override = _lane_override()
        assert override.paused is None
        assert override.consecutive_spawn_errors == 1
        assert override.circuit_half_open is True
        assert override.recent_spawn_errors == ["boom", "boom"]
        assert override.circuit_trip_streak == 1
        assert override.paused_at is None
        assert override.pause_source is None
        assert override.pause_trigger is None
        assert override.circuit_auto_resume_at is None
        assert override.lane_starved_notify_next_eligible_at is None
        assert events == [
            (
                OrchestratorEventType.LANE_RESUMED,
                {
                    "client": _CLIENT,
                    "lane": "default",
                    "source": "auto_resume",
                    "trip_streak": 1,
                    "paused_since": "2026-10-18T12:00:00Z",
                },
                None,
            )
        ]
        resumed_logs = [r for r in caplog.records if "auto-resumed" in r.getMessage()]
        assert len(resumed_logs) == 1
        assert resumed_logs[0].levelno == logging.INFO

    def test_worktree_trip_half_opens_the_worktree_counter(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(
            _tripped(
                pause_trigger="worktree_errors",
                consecutive_spawn_errors=1,
                consecutive_worktree_errors=6,
            )
        )

        with freeze_time(_DUE):
            assert _resume(breaker_config) is True

        override = _lane_override()
        assert override.consecutive_worktree_errors == 5
        assert override.consecutive_spawn_errors == 1

    def test_operator_pause_never_resumes(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(_tripped(pause_source="operator"))

        with freeze_time(_DUE):
            assert _resume(breaker_config) is False

        assert _lane_override().paused is True

    def test_legacy_pause_never_resumes(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        legacy = LaneConcurrencyOverride(paused=True, consecutive_spawn_errors=5)
        _save_lane_override(legacy)

        with freeze_time(_DUE + timedelta(days=30)):
            assert _resume(breaker_config) is False

        assert _lane_override() == legacy

    def test_disabled_never_resumes_even_with_a_leftover_schedule(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        config = breaker_config.model_copy(
            update={"lane_circuit_auto_resume_minutes": 0}
        )
        _save_lane_override(_tripped())

        with freeze_time(_DUE):
            assert _resume(config) is False

        assert _lane_override().paused is True

    @pytest.mark.parametrize(
        "raced",
        [
            LaneConcurrencyOverride(paused=True, pause_source="operator"),
            LaneConcurrencyOverride(paused=False),
        ],
    )
    def test_lock_recheck_sees_a_racing_operator_command(
        self,
        raced: LaneConcurrencyOverride,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """The precheck reads a stale snapshot; the lock re-read wins."""
        events = capture_events("cw.dispatch.lane_circuit")
        stale = ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: _tripped()})
        _save_lane_override(raced)

        with freeze_time(_DUE):
            assert _resume(breaker_config, stale) is False

        assert _lane_override() == raced
        assert events == []

    def test_lock_recheck_sees_a_removed_override(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        stale = ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: _tripped()})
        _save_concurrency_overrides(ConcurrencyOverrides())

        with freeze_time(_DUE):
            assert _resume(breaker_config, stale) is False

        assert _load_concurrency_overrides().lanes == {}

    def test_precheck_loads_nothing_when_not_due(
        self, breaker_config: OrchestratorConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loads: list[None] = []

        def _counting_load() -> ConcurrencyOverrides:
            loads.append(None)
            return ConcurrencyOverrides()

        monkeypatch.setattr(
            "cw.dispatch.lane_circuit._load_concurrency_overrides", _counting_load
        )
        snapshot = ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: _tripped()})

        with freeze_time(_T0 + timedelta(minutes=5)):
            assert _resume(breaker_config, snapshot) is False

        assert loads == []

    def test_declared_pause_survives_auto_resume(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        """R9: auto-resume writes paused=None, so clients.yaml's pause reapplies."""
        write_clients_yaml(
            sample_client_config.model_copy(
                update={
                    "lanes": [
                        LaneConfig(name="default", paused=True),
                        LaneConfig(name="fast"),
                    ]
                }
            )
        )
        _save_concurrency_overrides(
            ConcurrencyOverrides(
                lanes={
                    _BREAKER_LANE_KEY: _tripped(),
                    f"{_CLIENT}/fast": _tripped(),
                }
            )
        )

        with freeze_time(_DUE):
            assert _resume(breaker_config) is True
            assert (
                _maybe_auto_resume_lane(
                    LaneConfig(name="fast"),
                    _CLIENT,
                    overrides=_load_concurrency_overrides(),
                    config=breaker_config,
                )
                is True
            )

        assert _lane_override().paused is None
        lanes = {
            ln.name: ln for ln in load_effective_clients()[_CLIENT].effective_lanes
        }
        assert lanes["default"].paused is True
        assert lanes["fast"].paused is False


# ---------------------------------------------------------------------------
# Half-open and quiet paging
# ---------------------------------------------------------------------------


class TestHalfOpen:
    def test_one_more_error_retrips_with_a_doubled_cool_down(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(_tripped())
        with freeze_time(_DUE):
            assert _resume(breaker_config) is True

            _record_lane_spawn_error(_LANE, _CLIENT, "again", config=breaker_config)

        override = _lane_override()
        assert override.paused is True
        assert override.circuit_trip_streak == 2
        assert override.circuit_auto_resume_at == _DUE + timedelta(minutes=30)
        assert override.circuit_half_open is False

    def test_one_more_worktree_error_retrips_a_worktree_trip(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(
            _tripped(
                pause_trigger="worktree_errors",
                consecutive_spawn_errors=0,
                consecutive_worktree_errors=6,
            )
        )
        with freeze_time(_DUE):
            assert _resume(breaker_config) is True

            _record_lane_spawn_error(
                _LANE, _CLIENT, "wt", config=breaker_config, worktree_failure=True
            )

        override = _lane_override()
        assert override.paused is True
        assert override.pause_trigger == "worktree_errors"
        assert override.circuit_trip_streak == 2

    def test_spawn_counter_at_threshold_minus_one_also_retrips_a_worktree_trip(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        """S-C: "at least one" -- the other kind may sit at threshold-1 too."""
        _save_lane_override(
            _tripped(
                pause_trigger="worktree_errors",
                consecutive_spawn_errors=1,
                consecutive_worktree_errors=6,
            )
        )
        with freeze_time(_DUE):
            assert _resume(breaker_config) is True

            _record_lane_spawn_error(_LANE, _CLIENT, "boom", config=breaker_config)

        override = _lane_override()
        assert override.paused is True
        assert override.pause_trigger == "spawn_errors"

    def test_success_after_resume_resets_the_streak(
        self, breaker_config: OrchestratorConfig
    ) -> None:
        _save_lane_override(_tripped())
        with freeze_time(_DUE):
            assert _resume(breaker_config) is True

        _reset_lane_spawn_errors(_LANE, _CLIENT)

        override = _lane_override()
        assert override.circuit_trip_streak == 0
        assert override.circuit_half_open is False
        assert override.consecutive_spawn_errors == 0


def _handle(override: LaneConcurrencyOverride, config: OrchestratorConfig) -> None:
    """Run ``_handle_paused_lane`` before the resume is due."""
    _save_lane_override(override)
    with freeze_time(_T0 + timedelta(minutes=5)):
        _handle_paused_lane(
            _LANE,
            _pending_snapshot(),
            _CLIENT,
            overrides=ConcurrencyOverrides(lanes={_BREAKER_LANE_KEY: override}),
            config=config,
        )


def test_starved_notify_is_a_no_op_without_pending_work(
    breaker_config: OrchestratorConfig,
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """A genuinely idle paused lane stays silent (#1630)."""
    pages = capture_events("cw.dispatch.lane_circuit")

    _maybe_notify_lane_starved(_CLIENT, "default", 0, config=breaker_config)

    assert pages == []
    assert _load_concurrency_overrides().lanes == {}


class TestQuietPaging:
    def _pages(
        self,
        override: LaneConcurrencyOverride,
        config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> list[CapturedEvent]:
        pages = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        _handle(override, config)
        return pages

    def test_scheduled_early_trip_is_quiet(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        assert self._pages(_tripped(), breaker_config, capture_events) == []

    def test_third_trip_pages(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = self._pages(
            _tripped(circuit_trip_streak=3), breaker_config, capture_events
        )
        assert len(pages) == 1

    def test_disabled_auto_resume_pages(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        config = breaker_config.model_copy(
            update={"lane_circuit_auto_resume_minutes": 0}
        )
        assert len(self._pages(_tripped(), config, capture_events)) == 1

    @pytest.mark.parametrize("source", ["operator", None])
    def test_operator_and_legacy_pauses_page(
        self,
        source: str | None,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        held = LaneConcurrencyOverride(
            paused=True, pause_source=source, consecutive_spawn_errors=2
        )
        assert len(self._pages(held, breaker_config, capture_events)) == 1

    def test_auto_resumed_lane_is_not_reported_paused(
        self,
        breaker_config: OrchestratorConfig,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        pages = capture_events(
            "cw.dispatch.lane_circuit", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        _save_lane_override(_tripped())

        with freeze_time(_DUE):
            reported = _handle_paused_lane(
                _LANE,
                _pending_snapshot(),
                _CLIENT,
                overrides=_load_concurrency_overrides(),
                config=breaker_config,
            )

        assert reported is False
        assert pages == []


# ---------------------------------------------------------------------------
# No-charge probe (R16) and end-to-end ticks
# ---------------------------------------------------------------------------


def _seed_due_trip(**updates: object) -> None:
    _save_lane_override(_tripped(**updates))


class TestNoChargeProbe:
    """End to end through ``dispatch_tick``."""

    def _resume_then_fail(
        self,
        exc: Exception,
        client: ClientConfig,
        config: OrchestratorConfig,
        **trip: object,
    ) -> TicketTask:
        write_clients_yaml(client)
        _seed_due_trip(**trip)
        add_ticket(TicketTask(ticket_id="GEN-2696P", client=_CLIENT))
        with freeze_time(_DUE):
            dispatch_tick(config, native_daemon=FakeNativeDaemonClient())
            assert _lane_override().circuit_half_open is True
            dispatch_tick(config, native_daemon=_RaisingNativeDaemon(exc))
        (row,) = load_dev_queue().tasks
        return row

    def test_failed_probe_is_not_charged_and_retrips(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        row = self._resume_then_fail(
            RuntimeError("boom"), sample_client_config, breaker_config
        )

        assert row.unproductive_attempts == 0
        assert row.spawn_error_count == 1
        override = _lane_override()
        assert override.paused is True
        assert override.circuit_trip_streak == 2
        assert override.circuit_auto_resume_at == _DUE + timedelta(minutes=30)
        assert override.circuit_half_open is False

    def test_failed_worktree_probe_after_a_worktree_trip_is_not_charged(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        row = self._resume_then_fail(
            WorktreeError("wt broke"),
            sample_client_config,
            breaker_config,
            pause_trigger="worktree_errors",
            consecutive_spawn_errors=0,
            consecutive_worktree_errors=6,
        )

        assert row.unproductive_attempts == 0
        override = _lane_override()
        assert override.paused is True
        assert override.pause_trigger == "worktree_errors"
        assert override.circuit_half_open is False

    def test_failed_hook_context_probe_is_not_charged(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        """Decision 1: the hook-context-conflict revert honours the probe too."""
        row = self._resume_then_fail(
            HookContextConflictError("held", conflicting_session_id="sess-x"),
            sample_client_config,
            breaker_config,
        )

        assert row.unproductive_attempts == 0
        assert row.spawn_error_count == 1
        assert _lane_override().paused is True

    @pytest.mark.parametrize(
        ("exc", "charged"),
        [
            (RuntimeError("boom"), 1),
            (HookContextConflictError("held", conflicting_session_id="s"), 1),
            (OSError("adapter unreachable"), 0),
        ],
    )
    def test_failed_non_probe_spawn_charges_as_today(
        self,
        exc: Exception,
        charged: int,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-2696N", client=_CLIENT))

        dispatch_tick(breaker_config, native_daemon=_RaisingNativeDaemon(exc))

        (row,) = load_dev_queue().tasks
        assert row.unproductive_attempts == charged

    def test_successful_probe_clears_the_probe_state(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        write_clients_yaml(sample_client_config)
        _seed_due_trip()
        add_ticket(TicketTask(ticket_id="GEN-2696S", client=_CLIENT))
        daemon = FakeNativeDaemonClient()

        with freeze_time(_DUE):
            first = dispatch_tick(breaker_config, native_daemon=daemon)
            second = dispatch_tick(breaker_config, native_daemon=daemon)

        assert first.spawned == 0
        assert second.spawned == 1
        override = _lane_override()
        assert override.circuit_half_open is False
        assert override.consecutive_spawn_errors == 0
        assert override.circuit_trip_streak == 0

    def test_revert_reads_the_flag_before_the_trip_clears_it(
        self,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordering invariant: the charge decision precedes the breaker record."""
        from cw.dispatch.claim import spawn as spawn_mod

        order: list[str] = []
        real_charge = spawn_mod._charges_unproductive_attempt
        from cw.dispatch import lanes as lanes_mod

        real_record = lanes_mod._record_lane_spawn_error

        def _charge(exc: BaseException, client_name: str, lane: str) -> bool:
            order.append(f"charge:{_lane_override().circuit_half_open}")
            return real_charge(exc, client_name, lane)

        def _record(
            lane_cfg: LaneConfig,
            client_name: str,
            last_error: str,
            *,
            config: OrchestratorConfig,
            worktree_failure: bool = False,
        ) -> None:
            order.append("record")
            real_record(
                lane_cfg,
                client_name,
                last_error,
                config=config,
                worktree_failure=worktree_failure,
            )

        monkeypatch.setattr(spawn_mod, "_charges_unproductive_attempt", _charge)
        monkeypatch.setattr(lanes_mod, "_record_lane_spawn_error", _record)

        row = self._resume_then_fail(
            RuntimeError("boom"), sample_client_config, breaker_config
        )

        assert order == ["charge:True", "record"]
        assert row.unproductive_attempts == 0


class TestEndToEndAutoResume:
    def test_tripped_lane_resumes_then_spawns(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        write_clients_yaml(sample_client_config)
        _seed_due_trip()
        add_ticket(TicketTask(ticket_id="GEN-2696E", client=_CLIENT))
        daemon = FakeNativeDaemonClient()

        with freeze_time(_T0 + timedelta(minutes=5)):
            early = dispatch_tick(breaker_config, native_daemon=daemon)
        with freeze_time(_DUE):
            resumed = dispatch_tick(breaker_config, native_daemon=daemon)
            spawned = dispatch_tick(breaker_config, native_daemon=daemon)

        assert (early.spawned, resumed.spawned, spawned.spawned) == (0, 0, 1)
        resumes = _lane_events("t-2696-e2e", OrchestratorEventType.LANE_RESUMED)
        assert len(resumes) == 1
        assert _lane_override().circuit_trip_streak == 0

    def test_worktree_tripped_lane_resumes_then_spawns(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        write_clients_yaml(sample_client_config)
        _seed_due_trip(
            pause_trigger="worktree_errors",
            consecutive_spawn_errors=0,
            consecutive_worktree_errors=6,
        )
        add_ticket(TicketTask(ticket_id="GEN-2696EW", client=_CLIENT))
        daemon = FakeNativeDaemonClient()

        with freeze_time(_DUE):
            dispatch_tick(breaker_config, native_daemon=daemon)
            spawned = dispatch_tick(breaker_config, native_daemon=daemon)

        assert spawned.spawned == 1
        assert _lane_override().consecutive_worktree_errors == 0

    def test_declared_pause_lane_stays_unspawned(
        self, sample_client_config: ClientConfig, breaker_config: OrchestratorConfig
    ) -> None:
        write_clients_yaml(
            sample_client_config.model_copy(
                update={"lanes": [LaneConfig(name="default", paused=True)]}
            )
        )
        _seed_due_trip()
        add_ticket(TicketTask(ticket_id="GEN-2696D", client=_CLIENT))
        daemon = FakeNativeDaemonClient()

        with freeze_time(_DUE):
            dispatch_tick(breaker_config, native_daemon=daemon)
            later = dispatch_tick(breaker_config, native_daemon=daemon)

        assert later.spawned == 0
        assert daemon.spawn_calls == []
        assert _lane_override().paused is None


def test_stale_worktree_error_is_a_worktree_failure(
    sample_client_config: ClientConfig,
    breaker_config: OrchestratorConfig,
) -> None:
    """Subclasses of ``WorktreeError`` classify the same way (R3)."""
    write_clients_yaml(sample_client_config)
    add_ticket(TicketTask(ticket_id="GEN-2696SW", client=_CLIENT))

    dispatch_tick(
        breaker_config,
        native_daemon=_RaisingNativeDaemon(StaleWorktreeError("stale")),
    )

    assert _lane_override().consecutive_worktree_errors == 1
    assert _lane_override().consecutive_spawn_errors == 0
