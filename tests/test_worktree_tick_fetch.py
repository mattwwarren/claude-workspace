"""Tests for cw.worktree._tick_fetch - the tick's bound on network git (#2910)."""

from __future__ import annotations

import ast
import contextlib
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dispatch.loop import TICK_STALE_SECONDS
from cw.exceptions import FetchSkippedError, GitTimeoutError
from cw.worktree import TickFetchGate, active_tick_fetch, run_network_git
from cw.worktree._tick_fetch import (
    _MIN_ATTEMPT_SECONDS,
    BACKOFF_SKIP_REASON,
    BUDGET_SKIP_REASON,
    TICK_FETCH_BACKOFF_SECONDS,
    TICK_FETCH_BUDGET_SECONDS,
    TICK_FETCH_TIMEOUT_SECONDS,
    active_warned,
)
from tests._worktree_helpers import FakeClock, HangingRun, hanging_run

if TYPE_CHECKING:
    from cw.worktree import FetchWarningKey

_RUN = "cw.worktree._git.subprocess.run"
_SRC = Path(__file__).resolve().parent.parent / "src" / "cw"
_DEFAULT_TICK_INTERVAL = 30
_SHORT_FETCH = 10.0
_JUST_BEFORE = 119.0
# (N per call, M per tick, B backoff), the plan's pinned numbers.
_PINNED_NMB = (15.0, 30.0, 120.0)


def _fetch(client: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run_network_git(client, "fetch", "origin", "main", cwd=cwd, check=False)


class _ScriptedRun:
    """A ``subprocess.run`` stub following a script, one entry per call.

    A float is the seconds that call takes and succeeds (it fits the granted
    timeout when it is ``<=`` it, unlike :class:`HangingRun`, whose call of
    exactly the timeout expires); ``None`` hangs until the granted timeout fires.
    Fails the test on an unbounded call.
    """

    def __init__(self, clock: FakeClock, script: list[float | None]) -> None:
        self.clock = clock
        self.steps = iter(script)
        self.timeouts: list[float] = []

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        if not isinstance(timeout, int | float):
            pytest.fail(f"unbounded network git call: {cmd} (timeout={timeout!r})")
        self.timeouts.append(float(timeout))
        step = next(self.steps)
        if step is not None and step <= timeout:
            self.clock.advance(step)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.clock.advance(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)


def _scripted_run(clock: FakeClock, script: list[float | None]) -> _ScriptedRun:
    return _ScriptedRun(clock, script)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1000.0)


@pytest.fixture
def gate(clock: FakeClock) -> TickFetchGate:
    return TickFetchGate(warned=set(), clock=clock)


def _stub(monkeypatch: pytest.MonkeyPatch, run: object) -> None:
    monkeypatch.setattr(_RUN, run)


class TestConstants:
    def test_values_and_their_arithmetic_against_the_stale_threshold(self) -> None:
        assert (
            TICK_FETCH_TIMEOUT_SECONDS,
            TICK_FETCH_BUDGET_SECONDS,
            TICK_FETCH_BACKOFF_SECONDS,
        ) == _PINNED_NMB
        assert TICK_FETCH_TIMEOUT_SECONDS <= TICK_FETCH_BUDGET_SECONDS
        assert 2 * TICK_FETCH_BUDGET_SECONDS <= TICK_STALE_SECONDS
        assert (
            TICK_FETCH_BUDGET_SECONDS + _DEFAULT_TICK_INTERVAL
            <= TICK_STALE_SECONDS - _DEFAULT_TICK_INTERVAL
        )

    def test_the_default_clock_is_monotonic(self) -> None:
        assert TickFetchGate(warned=set()).clock is time.monotonic


class TestStallKillCheck:
    def test_a_gated_fetch_is_granted_a_finite_timeout(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removing the ``timeout=`` plumbing makes the stub fail this test."""
        run = hanging_run(clock, hang_for=2.0)
        _stub(monkeypatch, run)

        with gate.tick():
            result = _fetch("c1", tmp_path)

        assert result.returncode == 0
        assert len(run.timeouts) == 1
        assert 0 < run.timeouts[0] <= TICK_FETCH_TIMEOUT_SECONDS

    def test_expiry_raises_git_timeout_error(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub(monkeypatch, hanging_run(clock))

        with gate.tick(), pytest.raises(GitTimeoutError) as exc_info:
            _fetch("c1", tmp_path)

        assert not isinstance(exc_info.value, FetchSkippedError)
        assert str(exc_info.value) == "git fetch origin main timed out"


class TestAggregateBudget:
    def test_three_hung_clients_spend_at_most_the_budget(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Kill-check: without the budget c3 would reach the stub (3 calls, 45 s)."""
        run = hanging_run(clock)
        _stub(monkeypatch, run)
        start = clock()

        with gate.tick():
            for client in ("c1", "c2"):
                with pytest.raises(GitTimeoutError):
                    _fetch(client, tmp_path)
            with pytest.raises(FetchSkippedError) as exc_info:
                _fetch("c3", tmp_path)

        assert str(exc_info.value) == BUDGET_SKIP_REASON
        assert run.timeouts == [TICK_FETCH_TIMEOUT_SECONDS] * 2
        assert clock() - start == TICK_FETCH_BUDGET_SECONDS

    def test_the_granted_timeout_shrinks_as_the_budget_is_spent(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exact grants: the third is truncated to what is left (30 - 20).

        Kill-check: granting ``per_call_s`` untruncated yields ``[15, 15, 15]``.
        """
        run = _scripted_run(clock, [_SHORT_FETCH] * 3)
        _stub(monkeypatch, run)

        with gate.tick():
            for client in ("c1", "c2", "c3"):
                _fetch(client, tmp_path)

        assert run.timeouts == [
            TICK_FETCH_TIMEOUT_SECONDS,
            TICK_FETCH_TIMEOUT_SECONDS,
            TICK_FETCH_BUDGET_SECONDS - 2 * _SHORT_FETCH,
        ]

    def test_less_than_the_minimum_attempt_of_budget_skips_the_call(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _scripted_run(clock, [0.0])
        _stub(monkeypatch, run)
        gate.budget_s = _MIN_ATTEMPT_SECONDS / 2  # 0.5 s left

        with gate.tick(), pytest.raises(FetchSkippedError) as exc_info:
            _fetch("c1", tmp_path)

        assert str(exc_info.value) == BUDGET_SKIP_REASON
        assert run.timeouts == []  # never reached the stub

    def test_exactly_the_minimum_attempt_of_budget_is_admitted_and_granted(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _scripted_run(clock, [0.0])
        _stub(monkeypatch, run)
        gate.budget_s = _MIN_ATTEMPT_SECONDS  # exactly 1.0 s left

        with gate.tick():
            _fetch("c1", tmp_path)

        assert run.timeouts == [_MIN_ATTEMPT_SECONDS]

    def test_a_timeout_under_a_truncated_grant_still_backs_the_client_off(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Current, deliberate behavior: a truncated-grant timeout is
        indistinguishable from a real hang, so it is charged and backs off."""
        # Two successful fetches spend 20 s, leaving 10 s (< N) for the third.
        run = _scripted_run(clock, [15.0, 5.0, None, 0.0])
        _stub(monkeypatch, run)
        start = clock()

        with gate.tick():
            _fetch("c1", tmp_path)
            _fetch("c2", tmp_path)
            with pytest.raises(GitTimeoutError) as exc_info:
                _fetch("c3", tmp_path)

        assert not isinstance(exc_info.value, FetchSkippedError)
        assert run.timeouts == [
            TICK_FETCH_TIMEOUT_SECONDS,
            TICK_FETCH_TIMEOUT_SECONDS,
            _SHORT_FETCH,
        ]
        assert clock() - start == TICK_FETCH_BUDGET_SECONDS  # truncated time charged

        clock.advance(TICK_FETCH_BACKOFF_SECONDS - 1)
        with gate.tick(), pytest.raises(FetchSkippedError) as skipped:
            _fetch("c3", tmp_path)
        assert str(skipped.value) == BACKOFF_SKIP_REASON

        clock.advance(1)
        with gate.tick():
            _fetch("c3", tmp_path)  # backoff lapsed: admitted again
        assert len(run.timeouts) == 4

    def test_the_next_tick_resets_the_budget_but_keeps_the_backoff(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = hanging_run(clock)
        _stub(monkeypatch, run)
        with gate.tick():
            for client in ("c1", "c2", "c3"):
                with pytest.raises(GitTimeoutError):
                    _fetch(client, tmp_path)

        clock.advance(_DEFAULT_TICK_INTERVAL)
        with gate.tick():
            for client in ("c1", "c2"):
                with pytest.raises(FetchSkippedError) as exc_info:
                    _fetch(client, tmp_path)
                assert str(exc_info.value) == BACKOFF_SKIP_REASON
            with pytest.raises(GitTimeoutError):
                _fetch("c3", tmp_path)

        # Tick 1: c1 and c2 time out, c3 is budget-skipped (no backoff).
        # Tick 2: c1 and c2 are backing off, c3 is admitted.
        assert run.timeouts == [TICK_FETCH_TIMEOUT_SECONDS] * 3


class TestBackoff:
    def test_a_timed_out_client_is_skipped_until_the_backoff_lapses(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = hanging_run(clock)
        _stub(monkeypatch, run)
        with gate.tick(), pytest.raises(GitTimeoutError):
            _fetch("c1", tmp_path)
        timed_out_at = clock()

        clock.now = timed_out_at + _JUST_BEFORE
        with gate.tick(), pytest.raises(FetchSkippedError):
            _fetch("c1", tmp_path)
        clock.now = timed_out_at + TICK_FETCH_BACKOFF_SECONDS
        with gate.tick(), pytest.raises(GitTimeoutError):
            _fetch("c1", tmp_path)

        assert run.timeouts == [TICK_FETCH_TIMEOUT_SECONDS] * 2

    def test_another_client_is_unaffected(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub(monkeypatch, hanging_run(clock))
        with gate.tick(), pytest.raises(GitTimeoutError):
            _fetch("c1", tmp_path)

        clock.advance(_DEFAULT_TICK_INTERVAL)
        _stub(monkeypatch, hanging_run(clock, hang_for=1.0))
        with gate.tick():
            assert _fetch("c2", tmp_path).returncode == 0

    def test_a_prompt_non_zero_exit_sets_no_backoff(
        self, gate: TickFetchGate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refused(
            cmd: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 128, "", "Permission denied (publickey)"
            )

        _stub(monkeypatch, _refused)

        with gate.tick():
            first = _fetch("c1", tmp_path)
            second = _fetch("c1", tmp_path)

        assert (first.returncode, second.returncode) == (128, 128)
        assert gate.degraded("c1") is False

    def test_a_skipped_call_charges_no_budget(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = hanging_run(clock)
        _stub(monkeypatch, run)
        with gate.tick():
            with pytest.raises(GitTimeoutError):
                _fetch("c1", tmp_path)
            for _ in range(3):
                with pytest.raises(FetchSkippedError):
                    _fetch("c1", tmp_path)
            with pytest.raises(GitTimeoutError):
                _fetch("c2", tmp_path)

        assert run.timeouts == [
            TICK_FETCH_TIMEOUT_SECONDS,
            TICK_FETCH_BUDGET_SECONDS - TICK_FETCH_TIMEOUT_SECONDS,
        ]


class TestDegraded:
    def test_timeout_and_skip_degrade_until_the_next_tick(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub(monkeypatch, hanging_run(clock))
        gate.budget_s = TICK_FETCH_TIMEOUT_SECONDS
        with gate.tick():
            with pytest.raises(GitTimeoutError):
                _fetch("c1", tmp_path)
            with pytest.raises(FetchSkippedError):
                _fetch("c2", tmp_path)
            degraded_in_tick = (gate.degraded("c1"), gate.degraded("c2"))
            assert gate.degraded("c3") is False

        with gate.tick():
            assert (gate.degraded("c1"), gate.degraded("c2")) == (False, False)

        assert degraded_in_tick == (True, True)

    def test_a_successful_fetch_does_not_degrade(
        self,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub(monkeypatch, hanging_run(clock, hang_for=1.0))

        with gate.tick():
            _fetch("c1", tmp_path)
            assert gate.degraded("c1") is False


class TestScope:
    def test_active_gate_is_set_only_inside_tick(self, gate: TickFetchGate) -> None:
        assert active_tick_fetch() is None
        assert active_warned() is None
        with gate.tick():
            assert active_tick_fetch() is gate
            assert active_warned() is gate.warned
        assert active_tick_fetch() is None

    def test_the_scope_is_reset_even_if_the_body_raises(
        self, gate: TickFetchGate
    ) -> None:
        msg = "tick body failed"
        with pytest.raises(RuntimeError, match=msg), gate.tick():
            raise RuntimeError(msg)

        assert active_tick_fetch() is None

    def test_the_gate_shares_the_callers_warned_set(self) -> None:
        warned: set[FetchWarningKey] = set()
        assert TickFetchGate(warned=warned).warned is warned

    def test_outside_a_scope_the_fetch_is_unbounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interactive ``cw start`` and operator commands keep today's behavior."""
        seen: list[object] = []

        def _record(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs.get("timeout", "absent"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        _stub(monkeypatch, _record)

        _fetch("c1", tmp_path)
        run_network_git("c1", "ls-remote", "origin", "main", cwd=tmp_path)

        assert seen == [None, None]

    @pytest.mark.parametrize("in_scope", [False, True])
    def test_a_mutating_verb_is_refused(
        self,
        in_scope: bool,
        gate: TickFetchGate,
        clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run: HangingRun = hanging_run(clock, hang_for=1.0)
        _stub(monkeypatch, run)
        scope = gate.tick() if in_scope else contextlib.nullcontext()

        with scope, pytest.raises(ValueError, match="pull"):
            run_network_git("c", "pull", "--ff-only", cwd=tmp_path)

        assert run.argv == []


def _timeout_call_sites(tree: ast.AST) -> list[tuple[str, str]]:
    """``(enclosing function, callee)`` for every call passing ``timeout=``."""
    sites: list[tuple[str, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not any(kw.arg == "timeout" for kw in node.keywords):
                continue
            callee = node.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else ""
            )
            sites.append((func.name, name))
    return sites


class TestNoMutatingCommandIsBounded:
    def test_only_run_network_git_passes_a_timeout_to_run_git(self) -> None:
        """A ``_run_git(..., timeout=...)`` anywhere else fails this pin."""
        offenders = [
            f"{path.relative_to(_SRC)}:{func}"
            for path in sorted(_SRC.rglob("*.py"))
            for func, callee in _timeout_call_sites(ast.parse(path.read_text()))
            if callee == "_run_git"
            and not (path.name == "_tick_fetch.py" and func == "run_network_git")
        ]

        assert offenders == []

    def test_every_timeout_in_the_worktree_package_goes_through_the_seam(
        self,
    ) -> None:
        allowed = {
            ("_tick_fetch.py", "run_network_git", "_run_git"),
            ("_git.py", "_run_git", "run"),
        }
        found = {
            (path.name, func, callee)
            for path in sorted((_SRC / "worktree").glob("*.py"))
            for func, callee in _timeout_call_sites(ast.parse(path.read_text()))
        }

        assert found == allowed
