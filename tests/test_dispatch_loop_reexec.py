"""The supervised dispatch loop exits at an idle point on SIGHUP (#3726).

Real in-process SIGHUP: a tick in progress always finishes, the flag is read
only in ``idle_wait`` at the end of an iteration, and the exit event carries
``reason: "reexec"``. Also covers the ``loop_lifecycle`` helpers the loop's
``finally`` delegates to.
"""

from __future__ import annotations

import importlib.metadata
import signal
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import cw.dispatch.loop as loop_mod
from cw.config import load_effective_config
from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch import DispatchTickResult, run_dispatch_loop
from cw.dispatch.loop_lifecycle import (
    drain_codex_threads,
    idle_wait,
    loop_exit_payload,
)
from cw.dispatch_reexec import SUPERVISOR_PID_ENV, ReexecControl, sighup_reexec
from cw.exceptions import VersionDriftError
from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._loop_identity_helpers import sighup_disposition

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig

pytestmark = pytest.mark.platform_sensitive

_PKG = "claude-workspace"


@pytest.fixture(autouse=True)
def _no_supervisor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISOR_PID_ENV, raising=False)


class _SignallingDaemon(FakeNativeDaemonClient):
    """Raises a real SIGHUP inside the claim, before the spawn completes."""

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        signal.raise_signal(signal.SIGHUP)
        return super().spawn_bg(
            cwd=cwd,
            prompt=prompt,
            extra_args=extra_args,
            permission_mode=permission_mode,
        )


def _capture_exit_events(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    payloads: list[object] = []
    real = loop_mod.record_event

    def _capture(
        etype: OrchestratorEventType,
        payload: dict[str, object] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> object:
        if etype is OrchestratorEventType.DISPATCH_LOOP_EXITED:
            payloads.append(payload)
        return real(etype, payload, correlation_id=correlation_id)

    monkeypatch.setattr("cw.dispatch.loop.record_event", _capture)
    return payloads


def _count_ticks(monkeypatch: pytest.MonkeyPatch, *, real: bool) -> MagicMock:
    """Wrap ``dispatch_tick`` so a test can count ticks; *real* runs the tick."""
    counter = (
        MagicMock(wraps=loop_mod.dispatch_tick)
        if real
        else MagicMock(return_value=DispatchTickResult(spawned=0))
    )
    monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", counter)
    return counter


def _forbid_sleep(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Fail if the loop's own idle point sleeps; return each idle_wait result.

    Scoped to ``loop_mod.idle_wait``. Patching ``cw.dispatch.loop.time.sleep``
    would replace the process-wide ``time.sleep``, which stdlib ``subprocess``
    also polls with while a real ``git fetch`` child runs, so a slow CI worker
    tripped the guard far from the loop (#3882).
    """
    outcomes: list[bool] = []

    def _no_sleep(_seconds: float) -> None:
        msg = "the loop slept after a re-exec request"
        raise AssertionError(msg)

    def _spy(
        seconds: float,
        control: ReexecControl | None,
        *,
        sleep: Callable[[float], None],
    ) -> bool:
        outcomes.append(idle_wait(seconds, control, sleep=_no_sleep))
        return outcomes[-1]

    monkeypatch.setattr("cw.dispatch.loop.idle_wait", _spy)
    return outcomes


def test_forbid_sleep_leaves_global_time_sleep_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = time.sleep
    _forbid_sleep(monkeypatch)
    assert time.sleep is original


def test_forbid_sleep_guard_still_bites_on_an_unrequested_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _count_ticks(monkeypatch, real=False)
    _forbid_sleep(monkeypatch)
    with pytest.raises(AssertionError, match="slept after a re-exec request"):
        run_dispatch_loop(reexec=ReexecControl())


def test_sighup_mid_tick_never_interrupts_tick_and_claim_is_committed(
    sample_client_config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2: the claim in progress completes and the loop exits after."""
    write_clients_yaml(sample_client_config)
    add_ticket(TicketTask(ticket_id="CW-3726", client="test-client"))
    monkeypatch.setattr(
        "cw.dispatch.gating.freshness.is_main_behind_origin",
        lambda _client, **_kw: (False, "abc", "abc", 0),
    )
    monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", lambda **_kw: None)
    ticks = _count_ticks(monkeypatch, real=True)
    outcomes = _forbid_sleep(monkeypatch)
    payloads = _capture_exit_events(monkeypatch)
    daemon = _SignallingDaemon()
    control = ReexecControl()

    with sighup_disposition(signal.SIG_DFL), sighup_reexec(control):
        run_dispatch_loop(native_daemon=daemon, reexec=control)

    assert control.requested is True
    assert len(daemon.spawn_calls) == 1
    assert ticks.call_count == 1
    assert outcomes == [True]
    rows = load_dev_queue().running()
    assert [row.ticket_id for row in rows] == ["CW-3726"]
    assert rows[0].status == QueueItemStatus.RUNNING
    assert rows[0].session_id
    assert payloads == [
        {
            "normal": True,
            "exception_type": None,
            "codex_threads_still_running": 0,
            "reason": "reexec",
        }
    ]


def test_sighup_during_sleep_exits_within_one_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _count_ticks(monkeypatch, real=False)
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        signal.raise_signal(signal.SIGHUP)

    monkeypatch.setattr("cw.dispatch.loop.time.sleep", _sleep)
    control = ReexecControl()
    with sighup_disposition(signal.SIG_DFL), sighup_reexec(control):
        run_dispatch_loop(reexec=control)

    assert ticks.call_count == 1
    assert sum(slept) <= 1.0
    assert control.requested is True


def test_flag_set_before_first_tick_still_finishes_one_tick_then_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = _count_ticks(monkeypatch, real=False)
    outcomes = _forbid_sleep(monkeypatch)
    payloads = _capture_exit_events(monkeypatch)
    control = ReexecControl()
    control.requested = True

    run_dispatch_loop(reexec=control)

    assert ticks.call_count == 1
    assert outcomes == [True]
    assert payloads[0] == {
        "normal": True,
        "exception_type": None,
        "codex_threads_still_running": 0,
        "reason": "reexec",
    }


def test_idle_wait_without_control_sleeps_once_for_the_full_interval() -> None:
    calls: list[float] = []
    assert idle_wait(30, None, sleep=calls.append) is False
    assert calls == [30]


def test_idle_wait_with_control_slices_and_sums_to_interval() -> None:
    calls: list[float] = []
    assert idle_wait(2.5, ReexecControl(), sleep=calls.append) is False
    assert calls == [1.0, 1.0, 0.5]


def test_idle_wait_with_requested_control_returns_true_without_sleeping() -> None:
    control = ReexecControl()
    control.requested = True
    calls: list[float] = []
    assert idle_wait(30, control, sleep=calls.append) is True
    assert calls == []


def test_idle_wait_stops_slicing_once_the_flag_is_set() -> None:
    control = ReexecControl()
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            control.requested = True

    assert idle_wait(30, control, sleep=_sleep) is True
    assert calls == [1.0, 1.0]


def test_loop_py_passes_time_sleep_evaluated_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupervised loop sleeps once per tick through the patched global."""
    calls: list[float] = []
    ticks: list[int] = []

    def _tick(*_args: object, **_kwargs: object) -> DispatchTickResult:
        ticks.append(1)
        if len(ticks) == 2:
            raise KeyboardInterrupt
        return DispatchTickResult(spawned=0)

    monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", _tick)
    monkeypatch.setattr("cw.dispatch.loop.time.sleep", calls.append)
    with pytest.raises(KeyboardInterrupt):
        run_dispatch_loop()
    assert calls == [load_effective_config().tick_interval_seconds]


def test_exit_payload_reason_reexec_and_drift_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = ReexecControl()
    requested.requested = True
    base = {"normal": True, "exception_type": None, "codex_threads_still_running": 2}
    assert loop_exit_payload(
        None,
        codex_threads_still_running=2,
        loaded_version="1.0.0",
        package_name=_PKG,
        reexec=requested,
    ) == {**base, "reason": "reexec"}
    assert (
        loop_exit_payload(
            None,
            codex_threads_still_running=2,
            loaded_version="1.0.0",
            package_name=_PKG,
            reexec=ReexecControl(),
        )
        == base
    )
    assert (
        loop_exit_payload(
            None,
            codex_threads_still_running=2,
            loaded_version="1.0.0",
            package_name=_PKG,
            reexec=None,
        )
        == base
    )
    assert loop_exit_payload(
        KeyboardInterrupt(),
        codex_threads_still_running=0,
        loaded_version="1.0.0",
        package_name=_PKG,
        reexec=requested,
    ) == {"normal": True, "exception_type": None, "codex_threads_still_running": 0}
    monkeypatch.setattr(
        "cw.dispatch.loop_lifecycle.importlib.metadata.version", lambda _p: "9.9.9"
    )
    assert loop_exit_payload(
        VersionDriftError("drift"),
        codex_threads_still_running=0,
        loaded_version="1.0.0",
        package_name=_PKG,
        reexec=requested,
    ) == {
        "normal": False,
        "exception_type": "VersionDriftError",
        "codex_threads_still_running": 0,
        "reason": "version_drift",
        "loaded_version": "1.0.0",
        "installed_version": "9.9.9",
    }
    assert loop_exit_payload(
        RuntimeError("boom"),
        codex_threads_still_running=1,
        loaded_version="1.0.0",
        package_name=_PKG,
        reexec=None,
    ) == {
        "normal": False,
        "exception_type": "RuntimeError",
        "codex_threads_still_running": 1,
    }


def test_loop_exit_payload_helper_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_package: str) -> str:
        raise importlib.metadata.PackageNotFoundError(_package)

    monkeypatch.setattr("cw.dispatch.loop.importlib.metadata.version", _boom)
    assert loop_exit_payload(
        VersionDriftError("drift"),
        codex_threads_still_running=0,
        loaded_version="1.0.0",
        package_name=_PKG,
        reexec=None,
    ) == {
        "normal": False,
        "exception_type": "VersionDriftError",
        "codex_threads_still_running": 0,
        "reason": "version_drift",
        "loaded_version": "1.0.0",
    }


def test_drain_codex_threads_returns_the_join_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cw.dispatch.loop_lifecycle.join_outstanding_codex_threads", lambda: 3
    )
    assert drain_codex_threads() == 3


def test_drain_codex_threads_returns_zero_when_join_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> int:
        msg = "join failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "cw.dispatch.loop_lifecycle.join_outstanding_codex_threads", _boom
    )
    assert drain_codex_threads() == 0
