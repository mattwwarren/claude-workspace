"""Tests for cw.doctor.loop_code (#2729)."""

from __future__ import annotations

import pytest

from cw.cli.dev_queue._loop_warning import warn_if_dispatch_loop_stale
from cw.doctor import format_report, run_doctor
from cw.doctor.loop_code import _check_loop_code_identity
from tests._loop_identity_helpers import (
    COMMIT_A,
    COMMIT_B,
    patch_checkout_identity,
    write_loop_record,
)

_NAME = "dispatch-loop-code"
_RESTART = "restart `cw dev-queue serve`"


def test_no_loop_is_ok_not_warn() -> None:
    result = _check_loop_code_identity()
    assert result.name == _NAME
    assert result.ok is True
    assert result.warn is False
    assert result.detail == "no dispatch loop lock holder recorded"


def test_match_is_ok_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record()
    patch_checkout_identity(monkeypatch)
    result = _check_loop_code_identity()
    assert (result.ok, result.warn) == (True, False)
    assert "matches the checkout" in result.detail


def test_stale_is_an_advisory_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    result = _check_loop_code_identity()
    assert (result.ok, result.warn) == (True, True)
    assert COMMIT_A[:12] in result.detail
    assert COMMIT_B[:12] in result.detail
    assert _RESTART in result.detail


def test_identity_unknown_is_not_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    write_loop_record(with_identity=False)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    result = _check_loop_code_identity()
    assert (result.ok, result.warn) == (True, False)
    assert "identity unknown" in result.detail
    assert "restart" in result.detail
    assert "started on" not in result.detail


def test_run_doctor_renders_stale_as_warn_and_stays_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    report = run_doctor()
    checks = [c for c in report.checks if c.name == _NAME]
    assert len(checks) == 1
    assert checks[0].warn is True
    assert f"[WARN] {_NAME}" in format_report(report)
    # STALE never flips the exit code on its own.
    assert checks[0].ok is True


def test_run_doctor_ok_unaffected_by_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = run_doctor().ok
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    assert run_doctor().ok is baseline


def test_doctor_detail_equals_cli_warning_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_loop_record(commit=COMMIT_A)
    patch_checkout_identity(monkeypatch, commit=COMMIT_B)
    detail = _check_loop_code_identity().detail
    capsys.readouterr()
    warn_if_dispatch_loop_stale()
    assert capsys.readouterr().err == f"warning: {detail}\n"
