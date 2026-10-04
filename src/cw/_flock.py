"""Bounded ``flock`` polling shared by ``sessions_lock`` and ``_context_lock``.

Two callers need "take ``LOCK_EX``, but give up at a deadline" instead of a
blocking ``flock``: ``cw.config.sessions_lock(bounded=True)`` (GitHub #2491)
and ``cw._hook_context._context_lock``. They had near-identical hand-rolled
poll loops; :func:`try_flock_until` is the one loop, and the sessions-lock
timeout configuration (env var, default, poll interval, message) lives here so
``cw.config`` stays below its module-size ceiling.

Depends only on ``cw.exceptions``; importable from anywhere below ``cw.config``.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import time
from typing import TYPE_CHECKING

from cw.exceptions import SessionsLockTimeoutError

if TYPE_CHECKING:
    from pathlib import Path
    from typing import IO

_log = logging.getLogger(__name__)

# Env var bounding how long ``sessions_lock(bounded=True)`` waits for another
# cw process to release ``.sessions.lock`` (GitHub #2491). Read per
# acquisition, in seconds; ``0`` means a single non-blocking attempt (fail at
# once if held). It has NO effect on the default unbounded ``sessions_lock()``.
SESSIONS_LOCK_TIMEOUT_ENV = "CW_SESSIONS_LOCK_TIMEOUT_S"

# Default wait for a bounded acquisition: long enough to ride out the
# legitimate holders (a reconcile sweep or a spawn is seconds, a slow one tens
# of seconds), short enough that a wedged ``cw dev-queue serve`` surfaces as a
# clear error within a minute instead of the 72-minute silent hang that
# motivated #2491.
DEFAULT_SESSIONS_LOCK_TIMEOUT_S = 60.0

# Poll cadence for the sessions-lock retry loop. Short enough that a promptly
# released lock is picked up with negligible added latency, long enough that
# waiting costs no measurable CPU.
SESSIONS_LOCK_POLL_INTERVAL_S = 0.02

# Raw env values already warned about, so a typo'd knob read on every hot-path
# acquisition warns once per distinct value rather than once per call.
_warned_invalid_raw: set[str] = set()


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
    """Resolve the bounded ``sessions_lock`` wait from the environment.

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


def sessions_lock_timeout_message(
    lock_path: Path, *, waited_s: float, timeout_s: float
) -> str:
    """Build the operator-facing text for a bounded-acquire timeout.

    Ordered for an incident: who holds it, how to find them, what to do if the
    holder is wedged, and only then the retry/raise-the-wait escape hatch.
    """
    return (
        f"Timed out after {waited_s:.1f}s waiting for the sessions lock"
        f" {lock_path}. Another cw process is holding it, typically `cw"
        f" dev-queue serve`. Find the holder with `lsof {lock_path}` (the lock"
        " file records no PID). If `cw dev-queue serve` has stopped emitting"
        " `dispatch.tick` events it is wedged: restart it. If the holder is"
        " merely slow, retry, or raise the wait with"
        f" {SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently {timeout_s:g})."
    )


def acquire_sessions_flock(fd: IO[str], lock_path: Path, *, bounded: bool) -> None:
    """Take ``LOCK_EX`` on the sessions-lock *fd*.

    ``bounded=False`` blocks until acquired and never raises
    :class:`~cw.exceptions.SessionsLockTimeoutError`. ``bounded=True`` polls
    for :func:`sessions_lock_timeout_seconds` and raises the timeout error if
    another process still holds the lock at the deadline. Any ``OSError`` other
    than contention propagates unchanged.
    """
    if not bounded:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    timeout_s = sessions_lock_timeout_seconds()
    started = time.monotonic()
    if try_flock_until(
        fd, timeout_s=timeout_s, poll_interval_s=SESSIONS_LOCK_POLL_INTERVAL_S
    ):
        return
    waited_s = time.monotonic() - started
    msg = sessions_lock_timeout_message(
        lock_path, waited_s=waited_s, timeout_s=timeout_s
    )
    raise SessionsLockTimeoutError(msg, lock_path=lock_path, waited_s=waited_s)
