"""Lock-discipline guard shared by cw's state-file locks (ADR-0019, #1233).

Every guarded lock context manager (``sessions_lock``, the dev-queue and plan
locks, ``clients_lock``, ``concurrency_override_lock``, ``dispatch_state_lock``,
the focus lock, and the event/session inbox and history leaf locks) wraps its
body in :func:`lock_guard`. The guard keeps a per-thread stack of held locks
and checks it before any ``open()`` or ``flock()`` syscall:

1. **Re-entry** (always on): acquiring a lock whose path this thread already
   holds raises :class:`~cw.exceptions.CwLockReentrancyError`. Each lock is a
   per-open-fd ``flock``, so the second acquisition would otherwise block
   forever on a fresh fd (GitHub #1228).
2. **Order**: locks are ranked (:class:`LockRank`) and must be acquired in
   non-decreasing rank; nothing may be acquired while a ``LEAF`` lock is held.
   A violation raises :class:`~cw.exceptions.CwLockOrderError` only when
   ``CW_LOCK_DEBUG=1`` (the test suite sets it); otherwise it logs one WARNING
   per violating acquisition and proceeds.

The held state is thread-local, not process-wide: cw runs poller, notifier
and anyio worker threads, and a thread legitimately waiting on another
thread's lock is not a re-entry. A ``ContextVar`` would be wrong too, because
anyio copies the context into worker threads.

An optional observer (:func:`set_observer`) receives every acquire, release,
re-entry and order event before the guard raises, so a test harness can see a
violation that a caller's broad ``except CwError`` swallows. Production leaves
it unset. Depends only on ``cw.exceptions``; importable from ``cw.config``.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.exceptions import CwLockOrderError, CwLockReentrancyError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_log = logging.getLogger(__name__)

# ``CW_LOCK_DEBUG=1`` turns a rank/leaf violation from a WARNING into a raise.
LOCK_DEBUG_ENV = "CW_LOCK_DEBUG"


class LockRank(enum.IntEnum):
    """Acquisition rank: a thread acquires locks in non-decreasing rank."""

    SESSIONS = 0
    STATE = 1
    LEAF = 2


@dataclass(frozen=True, slots=True)
class HeldLock:
    """One lock on a thread's held stack: logical name, path key, rank."""

    name: str
    path: str
    rank: LockRank


type GuardEventKind = Literal["acquire", "release", "reentry", "order"]


@dataclass(frozen=True, slots=True)
class GuardEvent:
    """One observed guard event, with the emitting thread's held stack.

    ``held_snapshot`` is taken before the push for ``acquire``, ``reentry``
    and ``order``, and after the pop for ``release``. ``enforced`` is ``True``
    only for an ``order`` event that raised.
    """

    kind: GuardEventKind
    lock_name: str
    path: str
    rank: LockRank
    thread_id: int
    thread_name: str
    held_snapshot: tuple[HeldLock, ...]
    enforced: bool


Observer = Callable[[GuardEvent], None]


class _ThreadState(threading.local):
    """Per-thread held-lock stack, outermost first."""

    def __init__(self) -> None:
        self.stack: list[HeldLock] = []


class _ObserverSlot:
    """Holds the process-wide observer (a slot, not a ``global`` rebind)."""

    __slots__ = ("observer",)

    def __init__(self) -> None:
        self.observer: Observer | None = None


_state = _ThreadState()
_slot = _ObserverSlot()


def set_observer(fn: Observer | None) -> Observer | None:
    """Install *fn* as the guard observer and return the previous one.

    The observer is called synchronously on the acquiring thread, so it must
    be thread-safe. ``None`` silences observation.
    """
    previous = _slot.observer
    _slot.observer = fn
    return previous


def held_locks() -> tuple[HeldLock, ...]:
    """Return the calling thread's held locks, outermost first."""
    return tuple(_state.stack)


def is_rank_held(rank: LockRank) -> bool:
    """Return whether the calling thread holds any lock of *rank*."""
    return any(held.rank is rank for held in _state.stack)


def debug_enabled() -> bool:
    """Return whether ``CW_LOCK_DEBUG=1`` (read on every acquisition)."""
    return os.environ.get(LOCK_DEBUG_ENV) == "1"


def _emit(
    kind: GuardEventKind,
    lock: HeldLock,
    snapshot: tuple[HeldLock, ...],
    *,
    enforced: bool = False,
) -> None:
    observer = _slot.observer
    if observer is None:
        return
    observer(
        GuardEvent(
            kind=kind,
            lock_name=lock.name,
            path=lock.path,
            rank=lock.rank,
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            held_snapshot=snapshot,
            enforced=enforced,
        )
    )


def _names(held: tuple[HeldLock, ...]) -> tuple[str, ...]:
    return tuple(h.name for h in held)


def _check_reentry(lock: HeldLock, path: Path, held: tuple[HeldLock, ...]) -> None:
    if not any(h.path == lock.path for h in held):
        return
    _emit("reentry", lock, held)
    names = _names(held)
    msg = (
        f"lock {lock.name!r} re-entered on the same thread ({lock.path} is "
        f"already held; held, outermost first: {', '.join(names)}); a second "
        "flock() on a fresh fd would block forever. Release it first or work "
        "under the held lock. See ADR-0019."
    )
    raise CwLockReentrancyError(msg, lock_name=lock.name, path=path, held=names)


def _order_message(lock: HeldLock, held: tuple[HeldLock, ...]) -> str | None:
    """The violation message for acquiring *lock* under *held*, else ``None``."""
    stack = ", ".join(_names(held))
    leaf = next((h for h in held if h.rank is LockRank.LEAF), None)
    if leaf is not None:
        return (
            f"lock order violation: acquiring {lock.name!r} (rank "
            f"{lock.rank.name}, {lock.path}) while holding leaf lock "
            f"{leaf.name!r}; nothing may be acquired while a leaf lock is held. "
            f"Held, outermost first: {stack}. See ADR-0019."
        )
    higher = next((h for h in held if h.rank > lock.rank), None)
    if higher is None:
        return None
    return (
        f"lock order violation: acquiring {lock.name!r} (rank {lock.rank.name}, "
        f"{lock.path}) while holding {higher.name!r} (rank {higher.rank.name}); "
        "locks must be acquired in non-decreasing rank (sessions, state, leaf). "
        f"Held, outermost first: {stack}. See ADR-0019."
    )


def _warn_order(lock: HeldLock, held: tuple[HeldLock, ...]) -> None:
    held_desc = ", ".join(f"{h.name}@{h.rank.name}" for h in held)
    _log.warning(
        "lock order violation (CW_LOCK_DEBUG off, not raising): acquiring %s"
        " (rank %s, %s) while holding %s; see ADR-0019",
        lock.name,
        lock.rank.name,
        lock.path,
        held_desc,
    )


def _check_order(lock: HeldLock, path: Path, held: tuple[HeldLock, ...]) -> None:
    msg = _order_message(lock, held)
    if msg is None:
        return
    enforced = debug_enabled()
    _emit("order", lock, held, enforced=enforced)
    if enforced:
        raise CwLockOrderError(
            msg,
            lock_name=lock.name,
            path=path,
            rank_name=lock.rank.name,
            held=_names(held),
        )
    _warn_order(lock, held)


@contextlib.contextmanager
def lock_guard(name: str, path: Path, rank: LockRank) -> Iterator[None]:
    """Track *name* (keyed by *path*) as held by this thread for the block.

    Wrap a lock context manager's ``open()``/``flock()``/``yield`` in this.
    Raises :class:`~cw.exceptions.CwLockReentrancyError` on a same-thread
    re-entry, and :class:`~cw.exceptions.CwLockOrderError` on a rank or leaf
    violation when ``CW_LOCK_DEBUG=1``; both raise before anything is pushed.
    """
    lock = HeldLock(name=name, path=str(path), rank=rank)
    stack = _state.stack
    held = tuple(stack)
    _check_reentry(lock, path, held)
    _check_order(lock, path, held)
    stack.append(lock)
    try:
        _emit("acquire", lock, held)
        yield
    finally:
        stack.remove(lock)
        _emit("release", lock, tuple(stack))
