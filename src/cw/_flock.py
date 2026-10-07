"""Bounded ``flock`` acquisition shared by the state locks and ``_context_lock``.

Two kinds of caller need "take ``LOCK_EX``, but give up at a deadline" instead
of a blocking ``flock``: the state locks' opt-in ``bounded=True`` mode (GitHub
#2491, #2501) and ``cw._hook_context._context_lock``. :func:`try_flock_until`
is the one poll loop; the bounded-acquire configuration (env var, default,
poll interval, message) lives here so ``cw.config`` stays below its
module-size ceiling.

The bounded-acquisition rule (#2501):

- Which locks accept ``bounded``: the STATE locks an operator command can meet
  while a holder is wedged -- ``sessions`` (``cw.config.sessions_lock``,
  through :func:`acquire_sessions_flock`), ``dev_queue``
  (``cw.dev_queue.storage._lock``), ``clients`` and ``concurrency_override``
  (``cw.config``), the last three through :func:`acquire_flock`. The default
  is unbounded everywhere; only operator entry points with no irreversible
  side effect before the lock pass ``bounded=True`` (pinned by the allowlist
  test in ``tests/test_config.py``).
- Which locks deliberately do NOT: the LEAF locks (the events inbox,
  ``cw.events._inbox_lock``, and the per-session inbox,
  ``cw.session_inbox._inbox_lock``) and ``cw.dispatch_state.dispatch_state_lock``.
  None has an operator-during-wedge caller. A LEAF holder acquires nothing
  while it holds the lock and does only plain file I/O, so it cannot be stuck
  waiting on another lock (the #2491 mechanism). ``dispatch_state_lock`` is
  reached only from the dispatch loop, gating probes, executors and
  reconcile. Adding the parameter would thread ``bounded`` through unattended
  readers and ``record_event`` for no operator benefit.
- ``CW_SESSIONS_LOCK_TIMEOUT_S`` (historic name) governs EVERY bounded lock.
- Each bounded acquisition waits up to that timeout on its own, never one
  bound per command. A command that nests two bounded locks (``cw spawn
  complete``, ``cw dev-queue unblock``, ``cw lane rm``) holds the outer lock
  while it waits on the inner one, so it can wait up to 2x the timeout in
  total; the timeout error reports the wait of the acquisition that failed.

Depends only on ``cw.exceptions``; importable from anywhere below ``cw.config``.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import time
from typing import TYPE_CHECKING, NamedTuple

from cw.exceptions import LockTimeoutError, SessionsLockTimeoutError

if TYPE_CHECKING:
    from pathlib import Path
    from typing import IO

_log = logging.getLogger(__name__)

# Env var bounding how long a ``bounded=True`` state-lock acquisition (the
# sessions, dev_queue, clients and concurrency_override locks) waits for
# another cw process to release the lock (GitHub #2491, #2501). The name is
# historic: it governs every bounded lock, each acquisition on its own. Read
# per acquisition, in seconds; ``0`` means a single non-blocking attempt (fail
# at once if held). It has NO effect on the default unbounded acquisitions.
SESSIONS_LOCK_TIMEOUT_ENV = "CW_SESSIONS_LOCK_TIMEOUT_S"

# Default wait for a bounded acquisition: long enough to ride out the
# legitimate holders (a reconcile sweep or a spawn is seconds, a slow one tens
# of seconds), short enough that a wedged ``cw dev-queue serve`` surfaces as a
# clear error within a minute instead of the 72-minute silent hang that
# motivated #2491. Applies per bounded lock.
DEFAULT_SESSIONS_LOCK_TIMEOUT_S = 60.0

# Poll cadence for the bounded retry loop. Short enough that a promptly
# released lock is picked up with negligible added latency, long enough that
# waiting costs no measurable CPU.
SESSIONS_LOCK_POLL_INTERVAL_S = 0.02

# Locks ``cw dev-queue serve`` holds across a reconcile/dispatch pass, so a
# timeout on one of them gets the "typically serve / restart it if wedged"
# advice. Other locks get only the generic holder guidance.
SERVE_HELD_LOCKS = frozenset({"sessions", "dev_queue"})

# Raw env values already warned about, so a typo'd knob read on every hot-path
# acquisition warns once per distinct value rather than once per call.
_warned_invalid_raw: set[str] = set()


class _Timeout(NamedTuple):
    """A bounded acquisition that gave up: its own wait and operator text."""

    waited_s: float
    message: str


def try_flock_until(fd: IO[str], *, timeout_s: float, poll_interval_s: float) -> bool:
    """Take ``LOCK_EX`` on *fd*, polling non-blockingly until *timeout_s* elapses.

    Returns ``True`` once the lock is held, ``False`` when another open file
    description still holds it at the deadline. The first attempt is immediate
    (a free lock, or ``timeout_s == 0``, costs no sleep) and each sleep is
    clamped to the time remaining so the wait never overshoots the deadline by
    a poll interval. Only :class:`BlockingIOError` means contention; any other
    ``OSError`` propagates unchanged.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_interval_s, remaining))
        else:
            return True


def sessions_lock_timeout_seconds() -> float:
    """Resolve the bounded state-lock wait from the environment.

    Unset or empty -> :data:`DEFAULT_SESSIONS_LOCK_TIMEOUT_S`. ``0`` is valid
    and means "try once, never wait". A value that is not a finite,
    non-negative number is ignored (warning once per distinct raw value) and
    the default applies: this runs on hot paths (every ``cw list``, every
    dispatch tick), where raising over a typo'd knob would be worse than the
    default bound. The environment is read on every call.
    """
    raw = os.environ.get(SESSIONS_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SESSIONS_LOCK_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and value >= 0:
        return value
    if raw not in _warned_invalid_raw:
        _warned_invalid_raw.add(raw)
        _log.warning(
            "ignoring invalid %s=%r (expected a non-negative number of seconds);"
            " using the default of %ss",
            SESSIONS_LOCK_TIMEOUT_ENV,
            raw,
            DEFAULT_SESSIONS_LOCK_TIMEOUT_S,
        )
    return DEFAULT_SESSIONS_LOCK_TIMEOUT_S


def lock_timeout_message(
    lock_name: str, lock_path: Path, *, waited_s: float, timeout_s: float
) -> str:
    """Build the operator-facing text for a bounded-acquire timeout on *lock_name*.

    Ordered for an incident: who holds it, how to find them, what to do if the
    holder is wedged, and only then the retry/raise-the-wait escape hatch. The
    ``cw dev-queue serve`` holder hint and wedge sentence appear only for
    :data:`SERVE_HELD_LOCKS`. *waited_s* is this acquisition's own wait (each
    bounded lock waits up to *timeout_s* on its own).
    """
    serve_held = lock_name in SERVE_HELD_LOCKS
    holder_hint = ", typically `cw dev-queue serve`" if serve_held else ""
    wedge_advice = (
        " If `cw dev-queue serve` has stopped emitting `dispatch.tick` events it"
        " is wedged: restart it."
        if serve_held
        else ""
    )
    return (
        f"Timed out after {waited_s:.1f}s waiting for the {lock_name} lock"
        f" {lock_path}. Another cw process is holding it{holder_hint}. Find the"
        f" holder with `lsof {lock_path}` (the lock file records no PID)."
        f"{wedge_advice} If the holder is merely slow, retry, or raise the wait"
        f" with {SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently {timeout_s:g})."
    )


def _poll_for_lock(fd: IO[str], lock_path: Path, lock_name: str) -> _Timeout | None:
    """Poll for ``LOCK_EX`` up to the configured timeout.

    Returns ``None`` once acquired, else the timeout's own wait and message.
    The clock starts here, so a nested acquisition reports only its own wait.
    """
    timeout_s = sessions_lock_timeout_seconds()
    started = time.monotonic()
    if try_flock_until(
        fd, timeout_s=timeout_s, poll_interval_s=SESSIONS_LOCK_POLL_INTERVAL_S
    ):
        return None
    waited_s = time.monotonic() - started
    message = lock_timeout_message(
        lock_name, lock_path, waited_s=waited_s, timeout_s=timeout_s
    )
    return _Timeout(waited_s, message)


def acquire_flock(
    fd: IO[str], lock_path: Path, *, lock_name: str, bounded: bool
) -> None:
    """Take ``LOCK_EX`` on the state-lock *fd* named *lock_name*.

    ``bounded=False`` blocks until acquired and never raises
    :class:`~cw.exceptions.LockTimeoutError`. ``bounded=True`` polls for
    :func:`sessions_lock_timeout_seconds` and raises the timeout error if
    another process still holds the lock at the deadline. Any ``OSError`` other
    than contention propagates unchanged.
    """
    if not bounded:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    timeout = _poll_for_lock(fd, lock_path, lock_name)
    if timeout is None:
        return
    raise LockTimeoutError(
        timeout.message,
        lock_name=lock_name,
        lock_path=lock_path,
        waited_s=timeout.waited_s,
    )


def acquire_sessions_flock(fd: IO[str], lock_path: Path, *, bounded: bool) -> None:
    """Take ``LOCK_EX`` on the sessions-lock *fd*.

    As :func:`acquire_flock` for the ``sessions`` lock, raising the
    :class:`~cw.exceptions.SessionsLockTimeoutError` subclass on a bounded
    timeout (the type the dispatch tick and the reap paths catch).
    """
    if not bounded:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    timeout = _poll_for_lock(fd, lock_path, "sessions")
    if timeout is None:
        return
    raise SessionsLockTimeoutError(
        timeout.message, lock_path=lock_path, waited_s=timeout.waited_s
    )
