"""The stale-loop guard waits for a supervised restart (#3726).

A STALE loop whose supervisor is alive gets a bounded wait (90s, poll 2s) for
its supervisor to restart it onto the checkout; an unsupervised STALE loop is
still refused at once with the existing text. The wait runs on the shared
``_FakeClock``, installed as ``cw.cli.dev_queue._loop_warning.time``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from cw.dispatch_reexec import SUPERVISOR_PID_ENV

from cw.cli import main
from cw.cli.dev_queue._loop_warning import (
    require_dispatch_loop_safe,
    warn_if_dispatch_loop_stale,
)
from cw.dispatch_loop_identity import assess_dispatch_loop
from cw.exceptions import CwError
from cw.models import Stage
from tests._cli_review_helpers import _enter_operator_machine
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)
from tests._shared.lock_helpers import _FakeClock
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_DEAD_PID = 999_999_999
_SUBJECT = "an operator constraint"


def _waiting_line() -> str:
    return (
        "note: waiting for the supervised loop to restart..."
        f" (pid {os.getpid()}, up to 90s)"
    )


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISOR_PID_ENV, raising=False)
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


def _install_clock(
    monkeypatch: pytest.MonkeyPatch, clock: _FakeClock | None = None
) -> _FakeClock:
    installed = clock or _FakeClock()
    monkeypatch.setattr("cw.cli.dev_queue._loop_warning.time", installed)
    return installed


def _supervised_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record(commit=COMMIT_A, supervisor_pid=os.getpid())
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)


def _constrain(tmp_path: Path) -> Result:
    path = tmp_path / "constraints.md"
    path.write_text("Keep the row as the source of truth.", encoding="utf-8")
    return CliRunner().invoke(
        main, ["review", "constrain", ACME_TICKET, "--file", str(path), "-c", ACME]
    )


def test_unsupervised_stale_loop_still_refused_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 4: no wait, the existing refusal text."""
    clock = _install_clock(monkeypatch)
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    message = assess_dispatch_loop().message
    with pytest.raises(CwError) as info:
        require_dispatch_loop_safe()
    assert str(info.value) == (
        f"Cannot safely record {_SUBJECT} while {message}."
        " Restart `cw dev-queue serve` and retry."
    )
    assert clock.sleeps == []


def test_unsupervised_stale_loop_refused_via_review_constrain_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _install_clock(monkeypatch)
    seed_queue([make_task(stage=Stage.REVIEW)])
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    result = _constrain(tmp_path)
    assert result.exit_code == 1, result.output
    assert "Restart `cw dev-queue serve` and retry." in result.stderr
    assert "waiting for the supervised loop" not in result.stderr
    assert queue_row().operator_constraints == []
    assert clock.sleeps == []


def test_supervised_stale_loop_waits_then_proceeds_when_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1 for constrain: the write lands once the loop restarted."""

    def _restarted(count: int) -> None:
        if count == 3:
            write_loop_record(commit=COMMIT_B, supervisor_pid=os.getpid())

    clock = _install_clock(monkeypatch, _FakeClock(on_sleep=_restarted))
    seed_queue([make_task(stage=Stage.REVIEW)])
    _supervised_stale(monkeypatch)
    result = _constrain(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stderr.count(_waiting_line()) == 1
    assert "warning: dispatch loop" not in result.stderr
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert len(queue_row().operator_constraints) == 1


def test_supervised_stale_loop_refused_after_wait_budget_with_supervised_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _install_clock(monkeypatch)
    _supervised_stale(monkeypatch)
    message = assess_dispatch_loop().message
    with pytest.raises(CwError) as info:
        require_dispatch_loop_safe()
    assert str(info.value) == (
        f"Cannot safely record {_SUBJECT} while {message}. The supervised loop"
        " did not restart within 90s; send SIGHUP to its supervisor or restart"
        " `cw dev-queue serve --supervised`, then retry."
    )
    assert sum(clock.sleeps) == 90.0
    assert set(clock.sleeps) == {2.0}
    err = capsys.readouterr().err
    assert err.count(_waiting_line()) == 1
    assert f"warning: {message}" in err


def test_resolve_warn_path_waits_for_supervised_restart_and_never_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _install_clock(monkeypatch)
    _supervised_stale(monkeypatch)
    message = assess_dispatch_loop().message
    warn_if_dispatch_loop_stale()
    err = capsys.readouterr().err
    assert err.count(_waiting_line()) == 1
    assert f"warning: {message}" in err
    assert sum(clock.sleeps) == 90.0


def test_resolve_warn_path_is_silent_once_the_restart_lands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _restarted(count: int) -> None:
        if count == 1:
            write_loop_record(commit=COMMIT_B, supervisor_pid=os.getpid())

    clock = _install_clock(monkeypatch, _FakeClock(on_sleep=_restarted))
    _supervised_stale(monkeypatch)
    warn_if_dispatch_loop_stale()
    err = capsys.readouterr().err
    assert err.count(_waiting_line()) == 1
    assert "warning:" not in err
    assert clock.sleeps == [2.0]


def test_supervised_identity_unknown_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    write_loop_record(with_identity=False, supervisor_pid=os.getpid())
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    with pytest.raises(CwError) as info:
        require_dispatch_loop_safe()
    assert str(info.value).endswith(" Restart `cw dev-queue serve` and retry.")
    assert clock.sleeps == []


def test_supervised_loop_whose_supervisor_dies_mid_wait_falls_back_to_restart_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _supervisor_died(count: int) -> None:
        if count == 1:
            write_loop_record(commit=COMMIT_A, supervisor_pid=_DEAD_PID)

    clock = _install_clock(monkeypatch, _FakeClock(on_sleep=_supervisor_died))
    _supervised_stale(monkeypatch)
    with pytest.raises(CwError) as info:
        require_dispatch_loop_safe()
    text = str(info.value)
    assert text.endswith(
        "; restart `cw dev-queue serve`. Restart `cw dev-queue serve` and retry."
    )
    assert "--supervised" not in text
    assert clock.sleeps == [2.0]
