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
- ``CW_SESSIONS_LOCK_TIMEOUT_S`` (historic name) governs EVERY bounded lock,
  except where an operator command passes its own ``timeout_s`` (#2694):
  ``cw list``/``cw status`` wait :func:`operator_read_lock_wait_seconds` (the
  env value when set and valid, else 5 s) and ``cw spawn close`` plus the
  ``cw dev-queue cancel`` session step wait
  :func:`operator_close_lock_wait_seconds` (``min(env, 5 s)``, else 5 s).
- Each bounded acquisition waits up to that timeout on its own, never one
  bound per command. A command that nests two bounded locks (``cw spawn
  complete``, ``cw dev-queue unblock``, ``cw lane rm``) holds the outer lock
  while it waits on the inner one, so it can wait up to 2x the timeout in
  total; the timeout error reports the wait of the acquisition that failed.

Depends on ``cw.exceptions`` and ``cw._lock_guard``; importable from anywhere
below ``cw.config``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.exceptions import (
    DEV_QUEUE_LOCK_NAME,
    SESSIONS_LOCK_NAME,
    LockTimeoutError,
    SessionsLockTimeoutError,
)

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

# The operator commands' sessions-lock wait (#2694): ``cw list``/``cw status``
# (unless the env var is set) and, as a ceiling, ``cw spawn close`` and the
# ``cw dev-queue cancel`` session step. Long enough to ride out an ordinary
# reconcile pass, short enough that an operator learns of a wedged holder
# before reaching for a second terminal.
OPERATOR_COMMAND_LOCK_WAIT_S = 5.0

# Poll cadence for the bounded retry loop. Short enough that a promptly
# released lock is picked up with negligible added latency, long enough that
# waiting costs no measurable CPU.
SESSIONS_LOCK_POLL_INTERVAL_S = 0.02

# Locks ``cw dev-queue serve`` holds across a reconcile/dispatch pass, so a
# timeout on one of them gets the "typically serve / restart it if wedged"
# advice. Other locks get only the generic holder guidance.
SERVE_HELD_LOCKS = frozenset({SESSIONS_LOCK_NAME, DEV_QUEUE_LOCK_NAME})

# Raw env values already warned about, so a typo'd knob read on every hot-path
# acquisition warns once per distinct value rather than once per call.
_warned_invalid_raw: set[str] = set()


class _Timeout(NamedTuple):
    """A bounded acquisition that gave up: its own wait and operator text."""

    waited_s: float
    message: str


class LockHolder(NamedTuple):
    """The holder record a cw process writes into ``.sessions.lock`` (#2694).

    Self-reported and best effort: ``cmd`` is the Click command path (never
    option values), and a live ``pid`` proves liveness, not identity, so the
    timeout message still says to confirm with ``lsof``.
    """

    pid: int
    cmd: str
    acquired_at: datetime


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


def _read_env_lock_timeout() -> float | None:
    """Parse ``CW_SESSIONS_LOCK_TIMEOUT_S``; ``None`` when unset, blank or invalid.

    ``0`` is valid and means "try once, never wait". A value that is not a
    finite, non-negative number is ignored, warning once per distinct raw
    value: this runs on hot paths (every ``cw list``, every dispatch tick),
    where raising over a typo'd knob would be worse than a default bound. The
    environment is read on every call. Each caller picks its own default for
    ``None`` (#2694), so an invalid value never silently restores 60 s for
    the operator commands.
    """
    raw = os.environ.get(SESSIONS_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return None
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
            " using the caller's default wait",
            SESSIONS_LOCK_TIMEOUT_ENV,
            raw,
        )
    return None


def sessions_lock_timeout_seconds() -> float:
    """Resolve the bounded state-lock wait from the environment.

    The env value when set and valid, else
    :data:`DEFAULT_SESSIONS_LOCK_TIMEOUT_S` (see :func:`_read_env_lock_timeout`
    for what counts as valid). The default for every bounded caller that does
    not pass its own ``timeout_s``.
    """
    env = _read_env_lock_timeout()
    return DEFAULT_SESSIONS_LOCK_TIMEOUT_S if env is None else env


def operator_read_lock_wait_seconds() -> float:
    """The sessions-lock wait for ``cw list`` / ``cw status`` (#2694).

    An explicit, valid ``CW_SESSIONS_LOCK_TIMEOUT_S`` wins (so an operator can
    still ride out a long legitimate hold); otherwise, including an invalid
    value, :data:`OPERATOR_COMMAND_LOCK_WAIT_S`.
    """
    env = _read_env_lock_timeout()
    return OPERATOR_COMMAND_LOCK_WAIT_S if env is None else env


def operator_close_lock_wait_seconds() -> float:
    """The sessions-lock wait for ``cw spawn close`` and the cancel session step.

    ``min(CW_SESSIONS_LOCK_TIMEOUT_S, OPERATOR_COMMAND_LOCK_WAIT_S)``: a held
    lock queues the close instead of waiting (#2694), so a larger env value
    only delays the queue. Unset or invalid -> the 5 s ceiling.
    """
    env = _read_env_lock_timeout()
    if env is None:
        return OPERATOR_COMMAND_LOCK_WAIT_S
    return min(env, OPERATOR_COMMAND_LOCK_WAIT_S)


def record_lock_holder(fd: IO[str], *, cmd: str) -> None:
    """Write this process's holder record into the held lock file *fd* (#2694).

    ``{"pid", "cmd", "acquired_at"}``, replacing any earlier content. Best
    effort: an I/O failure (``OSError``, or the ``ValueError`` a closed fd
    raises) is logged at DEBUG and swallowed, because holder bookkeeping must
    never fail an acquisition. ``acquired_at`` comes from ``datetime.now``,
    not this module's ``time`` (which tests replace).
    """
    record = {
        "pid": os.getpid(),
        "cmd": cmd,
        "acquired_at": datetime.now(UTC).isoformat(),
    }
    try:
        fd.seek(0)
        fd.truncate(0)
        fd.write(json.dumps(record))
        fd.flush()
    except (OSError, ValueError):
        _log.debug("lock_holder_record_failed", exc_info=True)


def clear_lock_holder(fd: IO[str]) -> None:
    """Empty the holder record before the lock is released (best effort)."""
    try:
        fd.seek(0)
        fd.truncate(0)
        fd.flush()
    except (OSError, ValueError):
        _log.debug("lock_holder_clear_failed", exc_info=True)


def _parse_aware_time(raw: object) -> datetime | None:
    """*raw* as a timezone-aware datetime, or ``None`` (naive or unparseable)."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive value cannot be subtracted from the aware "now" the timeout
    # message uses, so it is as unusable as an unparseable one.
    return parsed if parsed.utcoffset() is not None else None


def _parse_lock_holder(raw: str) -> LockHolder | None:
    """Parse a holder record, or ``None`` when it is empty or unusable.

    Unusable: malformed JSON, a wrong-typed field, a pid that is not a
    positive int (``os.kill`` reads 0 and negatives as process groups), or an
    ``acquired_at`` that is not a timezone-aware ISO-8601 timestamp.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    cmd = data.get("cmd")
    if type(pid) is not int or pid <= 0 or not isinstance(cmd, str):
        return None
    acquired_at = _parse_aware_time(data.get("acquired_at"))
    if acquired_at is None:
        return None
    return LockHolder(pid=pid, cmd=cmd, acquired_at=acquired_at)


def _pid_alive(pid: int) -> bool:
    """True when *pid* names a live process (``PermissionError`` counts).

    A pid too large for ``os.kill`` (``OverflowError``) is not alive: the
    timeout path that calls this must never raise.
    """
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def read_lock_holder(fd: IO[str]) -> LockHolder | None:
    """Read the holder record through a contender's own *fd* (#2694).

    The fd must be readable (the sessions lock opens ``"a+"``); a write-only
    fd, an empty or malformed record, or a record whose pid is no longer
    alive all yield ``None``, and the caller falls back to the ``lsof`` text.
    """
    try:
        fd.seek(0)
        raw = fd.read()
    except (OSError, ValueError):
        return None
    holder = _parse_lock_holder(raw)
    if holder is None or not _pid_alive(holder.pid):
        return None
    return holder


def lock_timeout_message(
    lock_name: str,
    lock_path: Path,
    *,
    waited_s: float,
    timeout_s: float,
    holder: LockHolder | None = None,
) -> str:
    """Build the operator-facing text for a bounded-acquire timeout on *lock_name*.

    Ordered for an incident: who holds it, how to find them, what to do if the
    holder is wedged, and only then the retry/raise-the-wait escape hatch. The
    ``cw dev-queue serve`` holder hint and wedge sentence appear only for
    :data:`SERVE_HELD_LOCKS`. *waited_s* is this acquisition's own wait (each
    bounded lock waits up to *timeout_s* on its own). *holder* (#2694), when
    the lock file carries a live holder record, replaces the "records no PID"
    sentence with the recorded pid and command; ``None`` keeps the text
    byte-identical to the pre-#2694 message.
    """
    serve_held = lock_name in SERVE_HELD_LOCKS
    holder_hint = ", typically `cw dev-queue serve`" if serve_held else ""
    wedge_advice = (
        " If `cw dev-queue serve` has stopped emitting `dispatch.tick` events it"
        " is wedged: restart it."
        if serve_held
        else ""
    )
    if holder is None:
        find_holder = (
            f" Find the holder with `lsof {lock_path}` (the lock file records no PID)."
        )
    else:
        held_s = max((datetime.now(UTC) - holder.acquired_at).total_seconds(), 0.0)
        find_holder = (
            f" Recorded holder: pid {holder.pid} (`{holder.cmd}`), held for"
            f" {held_s:.1f}s; confirm with `lsof {lock_path}`."
        )
    return (
        f"Timed out after {waited_s:.1f}s waiting for the {lock_name} lock"
        f" {lock_path}. Another cw process is holding it{holder_hint}."
        f"{find_holder}"
        f"{wedge_advice} If the holder is merely slow, retry, or raise the wait"
        f" with {SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently {timeout_s:g})."
    )


def _poll_for_lock(
    fd: IO[str], lock_path: Path, lock_name: str, timeout_s: float | None = None
) -> _Timeout | None:
    """Poll for ``LOCK_EX`` up to *timeout_s*, else the configured timeout.

    Returns ``None`` once acquired, else the timeout's own wait and message.
    The clock starts here, so a nested acquisition reports only its own wait.
    Only a sessions-lock timeout reads the holder record (#2694): the other
    state locks open ``"w"`` and record nothing.
    """
    if timeout_s is None:
        timeout_s = sessions_lock_timeout_seconds()
    started = time.monotonic()
    if try_flock_until(
        fd, timeout_s=timeout_s, poll_interval_s=SESSIONS_LOCK_POLL_INTERVAL_S
    ):
        return None
    waited_s = time.monotonic() - started
    holder = read_lock_holder(fd) if lock_name == SESSIONS_LOCK_NAME else None
    message = lock_timeout_message(
        lock_name, lock_path, waited_s=waited_s, timeout_s=timeout_s, holder=holder
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


def acquire_sessions_flock(
    fd: IO[str], lock_path: Path, *, bounded: bool, timeout_s: float | None = None
) -> None:
    """Take ``LOCK_EX`` on the sessions-lock *fd*.

    As :func:`acquire_flock` for the ``sessions`` lock, raising the
    :class:`~cw.exceptions.SessionsLockTimeoutError` subclass on a bounded
    timeout (the type the dispatch tick and the reap paths catch). An explicit
    *timeout_s* (#2694) replaces the env-derived wait for this acquisition;
    ``bounded=False`` ignores it.
    """
    if not bounded:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    timeout = _poll_for_lock(fd, lock_path, SESSIONS_LOCK_NAME, timeout_s)
    if timeout is None:
        return
    raise SessionsLockTimeoutError(
        timeout.message, lock_path=lock_path, waited_s=timeout.waited_s
    )
