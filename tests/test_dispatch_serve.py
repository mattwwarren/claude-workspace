"""Tests for cw.dispatch_serve — in-process dispatch loop supervisor."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from cw.dispatch_serve import (
    _SERVE_BACKOFF_CAP_SECONDS,
    _SERVE_CRASH_WINDOW_SECONDS,
    _SERVE_HEALTHY_RUN_SECONDS,
    _SERVE_INITIAL_BACKOFF_SECONDS,
    _SERVE_MAX_CRASHES,
    _prune_crash_window,
    run_dispatch_serve,
)


# ---------------------------------------------------------------------------
# TestRunDispatchServeConstants
# ---------------------------------------------------------------------------


class TestRunDispatchServeConstants:
    def test_max_crashes(self) -> None:
        assert _SERVE_MAX_CRASHES == 5

    def test_crash_window(self) -> None:
        assert _SERVE_CRASH_WINDOW_SECONDS == 300

    def test_initial_backoff(self) -> None:
        assert _SERVE_INITIAL_BACKOFF_SECONDS == 5.0

    def test_backoff_cap(self) -> None:
        assert _SERVE_BACKOFF_CAP_SECONDS == 60.0

    def test_healthy_run(self) -> None:
        assert _SERVE_HEALTHY_RUN_SECONDS == 60.0


# ---------------------------------------------------------------------------
# TestPruneCrashWindow
# ---------------------------------------------------------------------------


class TestPruneCrashWindow:
    def test_empty_list(self) -> None:
        assert _prune_crash_window([], 1000.0) == []

    def test_all_inside_window(self) -> None:
        now = 1000.0
        times = [800.0, 850.0, 900.0]
        result = _prune_crash_window(times, now)
        assert result == times

    def test_all_outside_window(self) -> None:
        now = 1000.0
        times = [100.0, 200.0, 300.0]
        result = _prune_crash_window(times, now)
        assert result == []

    def test_boundary_exclusive(self) -> None:
        # cutoff = now - 300; exact-cutoff entry is excluded
        now = 1000.0
        cutoff = now - _SERVE_CRASH_WINDOW_SECONDS  # 700.0
        times = [cutoff - 1, cutoff, cutoff + 1]
        result = _prune_crash_window(times, now)
        assert result == [cutoff, cutoff + 1]

    def test_mixed(self) -> None:
        now = 1000.0
        inside = [750.0, 900.0]
        outside = [100.0, 699.9]
        result = _prune_crash_window(outside + inside, now)
        assert sorted(result) == sorted(inside)


# ---------------------------------------------------------------------------
# TestRunDispatchServeCleanExit
# ---------------------------------------------------------------------------


class TestRunDispatchServeCleanExit:
    def test_clean_return_exits_without_restart(self) -> None:
        """A normal return from run_dispatch_loop should stop the supervisor."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1

        with patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop):
            run_dispatch_serve()

        assert call_count == 1

    def test_keyboard_interrupt_exits_cleanly(self) -> None:
        """KeyboardInterrupt from the loop propagates as a clean stop."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise KeyboardInterrupt

        with patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop):
            run_dispatch_serve()

        assert call_count == 1

    def test_system_exit_propagates(self) -> None:
        """SystemExit raised inside the loop is re-raised by the supervisor."""

        def _fake_loop(**_kwargs: object) -> None:
            raise SystemExit(42)

        with patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop):
            with pytest.raises(SystemExit) as exc_info:
                run_dispatch_serve()

        assert exc_info.value.code == 42

    def test_kwargs_forwarded_to_loop(self) -> None:
        """All kwargs are forwarded to run_dispatch_loop."""
        captured: list[dict[str, object]] = []

        def _fake_loop(**kwargs: object) -> None:
            captured.append(dict(kwargs))

        with patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop):
            run_dispatch_serve(
                max_parallel=3,
                use_plan=True,
                parent="sess-abc",
                emit=print,
                auto_ff=False,
                client="acme",
            )

        assert len(captured) == 1
        kw = captured[0]
        assert kw["max_parallel"] == 3
        assert kw["use_plan"] is True
        assert kw["parent"] == "sess-abc"
        assert kw["emit"] is print
        assert kw["auto_ff"] is False
        assert kw["client"] == "acme"


# ---------------------------------------------------------------------------
# TestRunDispatchServeCrashRestart
# ---------------------------------------------------------------------------


class TestRunDispatchServeCrashRestart:
    def test_crash_triggers_restart(self) -> None:
        """A single crash is followed by one restart then clean exit."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            # Second call returns cleanly.

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
        ):
            run_dispatch_serve()

        assert call_count == 2

    def test_max_restarts_zero_exits_on_first_crash(self) -> None:
        """max_restarts=0 means no restarts — give up after the first crash."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("bang")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_dispatch_serve(max_restarts=0)

        assert exc_info.value.code == 1
        assert call_count == 1

    def test_max_restarts_one_allows_one_restart(self) -> None:
        """max_restarts=1 allows exactly 1 restart before giving up."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_dispatch_serve(max_restarts=1)

        assert exc_info.value.code == 1
        assert call_count == 2

    def test_error_logged_on_crash(self, caplog: pytest.LogCaptureFixture) -> None:
        """An ERROR log is emitted on each crash."""
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("oops")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
            caplog.at_level(logging.ERROR, logger="cw.dispatch_serve"),
        ):
            run_dispatch_serve()

        assert any("crashed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestRunDispatchServeBackoff
# ---------------------------------------------------------------------------


class TestRunDispatchServeBackoff:
    def test_initial_backoff(self) -> None:
        """First crash sleeps for _SERVE_INITIAL_BACKOFF_SECONDS."""
        sleep_calls: list[float] = []
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep", side_effect=sleep_calls.append),
        ):
            run_dispatch_serve()

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(_SERVE_INITIAL_BACKOFF_SECONDS)

    def test_backoff_doubles_on_second_crash(self) -> None:
        """Second crash waits double the initial backoff."""
        sleep_calls: list[float] = []
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep", side_effect=sleep_calls.append),
        ):
            run_dispatch_serve()

        assert len(sleep_calls) == 2
        # First sleep = initial (5s), second sleep = initial * 2 (10s)
        assert sleep_calls[0] == pytest.approx(_SERVE_INITIAL_BACKOFF_SECONDS)
        assert sleep_calls[1] == pytest.approx(_SERVE_INITIAL_BACKOFF_SECONDS * 2)

    def test_backoff_capped(self) -> None:
        """Backoff never exceeds _SERVE_BACKOFF_CAP_SECONDS."""
        sleep_calls: list[float] = []
        call_count = 0

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            # Crash until we've recorded enough sleeps to reach the cap.
            # With initial=5, doublings: 5, 10, 20, 40, 60, 60...
            if call_count <= 6:
                raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep", side_effect=sleep_calls.append),
            patch("cw.dispatch_serve.time.time", return_value=0.0),
            patch(
                "cw.dispatch_serve._prune_crash_window",
                side_effect=lambda times, _now: times[-3:],  # keep < 5 to avoid cap
            ),
        ):
            run_dispatch_serve()

        assert all(s <= _SERVE_BACKOFF_CAP_SECONDS for s in sleep_calls)
        # At least one sleep should have hit the cap (60.0)
        assert any(s == pytest.approx(_SERVE_BACKOFF_CAP_SECONDS) for s in sleep_calls)

    def test_healthy_run_resets_backoff(self) -> None:
        """After a healthy run (>= _SERVE_HEALTHY_RUN_SECONDS), backoff resets."""
        sleep_calls: list[float] = []
        call_count = 0
        # Call 1: short crash (0.5s) → sleep initial (5s), backoff → 10s
        # Call 2: healthy crash (70s) → sleep 10s, backoff resets → 5s
        # Call 3: clean return → exit
        monotonic_times = [
            0.0,   0.5,   # call 1: 0.5s run → crash
            0.0,  70.0,   # call 2: 70s run → crash (healthy)
            0.0,   0.0,   # call 3: clean return
        ]
        monotonic_iter = iter(monotonic_times)

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("crash")
            # Call 3 returns cleanly

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch(
                "cw.dispatch_serve.time.monotonic",
                side_effect=monotonic_iter,
            ),
            patch("cw.dispatch_serve.time.sleep", side_effect=sleep_calls.append),
            patch("cw.dispatch_serve.time.time", return_value=0.0),
            patch(
                "cw.dispatch_serve._prune_crash_window",
                side_effect=lambda times, _now: times[-3:],
            ),
        ):
            run_dispatch_serve()

        # crash 1 (short): sleep initial=5.0, backoff → 10.0
        # crash 2 (healthy 70s): sleep 10.0, backoff resets → 5.0
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == pytest.approx(_SERVE_INITIAL_BACKOFF_SECONDS)
        assert sleep_calls[1] == pytest.approx(_SERVE_INITIAL_BACKOFF_SECONDS * 2)

    def test_no_backoff_reset_below_healthy_threshold(self) -> None:
        """Runs shorter than _SERVE_HEALTHY_RUN_SECONDS do NOT reset backoff."""
        sleep_calls: list[float] = []
        call_count = 0
        monotonic_times = [
            0.0,  0.1,   # call 1: 0.1s run → crash
            0.0, 59.9,   # call 2: 59.9s run → crash (just below healthy threshold)
            0.0,  0.0,   # call 3: clean exit
        ]
        monotonic_iter = iter(monotonic_times)

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch(
                "cw.dispatch_serve.time.monotonic",
                side_effect=monotonic_iter,
            ),
            patch("cw.dispatch_serve.time.sleep", side_effect=sleep_calls.append),
            patch("cw.dispatch_serve.time.time", return_value=0.0),
            patch(
                "cw.dispatch_serve._prune_crash_window",
                side_effect=lambda times, _now: times[-3:],
            ),
        ):
            run_dispatch_serve()

        # Both runs were unhealthy; backoff must keep doubling
        assert sleep_calls[1] == pytest.approx(sleep_calls[0] * 2)


# ---------------------------------------------------------------------------
# TestRunDispatchServeCrashCap
# ---------------------------------------------------------------------------


class TestRunDispatchServeCrashCap:
    def test_fifth_crash_in_window_triggers_exit(self) -> None:
        """5 crashes in the window hits the cap and exits with code 1."""
        call_count = 0
        real_times = [float(i) for i in range(20)]
        time_iter = iter(real_times)

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
            patch("cw.dispatch_serve.time.time", side_effect=time_iter),
            patch("cw.dispatch_serve.time.monotonic", return_value=0.0),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_dispatch_serve()

        assert exc_info.value.code == 1
        assert call_count == _SERVE_MAX_CRASHES

    def test_crash_outside_window_does_not_count(self) -> None:
        """Crashes older than the window don't count toward the cap."""
        call_count = 0
        # Time jumps 400s between crashes (outside the 300s window).
        current_time = 0.0

        def _fake_time() -> float:
            return current_time

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count, current_time
            call_count += 1
            if call_count <= 3:
                current_time += 400.0  # well outside the window
                raise RuntimeError("crash")
            # 4th call returns cleanly

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
            patch("cw.dispatch_serve.time.time", side_effect=_fake_time),
            patch("cw.dispatch_serve.time.monotonic", return_value=0.0),
        ):
            run_dispatch_serve()  # Should NOT exit(1)

        assert call_count == 4

    def test_critical_log_on_crash_cap(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CRITICAL log is emitted when the crash cap is hit."""
        call_count = 0
        real_times = [float(i) for i in range(20)]
        time_iter = iter(real_times)

        def _fake_loop(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
            patch("cw.dispatch_serve.time.time", side_effect=time_iter),
            patch("cw.dispatch_serve.time.monotonic", return_value=0.0),
            caplog.at_level(logging.CRITICAL, logger="cw.dispatch_serve"),
        ):
            with pytest.raises(SystemExit):
                run_dispatch_serve()

        assert any("giving up" in r.message for r in caplog.records)

    def test_critical_log_on_max_restarts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CRITICAL log is emitted when max_restarts is exhausted."""

        def _fake_loop(**_kwargs: object) -> None:
            raise RuntimeError("crash")

        with (
            patch("cw.dispatch_serve.run_dispatch_loop", _fake_loop),
            patch("cw.dispatch_serve.time.sleep"),
            patch("cw.dispatch_serve.time.time", return_value=0.0),
            patch("cw.dispatch_serve.time.monotonic", return_value=0.0),
            caplog.at_level(logging.CRITICAL, logger="cw.dispatch_serve"),
        ):
            with pytest.raises(SystemExit):
                run_dispatch_serve(max_restarts=0)

        assert any("max_restarts" in r.message for r in caplog.records)
