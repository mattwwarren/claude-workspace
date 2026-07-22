"""Tests for the dispatch-loop singleton lock (``cw.config.dispatch_loop_lock``).

Guards #1362: only one ``run_dispatch_loop`` may run at a time against a given
``STATE_DIR``. The lock is a process-lifetime, non-blocking, holder-identifying
``fcntl.flock`` over ``DISPATCH_LOOP_LOCK``. A second acquisition fails fast
with a :class:`~cw.exceptions.DispatchLoopLockedError` naming the holder's PID
and command.
"""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import sys
import time
from typing import TYPE_CHECKING

import pytest

from cw.config import dispatch_loop_lock, dispatch_loop_lock_file
from cw.exceptions import DispatchLoopLockedError

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event as EventType


def test_second_acquisition_raises_when_held() -> None:
    """A second acquisition while the lock is held raises DispatchLoopLockedError."""
    with (
        dispatch_loop_lock(),
        pytest.raises(DispatchLoopLockedError),
        dispatch_loop_lock(),
    ):
        pass


def test_error_message_names_holder_pid_and_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raised message names the holder's os.getpid() and normalized command."""
    monkeypatch.setattr(sys, "argv", ["cw", "dev-queue", "serve", "--quiet"])
    with (
        dispatch_loop_lock(),
        pytest.raises(DispatchLoopLockedError) as exc_info,
        dispatch_loop_lock(),
    ):
        pass
    message = str(exc_info.value)
    assert str(os.getpid()) in message
    assert "cw dev-queue serve --quiet" in message


def test_lock_releases_on_normal_exit_and_is_reacquirable() -> None:
    """Normal ``with`` exit releases the lock so it can be acquired again."""
    with dispatch_loop_lock():
        pass
    # Re-acquire — must not raise.
    with dispatch_loop_lock():
        pass


def test_lock_releases_on_exception_inside_with_block() -> None:
    """An exception inside the block propagates AND still releases the lock."""
    sentinel_msg = "boom"
    with pytest.raises(RuntimeError, match=sentinel_msg), dispatch_loop_lock():
        raise RuntimeError(sentinel_msg)
    # The lock must have been released despite the exception.
    with dispatch_loop_lock():
        pass


@pytest.mark.parametrize(
    "content",
    ["", "not-json {{{", "[1, 2, 3]", '{"pid": 123}'],
)
def test_malformed_or_empty_holder_content_falls_back_to_unknown(
    content: str,
) -> None:
    """Empty/garbage/incomplete holder JSON yields 'holder unknown', no crash.

    ``'{"pid": 123}'`` (valid JSON, missing "cmd") exercises the KeyError
    branch specifically -- the shape a partial/truncated write is most
    likely to produce, since the holder's own write is seek/truncate/write,
    not atomic.
    """
    lock_path = dispatch_loop_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(content)
    fd = lock_path.open("r+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(DispatchLoopLockedError) as exc_info, dispatch_loop_lock():
            pass
        assert "holder unknown" in str(exc_info.value)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _hold_lock_forever(ready: EventType) -> None:
    """Child entrypoint: acquire the lock, signal readiness, then block."""
    with dispatch_loop_lock():
        ready.set()
        time.sleep(30)


def test_sigkill_leaves_no_stale_lock() -> None:
    """A SIGKILLed holder leaves no stale lock — the kernel releases flock on death.

    This is the one acceptance-criterion test that genuinely needs a second OS
    process: flock is released when a process's fd table is torn down, which a
    thread-based simulation cannot exercise.

    Why ``get_context("fork")`` specifically: ``spawn`` would re-import
    ``cw.config`` fresh in the child, losing the ``tmp_config_dir`` fixture's
    monkeypatch of ``DISPATCH_LOOP_LOCK`` and pointing the child at the real
    ``~/.local/share/cw`` lock path instead of the test's tmp dir. CI's matrix
    includes ``macos-latest``, where ``fork``-based multiprocessing carries
    known caveats in multi-threaded processes; this repo has no other
    ``multiprocessing`` usage yet to confirm against. pytest itself does not
    run multi-threaded by default, so no threads should be alive across this
    fork -- if this test ever flakes specifically on the macOS CI leg, that
    assumption is the first thing to revisit.
    """
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold_lock_forever, args=(ready,))
    proc.start()
    try:
        assert ready.wait(timeout=10), "child never acquired the lock"
        # The child holds the lock; a same-process acquisition must be denied.
        with pytest.raises(DispatchLoopLockedError), dispatch_loop_lock():
            pass
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.join(timeout=10)
    assert not proc.is_alive()
    # The kernel released the flock on the child's death — re-acquire freely.
    with dispatch_loop_lock():
        pass
