"""Suite-wide lock-invariant harness (#1233, ADR-0019).

The autouse ``_lock_invariants`` fixture in ``tests/conftest.py`` calls
:func:`install` before every test and :func:`finish` after it. While a test
runs, a :class:`LockTrace` records:

- every guarded lock event (acquire, release, re-entry, order) via the
  ``cw._lock_guard`` observer seam, with ``CW_LOCK_DEBUG=1`` set so a rank or
  leaf violation raises;
- every real subprocess launched while the calling thread holds
  ``sessions_lock`` from a ``cw.*`` frame (``subprocess.Popen.__init__`` is
  wrapped; ``run``, ``check_output`` and ``cw.gh``'s ``_sp`` alias all funnel
  through it).

At teardown the test fails on a re-entry, an order violation, an in-lock
subprocess not forgiven by :data:`SUBPROCESS_UNDER_SESSIONS_ALLOWLIST`, or a
lock still held on any thread. The trace asserts from its recording, not from
exceptions propagating, so a re-entry swallowed by a caller's broad
``except CwError`` still fails the test (#1228).

Opt-out: ``@pytest.mark.lock_violations_expected("reentry", "order",
"subprocess")`` exempts a test that causes those kinds on purpose. A leaked
lock can never be opted out of.

Known limits: only real subprocess execs are visible, so a test that mocks
``subprocess.run``/``Popen`` bypasses the hook (no false positive, but that
path is not covered); a subprocess launched from a helper thread while the
PARENT thread holds the lock is invisible, because held state is per thread.
"""

from __future__ import annotations

import contextlib
import inspect
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw._lock_guard import (
    LOCK_DEBUG_ENV,
    GuardEvent,
    HeldLock,
    LockRank,
    held_locks,
    set_observer,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

    import pytest

    from cw._lock_guard import Observer

MARKER = "lock_violations_expected"
VIOLATION_KINDS = frozenset({"reentry", "order", "subprocess"})

# Modules allowed to launch a subprocess while ``sessions_lock`` is held,
# mapped to the follow-up ticket that removes the exception. A call is forgiven
# when ANY ``cw.*`` frame on its stack is one of these modules (coarse by
# design). Shrink condition: delete the entry when its ticket closes. Every
# entry needs a ticket in its value AND its trailing comment (hygiene test).
# Seeded from the #1233 record-mode probe; the full exceptions table is in
# docs/adr/0019-lock-hierarchy-and-no-subprocess-under-sessions-lock.md.
SUBPROCESS_UNDER_SESSIONS_ALLOWLIST: dict[str, str] = {
    "cw.reconcile.main_drift": "#2546",  # main-drift git checks in-lock, #2546
    "cw.reconcile.phantom._detect": "#2548",  # phantom dirty-check git, #2548
    "cw.reconcile.tasks": "#2548",  # phantom dirty-check git (tasks path), #2548
    "cw.cli.stop_hook.locked": "#2566",  # Stop-hook headless scope git, #2566
}


@dataclass(frozen=True, slots=True)
class SubprocessCall:
    """A real subprocess launched from ``cw`` code while ``sessions_lock`` was held."""

    thread_id: int
    argv0: str
    cw_modules: tuple[str, ...]
    held: tuple[HeldLock, ...]
    forgiven_by: str | None


@dataclass(frozen=True, slots=True)
class Violation:
    """One harness failure, rendered by :meth:`LockTrace.assert_clean`."""

    kind: Literal["reentry", "order", "subprocess", "leak"]
    thread_id: int
    thread_name: str
    lock_name: str
    detail: str


def _stack_desc(held: tuple[HeldLock, ...]) -> str:
    return ">".join(h.name for h in held) or "nothing"


def _event_violation(event: GuardEvent) -> Violation | None:
    held = _stack_desc(event.held_snapshot)
    who = f"thread {event.thread_id} ({event.thread_name})"
    if event.kind == "reentry":
        detail = f"{who} re-entered {event.lock_name!r} while holding {held}"
        return Violation(
            "reentry", event.thread_id, event.thread_name, event.lock_name, detail
        )
    if event.kind == "order":
        detail = (
            f"{who} acquired {event.lock_name!r} (rank {event.rank.name}) while "
            f"holding {held} (enforced={event.enforced})"
        )
        return Violation(
            "order", event.thread_id, event.thread_name, event.lock_name, detail
        )
    return None


class LockTrace:
    """Thread-safe recorder of guard events and in-lock subprocess launches.

    Every mutation and read goes through one internal mutex, so concurrent
    threads produce one totally ordered, loss-free record.
    """

    def __init__(self, allowlist: Mapping[str, str] | None = None) -> None:
        self.allowlist: Mapping[str, str] = (
            SUBPROCESS_UNDER_SESSIONS_ALLOWLIST if allowlist is None else allowlist
        )
        self._mutex = threading.Lock()
        self._record: list[GuardEvent | SubprocessCall] = []

    def __call__(self, event: GuardEvent) -> None:
        with self._mutex:
            self._record.append(event)

    def note_subprocess(self, call: SubprocessCall) -> None:
        with self._mutex:
            self._record.append(call)

    def _snapshot(self) -> list[GuardEvent | SubprocessCall]:
        with self._mutex:
            return list(self._record)

    def events(self) -> tuple[GuardEvent, ...]:
        return tuple(e for e in self._snapshot() if isinstance(e, GuardEvent))

    def subprocess_calls(self) -> tuple[SubprocessCall, ...]:
        return tuple(c for c in self._snapshot() if isinstance(c, SubprocessCall))

    def _open_acquires(self) -> dict[int, list[GuardEvent]]:
        open_by_thread: dict[int, list[GuardEvent]] = {}
        for event in self.events():
            opened = open_by_thread.setdefault(event.thread_id, [])
            if event.kind == "acquire":
                opened.append(event)
            elif event.kind == "release":
                match = next(
                    (
                        e
                        for e in reversed(opened)
                        if (e.lock_name, e.path) == (event.lock_name, event.path)
                    ),
                    None,
                )
                if match is not None:
                    opened.remove(match)
        return {tid: acquires for tid, acquires in open_by_thread.items() if acquires}

    def leaked(self) -> dict[int, tuple[HeldLock, ...]]:
        """Locks acquired with no matching release, keyed by thread id."""
        return {
            tid: tuple(HeldLock(e.lock_name, e.path, e.rank) for e in acquires)
            for tid, acquires in self._open_acquires().items()
        }

    def _thread_names(self) -> dict[int, str]:
        return {e.thread_id: e.thread_name for e in self.events()}

    def _subprocess_violation(
        self, call: SubprocessCall, names: Mapping[int, str]
    ) -> Violation:
        name = names.get(call.thread_id, "?")
        detail = (
            f"thread {call.thread_id} ({name}) ran {call.argv0!r} under "
            f"sessions_lock (holding {_stack_desc(call.held)}) from "
            f"{', '.join(call.cw_modules)}; move it after the lock is released "
            "or allowlist the module with its follow-up ticket (ADR-0019)"
        )
        return Violation("subprocess", call.thread_id, name, "sessions", detail)

    def _leak_violations(self) -> list[Violation]:
        alive = {t.ident for t in threading.enumerate()}
        violations: list[Violation] = []
        for tid, acquires in self._open_acquires().items():
            status = "alive" if tid in alive else "not alive"
            for event in acquires:
                detail = (
                    f"thread {tid} ({event.thread_name}, {status}) still holds "
                    f"{event.lock_name!r} at teardown"
                )
                violations.append(
                    Violation("leak", tid, event.thread_name, event.lock_name, detail)
                )
        return violations

    def violations(self) -> list[Violation]:
        """Every violation in record order, then leaks."""
        names = self._thread_names()
        found: list[Violation] = []
        for entry in self._snapshot():
            if isinstance(entry, SubprocessCall):
                if entry.forgiven_by is None:
                    found.append(self._subprocess_violation(entry, names))
                continue
            violation = _event_violation(entry)
            if violation is not None:
                found.append(violation)
        return found + self._leak_violations()

    def assert_clean(self, expected: Collection[str] = ()) -> None:
        """Raise ``AssertionError`` listing every violation not in *expected*."""
        unknown = set(expected) - VIOLATION_KINDS
        if unknown:
            msg = (
                f"{MARKER} accepts only {sorted(VIOLATION_KINDS)}; got "
                f"{sorted(unknown)} (a lock leak can never be opted out of)"
            )
            raise ValueError(msg)
        failures = [v for v in self.violations() if v.kind not in expected]
        if failures:
            lines = "\n".join(f"{v.kind}: {v.detail}" for v in failures)
            msg = f"lock invariant violations (ADR-0019):\n{lines}"
            raise AssertionError(msg)

    def classify_popen(self, args: object) -> SubprocessCall | None:
        """Build the record for a ``Popen`` about to launch, or ``None`` to ignore.

        Counts only when the calling thread holds a ``SESSIONS``-rank lock and
        at least one ``cw.*`` frame is on the stack.
        """
        held = held_locks()
        if not any(h.rank is LockRank.SESSIONS for h in held):
            return None
        modules = _cw_frame_modules()
        if not modules:
            return None
        forgiven = next((m for m in modules if m in self.allowlist), None)
        return SubprocessCall(
            thread_id=threading.get_ident(),
            argv0=_argv0(args),
            cw_modules=modules,
            held=held,
            forgiven_by=forgiven,
        )


def _cw_frame_modules() -> tuple[str, ...]:
    """The distinct ``cw.*`` modules on the calling stack, innermost first."""
    modules: list[str] = []
    frame = inspect.currentframe()
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if isinstance(name, str) and name.startswith("cw.") and name not in modules:
            modules.append(name)
        frame = frame.f_back
    return tuple(modules)


def _argv0(args: object) -> str:
    if isinstance(args, (str, bytes)):
        text = args.decode() if isinstance(args, bytes) else args
        return text.split(" ", 1)[0]
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    return str(args)


def _chain(first: Observer, second: Observer) -> Observer:
    def _both(event: GuardEvent) -> None:
        first(event)
        second(event)

    return _both


@contextlib.contextmanager
def observing(observer: Observer) -> Iterator[None]:
    """Chain *observer* in front of the current one for the block's duration."""
    outer = set_observer(None)
    set_observer(observer if outer is None else _chain(observer, outer))
    try:
        yield
    finally:
        set_observer(outer)


def _hook_popen(trace: LockTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    real_init: Callable[..., None] = subprocess.Popen.__init__

    def _recording_init(
        self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object
    ) -> None:
        call = trace.classify_popen(args)
        if call is not None:
            trace.note_subprocess(call)
        real_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _recording_init)


def install(
    monkeypatch: pytest.MonkeyPatch, *, allowlist: Mapping[str, str] | None = None
) -> LockTrace:
    """Start recording: chain a new trace onto the observer and hook ``Popen``.

    Chaining (not replacing) means a test that installs its own trace leaves
    the autouse fixture's trace still recording, so a deliberate violation
    still needs its marker.
    """
    trace = LockTrace(allowlist)
    outer = set_observer(None)
    set_observer(trace if outer is None else _chain(trace, outer))
    monkeypatch.setenv(LOCK_DEBUG_ENV, "1")
    _hook_popen(trace, monkeypatch)
    return trace


def expected_kinds(node: pytest.Item) -> frozenset[str]:
    """Union of the kinds named by every ``lock_violations_expected`` marker."""
    return frozenset(
        str(kind) for marker in node.iter_markers(MARKER) for kind in marker.args
    )


def finish(trace: LockTrace, node: pytest.Item) -> None:
    """Stop observing and fail the test on any unexpected violation."""
    set_observer(None)
    trace.assert_clean(expected=expected_kinds(node))
