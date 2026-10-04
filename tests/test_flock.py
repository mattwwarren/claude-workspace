"""Tests for ``cw._flock``: the shared bounded-flock poll helper (GitHub #2491).

``try_flock_until`` is exercised against a REAL contended lock (a second open
file description) with a fake monotonic clock, so the deadline arithmetic is
pinned without wall-clock thresholds.
"""

from __future__ import annotations

import fcntl
import logging
from typing import TYPE_CHECKING

import pytest

from cw import _flock
from cw._flock import (
    DEFAULT_SESSIONS_LOCK_TIMEOUT_S,
    SESSIONS_LOCK_POLL_INTERVAL_S,
    SESSIONS_LOCK_TIMEOUT_ENV,
    acquire_sessions_flock,
    sessions_lock_timeout_message,
    sessions_lock_timeout_seconds,
    try_flock_until,
)
from cw.exceptions import SessionsLockTimeoutError
from tests.conftest import (
    _assert_lock_held,
    _fake_fcntl,
    _FakeClock,
    _hold_flock,
    _raise_eio,
)

if TYPE_CHECKING:
    from pathlib import Path

# Dyadic fractions: the shared ``_FakeClock`` accumulates them with no float error,
# so ``remaining`` hits exactly 0.0 and the clamp assertion can be exact.
_POLL_S = 0.5
_TIMEOUT_S = 0.75


@pytest.fixture(autouse=True)
def _reset_invalid_env_warning_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fresh warn-once memory so tests do not order-depend."""
    monkeypatch.setattr(_flock, "_warned_invalid_raw", set())


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(_flock, "time", clock)


class TestTryFlockUntil:
    def test_free_lock_acquired_without_sleeping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)

        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquired = try_flock_until(
                fd, timeout_s=_TIMEOUT_S, poll_interval_s=_POLL_S
            )

            assert acquired is True
            assert clock.sleeps == []
            _assert_lock_held(lock)

    def test_zero_timeout_held_fails_with_zero_sleeps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        lock = tmp_path / "lock"

        with _hold_flock(lock), lock.open("w") as fd:
            acquired = try_flock_until(fd, timeout_s=0, poll_interval_s=_POLL_S)

        assert acquired is False
        assert clock.sleeps == []

    def test_zero_timeout_free_acquires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)

        with (tmp_path / "lock").open("w") as fd:
            acquired = try_flock_until(fd, timeout_s=0, poll_interval_s=_POLL_S)

        assert acquired is True
        assert clock.sleeps == []

    def test_sleeps_are_poll_sized_and_last_is_clamped_to_remaining(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins ``min(poll, remaining)``: never sleep past the deadline."""
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        lock = tmp_path / "lock"

        with _hold_flock(lock), lock.open("w") as fd:
            acquired = try_flock_until(
                fd, timeout_s=_TIMEOUT_S, poll_interval_s=_POLL_S
            )

        assert acquired is False
        assert clock.sleeps == [_POLL_S, _TIMEOUT_S - _POLL_S]
        assert all(s <= _POLL_S for s in clock.sleeps)
        assert sum(clock.sleeps) == _TIMEOUT_S

    def test_acquires_when_holder_releases_mid_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = tmp_path / "lock"
        with _hold_flock(lock) as release:
            clock = _FakeClock(on_sleep=lambda _n: release())
            _install_clock(monkeypatch, clock)

            with lock.open("w") as fd:
                acquired = try_flock_until(
                    fd, timeout_s=_TIMEOUT_S, poll_interval_s=_POLL_S
                )

        assert acquired is True
        assert clock.sleeps == [_POLL_S]

    def test_non_contention_oserror_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_flock, "fcntl", _fake_fcntl(_raise_eio))
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)

        with (
            (tmp_path / "lock").open("w") as fd,
            pytest.raises(OSError, match="disk on fire"),
        ):
            try_flock_until(fd, timeout_s=_TIMEOUT_S, poll_interval_s=_POLL_S)

        assert clock.sleeps == []


class TestSessionsLockTimeoutSeconds:
    """Env parsing for CW_SESSIONS_LOCK_TIMEOUT_S."""

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SESSIONS_LOCK_TIMEOUT_ENV, raising=False)

        assert sessions_lock_timeout_seconds() == DEFAULT_SESSIONS_LOCK_TIMEOUT_S

    def test_blank_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "  ")

        assert sessions_lock_timeout_seconds() == DEFAULT_SESSIONS_LOCK_TIMEOUT_S

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("5", 5.0), ("0.25", 0.25), (" 12 ", 12.0), ("0", 0.0)],
    )
    def test_valid_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)

        assert sessions_lock_timeout_seconds() == expected

    @pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf", "-inf", "1,5"])
    def test_invalid_values_fall_back_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        raw: str,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)
        caplog.set_level(logging.WARNING, logger="cw._flock")

        value = sessions_lock_timeout_seconds()

        assert value == DEFAULT_SESSIONS_LOCK_TIMEOUT_S
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert SESSIONS_LOCK_TIMEOUT_ENV in warnings[0].getMessage()
        assert repr(raw) in warnings[0].getMessage()

    def test_bad_value_warns_once_across_calls_and_again_for_a_new_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="cw._flock")
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "banana")

        results = [sessions_lock_timeout_seconds() for _ in range(5)]

        assert results == [DEFAULT_SESSIONS_LOCK_TIMEOUT_S] * 5
        banana = [r for r in caplog.records if "banana" in r.getMessage()]
        assert len(banana) == 1
        assert banana[0].levelno == logging.WARNING

        # The environment is still read on every call: a different bad value
        # warns for itself, and a good value takes effect immediately.
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "mango")
        assert sessions_lock_timeout_seconds() == DEFAULT_SESSIONS_LOCK_TIMEOUT_S
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "7")
        assert sessions_lock_timeout_seconds() == 7.0
        assert len([r for r in caplog.records if "mango" in r.getMessage()]) == 1
        assert len(caplog.records) == 2


class TestSessionsLockTimeoutMessage:
    def test_names_path_holder_discovery_wedge_remedy_and_knob_in_order(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / ".sessions.lock"

        message = sessions_lock_timeout_message(lock, waited_s=60.04, timeout_s=60.0)

        assert "Timed out after 60.0s" in message
        assert str(lock) in message
        assert "cw dev-queue serve" in message
        assert f"lsof {lock}" in message
        assert "records no PID" in message
        assert "dispatch.tick" in message
        assert "wedged" in message
        assert "restart" in message
        assert f"{SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently 60)" in message
        # Incident order: wedge guidance precedes the retry/raise-the-wait hatch.
        assert message.index("wedged") < message.index("merely slow")
        assert message.index("lsof") < message.index("wedged")


class TestAcquireSessionsFlock:
    def test_unbounded_uses_a_plain_blocking_lock_ex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default mode never polls: one blocking ``LOCK_EX``, env ignored."""
        calls: list[int] = []

        def _record(_fd: object, operation: int) -> None:
            calls.append(operation)

        monkeypatch.setattr(_flock, "fcntl", _fake_fcntl(_record))
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquire_sessions_flock(fd, lock, bounded=False)

        assert calls == [fcntl.LOCK_EX]

    def test_bounded_timeout_raises_with_path_wait_and_no_log_duplicate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, str(_TIMEOUT_S))
        caplog.set_level(logging.DEBUG, logger="cw._flock")
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(SessionsLockTimeoutError) as exc_info,
        ):
            acquire_sessions_flock(fd, lock, bounded=True)

        err = exc_info.value
        assert err.lock_path == lock
        assert err.waited_s == pytest.approx(_TIMEOUT_S)
        assert str(lock) in str(err)
        # Log-and-raise duplication removed: the raiser itself never logs.
        assert caplog.records == []
        assert max(clock.sleeps) <= SESSIONS_LOCK_POLL_INTERVAL_S

    def test_bounded_free_lock_acquires_without_sleeping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquire_sessions_flock(fd, lock, bounded=True)

        assert clock.sleeps == []
