"""Tests for ``cw._flock``: the shared bounded-flock poll helper (GitHub #2491).

``try_flock_until`` is exercised against a REAL contended lock (a second open
file description) with a fake monotonic clock, so the deadline arithmetic is
pinned without wall-clock thresholds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import pytest

import cw.config
from cw import _flock
from cw._flock import (
    DEFAULT_SESSIONS_LOCK_TIMEOUT_S,
    SERVE_HELD_LOCKS,
    SESSIONS_LOCK_POLL_INTERVAL_S,
    SESSIONS_LOCK_TIMEOUT_ENV,
    acquire_flock,
    acquire_sessions_flock,
    lock_timeout_message,
    sessions_lock_timeout_seconds,
    try_flock_until,
)
from cw._lock_guard import held_locks
from cw.config import clients_lock, concurrency_override_lock
from cw.dev_queue import dev_queue_lock
from cw.exceptions import CwError, LockTimeoutError, SessionsLockTimeoutError
from tests.conftest import (
    _assert_lock_held,
    _fake_fcntl,
    _FakeClock,
    _hold_flock,
    _raise_eio,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import IO

    from tests.conftest import _RecordingLockPath

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


def _assert_bounded_lock_times_out(lock: _StateLock) -> None:
    with lock.acquire(bounded=True):
        pytest.fail("must not reach body")


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


class TestLockTimeoutError:
    """The generic bounded-lock error and its sessions subclass (#2501)."""

    def test_generic_error_carries_lock_name_path_and_wait(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / "x.lock"

        err = LockTimeoutError(
            "boom", lock_name="clients", lock_path=lock, waited_s=1.5
        )

        assert isinstance(err, CwError)
        assert type(err) is LockTimeoutError
        assert str(err) == "boom"
        assert err.lock_name == "clients"
        assert err.lock_path == lock
        assert err.waited_s == 1.5

    def test_sessions_error_is_a_subclass_with_its_old_constructor(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / ".sessions.lock"

        err = SessionsLockTimeoutError("boom", lock_path=lock, waited_s=2.0)

        assert isinstance(err, LockTimeoutError)
        assert isinstance(err, CwError)
        assert err.lock_name == "sessions"
        assert err.lock_path == lock
        assert err.waited_s == 2.0


# Golden literal captured from the pre-#2501 ``sessions_lock_timeout_message``
# for ``waited_s=60.04, timeout_s=60.0`` and path ``P``: the sessions text must
# stay byte-identical across the rename.
_SESSIONS_GOLDEN = (
    "Timed out after 60.0s waiting for the sessions lock P. Another cw process"
    " is holding it, typically `cw dev-queue serve`. Find the holder with"
    " `lsof P` (the lock file records no PID). If `cw dev-queue serve` has"
    " stopped emitting `dispatch.tick` events it is wedged: restart it. If the"
    " holder is merely slow, retry, or raise the wait with"
    " CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently 60)."
)
_DEV_QUEUE_EXPECTED = (
    "Timed out after 60.0s waiting for the dev_queue lock P. Another cw process"
    " is holding it, typically `cw dev-queue serve`. Find the holder with"
    " `lsof P` (the lock file records no PID). If `cw dev-queue serve` has"
    " stopped emitting `dispatch.tick` events it is wedged: restart it. If the"
    " holder is merely slow, retry, or raise the wait with"
    " CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently 60)."
)
_CLIENTS_EXPECTED = (
    "Timed out after 60.0s waiting for the clients lock P. Another cw process"
    " is holding it. Find the holder with `lsof P` (the lock file records no"
    " PID). If the holder is merely slow, retry, or raise the wait with"
    " CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently 60)."
)
_CONCURRENCY_OVERRIDE_EXPECTED = (
    "Timed out after 60.0s waiting for the concurrency_override lock P. Another"
    " cw process is holding it. Find the holder with `lsof P` (the lock file"
    " records no PID). If the holder is merely slow, retry, or raise the wait"
    " with CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently 60)."
)


class TestLockTimeoutMessage:
    @pytest.mark.parametrize(
        ("lock_name", "expected"),
        [
            ("sessions", _SESSIONS_GOLDEN),
            ("dev_queue", _DEV_QUEUE_EXPECTED),
            ("clients", _CLIENTS_EXPECTED),
            ("concurrency_override", _CONCURRENCY_OVERRIDE_EXPECTED),
        ],
    )
    def test_exact_text_per_lock(self, lock_name: str, expected: str) -> None:
        message = lock_timeout_message(
            lock_name, Path("P"), waited_s=60.04, timeout_s=60.0
        )

        assert message == expected

    def test_serve_advice_only_for_locks_serve_holds(self) -> None:
        assert frozenset({"sessions", "dev_queue"}) == SERVE_HELD_LOCKS
        for lock_name in ("sessions", "dev_queue", "clients", "concurrency_override"):
            message = lock_timeout_message(
                lock_name, Path("P"), waited_s=1.0, timeout_s=2.0
            )
            serve_held = lock_name in SERVE_HELD_LOCKS
            for needle in ("cw dev-queue serve", "dispatch.tick", "wedged"):
                assert (needle in message) is serve_held, (lock_name, needle)
            assert f"the {lock_name} lock P" in message
            assert "`lsof P`" in message
            assert f"{SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently 2)" in message

    def test_names_path_holder_discovery_wedge_remedy_and_knob_in_order(
        self, tmp_path: Path
    ) -> None:
        lock = tmp_path / ".sessions.lock"

        message = lock_timeout_message("sessions", lock, waited_s=60.04, timeout_s=60.0)

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


class TestAcquireFlock:
    """The generic named-lock acquire helper (#2501)."""

    def test_unbounded_uses_a_plain_blocking_lock_ex_and_ignores_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _record(_fd: object, operation: int) -> None:
            calls.append(operation)

        monkeypatch.setattr(_flock, "fcntl", _fake_fcntl(_record))
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquire_flock(fd, lock, lock_name="clients", bounded=False)

        assert calls == [fcntl.LOCK_EX]

    def test_bounded_timeout_raises_generic_error_with_name_and_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, str(_TIMEOUT_S))
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(LockTimeoutError) as exc_info,
        ):
            acquire_flock(fd, lock, lock_name="clients", bounded=True)

        err = exc_info.value
        assert type(err) is LockTimeoutError
        assert err.lock_name == "clients"
        assert err.lock_path == lock
        assert err.waited_s == pytest.approx(_TIMEOUT_S)
        assert "the clients lock" in str(err)
        assert max(clock.sleeps) <= SESSIONS_LOCK_POLL_INTERVAL_S

    def test_bounded_free_lock_acquires_without_sleeping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquire_flock(fd, lock, lock_name="dev_queue", bounded=True)
            _assert_lock_held(lock)

        assert clock.sleeps == []

    def test_bounded_zero_timeout_fails_at_once_when_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(LockTimeoutError) as exc_info,
        ):
            acquire_flock(fd, lock, lock_name="dev_queue", bounded=True)

        assert clock.sleeps == []
        assert exc_info.value.waited_s == 0

    @pytest.mark.parametrize("bounded", [False, True], ids=["default", "bounded"])
    def test_non_contention_oserror_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounded: bool
    ) -> None:
        monkeypatch.setattr(_flock, "fcntl", _fake_fcntl(_raise_eio))

        with (
            (tmp_path / "lock").open("w") as fd,
            pytest.raises(OSError, match="disk on fire"),
        ):
            acquire_flock(fd, tmp_path / "lock", lock_name="x", bounded=bounded)


class _StateLock(NamedTuple):
    """One bounded STATE lock under test: its context manager and path seam."""

    name: str
    acquire: Callable[..., contextlib.AbstractContextManager[None]]
    path: Callable[[], Path]
    patch_target: str


_STATE_LOCKS = [
    _StateLock(
        "dev_queue",
        dev_queue_lock,
        cw.config.dev_queue_lock,
        "cw.dev_queue.storage._dev_queue_lock_file",
    ),
    _StateLock(
        "clients",
        clients_lock,
        cw.config.clients_lock_file,
        "cw.config.clients_lock_file",
    ),
    _StateLock(
        "concurrency_override",
        concurrency_override_lock,
        cw.config.concurrency_override_lock_file,
        "cw.config.concurrency_override_lock_file",
    ),
]

# Dyadic so the fake clock lands on it exactly and ``:.1f`` / ``:g`` render it
# without rounding ambiguity.
_STATE_LOCK_TIMEOUT_S = 0.5


def _expected_state_lock_message(lock_name: str, lock_path: str) -> str:
    serve = lock_name in {"sessions", "dev_queue"}
    holder = ", typically `cw dev-queue serve`" if serve else ""
    wedge = (
        " If `cw dev-queue serve` has stopped emitting `dispatch.tick` events it"
        " is wedged: restart it."
        if serve
        else ""
    )
    return (
        f"Timed out after 0.5s waiting for the {lock_name} lock {lock_path}."
        f" Another cw process is holding it{holder}. Find the holder with"
        f" `lsof {lock_path}` (the lock file records no PID).{wedge} If the"
        " holder is merely slow, retry, or raise the wait with"
        " CW_SESSIONS_LOCK_TIMEOUT_S (seconds; currently 0.5)."
    )


@pytest.mark.parametrize("lock", _STATE_LOCKS, ids=[lk.name for lk in _STATE_LOCKS])
class TestBoundedStateLocks:
    """dev_queue, clients and concurrency_override accept ``bounded`` (#2501)."""

    def test_bounded_times_out_with_exact_message_and_skips_body(
        self, lock: _StateLock, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, str(_STATE_LOCK_TIMEOUT_S))
        _install_clock(monkeypatch, _FakeClock())
        lock_path = lock.path()

        with _hold_flock(lock_path), pytest.raises(LockTimeoutError) as exc_info:
            _assert_bounded_lock_times_out(lock)

        err = exc_info.value
        assert type(err) is LockTimeoutError
        assert err.lock_name == lock.name
        assert str(err.lock_path) == str(lock_path)
        assert err.waited_s == pytest.approx(_STATE_LOCK_TIMEOUT_S)
        assert str(err) == _expected_state_lock_message(lock.name, str(lock_path))
        assert held_locks() == ()

    def test_timeout_closes_every_handle_and_lock_stays_usable(
        self,
        lock: _StateLock,
        record_lock_path: Callable[[str, Path], _RecordingLockPath],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = lock.path()
        recorder = record_lock_path(lock.patch_target, real)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")

        with _hold_flock(real), pytest.raises(LockTimeoutError):
            _assert_bounded_lock_times_out(lock)

        assert [h.closed for h in recorder.handles] == [True]
        with lock.acquire(bounded=True):
            _assert_lock_held(real)

    def test_default_waits_out_the_holder_despite_a_tiny_env_timeout(
        self, lock: _StateLock, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commit-after-side-effect callers rely on the unbounded default."""
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.01")
        fired = threading.Event()

        with _hold_flock(lock.path()) as release:

            def _fire() -> None:
                fired.set()
                release()

            timer = threading.Timer(0.2, _fire)
            timer.start()
            try:
                with lock.acquire():
                    released_before_entry = fired.is_set()
            finally:
                timer.cancel()

        assert released_before_entry

    @pytest.mark.parametrize("bounded", [False, True], ids=["default", "bounded"])
    def test_non_contention_oserror_propagates_and_closes_fd(
        self,
        lock: _StateLock,
        record_lock_path: Callable[[str, Path], _RecordingLockPath],
        monkeypatch: pytest.MonkeyPatch,
        bounded: bool,
    ) -> None:
        recorder = record_lock_path(lock.patch_target, lock.path())

        with monkeypatch.context() as patch_ctx:
            patch_ctx.setattr(_flock, "fcntl", _fake_fcntl(_raise_eio))
            with (
                pytest.raises(OSError, match="disk on fire"),
                lock.acquire(bounded=bounded),
            ):
                pytest.fail("must not reach body")

        assert [h.closed for h in recorder.handles] == [True]
        assert held_locks() == ()


# ---------------------------------------------------------------------------
# #2694: operator-command waits, explicit timeout_s, holder record
# ---------------------------------------------------------------------------

_INVALID_RAWS = ["abc", "-1", "nan", "inf", "1,5"]


class TestReadEnvLockTimeout:
    """The shared env parse: ``None`` means "not set to a usable value"."""

    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SESSIONS_LOCK_TIMEOUT_ENV, raising=False)

        assert _flock._read_env_lock_timeout() is None

    def test_blank_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "   ")

        assert _flock._read_env_lock_timeout() is None

    @pytest.mark.parametrize(("raw", "expected"), [("0", 0.0), ("7", 7.0)])
    def test_valid_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)

        assert _flock._read_env_lock_timeout() == expected

    @pytest.mark.parametrize("raw", _INVALID_RAWS)
    def test_invalid_is_none_with_one_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        raw: str,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)
        caplog.set_level(logging.WARNING, logger="cw._flock")

        first = _flock._read_env_lock_timeout()
        second = _flock._read_env_lock_timeout()

        assert first is None
        assert second is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert SESSIONS_LOCK_TIMEOUT_ENV in warnings[0].getMessage()
        assert repr(raw) in warnings[0].getMessage()


class TestOperatorLockWaits:
    """``cw list``/``cw status`` vs ``cw spawn close``/cancel waits (#2694)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, 5.0),
            ("  ", 5.0),
            ("abc", 5.0),
            ("7", 7.0),
            ("300", 300.0),
            ("0", 0.0),
        ],
    )
    def test_read_wait_env_wins_else_five_seconds(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
    ) -> None:
        if raw is None:
            monkeypatch.delenv(SESSIONS_LOCK_TIMEOUT_ENV, raising=False)
        else:
            monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)

        assert _flock.operator_read_lock_wait_seconds() == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, 5.0),
            ("abc", 5.0),
            ("0.2", 0.2),
            ("7", 5.0),
            ("300", 5.0),
            ("0", 0.0),
        ],
    )
    def test_close_wait_is_capped_at_five_seconds(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
    ) -> None:
        if raw is None:
            monkeypatch.delenv(SESSIONS_LOCK_TIMEOUT_ENV, raising=False)
        else:
            monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, raw)

        assert _flock.operator_close_lock_wait_seconds() == expected

    def test_default_constant_is_five_seconds(self) -> None:
        assert _flock.OPERATOR_COMMAND_LOCK_WAIT_S == 5.0


class TestExplicitTimeout:
    """An explicit ``timeout_s`` overrides the env for one acquisition."""

    def test_explicit_timeout_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "60")
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(SessionsLockTimeoutError) as exc_info,
        ):
            acquire_sessions_flock(fd, lock, bounded=True, timeout_s=5.0)

        assert clock.now == 5.0
        assert exc_info.value.waited_s == pytest.approx(5.0)
        assert "(seconds; currently 5)" in str(exc_info.value)

    def test_zero_is_one_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(SessionsLockTimeoutError),
        ):
            acquire_sessions_flock(fd, lock, bounded=True, timeout_s=0)

        assert clock.sleeps == []

    def test_none_falls_back_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        _install_clock(monkeypatch, clock)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, str(_TIMEOUT_S))
        lock = tmp_path / "lock"

        with (
            _hold_flock(lock),
            lock.open("w") as fd,
            pytest.raises(SessionsLockTimeoutError),
        ):
            acquire_sessions_flock(fd, lock, bounded=True, timeout_s=None)

        assert clock.now == _TIMEOUT_S

    def test_unbounded_ignores_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _record(_fd: object, operation: int) -> None:
            calls.append(operation)

        monkeypatch.setattr(_flock, "fcntl", _fake_fcntl(_record))
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            acquire_sessions_flock(fd, lock, bounded=False, timeout_s=0)

        assert calls == [fcntl.LOCK_EX]


def _live_holder_json(pid: int, *, cmd: str = "cw list") -> str:
    acquired = datetime.now(UTC) - timedelta(seconds=12)
    return json.dumps({"pid": pid, "cmd": cmd, "acquired_at": acquired.isoformat()})


class TestHolderRecord:
    """The sessions-lock holder record (#2694): best effort, never a failure."""

    def test_record_writes_pid_cmd_and_iso_acquired_at(self, tmp_path: Path) -> None:
        lock = tmp_path / "lock"

        with lock.open("a+") as fd:
            fd.write("stale previous content")
            fd.flush()
            _flock.record_lock_holder(fd, cmd="cw spawn close")

        data = json.loads(lock.read_text())
        assert set(data) == {"pid", "cmd", "acquired_at"}
        assert data["pid"] == os.getpid()
        assert data["cmd"] == "cw spawn close"
        assert datetime.fromisoformat(data["acquired_at"]).tzinfo is not None

    def test_record_needs_no_wall_clock_time_function(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tests replace ``cw._flock.time`` with a clock that has no ``time()``."""
        _install_clock(monkeypatch, _FakeClock())
        lock = tmp_path / "lock"

        with lock.open("a+") as fd:
            _flock.record_lock_holder(fd, cmd="cw list")

        assert json.loads(lock.read_text())["cmd"] == "cw list"

    def test_clear_empties_the_file(self, tmp_path: Path) -> None:
        lock = tmp_path / "lock"

        with lock.open("a+") as fd:
            _flock.record_lock_holder(fd, cmd="cw list")
            _flock.clear_lock_holder(fd)

        assert lock.read_text() == ""

    def test_read_returns_the_live_holder(self, tmp_path: Path) -> None:
        lock = tmp_path / "lock"
        lock.write_text(_live_holder_json(os.getpid()))

        with lock.open("a+") as fd:
            holder = _flock.read_lock_holder(fd)

        assert holder is not None
        assert holder.pid == os.getpid()
        assert holder.cmd == "cw list"

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "{not json",
            "[1, 2]",
            json.dumps({"pid": "12", "cmd": "cw", "acquired_at": "2026-01-01"}),
            json.dumps({"pid": True, "cmd": "cw", "acquired_at": "2026-01-01"}),
            json.dumps({"pid": 12, "cmd": 3, "acquired_at": "2026-01-01"}),
            json.dumps({"pid": 12, "cmd": "cw", "acquired_at": "yesterday"}),
            json.dumps({"pid": 12, "cmd": "cw"}),
        ],
        ids=[
            "empty",
            "malformed",
            "not-an-object",
            "pid-str",
            "pid-bool",
            "cmd-int",
            "bad-timestamp",
            "missing-key",
        ],
    )
    def test_read_returns_none_for_unusable_content(
        self, tmp_path: Path, content: str
    ) -> None:
        lock = tmp_path / "lock"
        lock.write_text(content)

        with lock.open("a+") as fd:
            assert _flock.read_lock_holder(fd) is None

    @pytest.mark.parametrize(
        "record",
        [
            {"pid": os.getpid(), "cmd": "cw", "acquired_at": "2026-10-01T12:00:00"},
            {"pid": 0, "cmd": "cw", "acquired_at": "2026-10-01T12:00:00+00:00"},
            {"pid": -5, "cmd": "cw", "acquired_at": "2026-10-01T12:00:00+00:00"},
            {"pid": 2**70, "cmd": "cw", "acquired_at": "2026-10-01T12:00:00+00:00"},
        ],
        ids=["naive-timestamp", "pid-zero", "pid-negative", "pid-overflows-os-kill"],
    )
    def test_read_treats_an_unverifiable_record_as_no_holder(
        self, tmp_path: Path, record: dict[str, object]
    ) -> None:
        """A naive timestamp or a pid ``os.kill`` cannot probe is an unknown holder."""
        lock = tmp_path / "lock"
        lock.write_text(json.dumps(record))

        with lock.open("a+") as fd:
            assert _flock.read_lock_holder(fd) is None

    def test_naive_holder_timestamp_never_breaks_the_timeout_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch, _FakeClock())
        lock = tmp_path / ".sessions.lock"
        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=3)
        lock.write_text(
            json.dumps(
                {"pid": os.getpid(), "cmd": "cw list", "acquired_at": naive.isoformat()}
            )
        )

        with (
            _hold_flock_append(lock),
            lock.open("a+") as fd,
            pytest.raises(SessionsLockTimeoutError) as exc_info,
        ):
            acquire_sessions_flock(fd, lock, bounded=True, timeout_s=0)

        assert "the lock file records no PID" in str(exc_info.value)
        assert "Recorded holder" not in str(exc_info.value)

    def test_read_returns_none_for_a_write_only_fd(self, tmp_path: Path) -> None:
        lock = tmp_path / "lock"

        with lock.open("w") as fd:
            assert _flock.read_lock_holder(fd) is None

    def test_read_returns_none_for_a_dead_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _gone(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(_flock.os, "kill", _gone)
        lock = tmp_path / "lock"
        lock.write_text(_live_holder_json(424242))

        with lock.open("a+") as fd:
            assert _flock.read_lock_holder(fd) is None

    def test_permission_error_counts_as_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _other_user(_pid: int, _sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(_flock.os, "kill", _other_user)
        lock = tmp_path / "lock"
        lock.write_text(_live_holder_json(1))

        with lock.open("a+") as fd:
            holder = _flock.read_lock_holder(fd)

        assert holder is not None
        assert holder.pid == 1

    @pytest.mark.parametrize(
        "error",
        [OSError(5, "disk on fire"), ValueError("I/O operation on closed file")],
        ids=["oserror", "closed-fd-valueerror"],
    )
    def test_record_and_clear_swallow_io_errors_with_a_debug_log(
        self, caplog: pytest.LogCaptureFixture, error: Exception
    ) -> None:
        class _BrokenFd:
            def seek(self, _offset: int) -> int:
                raise error

        caplog.set_level(logging.DEBUG, logger="cw._flock")
        broken = cast("IO[str]", _BrokenFd())

        _flock.record_lock_holder(broken, cmd="cw list")
        _flock.clear_lock_holder(broken)

        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug) == 2
        assert all(r.exc_info is not None for r in debug)


class TestHolderInTimeoutMessage:
    def test_message_names_the_recorded_holder(self) -> None:
        holder = _flock.LockHolder(
            pid=4242,
            cmd="cw dev-queue serve",
            acquired_at=datetime.now(UTC) - timedelta(seconds=30),
        )

        message = lock_timeout_message(
            "sessions", Path("P"), waited_s=5.0, timeout_s=5.0, holder=holder
        )

        assert "Recorded holder: pid 4242 (`cw dev-queue serve`), held for" in message
        assert "confirm with `lsof P`" in message
        assert "records no PID" not in message
        assert "cw dev-queue serve" in message
        assert "wedged" in message
        assert SESSIONS_LOCK_TIMEOUT_ENV in message

    def test_no_holder_is_byte_identical(self) -> None:
        message = lock_timeout_message(
            "sessions", Path("P"), waited_s=60.04, timeout_s=60.0, holder=None
        )

        assert message == _SESSIONS_GOLDEN

    def test_sessions_timeout_reads_the_holder_through_the_contender_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch, _FakeClock())
        lock = tmp_path / ".sessions.lock"
        lock.write_text(_live_holder_json(os.getpid(), cmd="cw status"))

        with (
            _hold_flock_append(lock),
            lock.open("a+") as fd,
            pytest.raises(SessionsLockTimeoutError) as exc_info,
        ):
            acquire_sessions_flock(fd, lock, bounded=True, timeout_s=0)

        assert f"pid {os.getpid()} (`cw status`)" in str(exc_info.value)

    def test_other_locks_never_read_a_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch, _FakeClock())
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
        lock = tmp_path / ".dev_queue.lock"
        lock.write_text(_live_holder_json(os.getpid()))

        with (
            _hold_flock_append(lock),
            lock.open("a+") as fd,
            pytest.raises(LockTimeoutError) as exc_info,
        ):
            acquire_flock(fd, lock, lock_name="dev_queue", bounded=True)

        assert "records no PID" in str(exc_info.value)
        assert "Recorded holder" not in str(exc_info.value)


@contextlib.contextmanager
def _hold_flock_append(path: Path) -> Iterator[None]:
    """Hold *path* on a second fd opened ``"a+"`` so its content survives."""
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
