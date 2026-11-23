"""Tests for cw.dispatch_supervisor: the ``serve --supervised`` parent (#3726).

The child process, the import probe and the loop assessment are faked by the
private ``_FakeChild`` / ``_Harness`` helpers below; the clock is the shared
``_FakeClock`` installed as ``cw.dispatch_supervisor.time``, so every poll,
backoff and stall window is a pure function of the scripted children. Tests
that raise a real signal or spawn a real process carry ``platform_sensitive``
and pin the SIGHUP disposition with ``sighup_disposition``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from cw import dispatch_supervisor as sup
from cw.config import dispatch_loop_lock, dispatch_loop_lock_file
from cw.dispatch_loop_identity import CodeIdentity, LoopAssessment, LoopStatus
from cw.dispatch_reexec import REEXEC_EXIT_CODE, SUPERVISOR_PID_ENV
from cw.dispatch_supervisor import (
    _SIGHUP_DEFAULT_TRAMPOLINE,
    ServeOptions,
    build_child_argv,
    probe_install,
    run_supervised_serve,
)
from cw.exceptions import DispatchServeError
from cw.models import OrchestratorEventType
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    VERSION,
    patch_checkout_identity,
    sighup_disposition,
)
from tests._shared.lock_helpers import _FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_IDENT_A = CodeIdentity(VERSION, COMMIT_A)
_IDENT_B = CodeIdentity(VERSION, COMMIT_B)
_IDENT_C = CodeIdentity(VERSION, "c" * 40)
_LABEL_B = f"{VERSION} @ {COMMIT_B[:12]}"
_LIMIT_SECONDS = 20_000.0
_EXITED = OrchestratorEventType.DISPATCH_LOOP_EXITED
_STALLED = OrchestratorEventType.DISPATCH_RESTART_STALLED


@pytest.fixture(autouse=True)
def _no_supervisor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISOR_PID_ENV, raising=False)


@dataclass
class _Script:
    """How one fake child behaves; times are seconds after its spawn."""

    exit_after: float | None = None
    code: int = 0
    on_sighup: int | None = REEXEC_EXIT_CODE
    hup_delay: float = 0.0
    on_sigint: int | None = 0
    stale: bool = False
    current: CodeIdentity = _IDENT_B
    register_after: float = 0.0
    hold_lock: bool = False


@dataclass
class _FakeChild:
    pid: int
    clock: _FakeClock
    script: _Script
    argv: list[str]
    env: dict[str, str]
    spawned_at: float = 0.0
    exit_at: float | None = None
    exit_code: int = 0
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    signal_times: list[float] = field(default_factory=list)
    killed: bool = False
    lock: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)

    def __post_init__(self) -> None:
        self.spawned_at = self.clock.now
        if self.script.exit_after is not None:
            self.exit_at = self.spawned_at + self.script.exit_after
            self.exit_code = self.script.code
        if self.script.hold_lock:
            self.lock.enter_context(dispatch_loop_lock())

    def _finish(self, code: int) -> int:
        self.returncode = code
        self.lock.close()
        return code

    def poll(self) -> int | None:
        due = self.exit_at is not None and self.clock.now >= self.exit_at
        if self.returncode is None and due:
            return self._finish(self.exit_code)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if self.exit_at is not None and (
            timeout is None or self.exit_at <= self.clock.now + timeout
        ):
            self.clock.now = max(self.clock.now, self.exit_at)
            return self._finish(self.exit_code)
        assert timeout is not None, "wait() without a timeout on a live child"
        self.clock.now += timeout
        raise subprocess.TimeoutExpired(cmd="fake-child", timeout=timeout)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.signal_times.append(self.clock.now)
        exit_code = {
            signal.SIGHUP: self.script.on_sighup,
            signal.SIGINT: self.script.on_sigint,
        }.get(signal.Signals(sig))
        if exit_code is None:
            return
        delay = self.script.hup_delay if sig == signal.SIGHUP else 0.0
        at = self.clock.now + delay
        if self.exit_at is None or at < self.exit_at:
            self.exit_at = at
            self.exit_code = exit_code

    def kill(self) -> None:
        self.killed = True
        self._finish(-signal.SIGKILL)

    @property
    def alive(self) -> bool:
        return self.returncode is None


class _Harness:
    """Fakes the child processes, the probe, the assessment and the events."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripts: list[_Script],
        *,
        probe_results: list[tuple[bool, str]] | None = None,
    ) -> None:
        self.scripts = scripts
        self.children: list[_FakeChild] = []
        self.probes: list[float] = []
        self.probe_results = list(probe_results or [])
        self.events: list[tuple[OrchestratorEventType, dict[str, object], float]] = []
        self.hooks: list[Callable[[_Harness], None]] = []
        self.current_override: CodeIdentity | None = None
        self.clock = _FakeClock(on_sleep=self.on_tick)
        monkeypatch.setattr("cw.dispatch_supervisor.time", self.clock)
        monkeypatch.setattr("cw.dispatch_supervisor._spawn_child", self.spawn)
        monkeypatch.setattr("cw.dispatch_supervisor.probe_install", self.probe)
        monkeypatch.setattr("cw.dispatch_supervisor.assess_dispatch_loop", self.assess)
        monkeypatch.setattr("cw.dispatch_supervisor.record_event", self.record)

    def spawn(self, argv: list[str], env: dict[str, str]) -> _FakeChild:
        script = self.scripts[len(self.children)]
        child = _FakeChild(1000 + len(self.children), self.clock, script, argv, env)
        self.children.append(child)
        return child

    def probe(self, timeout: float) -> tuple[bool, str]:
        assert timeout == sup._PROBE_TIMEOUT_SECONDS
        self.probes.append(self.clock.now)
        return self.probe_results.pop(0) if self.probe_results else (True, "")

    def assess(self) -> LoopAssessment:
        child = self.children[-1]
        registered = (
            child.alive
            and self.clock.now >= child.spawned_at + child.script.register_after
        )
        if not registered:
            return LoopAssessment(LoopStatus.NO_LOOP, None, None, None)
        current = self.current_override or child.script.current
        status = LoopStatus.STALE if child.script.stale else LoopStatus.MATCH
        return LoopAssessment(status, child.pid, _IDENT_A, current)

    def record(
        self,
        etype: OrchestratorEventType,
        payload: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> None:
        self.events.append((etype, dict(payload or {}), self.clock.now))

    def on_tick(self, _count: int) -> None:
        assert self.clock.now < _LIMIT_SECONDS, "supervisor never finished"
        for hook in list(self.hooks):
            hook(self)

    def run(self, disposition: int = signal.SIG_DFL) -> None:
        with sighup_disposition(disposition):
            run_supervised_serve(
                max_parallel=None,
                use_plan=False,
                parent=None,
                quiet=False,
                auto_ff=True,
                client=None,
                max_restarts=-1,
            )

    def other_sleeps(self) -> list[float]:
        return [s for s in self.clock.sleeps if s != sup._POLL_SECONDS]


def _stale_child(**overrides: object) -> _Script:
    return dataclasses.replace(_Script(stale=True), **overrides)


def _once_at(
    when: float, action: Callable[[_Harness], None]
) -> Callable[[_Harness], None]:
    fired: list[bool] = []

    def _hook(harness: _Harness) -> None:
        if not fired and harness.clock.now >= when:
            fired.append(True)
            action(harness)

    return _hook


def _raise(sig: signal.Signals) -> Callable[[_Harness], None]:
    def _action(_harness: _Harness) -> None:
        signal.raise_signal(sig)

    return _action


def _stderr_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line]


# ---------------------------------------------------------------------------
# argv, env and the probe
# ---------------------------------------------------------------------------


def test_child_argv_forwards_every_option_and_drops_supervised() -> None:
    options = ServeOptions(
        max_parallel=3,
        use_plan=True,
        parent="sess-1",
        quiet=True,
        auto_ff=False,
        client="acme",
        max_restarts=2,
    )
    assert build_child_argv(options, reset_sighup=False) == [
        sys.executable,
        "-m",
        "cw",
        "dev-queue",
        "serve",
        "--max-parallel",
        "3",
        "--use-plan",
        "--parent",
        "sess-1",
        "--quiet",
        "--no-auto-ff",
        "--client",
        "acme",
        "--max-restarts",
        "2",
    ]
    defaults = ServeOptions(
        max_parallel=None,
        use_plan=False,
        parent=None,
        quiet=False,
        auto_ff=True,
        client=None,
        max_restarts=-1,
    )
    argv = build_child_argv(defaults, reset_sighup=False)
    assert argv[3:] == ["dev-queue", "serve", "--auto-ff", "--max-restarts", "-1"]
    assert "--supervised" not in argv
    assert "--force" not in argv


def test_spawn_env_carries_supervisor_pid_and_uses_sys_executable_dash_m_cw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=5)])
    harness.run()
    child = harness.children[0]
    assert child.env[SUPERVISOR_PID_ENV] == str(os.getpid())
    assert child.argv[:5] == [sys.executable, "-m", "cw", "dev-queue", "serve"]


@pytest.mark.platform_sensitive
def test_probe_install_passes_on_this_install() -> None:
    assert probe_install(120.0) == (True, "")


@pytest.mark.platform_sensitive
def test_probe_install_reports_the_last_stderr_line_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sup, "_PROBE_CODE", "import cw_no_such_module_3726")
    ok, detail = probe_install(120.0)
    assert ok is False
    assert detail.startswith("exit 1: ModuleNotFoundError:")
    assert "cw_no_such_module_3726" in detail


def test_probe_install_timeout_and_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="probe", timeout=120.0)

    monkeypatch.setattr("cw.dispatch_supervisor.subprocess.run", _timeout)
    assert probe_install(120.0) == (False, "timed out after 120s")

    def _missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("cw.dispatch_supervisor.subprocess.run", _missing)
    assert probe_install(120.0) == (False, "could not start: FileNotFoundError")


# ---------------------------------------------------------------------------
# restart trigger
# ---------------------------------------------------------------------------


def test_clean_child_exit_returns_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=5)])
    harness.run()
    assert len(harness.children) == 1
    assert harness.probes == []
    assert harness.events == []


def test_stale_loop_probe_ok_sends_sighup_then_respawns_on_exit_75(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 1: STALE -> probe -> SIGHUP -> exit 75 -> fresh child."""
    harness = _Harness(monkeypatch, [_stale_child(), _Script(exit_after=100)])
    harness.run()
    first, second = harness.children
    assert first.signals == [signal.SIGHUP]
    assert first.signal_times == [60.0]
    assert harness.probes == [60.0]
    assert first.returncode == REEXEC_EXIT_CODE
    assert second.argv == first.argv
    assert second.spawned_at == 61.0
    assert second.returncode == 0
    assert harness.events == []


def test_match_status_sends_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=300)])
    harness.run()
    assert harness.children[0].signals == []
    assert harness.probes == []


def test_sighup_deferred_until_child_registered_in_lock_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The automatic path waits for registration; an unregistered child reads
    NO_LOOP. ``test_operator_sighup_waits_for_child_registration`` pins the
    ``assessment.pid == child.pid`` guard itself, since the operator path
    forwards whatever the status."""
    harness = _Harness(
        monkeypatch, [_stale_child(register_after=125), _Script(exit_after=5)]
    )
    harness.run()
    assert harness.children[0].signal_times == [130.0]


def test_no_auto_restart_within_min_interval_of_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(exit_after=59)])
    harness.run()
    assert harness.children[0].signals == []
    assert harness.probes == []


# ---------------------------------------------------------------------------
# the import probe and its backoff
# ---------------------------------------------------------------------------


def test_probe_failure_keeps_old_child_and_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(
        monkeypatch,
        [_stale_child(exit_after=75)],
        probe_results=[(False, "exit 1: ImportError: boom")],
    )
    harness.run()
    assert harness.children[0].signals == []
    assert _stderr_lines(capsys) == [
        f"supervisor: not restarting the dispatch loop onto {_LABEL_B}: the new"
        " install failed its import check (exit 1: ImportError: boom); keeping the"
        " old loop (pid 1000) and re-checking in 30s"
    ]


def test_probe_failure_sets_no_memo_and_counts_no_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_fail = _Script(exit_after=2, code=1)
    harness = _Harness(
        monkeypatch,
        [_stale_child(), fast_fail, fast_fail, fast_fail],
        probe_results=[(False, "exit 1: ImportError: boom")],
    )
    with pytest.raises(DispatchServeError):
        harness.run()
    assert harness.probes == [60.0, 90.0]
    assert harness.children[0].signal_times == [90.0]
    assert harness.events[-1][1]["failed_starts"] == 3


def test_failed_probe_is_reprobed_after_backoff_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch,
        [_stale_child(), _Script(exit_after=5)],
        probe_results=[(False, "exit 1: ImportError: half-synced")],
    )
    harness.run()
    assert harness.probes == [60.0, 90.0]
    assert harness.children[0].signals == [signal.SIGHUP]
    assert len(harness.children) == 2


def test_probe_backoff_doubles_to_cap_and_resets_on_identity_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(
        monkeypatch,
        [_stale_child(exit_after=2210)],
        probe_results=[(False, "exit 1: ImportError: boom")] * 9,
    )

    def _move(h: _Harness) -> None:
        h.current_override = _IDENT_C

    harness.hooks.append(_once_at(2195, _move))
    harness.run()
    assert harness.probes == [60, 90, 150, 270, 510, 990, 1590, 2190, 2200]
    delays = [line.rsplit("re-checking in ", 1)[1] for line in _stderr_lines(capsys)]
    assert delays == [
        "30s",
        "60s",
        "120s",
        "240s",
        "480s",
        "600s",
        "600s",
        "600s",
        "30s",
    ]
    assert harness.children[0].signals == []


# ---------------------------------------------------------------------------
# one automatic restart per checkout identity; the operator override
# ---------------------------------------------------------------------------


def test_same_identity_second_auto_restart_suppressed_and_noted_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(), _stale_child(exit_after=300)])
    harness.run()
    assert harness.children[1].signals == []
    assert harness.probes == [60.0]
    assert _stderr_lines(capsys) == [
        "supervisor: the dispatch loop is still stale against"
        f" {_LABEL_B} after an automatic restart; not restarting again for this"
        f" checkout. Send SIGHUP to the supervisor (pid {os.getpid()}) to force one"
    ]


def test_new_identity_allows_another_auto_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch,
        [_stale_child(), _stale_child(current=_IDENT_C), _Script(exit_after=5)],
    )
    harness.run()
    assert len(harness.children) == 3
    assert harness.children[1].signal_times == [130.0]


@pytest.mark.platform_sensitive
def test_operator_sighup_overrides_identity_memo_and_min_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch, [_stale_child(), _stale_child(), _Script(exit_after=5)]
    )
    harness.hooks.append(_once_at(66, _raise(signal.SIGHUP)))
    harness.run()
    second = harness.children[1]
    assert second.spawned_at == 61.0
    assert second.signals == [signal.SIGHUP]
    assert second.signal_times[0] - second.spawned_at < 60
    assert len(harness.children) == 3
    # The operator's restart is a requested re-exec, not a failed start: the
    # replacement is spawned at once, with no back-off and no give-up event.
    assert harness.other_sleeps() == []
    assert harness.events == []


@pytest.mark.platform_sensitive
def test_repeated_operator_restarts_within_60s_never_give_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three operator SIGHUPs, each within 60s of the last re-exec, are no storm."""
    harness = _Harness(
        monkeypatch,
        [*(_stale_child() for _ in range(4)), _Script(exit_after=5)],
    )
    for when in (66, 75, 85):
        harness.hooks.append(_once_at(when, _raise(signal.SIGHUP)))
    harness.run()
    assert len(harness.children) == 5
    assert [c.signals for c in harness.children[1:4]] == [[signal.SIGHUP]] * 3
    assert harness.other_sleeps() == []
    assert harness.events == []


@pytest.mark.platform_sensitive
def test_operator_sighup_during_forwarded_restart_does_not_restart_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGHUP that lands while the restart is in flight is spent with it."""
    harness = _Harness(
        monkeypatch, [_stale_child(hup_delay=30), _Script(exit_after=300)]
    )
    harness.hooks.append(_once_at(70, _raise(signal.SIGHUP)))
    harness.run()
    assert harness.children[0].signal_times == [60.0]
    assert len(harness.children) == 2
    assert harness.children[1].signals == []
    assert harness.probes == [60.0]


@pytest.mark.platform_sensitive
def test_operator_sighup_with_failing_probe_still_refuses_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(
        monkeypatch,
        [_Script(exit_after=100)],
        probe_results=[(False, "exit 1: ImportError: boom")],
    )
    harness.hooks.append(_once_at(5, _raise(signal.SIGHUP)))
    harness.run()
    assert harness.children[0].signals == []
    assert harness.probes == [5.0]
    lines = _stderr_lines(capsys)
    assert len(lines) == 1
    assert lines[0].startswith("supervisor: not restarting the dispatch loop onto")


@pytest.mark.platform_sensitive
def test_external_sighup_to_parent_probes_then_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=500), _Script(exit_after=5)])
    harness.hooks.append(_once_at(3, _raise(signal.SIGHUP)))
    harness.run()
    assert harness.probes == [3.0]
    assert harness.children[0].signal_times == [3.0]
    assert len(harness.children) == 2


@pytest.mark.platform_sensitive
def test_operator_sighup_waits_for_child_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch, [_Script(exit_after=500, register_after=20), _Script(exit_after=5)]
    )
    harness.hooks.append(_once_at(3, _raise(signal.SIGHUP)))
    harness.run()
    assert harness.probes == [20.0]
    assert harness.children[0].signal_times == [20.0]


# ---------------------------------------------------------------------------
# start-failure policy
# ---------------------------------------------------------------------------


def _gave_up_payload(returncode: int, failed_starts: int) -> dict[str, object]:
    return {
        "normal": False,
        "exception_type": None,
        "codex_threads_still_running": 0,
        "reason": "supervisor_gave_up",
        "child_returncode": returncode,
        "failed_starts": failed_starts,
    }


def test_failed_new_install_does_not_restart_loop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: three fast failures after a re-exec stop the supervisor.

    The three children launched after the re-exec each exit 1 within 5s, so
    there are three post-re-exec spawns (four in all, counting the original).
    """
    fast_fail = _Script(exit_after=2, code=1)
    harness = _Harness(monkeypatch, [_stale_child(), fast_fail, fast_fail, fast_fail])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert len(harness.children[1:]) == 3
    assert harness.other_sleeps() == [5.0, 10.0]
    assert _stderr_lines(capsys) == [
        "supervisor: giving up after 3 consecutive failed starts of the dispatch"
        " loop (last exit status 1); the loop is NOT running. Fix the install,"
        " then start `cw dev-queue serve --supervised` again"
    ]
    assert [(etype, payload) for etype, payload, _ in harness.events] == [
        (_EXITED, _gave_up_payload(1, 3))
    ]


def test_reexec_storm_returning_75_repeatedly_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storm = _Script(exit_after=5, code=REEXEC_EXIT_CODE)
    harness = _Harness(monkeypatch, [_stale_child(), storm, storm, storm])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert len(harness.children) == 4
    assert harness.events[-1][1] == _gave_up_payload(REEXEC_EXIT_CODE, 3)


def test_healthy_run_resets_start_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_fail = _Script(exit_after=2, code=1)
    healthy_reexec = _Script(exit_after=100, code=REEXEC_EXIT_CODE)
    harness = _Harness(
        monkeypatch,
        [
            _stale_child(),
            fast_fail,
            healthy_reexec,
            fast_fail,
            fast_fail,
            _Script(exit_after=5),
        ],
    )
    harness.run()
    assert len(harness.children) == 6
    assert harness.other_sleeps() == [5.0, 5.0, 10.0]
    assert harness.events == []


def test_first_child_fast_failure_stops_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=1, code=1)])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert len(harness.children) == 1
    assert harness.other_sleeps() == []
    assert _stderr_lines(capsys) == [
        "supervisor: dispatch loop (pid 1000) exited with status 1; not"
        " restarting. The loop is NOT running; restart `cw dev-queue serve"
        " --supervised` after fixing the cause"
    ]
    assert harness.events[0][1] == _gave_up_payload(1, 0)


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_stop_during_failed_start_backoff_spawns_no_new_child(
    monkeypatch: pytest.MonkeyPatch, stop_signal: signal.Signals
) -> None:
    """A stop requested while backing off from a failed start is honoured."""
    harness = _Harness(
        monkeypatch,
        [_stale_child(), _Script(exit_after=2, code=1), _Script(exit_after=5)],
    )
    harness.hooks.append(_once_at(64, _raise(stop_signal)))
    harness.run()
    assert len(harness.children) == 2
    assert harness.other_sleeps() == [5.0]
    assert harness.events == []


@pytest.mark.platform_sensitive
def test_operator_sighup_during_failed_start_backoff_does_not_restart_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGHUP that lands while no child runs must not hit the replacement."""
    harness = _Harness(
        monkeypatch,
        [_stale_child(), _Script(exit_after=2, code=1), _Script(exit_after=200)],
    )
    harness.hooks.append(_once_at(64, _raise(signal.SIGHUP)))
    harness.run()
    assert len(harness.children) == 3
    assert harness.other_sleeps() == [5.0]
    assert harness.children[2].signals == []
    assert harness.probes == [60.0]


def test_child_nonzero_after_healthy_run_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(), _Script(exit_after=100, code=1)])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert len(harness.children) == 2
    assert harness.other_sleeps() == []


def test_first_child_exit_75_restarts_even_when_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first child's own 75 (version drift, a SIGHUP to the child) re-execs."""
    harness = _Harness(
        monkeypatch,
        [_Script(exit_after=3, code=REEXEC_EXIT_CODE), _Script(exit_after=5)],
    )
    harness.run()
    assert len(harness.children) == 2


def test_child_killed_by_signal_stops_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=5, code=-signal.SIGKILL)])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert _stderr_lines(capsys) == [
        "supervisor: dispatch loop (pid 1000) was killed by SIGKILL; not"
        " restarting. The loop is NOT running; restart `cw dev-queue serve"
        " --supervised`"
    ]
    assert harness.events[0][1] == _gave_up_payload(-signal.SIGKILL, 0)


def test_child_killed_by_unknown_signal_number_is_named_by_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(monkeypatch, [_Script(exit_after=5, code=-250)])
    with pytest.raises(DispatchServeError):
        harness.run()
    assert "was killed by signal 250;" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the 10-minute stall signal
# ---------------------------------------------------------------------------


def _stall_line(pid: int) -> str:
    return (
        f"supervisor: dispatch loop (pid {pid}) has not exited 10 minutes after"
        " the restart SIGHUP; a tick may be stuck on an external call. Not"
        f" forcing the exit (never mid-tick); inspect or stop pid {pid} yourself"
    )


def test_stalled_exit_signal_after_ten_minutes_emits_event_and_stderr_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(on_sighup=None, exit_after=1300)])
    harness.run()
    stalls = [
        (payload, at) for etype, payload, at in harness.events if etype is _STALLED
    ]
    assert stalls == [
        ({"child_pid": 1000, "stalled_seconds": 600.0, "checkout": _LABEL_B}, 660.0)
    ]
    assert _stderr_lines(capsys) == [_stall_line(1000)]


def test_stalled_child_is_never_signalled_again_or_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(on_sighup=None, exit_after=3700)])
    harness.run()
    child = harness.children[0]
    assert child.signals == [signal.SIGHUP]
    assert child.killed is False
    assert child.returncode == 0
    assert len(harness.children) == 1


def test_stall_clock_resets_when_child_exits_and_next_restart_can_stall_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch,
        [
            _stale_child(hup_delay=700),
            _stale_child(hup_delay=700, current=_IDENT_C),
            _Script(exit_after=5),
        ],
    )
    harness.run()
    stalls = [at for etype, _payload, at in harness.events if etype is _STALLED]
    assert stalls == [660.0, 1420.0]
    assert len(harness.children) == 3


# ---------------------------------------------------------------------------
# SIGTERM / SIGINT, the lock, and the SIGHUP disposition
# ---------------------------------------------------------------------------


@pytest.mark.platform_sensitive
def test_sigterm_sends_child_sigint_then_kill_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_Script(on_sigint=None)])
    harness.hooks.append(_once_at(5, _raise(signal.SIGTERM)))
    harness.run()
    child = harness.children[0]
    assert child.signals == [signal.SIGINT]
    assert child.killed is True
    assert harness.clock.now == 5.0 + sup._STOP_GRACE_SECONDS
    assert len(harness.children) == 1


@pytest.mark.platform_sensitive
def test_sigterm_child_that_stops_in_grace_is_not_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_stale_child()])
    harness.hooks.append(_once_at(5, _raise(signal.SIGTERM)))
    harness.run()
    child = harness.children[0]
    assert child.signals == [signal.SIGINT]
    assert child.killed is False
    assert harness.probes == []


@pytest.mark.platform_sensitive
def test_sigint_sets_stopping_without_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch, [_stale_child(exit_after=200, code=1)])
    harness.hooks.append(_once_at(5, _raise(signal.SIGINT)))
    harness.run()
    child = harness.children[0]
    assert child.signals == []
    assert harness.probes == []
    assert harness.events == []
    assert len(harness.children) == 1


def test_supervisor_never_holds_dispatch_loop_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        monkeypatch,
        [_stale_child(hold_lock=True), _Script(exit_after=5, hold_lock=True)],
    )
    harness.run()
    first, second = harness.children
    assert first.returncode == REEXEC_EXIT_CODE
    assert second.spawned_at > first.signal_times[0]
    with dispatch_loop_lock():
        pass


@pytest.mark.platform_sensitive
def test_parent_with_sighup_default_spawns_plain_dash_m_cw_and_installs_sighup_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    harness = _Harness(monkeypatch, [_Script(exit_after=3)])
    harness.hooks.append(lambda _h: seen.append(signal.getsignal(signal.SIGHUP)))
    harness.run(signal.SIG_DFL)
    assert harness.children[0].argv[:3] == [sys.executable, "-m", "cw"]
    assert seen
    assert all(callable(handler) for handler in seen)
    assert signal.SIG_DFL not in seen


@pytest.mark.platform_sensitive
def test_parent_with_sighup_ignored_skips_handler_and_spawns_through_default_trampoline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    harness = _Harness(monkeypatch, [_Script(exit_after=3)])
    harness.hooks.append(lambda _h: seen.append(signal.getsignal(signal.SIGHUP)))
    harness.run(signal.SIG_IGN)
    assert harness.children[0].argv[:6] == [
        sys.executable,
        "-c",
        _SIGHUP_DEFAULT_TRAMPOLINE,
        sys.executable,
        "-m",
        "cw",
    ]
    assert seen
    assert all(handler is signal.SIG_IGN for handler in seen)


# ---------------------------------------------------------------------------
# real processes
# ---------------------------------------------------------------------------


@pytest.mark.platform_sensitive
def test_trampoline_restores_sighup_default_in_a_real_subprocess() -> None:
    show = "import signal; print(int(signal.getsignal(signal.SIGHUP)))"
    with sighup_disposition(signal.SIG_IGN):
        plain = subprocess.run(
            [sys.executable, "-c", show],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        tramp = subprocess.run(
            [
                sys.executable,
                "-c",
                _SIGHUP_DEFAULT_TRAMPOLINE,
                sys.executable,
                "-c",
                show,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    assert plain.stdout.strip() == str(int(signal.SIG_IGN))
    assert tramp.stdout.strip() == str(int(signal.SIG_DFL))


_REAL_CHILD = """
import json, os, signal, sys, time
from cw.local_runner import read_process_start_time_ns
lock_path, flag_path, version, commit = sys.argv[1:5]
with open(flag_path + ".starts", "a") as starts:
    starts.write("start\\n")
if os.path.exists(flag_path):
    sys.exit(0)
open(flag_path, "w").close()
got = []
signal.signal(signal.SIGHUP, lambda signum, frame: got.append(signum))
record = {
    "pid": os.getpid(),
    "cmd": "cw dev-queue serve",
    "proc_start_ns": read_process_start_time_ns(os.getpid()),
    "version": version,
    "commit": commit,
}
with open(lock_path, "w") as fh:
    json.dump(record, fh)
deadline = time.monotonic() + 30
while not got and time.monotonic() < deadline:
    time.sleep(0.05)
sys.exit(75 if got else 3)
"""


@pytest.mark.platform_sensitive
def test_real_subprocess_sighup_roundtrip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = dispatch_loop_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flag = tmp_path / "first-child-ran"
    argv = [
        sys.executable,
        "-c",
        _REAL_CHILD,
        str(lock_path),
        str(flag),
        VERSION,
        COMMIT_A,
    ]

    def _argv(_options: ServeOptions, *, reset_sighup: bool) -> list[str]:
        assert reset_sighup is False
        return argv

    monkeypatch.setattr(sup, "build_child_argv", _argv)
    monkeypatch.setattr(sup, "probe_install", lambda _timeout: (True, ""))
    monkeypatch.setattr(sup, "_MIN_RESTART_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(sup, "_DRIFT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(sup, "_POLL_SECONDS", 0.05)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    with sighup_disposition(signal.SIG_DFL):
        run_supervised_serve(
            max_parallel=None,
            use_plan=False,
            parent=None,
            quiet=True,
            auto_ff=True,
            client=None,
            max_restarts=-1,
        )
    assert flag.exists()
    # The flag exists once the first child starts, before any SIGHUP: only a
    # second start proves the supervisor respawned after the child's exit 75.
    starts = (tmp_path / "first-child-ran.starts").read_text().splitlines()
    assert starts == ["start", "start"]
