"""Tests for ``cw._lock_guard``: re-entry, rank order, observer, AST completeness.

The guard (ADR-0019, #1233) wraps every state-file lock context manager. A
same-thread re-entry raises :class:`~cw.exceptions.CwLockReentrancyError`
before any ``open()``/``flock()`` syscall; a rank or leaf violation raises
:class:`~cw.exceptions.CwLockOrderError` under ``CW_LOCK_DEBUG=1`` and logs one
WARNING otherwise.

NOTE: a wrong implementation of the re-entry check does not fail these tests,
it HANGS the run: the nested acquisition would block forever in ``flock()``
against the fd the outer acquisition already holds.
"""

from __future__ import annotations

import ast
import fcntl
import logging
import threading
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

import pytest

from cw import events, history, session_inbox
from cw._lock_guard import (
    GuardEvent,
    HeldLock,
    LockRank,
    held_locks,
    is_rank_held,
    lock_guard,
    set_observer,
)
from cw.config import (
    clients_lock,
    concurrency_override_lock,
    sessions_lock,
    sessions_lock_file,
)
from cw.config import dev_queue_lock as dev_queue_lock_file
from cw.dev_queue.storage import _plan_lock, dev_queue_lock
from cw.dispatch_state import dispatch_state_lock
from cw.exceptions import (
    CwError,
    CwLockOrderError,
    CwLockReentrancyError,
    SessionsLockTimeoutError,
)
from cw.focus import focus_lock
from tests._lock_invariants import observing
from tests.conftest import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import IO

_LOG_NAME = "cw._lock_guard"
_JOIN_TIMEOUT_S = 10.0

# Every guarded lock context manager, keyed by a readable id. Each factory
# returns a fresh context manager so a test can nest two acquisitions.
_GUARDED_CMS: dict[str, Callable[[], AbstractContextManager[None]]] = {
    "sessions": sessions_lock,
    "sessions-bounded": lambda: sessions_lock(bounded=True),
    "dev_queue": dev_queue_lock,
    "dev_queue_plan": _plan_lock,
    "concurrency_override": concurrency_override_lock,
    "clients": clients_lock,
    "dispatch_state": dispatch_state_lock,
    "focus": focus_lock,
    "events_inbox": events._inbox_lock,
    "session_inbox": lambda: session_inbox._inbox_lock("s"),
    "history": lambda: history._history_lock("c"),
}


class _Recorder:
    """Thread-safe list observer."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self.events: list[GuardEvent] = []

    def __call__(self, event: GuardEvent) -> None:
        with self._mutex:
            self.events.append(event)


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    """A recorder chained in front of the harness's own observer."""
    rec = _Recorder()
    with observing(rec):
        yield rec


# ---------------------------------------------------------------------------
# Re-entry
# ---------------------------------------------------------------------------


@pytest.mark.lock_violations_expected("reentry")
@pytest.mark.parametrize("cm_id", sorted(_GUARDED_CMS))
def test_every_guarded_lock_refuses_same_thread_reentry(
    tmp_config_dir: Path, cm_id: str
) -> None:
    factory = _GUARDED_CMS[cm_id]

    with (
        factory(),
        pytest.raises(CwLockReentrancyError, match="re-entered on the same thread"),
        factory(),
    ):
        pytest.fail("must not reach body")


@pytest.mark.lock_violations_expected("reentry")
def test_reentry_message_and_attributes(tmp_config_dir: Path) -> None:
    path = dev_queue_lock_file()

    with (
        sessions_lock(),
        dev_queue_lock(),
        pytest.raises(CwLockReentrancyError) as excinfo,
        dev_queue_lock(),
    ):
        pytest.fail("must not reach body")

    err = excinfo.value
    assert str(err) == (
        f"lock 'dev_queue' re-entered on the same thread ({path} is already "
        "held; held, outermost first: sessions, dev_queue); a second flock() on "
        "a fresh fd would block forever. Release it first or work under the "
        "held lock. See ADR-0019."
    )
    assert isinstance(err, CwError)
    assert err.lock_name == "dev_queue"
    assert str(err.path) == str(path)
    assert err.held == ("sessions", "dev_queue")


@pytest.mark.lock_violations_expected("reentry")
def test_reentry_raises_before_any_flock(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_flock = fcntl.flock

    def _counting_flock(fd: IO[str], op: int) -> None:
        calls.append(op)
        real_flock(fd, op)

    with dev_queue_lock():
        with monkeypatch.context() as patch:
            patch.setattr("fcntl.flock", _counting_flock)
            with pytest.raises(CwLockReentrancyError), dev_queue_lock():
                pytest.fail("must not reach body")
        assert calls == []


def test_sequential_reacquire_succeeds(tmp_config_dir: Path) -> None:
    for factory in _GUARDED_CMS.values():
        with factory():
            pass
        with factory():
            pass
    assert held_locks() == ()


def test_held_stack_cleared_after_exception_in_body(tmp_config_dir: Path) -> None:
    msg = "boom"
    with pytest.raises(ValueError, match="boom"), sessions_lock(), dev_queue_lock():
        raise ValueError(msg)

    assert held_locks() == ()
    with sessions_lock():
        pass


def test_held_stack_cleared_after_bounded_timeout(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_SESSIONS_LOCK_TIMEOUT_S", "0")
    lock_path = sessions_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as other:
        # A second open file description conflicts with flock even in-process.
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(SessionsLockTimeoutError), sessions_lock(bounded=True):
            pytest.fail("must not acquire")
        assert held_locks() == ()
        fcntl.flock(other, fcntl.LOCK_UN)

    with sessions_lock(bounded=True):
        assert is_rank_held(LockRank.SESSIONS)


def test_other_thread_blocks_instead_of_raising_reentry(
    tmp_config_dir: Path, recorder: _Recorder
) -> None:
    holding = threading.Event()
    release = threading.Event()
    acquired = threading.Event()
    errors: list[CwError] = []

    def _holder() -> None:
        with dev_queue_lock():
            holding.set()
            release.wait(_JOIN_TIMEOUT_S)

    def _contender() -> None:
        try:
            with dev_queue_lock():
                acquired.set()
        except CwError as exc:
            errors.append(exc)

    holder = threading.Thread(target=_holder, name="holder")
    contender = threading.Thread(target=_contender, name="contender")
    holder.start()
    assert holding.wait(_JOIN_TIMEOUT_S)
    contender.start()

    # The contender is blocked in flock(), not refused as a re-entry.
    assert not acquired.wait(0.2)
    release.set()
    holder.join(_JOIN_TIMEOUT_S)
    contender.join(_JOIN_TIMEOUT_S)

    assert errors == []
    assert acquired.is_set()
    by_thread = {e.thread_name: e.thread_id for e in recorder.events}
    assert set(by_thread) == {"holder", "contender"}
    assert by_thread["holder"] != by_thread["contender"]
    assert by_thread["holder"] == holder.ident
    assert by_thread["contender"] == contender.ident


# ---------------------------------------------------------------------------
# Rank order
# ---------------------------------------------------------------------------


def test_ascending_and_peer_nesting_allowed(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_LOCK_DEBUG", "1")

    with sessions_lock(), dev_queue_lock(), events._inbox_lock():
        assert [h.name for h in held_locks()] == [
            "sessions",
            "dev_queue",
            "events_inbox",
        ]
    with dev_queue_lock(), _plan_lock(), clients_lock():
        assert is_rank_held(LockRank.STATE)
        assert not is_rank_held(LockRank.SESSIONS)


@pytest.mark.lock_violations_expected("order")
def test_descending_order_raises_under_debug(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_LOCK_DEBUG", "1")
    path = sessions_lock_file()

    with dev_queue_lock():
        with pytest.raises(CwLockOrderError) as excinfo, sessions_lock():
            pytest.fail("must not reach body")
        assert [h.name for h in held_locks()] == ["dev_queue"]

    err = excinfo.value
    assert str(err) == (
        "lock order violation: acquiring 'sessions' (rank SESSIONS, "
        f"{path}) while holding 'dev_queue' (rank STATE); locks must be "
        "acquired in non-decreasing rank (sessions, state, leaf). Held, "
        "outermost first: dev_queue. See ADR-0019."
    )
    assert isinstance(err, CwError)
    assert err.lock_name == "sessions"
    assert str(err.path) == str(path)
    assert err.rank_name == "SESSIONS"
    assert err.held == ("dev_queue",)


@pytest.mark.lock_violations_expected("order")
@pytest.mark.parametrize("inner_id", ["history", "session_inbox", "dev_queue"])
def test_nothing_acquired_under_a_leaf(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, inner_id: str
) -> None:
    monkeypatch.setenv("CW_LOCK_DEBUG", "1")

    with (
        events._inbox_lock(),
        pytest.raises(CwLockOrderError, match="lock order violation") as excinfo,
        _GUARDED_CMS[inner_id](),
    ):
        pytest.fail("must not reach body")

    message = str(excinfo.value)
    assert "while holding leaf lock 'events_inbox'" in message
    assert "nothing may be acquired while a leaf lock is held" in message
    assert "Held, outermost first: events_inbox. See ADR-0019." in message


@pytest.mark.lock_violations_expected("order")
def test_leaf_message_verbatim(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_LOCK_DEBUG", "1")
    path = history._lock_path("acme")

    with (
        events._inbox_lock(),
        pytest.raises(CwLockOrderError) as excinfo,
        history._history_lock("acme"),
    ):
        pytest.fail("must not reach body")

    assert str(excinfo.value) == (
        f"lock order violation: acquiring 'history' (rank LEAF, {path}) while "
        "holding leaf lock 'events_inbox'; nothing may be acquired while a "
        "leaf lock is held. Held, outermost first: events_inbox. See ADR-0019."
    )


@pytest.mark.lock_violations_expected("order")
def test_descending_order_without_debug_runs_body_and_reports(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.delenv("CW_LOCK_DEBUG", raising=False)
    ran = False

    with dev_queue_lock(), sessions_lock():
        ran = True
        assert [h.name for h in held_locks()] == ["dev_queue", "sessions"]

    assert ran
    order = [e for e in recorder.events if e.kind == "order"]
    assert len(order) == 1
    assert order[0].lock_name == "sessions"
    assert order[0].enforced is False
    assert [h.name for h in order[0].held_snapshot] == ["dev_queue"]


# ---------------------------------------------------------------------------
# WARNING log (debug off)
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _LOG_NAME and r.levelno == logging.WARNING
    ]


@pytest.mark.lock_violations_expected("order")
def test_debug_off_descending_logs_one_warning(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("CW_LOCK_DEBUG", raising=False)
    path = sessions_lock_file()

    with (
        caplog.at_level(logging.WARNING, logger=_LOG_NAME),
        dev_queue_lock(),
        sessions_lock(),
    ):
        pass

    records = _warnings(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == (
        "lock order violation (CW_LOCK_DEBUG off, not raising): acquiring "
        f"sessions (rank SESSIONS, {path}) while holding dev_queue@STATE; "
        "see ADR-0019"
    )


@pytest.mark.lock_violations_expected("order")
def test_debug_off_leaf_violation_logs_one_warning(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("CW_LOCK_DEBUG", raising=False)

    with (
        caplog.at_level(logging.WARNING, logger=_LOG_NAME),
        dev_queue_lock(),
        events._inbox_lock(),
        history._history_lock("acme"),
    ):
        pass

    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "acquiring history (rank LEAF," in message
    assert "while holding dev_queue@STATE, events_inbox@LEAF" in message


@pytest.mark.lock_violations_expected("order")
def test_debug_off_two_violations_log_two_warnings(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("CW_LOCK_DEBUG", raising=False)

    with caplog.at_level(logging.WARNING, logger=_LOG_NAME):
        for _ in range(2):
            with dev_queue_lock(), sessions_lock():
                pass

    assert len(_warnings(caplog)) == 2


@pytest.mark.lock_violations_expected("order", "reentry")
def test_raising_paths_and_clean_sequence_log_nothing(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOG_NAME):
        monkeypatch.setenv("CW_LOCK_DEBUG", "1")
        with dev_queue_lock(), pytest.raises(CwLockOrderError), sessions_lock():
            pass
        with dev_queue_lock(), pytest.raises(CwLockReentrancyError), dev_queue_lock():
            pass
        monkeypatch.delenv("CW_LOCK_DEBUG")
        with sessions_lock(), dev_queue_lock(), events._inbox_lock():
            pass

    assert _warnings(caplog) == []


# ---------------------------------------------------------------------------
# Observer seam and held-state accessors
# ---------------------------------------------------------------------------


def test_observer_sees_acquire_and_release_with_snapshots(
    tmp_config_dir: Path, recorder: _Recorder
) -> None:
    with sessions_lock(), dev_queue_lock():
        pass

    sessions = HeldLock("sessions", str(sessions_lock_file()), LockRank.SESSIONS)
    got = [(e.kind, e.lock_name, e.held_snapshot) for e in recorder.events]
    assert got == [
        ("acquire", "sessions", ()),
        ("acquire", "dev_queue", (sessions,)),
        ("release", "dev_queue", (sessions,)),
        ("release", "sessions", ()),
    ]
    assert all(e.enforced is False for e in recorder.events)
    assert {e.thread_id for e in recorder.events} == {threading.get_ident()}
    assert {e.thread_name for e in recorder.events} == {threading.current_thread().name}


@pytest.mark.lock_violations_expected("reentry", "order")
def test_observer_sees_reentry_and_order_before_the_raise(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("CW_LOCK_DEBUG", "1")

    with dev_queue_lock():
        with pytest.raises(CwLockReentrancyError), dev_queue_lock():
            pass
        with pytest.raises(CwLockOrderError), sessions_lock():
            pass

    kinds = [(e.kind, e.lock_name) for e in recorder.events]
    assert kinds == [
        ("acquire", "dev_queue"),
        ("reentry", "dev_queue"),
        ("order", "sessions"),
        ("release", "dev_queue"),
    ]
    reentry, order = recorder.events[1], recorder.events[2]
    assert [h.name for h in reentry.held_snapshot] == ["dev_queue"]
    assert reentry.enforced is False
    assert order.enforced is True
    assert order.rank is LockRank.SESSIONS


def test_held_locks_is_per_thread(tmp_config_dir: Path) -> None:
    seen: list[tuple[HeldLock, ...]] = []

    with sessions_lock():
        assert [h.name for h in held_locks()] == ["sessions"]
        assert is_rank_held(LockRank.SESSIONS)
        assert not is_rank_held(LockRank.LEAF)
        worker = threading.Thread(target=lambda: seen.append(held_locks()))
        worker.start()
        worker.join(_JOIN_TIMEOUT_S)

    assert seen == [()]
    assert held_locks() == ()
    assert not is_rank_held(LockRank.SESSIONS)


def test_set_observer_returns_previous_and_none_silences(
    tmp_config_dir: Path,
) -> None:
    first = _Recorder()
    second = _Recorder()
    original = set_observer(first)
    try:
        assert set_observer(second) is first
        assert set_observer(None) is second
        with dev_queue_lock():
            pass
    finally:
        set_observer(original)

    assert first.events == []
    assert second.events == []


def test_lock_guard_accepts_any_path_key(tmp_path: Path) -> None:
    with lock_guard("probe", tmp_path / "a.lock", LockRank.STATE):
        assert held_locks() == (
            HeldLock("probe", str(tmp_path / "a.lock"), LockRank.STATE),
        )
    assert held_locks() == ()


# ---------------------------------------------------------------------------
# AST completeness guard: every flock context manager is guarded or exempt
# ---------------------------------------------------------------------------

_CM_DECORATORS = frozenset({"contextmanager", "asynccontextmanager"})
_ACQUIRE_HELPERS = frozenset({"try_flock_until", "acquire_sessions_flock"})
_GUARD_NAME = "lock_guard"

# Matched (flock LOCK_EX / bounded helper) context managers deliberately left
# outside the guard. Reasons are recorded in ADR-0019.
_UNGUARDED: dict[tuple[str, str], str] = {
    ("cw/config.py", "dispatch_loop_lock"): (
        "process-lifetime singleton; LOCK_EX | LOCK_NB fails fast, cannot hang"
    ),
    ("cw/_hook_context.py", "_context_lock"): (
        "per-worktree, bounded try_flock_until, fails open"
    ),
    ("cw/doctor/routed_result_wedge.py", "_audit_outbox_lock"): (
        "deliberately reentrant by contextvar depth permit"
    ),
    ("cw/codex_legacy_recovery.py", "codex_legacy_marker_lock"): (
        "single-use marker file"
    ),
    ("cw/session_resume_trigger.py", "_resume_trigger_lock"): (
        "per-session, held across a spawn that takes sessions_lock (ADR follow-up)"
    ),
}

_GUARDED: frozenset[tuple[str, str]] = frozenset(
    {
        ("cw/config.py", "sessions_lock"),
        ("cw/config.py", "concurrency_override_lock"),
        ("cw/config.py", "clients_lock"),
        ("cw/dev_queue/storage.py", "_lock"),
        ("cw/dev_queue/storage.py", "_plan_lock"),
        ("cw/dispatch_state.py", "dispatch_state_lock"),
        ("cw/focus.py", "_lock"),
        ("cw/events.py", "_inbox_lock"),
        ("cw/session_inbox.py", "_inbox_lock"),
        ("cw/history.py", "_history_lock"),
    }
)

type _FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_guard_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id == _GUARD_NAME
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == _GUARD_NAME
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "_lock_guard"
    )


def _is_context_manager(fn: _FunctionNode) -> bool:
    for decorator in fn.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id in _CM_DECORATORS:
            return True
        if isinstance(target, ast.Attribute) and target.attr in _CM_DECORATORS:
            return True
    return False


def _own_nodes(fn: _FunctionNode) -> Iterator[ast.AST]:
    """Every node in *fn*'s body, excluding nested function/class bodies."""
    pending: list[ast.AST] = list(fn.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _SCOPE_NODES):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _mentions_lock_ex(node: ast.AST) -> bool:
    return any(
        (isinstance(n, ast.Attribute) and n.attr == "LOCK_EX")
        or (isinstance(n, ast.Name) and n.id == "LOCK_EX")
        for n in ast.walk(node)
    )


def _is_acquisition(call: ast.Call) -> bool:
    name = _call_name(call)
    if name in _ACQUIRE_HELPERS:
        return True
    if name != "flock" or not isinstance(call.func, ast.Attribute):
        return False
    owner = call.func.value
    if not (isinstance(owner, ast.Name) and owner.id == "fcntl"):
        return False
    return any(_mentions_lock_ex(arg) for arg in call.args)


def _guard_spans(fn: _FunctionNode) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in _own_nodes(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if any(
            isinstance(item.context_expr, ast.Call) and _is_guard_call(item.context_expr)
            for item in node.items
        ):
            first, last = node.body[0], node.body[-1]
            spans.append((first.lineno, last.end_lineno or last.lineno))
    return spans


def _classify(fn: _FunctionNode) -> str | None:
    """``"guarded"``/``"unguarded"`` for a flock context manager, else ``None``."""
    if not _is_context_manager(fn):
        return None
    acquisitions = [
        n.lineno
        for n in _own_nodes(fn)
        if isinstance(n, ast.Call) and _is_acquisition(n)
    ]
    if not acquisitions:
        return None
    spans = _guard_spans(fn)
    enclosed = all(any(lo <= line <= hi for lo, hi in spans) for line in acquisitions)
    return "guarded" if enclosed else "unguarded"


class _LockCmCollector(ast.NodeVisitor):
    """Classify every flock context manager by qualified name."""

    def __init__(self) -> None:
        self._stack: list[tuple[str, str]] = []
        self.found: dict[str, str] = {}

    def _qualname(self) -> str:
        parts: list[str] = []
        for index, (kind, name) in enumerate(self._stack):
            parts.append(name)
            if kind == "function" and index < len(self._stack) - 1:
                parts.append("<locals>")
        return ".".join(parts)

    def _visit_function(self, node: _FunctionNode) -> None:
        self._stack.append(("function", node.name))
        verdict = _classify(node)
        if verdict is not None:
            self.found[self._qualname()] = verdict
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(("class", node.name))
        self.generic_visit(node)
        self._stack.pop()


def _scan_source(source: str, relpath: str) -> dict[tuple[str, str], str]:
    collector = _LockCmCollector()
    collector.visit(ast.parse(source, filename=relpath))
    return {(relpath, qualname): v for qualname, v in collector.found.items()}


def _scan_tree() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in _iter_src_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        found.update(_scan_source(path.read_text(encoding="utf-8"), relpath))
    return found


def test_every_flock_context_manager_is_guarded_or_exempt() -> None:
    scanned = _scan_tree()
    unguarded = {key for key, verdict in scanned.items() if verdict == "unguarded"}
    guarded = {key for key, verdict in scanned.items() if verdict == "guarded"}

    assert unguarded == set(_UNGUARDED), (
        "A flock context manager under src/cw is neither wrapped in "
        "cw._lock_guard.lock_guard nor listed in _UNGUARDED with a reason "
        f"(ADR-0019). Unguarded: {sorted(unguarded)}"
    )
    assert guarded == _GUARDED
    assert len(scanned) == len(_UNGUARDED) + len(_GUARDED)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    yield", "unguarded"),
        ("fcntl.flock(fd, fcntl.LOCK_EX)\n    yield", "unguarded"),
        ("try_flock_until(fd, timeout_s=1, poll_interval_s=1)\n    yield", "unguarded"),
        ("acquire_sessions_flock(fd, p, bounded=False)\n    yield", "unguarded"),
        ("fcntl.flock(fd, fcntl.LOCK_UN)\n    yield", None),
        (
            "with lock_guard('n', p, r):\n"
            "        fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "        yield",
            "guarded",
        ),
        (
            "with _lock_guard.lock_guard('n', p, r):\n"
            "        acquire_sessions_flock(fd, p, bounded=True)\n"
            "        yield",
            "guarded",
        ),
        (
            "with unrelated.lock_guard('n', p, r):\n"
            "        fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "        yield",
            "unguarded",
        ),
        (
            "with lock_guard('n', p, r):\n"
            "        pass\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "    yield",
            "unguarded",
        ),
        (
            "def inner():\n        fcntl.flock(fd, fcntl.LOCK_EX)\n    yield",
            None,
        ),
    ],
)
@pytest.mark.parametrize("decorator", ["@contextlib.contextmanager", "@contextmanager"])
def test_predicate_on_synthetic_sources(
    decorator: str, body: str, expected: str | None
) -> None:
    source = f"{decorator}\ndef cm():\n    {body}\n"

    scanned = _scan_source(source, "cw/x.py")

    assert scanned == ({} if expected is None else {("cw/x.py", "cm"): expected})


def test_predicate_ignores_non_context_managers_and_finds_async() -> None:
    source = (
        "def plain():\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "\n"
        "@contextlib.asynccontextmanager\n"
        "async def acm():\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "    yield\n"
        "\n"
        "class Holder:\n"
        "    @contextmanager\n"
        "    def method(self):\n"
        "        fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "        yield\n"
    )

    assert _scan_source(source, "cw/x.py") == {
        ("cw/x.py", "acm"): "unguarded",
        ("cw/x.py", "Holder.method"): "unguarded",
    }
