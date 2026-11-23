"""Tests for cw.dispatch_reexec: the child-side SIGHUP flag (#3726).

A supervised dispatch loop turns SIGHUP into a plain flag that the loop reads
at its next idle point. Every real-signal case pins the SIGHUP disposition
with ``sighup_disposition`` so a runner started under ``nohup`` cannot flip it.
"""

from __future__ import annotations

import os
import signal
import threading

import pytest

from cw.dispatch_reexec import (
    REEXEC_EXIT_CODE,
    SUPERVISOR_PID_ENV,
    ReexecControl,
    handling_signals,
    sighup_ignored,
    sighup_reexec,
    supervised_by_parent,
    supervisor_pid,
)
from tests._loop_identity_helpers import sighup_disposition

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture(autouse=True)
def _no_supervisor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISOR_PID_ENV, raising=False)


def _noop(_signum: int, _frame: object) -> None:
    return None


def test_reexec_exit_code_is_ex_tempfail() -> None:
    assert REEXEC_EXIT_CODE == os.EX_TEMPFAIL == 75


def test_supervised_by_parent_true_only_when_env_matches_getppid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SUPERVISOR_PID_ENV, str(os.getppid()))
    assert supervised_by_parent() is True
    assert supervisor_pid() == os.getppid()


@pytest.mark.parametrize("value", [None, "", "abc", "0", "-4", "999999999"])
def test_supervised_by_parent_false_for_orphan_or_unset(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv(SUPERVISOR_PID_ENV, value)
    assert supervised_by_parent() is False


def test_supervisor_pid_parses_a_positive_int_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SUPERVISOR_PID_ENV, "4242")
    assert supervisor_pid() == 4242
    monkeypatch.setenv(SUPERVISOR_PID_ENV, "x")
    assert supervisor_pid() is None
    monkeypatch.setenv(SUPERVISOR_PID_ENV, "0")
    assert supervisor_pid() is None


def test_handler_only_sets_flag() -> None:
    control = ReexecControl()
    assert control.requested is False
    control.request(signal.SIGHUP, None)
    assert control.requested is True


def test_sighup_handler_installed_and_previous_restored() -> None:
    control = ReexecControl()
    with sighup_disposition(signal.SIG_DFL):
        with sighup_reexec(control):
            assert signal.getsignal(signal.SIGHUP) == control.request
            signal.raise_signal(signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
    assert control.requested is True


def test_no_handler_off_main_thread() -> None:
    seen: list[object] = []
    control = ReexecControl()

    def _worker() -> None:
        with sighup_reexec(control):
            seen.append(signal.getsignal(signal.SIGHUP))

    with sighup_disposition(signal.SIG_DFL):
        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()
    assert seen == [signal.SIG_DFL]


def test_unsupervised_installs_no_handler() -> None:
    with sighup_disposition(signal.SIG_DFL), sighup_reexec(None):
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL


def test_child_handler_skipped_when_sighup_inherited_ignored() -> None:
    control = ReexecControl()
    with sighup_disposition(signal.SIG_IGN):
        assert sighup_ignored() is True
        with sighup_reexec(control):
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            signal.raise_signal(signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    assert control.requested is False


def test_handling_signals_skips_ignored_sighup_but_installs_others() -> None:
    previous_term = signal.getsignal(signal.SIGTERM)
    handlers = {signal.SIGHUP: _noop, signal.SIGTERM: _noop}
    respect = frozenset({signal.SIGHUP})
    with sighup_disposition(signal.SIG_IGN):
        assert sighup_ignored() is True
        with handling_signals(handlers, respect_ignored=respect) as installed:
            assert installed == frozenset({signal.SIGTERM})
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            assert signal.getsignal(signal.SIGTERM) == _noop
        assert signal.getsignal(signal.SIGTERM) == previous_term
    with sighup_disposition(signal.SIG_DFL):
        assert sighup_ignored() is False
        with handling_signals(handlers, respect_ignored=respect) as installed:
            assert installed == frozenset({signal.SIGHUP, signal.SIGTERM})
            assert signal.getsignal(signal.SIGHUP) == _noop
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
        assert signal.getsignal(signal.SIGTERM) == previous_term
