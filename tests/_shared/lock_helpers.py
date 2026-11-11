"""Lock test doubles: flock holders, the fake lock clock and fcntl, lock-hold
record seeding, and the recording lock path, with their fixtures. A
registered plugin; the autouse ``_lock_hold_isolation`` lives in
``autouse_isolation``. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn

import pytest

from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw._lock_hold import LockHoldRecord
from cw._lock_hold import day_path as lock_hold_day_path
from cw.config import sessions_lock_file, state_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import IO


@contextlib.contextmanager
def _hold_flock(path: Path) -> Iterator[Callable[[], None]]:
    """Hold an exclusive ``flock`` on *path* from a SEPARATE open file description.

    flock locks belong to the open file description, so a second ``open()`` in
    this same process contends exactly like another cw process would. The one
    lock-holder technique every contention test shares (the sessions lock and
    the context-file lock): create the parent dir, open, ``LOCK_EX``, release in
    ``finally``.

    Yields an idempotent ``release()`` for tests that must free the lock
    mid-wait (e.g. from a patched ``sleep``); the context exit releases too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                fcntl.flock(handle, fcntl.LOCK_UN)

        try:
            yield release
        finally:
            release()


def _assert_lock_held(path: Path) -> None:
    """Assert *path* is flocked: a second open file description cannot take it.

    The probe counterpart of :func:`_hold_flock`; every "is the lock held in
    here?" check (``sessions_lock`` body, ``cw._flock`` helpers) shares it.
    """
    with path.open("w") as probe, pytest.raises(BlockingIOError):
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


class _FakeClock:
    """Deterministic stand-in for the ``time`` module as ``cw._flock`` sees it.

    ``sleep`` records its argument and advances ``monotonic`` by exactly that
    amount, so a poll loop's behaviour is a pure function of its arguments and
    no test depends on the wall clock. ``on_sleep`` receives the 1-based sleep
    count and lets a test act mid-wait (release the lock, raise).
    """

    def __init__(self, on_sleep: Callable[[int], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


@pytest.fixture
def fake_lock_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Bound every ``cw._flock`` acquisition at 0.5s on a fake clock.

    No wall-clock waiting. Returns the clock so a caller may inspect
    ``clock.sleeps``. A module-local fixture of the same name still shadows
    this one (``tests/test_dev_queue_lock.py``, ``tests/test_cli_config_cmds.py``).
    """
    monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.5")
    clock = _FakeClock()
    monkeypatch.setattr("cw._flock.time", clock)
    return clock


def _fake_fcntl(flock_fn: Callable[..., object]) -> SimpleNamespace:
    """A ``fcntl`` stand-in whose ``flock`` is *flock_fn*, constants real."""
    return SimpleNamespace(
        flock=flock_fn,
        LOCK_EX=fcntl.LOCK_EX,
        LOCK_NB=fcntl.LOCK_NB,
        LOCK_UN=fcntl.LOCK_UN,
    )


def _raise_eio(*_args: object, **_kwargs: object) -> NoReturn:
    """A ``flock`` replacement that fails with a non-contention ``OSError``."""
    raise OSError(errno.EIO, "disk on fire")


@contextlib.contextmanager
def _hold_sessions_lock() -> Iterator[Path]:
    """Hold ``.sessions.lock`` exclusively on a second fd; yield its path (#2491).

    Resolves the path at call time via ``cw.config.sessions_lock_file()`` so it
    follows the autouse ``tmp_config_dir`` redirect.
    """
    lock_path = sessions_lock_file()
    with _hold_flock(lock_path):
        yield lock_path


@pytest.fixture
def held_sessions_lock() -> Iterator[Path]:
    """Fixture form of :func:`_hold_sessions_lock` for whole-test contention."""
    with _hold_sessions_lock() as lock_path:
        yield lock_path


# Defaults for one seeded lock-hold record; ``at`` is filled in at call time.
_LOCK_HOLD_DEFAULTS: dict[str, object] = {
    "lock": "sessions",
    "held_s": 1.0,
    "waited_s": 0.0,
    "caller": "tests.fake:caller",
    "pid": 1,
    "state_io_s": None,
    "state_bytes": None,
}


def _seed_lock_holds(
    *holds: Mapping[str, object], raw_lines: Sequence[str] = ()
) -> Path:
    """Append lock-hold records to the dated sidecar under ``state_dir()`` (#2637).

    Each mapping overrides :data:`_LOCK_HOLD_DEFAULTS` (``at`` defaults to now,
    UTC), is validated through ``LockHoldRecord`` and lands in the file for its
    own ``at`` day, so a record from yesterday lands in yesterday's file.
    *raw_lines* are appended verbatim to today's file (malformed lines, capped
    markers). Returns today's path.
    """
    sink = state_dir()
    sink.mkdir(parents=True, exist_ok=True)
    for hold in holds:
        record = LockHoldRecord.model_validate(
            {**_LOCK_HOLD_DEFAULTS, "at": datetime.now(UTC), **hold}
        )
        path = lock_hold_day_path(sink, record.at.astimezone(UTC).date())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
    today = lock_hold_day_path(sink, datetime.now(UTC).date())
    if raw_lines:
        with today.open("a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in raw_lines)
    return today


@pytest.fixture
def lock_hold_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """One :class:`_FakeClock` behind the seam's and the recorder's ``monotonic``.

    The hold is timed in ``cw.config`` (``_guarded_state_lock``) and the state
    I/O in ``cw._lock_hold`` (``timed_state_io``); both read ``time.monotonic``
    through their own module, so both are patched to the same clock. The
    ``cw._flock.time`` patch used for bounded waits does not reach either.
    """
    clock = _FakeClock()
    monkeypatch.setattr("cw.config.time", clock)
    monkeypatch.setattr("cw._lock_hold.time", clock)
    return clock


class _RecordingLockPath:
    """Duck-typed lock path that remembers every handle a lock opens on it.

    Hoisted from ``test_config.py`` (#2501) so every bounded lock's
    "timeout closes its fd" test shares it.
    """

    def __init__(self, real: Path) -> None:
        self._real = real
        self.handles: list[IO[str]] = []

    def open(self, mode: str) -> IO[str]:
        handle = self._real.open(mode)
        self.handles.append(handle)
        return handle

    def __str__(self) -> str:
        return str(self._real)


@pytest.fixture
def record_lock_path(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str, Path], _RecordingLockPath]:
    """Factory: patch the lock-path function *patch_target* to a recorder.

    ``record_lock_path("cw.config.clients_lock_file", clients_lock_file())``
    wraps the real path in a :class:`_RecordingLockPath`, makes the patched
    function return it, and returns the recorder.
    """

    def _install(patch_target: str, real: Path) -> _RecordingLockPath:
        recorder = _RecordingLockPath(real)
        monkeypatch.setattr(patch_target, lambda: recorder)
        return recorder

    return _install


@pytest.fixture
def recording_lock_path(
    record_lock_path: Callable[[str, Path], _RecordingLockPath],
) -> _RecordingLockPath:
    """The sessions-lock form of :func:`record_lock_path`."""
    return record_lock_path("cw.config.sessions_lock_file", sessions_lock_file())
