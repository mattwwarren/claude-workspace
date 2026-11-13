"""The running dispatch loop honours an operator usage-limit clear (#3547 R3).

Loop-side helper units (``_drop_operator_cleared_windows``, the arm step's
``clears_seen`` filter, the transition handler's ``operator_cleared``) and the
multi-tick ``run_dispatch_loop`` acceptance tests: a clear between two ticks
lifts the window with no restart, a post-tick arm cannot resurrect it, a
later arm is honoured, and a same-tick detection overrides the clear.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cw.dispatch_state_marks import (
    clear_usage_limited_until,
    load_usage_limit_clear_marks,
)
from cw.usage_limit_window import apply_usage_limit_clear, plan_usage_limit_clear
from freezegun import freeze_time

import cw.dispatch.loop
from cw.dev_queue import add_ticket
from cw.dispatch import DispatchTickResult, run_dispatch_loop
from cw.dispatch.loop import _handle_usage_limit_window_transition
from cw.dispatch.usage_limit_windows import (
    _arm_usage_limit_windows,
    _drop_operator_cleared_windows,
    _merge_persisted_usage_limited_until,
)
from cw.dispatch_state import load_usage_limited_until
from cw.events import read_events
from cw.models import ClientConfig, OrchestratorEventType, TicketTask
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._shared.dispatch_fixtures import advancing_tick, seed_usage_limit_window

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CLIENT = "test-client"
_FROZEN = "2026-07-16 12:00:00"
_SUPPRESSION = "suppressing the loop's dispatch.usage_limit_cleared"


def _events(event_type: OrchestratorEventType) -> list[dict[str, object]]:
    return [e.payload for e in read_events(event_types=[event_type])]


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def _operator_clear() -> None:
    apply_usage_limit_clear(
        plan_usage_limit_clear(_CLIENT),
        actor="operator-login",
        forced=True,
        probe_verdict="skipped",
    )


class TestDropOperatorClearedWindows:
    def test_drops_window_for_unseen_mark_and_records_it_seen(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seen: dict[str, datetime] = {}

        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            kept, observed = _drop_operator_cleared_windows(
                {_CLIENT: until, "other": until}, seen
            )

        assert kept == {"other": until}
        assert observed == {_CLIENT}
        assert seen == {_CLIENT: now}
        assert any(
            "operator cleared the usage limit window for" in m and _CLIENT in m
            for m in _messages(caplog)
        )

    def test_reports_unseen_mark_with_no_in_memory_window(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seen: dict[str, datetime] = {}

        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            kept, observed = _drop_operator_cleared_windows({}, seen)

        assert kept == {}
        assert observed == {_CLIENT}
        assert seen == {_CLIENT: now}
        assert any(
            "operator clear mark observed for" in m and "no in-memory window" in m
            for m in _messages(caplog)
        )

    def test_second_call_with_same_mark_drops_nothing(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seen: dict[str, datetime] = {}
        _drop_operator_cleared_windows({}, seen)

        kept, observed = _drop_operator_cleared_windows({_CLIENT: until}, seen)

        assert kept == {_CLIENT: until}
        assert observed == set()

    def test_fresh_disk_arm_after_the_clear_is_retained(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """No value-equality dependence: the re-arm has the cleared deadline."""
        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        seed_usage_limit_window(_CLIENT, until)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seen: dict[str, datetime] = {}
        kept, _ = _drop_operator_cleared_windows({_CLIENT: until}, seen)
        assert _merge_persisted_usage_limited_until(kept) == {}

        seed_usage_limit_window(_CLIENT, until)
        kept, observed = _drop_operator_cleared_windows(kept, seen)

        assert observed == set()
        assert _merge_persisted_usage_limited_until(kept) == {_CLIENT: until}


class TestArmClearsSeen:
    def test_stale_carry_is_not_re_persisted(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        stale = now + timedelta(hours=2)
        seed_usage_limit_window(_CLIENT, stale)
        seen = load_usage_limit_clear_marks()
        clear_usage_limited_until(_CLIENT, cleared_at=now)

        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            merged = _arm_usage_limit_windows(
                {_CLIENT: stale},
                {"other": None},
                backoff_seconds=600,
                clears_seen=seen,
            )

        assert _CLIENT not in merged
        assert "other" in merged
        assert set(load_usage_limited_until()) == {"other"}
        armed = _events(OrchestratorEventType.USAGE_LIMIT_ARMED)
        assert [e["client"] for e in armed] == ["other"]
        assert any(
            "not re-persisting the stale usage limit window for" in m and _CLIENT in m
            for m in _messages(caplog)
        )

    def test_fresh_detection_for_the_cleared_client_survives(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        stale = now + timedelta(hours=2)
        seed_usage_limit_window(_CLIENT, stale)
        seen = load_usage_limit_clear_marks()
        clear_usage_limited_until(_CLIENT, cleared_at=now)

        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            merged = _arm_usage_limit_windows(
                {_CLIENT: stale},
                {_CLIENT: None},
                backoff_seconds=600,
                clears_seen=seen,
            )

        fresh = merged[_CLIENT]
        assert fresh < stale
        assert load_usage_limited_until() == {_CLIENT: fresh}
        armed = _events(OrchestratorEventType.USAGE_LIMIT_ARMED)
        assert [e["client"] for e in armed] == [_CLIENT]
        assert not any("did not persist" in m for m in _messages(caplog))

    def test_arm_does_not_consume_the_mark(self, tmp_dispatch_dirs: Path) -> None:
        now = datetime.now(UTC)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seen: dict[str, datetime] = {}

        _arm_usage_limit_windows(
            {_CLIENT: now + timedelta(hours=1)},
            {"other": None},
            backoff_seconds=600,
            clears_seen=seen,
        )

        assert seen == {}

    def test_restart_seeded_mark_keeps_a_fresh_disk_arm(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        now = datetime.now(UTC)
        until = now + timedelta(hours=1)
        clear_usage_limited_until(_CLIENT, cleared_at=now)
        seed_usage_limit_window(_CLIENT, until)
        seen = load_usage_limit_clear_marks()

        merged = _arm_usage_limit_windows(
            {_CLIENT: until}, {"other": None}, backoff_seconds=600, clears_seen=seen
        )

        assert merged[_CLIENT] == until
        assert load_usage_limited_until()[_CLIENT] == until


class TestTransitionHandler:
    def test_operator_cleared_suppresses_the_emit(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            active = _handle_usage_limit_window_transition(
                True, usage_limited_until={}, armed_at=None, operator_cleared=True
            )

        assert active is False
        assert _events(OrchestratorEventType.USAGE_LIMIT_CLEARED) == []
        assert any(_SUPPRESSION in m for m in _messages(caplog))

    def test_default_still_emits(self, tmp_dispatch_dirs: Path) -> None:
        active = _handle_usage_limit_window_transition(
            True, usage_limited_until={}, armed_at=None
        )

        assert active is False
        assert len(_events(OrchestratorEventType.USAGE_LIMIT_CLEARED)) == 1


def _run_loop(
    monkeypatch: pytest.MonkeyPatch,
    tick: object,
    daemon: FakeNativeDaemonClient,
) -> None:
    monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", tick)
    monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)
    with contextlib.suppress(KeyboardInterrupt):
        run_dispatch_loop(native_daemon=daemon)


def _fake_tick(
    usage_limit_clients: dict[str, datetime | None],
) -> Callable[..., DispatchTickResult]:
    def _tick(*_args: object, **_kwargs: object) -> DispatchTickResult:
        return DispatchTickResult(
            spawned=0,
            usage_limit_detected=bool(usage_limit_clients),
            usage_limit_clients=dict(usage_limit_clients),
        )

    return _tick


class TestLoopAcceptance:
    def test_clear_between_two_ticks_drops_in_memory_window(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-3547-A", client=_CLIENT))
        daemon = FakeNativeDaemonClient()
        captured: list[dict[str, datetime]] = []

        with freeze_time(_FROZEN), caplog.at_level(logging.INFO, logger="cw.dispatch"):
            until = datetime.now(UTC) + timedelta(hours=2)
            seed_usage_limit_window(_CLIENT, until)
            tick = advancing_tick(
                cw.dispatch.loop.dispatch_tick, captured, after={1: _operator_clear}
            )
            _run_loop(monkeypatch, tick, daemon)

        assert captured[0] == {_CLIENT: until}
        assert captured[1] == {}
        assert len(daemon.spawn_calls) == 1
        cleared = _events(OrchestratorEventType.USAGE_LIMIT_CLEARED)
        assert len(cleared) == 1
        assert cleared[0]["trigger"] == "operator"
        assert any(_SUPPRESSION in m for m in _messages(caplog))

    def test_post_tick_arm_does_not_resurrect_cleared_window(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        daemon = FakeNativeDaemonClient()
        captured: list[dict[str, datetime]] = []

        with freeze_time(_FROZEN):
            until = datetime.now(UTC) + timedelta(hours=2)
            seed_usage_limit_window(_CLIENT, until)
            tick = advancing_tick(
                _fake_tick({"other": None}), captured, after={1: _operator_clear}
            )
            _run_loop(monkeypatch, tick, daemon)
            on_disk = load_usage_limited_until()

        assert set(on_disk) == {"other"}
        assert set(captured[1]) == {"other"}
        cleared = _events(OrchestratorEventType.USAGE_LIMIT_CLEARED)
        assert [e.get("trigger") for e in cleared] == ["operator"]

    def test_arm_after_clear_is_honored(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        daemon = FakeNativeDaemonClient()
        captured: list[dict[str, datetime]] = []

        with freeze_time(_FROZEN):
            until = datetime.now(UTC) + timedelta(hours=2)
            rearm = datetime.now(UTC) + timedelta(hours=1)
            seed_usage_limit_window(_CLIENT, until)
            tick = advancing_tick(
                _fake_tick({}),
                captured,
                after={
                    1: _operator_clear,
                    2: lambda: seed_usage_limit_window(_CLIENT, rearm),
                },
                stop_on_call=4,
            )
            _run_loop(monkeypatch, tick, daemon)

        assert captured[1] == {}
        assert captured[2] == {_CLIENT: rearm}
        assert captured[3] == {_CLIENT: rearm}

    def test_same_tick_detection_overrides_clear_and_is_not_lost(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        daemon = FakeNativeDaemonClient()
        captured: list[dict[str, datetime]] = []

        with freeze_time(_FROZEN):
            now = datetime.now(UTC)
            until = now + timedelta(days=2)
            seed_usage_limit_window(_CLIENT, until)
            tick = advancing_tick(
                _fake_tick({_CLIENT: None}), captured, after={1: _operator_clear}
            )
            _run_loop(monkeypatch, tick, daemon)
            on_disk = load_usage_limited_until()

        fresh = on_disk[_CLIENT]
        assert now < fresh < until
        assert captured[1] == {_CLIENT: fresh}
        armed = _events(OrchestratorEventType.USAGE_LIMIT_ARMED)
        assert any(
            e["client"] == _CLIENT and e["until"] == fresh.isoformat() for e in armed
        )
