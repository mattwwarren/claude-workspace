"""Self-tests for the suite-wide lock-invariant harness (``tests/_lock_invariants.py``).

The harness (#1233, ADR-0019) records every guarded lock event and every real
subprocess launched under ``sessions_lock``, then fails the test at teardown on
a re-entry, an order violation, an unallowlisted in-lock subprocess, or a lock
still held. These tests drive the real :class:`LockTrace` with real locks and
real subprocesses; a final end-to-end test runs an inner pytest session to
prove the genuine autouse fixture fails a seeded violation.

Tests here that cause a violation on purpose carry the matching
``lock_violations_expected`` marker: the autouse fixture's own trace sees the
event too, because :func:`install` chains in front of it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cw import history
from cw._git import run_git
from cw._lock_guard import GuardEvent, LockRank
from cw.config import sessions_lock
from cw.dev_queue.storage import dev_queue_lock
from cw.exceptions import CwError, CwLockOrderError
from tests._lock_invariants import (
    SUBPROCESS_UNDER_SESSIONS_ALLOWLIST,
    LockTrace,
    install,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HARNESS_SOURCE = _REPO_ROOT / "tests" / "_lock_invariants.py"
_JOIN_TIMEOUT_S = 10.0
_INNER_PYTEST_TIMEOUT_S = 120
_THREADS = 8
_PAIRS_PER_THREAD = 50
_TICKET_RE = re.compile(r"^#\d+$")

# Follow-up tickets that track the in-lock subprocess exceptions documented in
# ADR-0019 (invariant 3): the #1232 follow-ups plus the four the #1233 probe
# filed. An allowlist entry must cite one of these; a ticket stays listed after
# its entry is deleted (#2563's was), since this is the documented universe,
# not a mirror of the allowlist. #2545 is also closed, by deletion-free means:
# it never had an entry (the probe never recorded the stubbed in-lock plan
# read), and its read now runs in a lockless pre-pass. #2547's ADR row is
# deleted too; it never had an allowlist entry either (the fake daemon stop
# launches no real subprocess).
_DOCUMENTED_TICKETS = frozenset(
    {
        "#2545",
        "#2546",
        "#2547",
        "#2548",
        "#2549",
        "#2550",
        "#2551",
        "#2557",
        "#2563",
        "#2564",
        "#2565",
        "#2566",
        "#2641",
    }
)


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> LockTrace:
    """A trace with an empty allowlist, chained in front of the harness's."""
    return install(monkeypatch, allowlist={})


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@pytest.mark.lock_violations_expected("reentry")
def test_swallowed_reentry_is_recorded_and_fails(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    # The #1228 failure mode: a handler's broad ``except CwError`` swallows
    # the re-entry error, so only the recording can see it.
    with sessions_lock(), contextlib.suppress(CwError), sessions_lock():
        pass

    assert [v.kind for v in trace.violations()] == ["reentry"]
    with pytest.raises(
        AssertionError,
        match=r"reentry: thread \d+ \(MainThread\) re-entered 'sessions' "
        r"while holding sessions",
    ):
        trace.assert_clean()
    trace.assert_clean(expected=("reentry",))


@pytest.mark.lock_violations_expected("order")
def test_descending_order_is_recorded(tmp_config_dir: Path, trace: LockTrace) -> None:
    with dev_queue_lock(), contextlib.suppress(CwLockOrderError), sessions_lock():
        pass

    violations = trace.violations()
    assert [(v.kind, v.lock_name) for v in violations] == [("order", "sessions")]
    assert "while holding dev_queue" in violations[0].detail
    with pytest.raises(AssertionError, match="order: thread"):
        trace.assert_clean(expected=("reentry", "subprocess"))


@pytest.mark.lock_violations_expected("subprocess")
def test_cw_subprocess_under_sessions_lock_is_recorded(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    with sessions_lock():
        run_git(["--version"], capture_output=True)

    calls = trace.subprocess_calls()
    assert len(calls) == 1
    call = calls[0]
    assert call.argv0 == "git"
    assert call.cw_modules[0] == "cw._git"
    assert [h.name for h in call.held] == ["sessions"]
    assert call.forgiven_by is None
    assert call.thread_id == threading.get_ident()
    assert [v.kind for v in trace.violations()] == ["subprocess"]
    with pytest.raises(AssertionError, match=r"subprocess: thread \d+"):
        trace.assert_clean()


def test_cw_subprocess_after_release_is_clean(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    with sessions_lock():
        pass
    run_git(["--version"], capture_output=True)
    with dev_queue_lock():
        run_git(["--version"], capture_output=True)

    assert trace.subprocess_calls() == ()
    trace.assert_clean()


def test_subprocess_from_test_code_is_ignored(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    with sessions_lock():
        subprocess.run(["git", "--version"], capture_output=True, check=False)

    assert trace.subprocess_calls() == ()
    trace.assert_clean()


@pytest.mark.lock_violations_expected("subprocess")
def test_allowlisted_module_is_forgiven(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgiving = install(monkeypatch, allowlist={"cw._git": "#2550"})

    with sessions_lock():
        run_git(["--version"], capture_output=True)

    (call,) = forgiving.subprocess_calls()
    assert call.forgiven_by == "cw._git"
    assert forgiving.violations() == []
    forgiving.assert_clean()


@pytest.mark.parametrize("kinds", [("leak",), ("bogus",), ("reentry", "leak")])
def test_unknown_expected_kinds_raise(trace: LockTrace, kinds: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="lock_violations_expected"):
        trace.assert_clean(expected=kinds)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_concurrent_threads_lose_no_events(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    barrier = threading.Barrier(_THREADS)
    idents: dict[str, int] = {}

    def _worker(client: str) -> None:
        idents[client] = threading.get_ident()
        barrier.wait(_JOIN_TIMEOUT_S)
        for _ in range(_PAIRS_PER_THREAD):
            with history._history_lock(client):
                pass

    workers = [
        threading.Thread(target=_worker, args=(f"t{i}",), name=f"w{i}")
        for i in range(_THREADS)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(_JOIN_TIMEOUT_S)

    events = [e for e in trace.events() if e.lock_name == "history"]
    assert len(events) == 2 * _THREADS * _PAIRS_PER_THREAD
    for client, ident in idents.items():
        path = str(history._lock_path(client))
        mine = [e for e in events if e.path == path]
        assert {e.thread_id for e in mine} == {ident}
        assert [e.kind for e in mine] == ["acquire", "release"] * _PAIRS_PER_THREAD
    assert trace.leaked() == {}
    trace.assert_clean()


def test_lock_held_on_another_thread_is_reported_as_leak(
    tmp_config_dir: Path, trace: LockTrace
) -> None:
    holding = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with dev_queue_lock():
            holding.set()
            release.wait(_JOIN_TIMEOUT_S)

    worker = threading.Thread(target=_holder, name="leaky")
    worker.start()
    try:
        assert holding.wait(_JOIN_TIMEOUT_S)
        leaked = trace.leaked()
        assert list(leaked) == [worker.ident]
        assert [h.name for h in leaked[worker.ident or 0]] == ["dev_queue"]
        assert [h.rank for h in leaked[worker.ident or 0]] == [LockRank.STATE]
        expected_line = (
            rf"leak: thread {worker.ident} \(leaky, alive\) still holds "
            r"'dev_queue'"
        )
        with pytest.raises(AssertionError, match=expected_line):
            trace.assert_clean()
        # A leak can never be opted out of.
        with pytest.raises(AssertionError, match="leak: thread"):
            trace.assert_clean(expected=("reentry", "order", "subprocess"))
    finally:
        release.set()
        worker.join(_JOIN_TIMEOUT_S)

    assert trace.leaked() == {}
    trace.assert_clean()


def test_leak_on_dead_thread_reports_not_alive(trace: LockTrace) -> None:
    # Synthesize an acquire with no release from a thread id no live thread
    # has, the shape a thread that died holding a lock leaves behind.
    trace(
        GuardEvent(
            kind="acquire",
            lock_name="focus",
            path="/state/focus.lock",
            rank=LockRank.STATE,
            thread_id=-1,
            thread_name="gone",
            held_snapshot=(),
            enforced=False,
        )
    )

    with pytest.raises(AssertionError, match=r"leak: thread -1 \(gone, not alive\)"):
        trace.assert_clean()


# ---------------------------------------------------------------------------
# End to end through the real autouse fixture
# ---------------------------------------------------------------------------

_INNER_CONFTEST = 'pytest_plugins = ["tests.conftest"]\n'

_INNER_INI = """\
[pytest]
markers =
    integration: inner copy of the outer marker registry
    binary_on_path(*names): inner copy of the outer marker registry
    lock_violations_expected(*kinds): inner copy of the outer marker registry
"""

_INNER_TESTS = """\
import contextlib

import pytest

from cw.config import sessions_lock
from cw.exceptions import CwError


def test_seeded_violation(tmp_config_dir):
    with sessions_lock(), contextlib.suppress(CwError), sessions_lock():
        pass


def test_clean_control(tmp_config_dir):
    with sessions_lock():
        pass


@pytest.mark.lock_violations_expected("reentry")
def test_marker_opt_out(tmp_config_dir):
    with sessions_lock(), contextlib.suppress(CwError), sessions_lock():
        pass
"""


def test_real_fixture_fails_a_seeded_violation(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "conftest.py").write_text(_INNER_CONFTEST)
    (inner / "pytest.ini").write_text(_INNER_INI)
    (inner / "test_inner.py").write_text(_INNER_TESTS)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(inner / "pytest.ini"),
            "--rootdir",
            str(inner),
            "-p",
            "no:cacheprovider",
            "--color=no",
            "-v",
            str(inner / "test_inner.py"),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_INNER_PYTEST_TIMEOUT_S,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "test_inner.py::test_clean_control PASSED" in output, output
    assert "test_inner.py::test_marker_opt_out PASSED" in output, output
    assert "ERROR at teardown of test_seeded_violation" in output, output
    assert "re-entered 'sessions' while holding sessions" in output, output
    assert "1 error" in output, output


# ---------------------------------------------------------------------------
# Allowlist hygiene
# ---------------------------------------------------------------------------


def test_allowlist_modules_resolve() -> None:
    for module in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST:
        assert module.startswith("cw."), module
        assert importlib.util.find_spec(module) is not None, module


def test_codex_boot_is_not_allowlisted() -> None:
    """#2563: the codex orphan clean check's git runs in a lockless pre-pass,
    so no in-lock subprocess under a ``cw.reconcile.codex_boot`` frame is
    forgiven any more."""
    assert "cw.reconcile.codex_boot" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST


def test_gate_recipes_is_not_allowlisted() -> None:
    """#2545: the gate recipes' plan-of-record read (``gh``, and ``git`` for
    the ``.cw/plan.md`` fallback) runs in a lockless pre-pass, so no in-lock
    subprocess under either gate-recipe module is forgiven."""
    assert "cw.reconcile.gate_recipes" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
    assert "cw.reconcile.gate_plan_probes" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST


def test_pr_hydrate_is_not_allowlisted() -> None:
    """#2564: the review recipes' repo-slug ``git remote get-url`` runs in a
    lockless pre-pass, so no in-lock subprocess under a ``cw.pr_hydrate``
    frame is forgiven any more."""
    assert "cw.pr_hydrate" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST


def test_local_runner_is_not_allowlisted() -> None:
    """#2565: the local harvest's git facts are captured in a lockless
    pre-pass, so no in-lock subprocess under a ``cw.local_runner`` frame is
    forgiven any more."""
    assert "cw.local_runner" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST


def test_phantom_detect_and_tasks_are_not_allowlisted() -> None:
    """#2548: the phantom and task-backstop worktree dirty checks are captured
    in a lockless pre-pass, so no in-lock subprocess under either module's
    frame is forgiven any more."""
    assert "cw.reconcile.phantom._detect" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
    assert "cw.reconcile.tasks" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
    assert "#2548" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST.values()


def test_cli_spawn_is_not_allowlisted() -> None:
    """#2547: ``cw spawn close`` / ``complete --force`` stop the daemon surface
    after ``sessions_lock`` releases, so no in-lock subprocess under a
    ``cw.cli.spawn`` frame is forgiven."""
    assert "cw.cli.spawn" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST


def test_stop_hook_is_not_allowlisted() -> None:
    """#2566: the Stop hook's headless scope verification runs before
    sessions_lock, so no in-lock subprocess under any ``cw.cli.stop_hook``
    frame is forgiven any more."""
    assert "cw.cli.stop_hook.locked" not in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
    assert not any(
        module.startswith("cw.cli.stop_hook")
        for module in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST
    )


def test_stop_hook_headless_context_readers_share_one_key() -> None:
    """The Stop hook's two persisted headless reads use one key symbol."""
    source = (_REPO_ROOT / "src" / "cw" / "cli" / "stop_hook" / "locked.py").read_text(
        encoding="utf-8"
    )
    assert source.count('_HEADLESS_CONTEXT_KEY = "headless"') == 1
    assert source.count("context.get(_HEADLESS_CONTEXT_KEY)") == 2
    assert 'context.get("headless")' not in source


def test_allowlist_entries_cite_a_documented_ticket() -> None:
    source_lines = _HARNESS_SOURCE.read_text(encoding="utf-8").splitlines()
    for module, ticket in SUBPROCESS_UNDER_SESSIONS_ALLOWLIST.items():
        assert _TICKET_RE.match(ticket), (module, ticket)
        assert ticket in _DOCUMENTED_TICKETS, (module, ticket)
        entry = f'"{module}": "{ticket}",'
        (line,) = [ln for ln in source_lines if entry in ln]
        comment = line.split(entry, 1)[1]
        assert comment.lstrip().startswith("#"), line
        assert ticket in comment, line
