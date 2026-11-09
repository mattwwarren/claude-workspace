"""record-round files the non-engine SHOULD_FIX defers it records (#3137)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from cw.cli.review import converge_confirm
from cw.cli.review.converge_confirm import file_round_deferrals
from cw.review_adjudication import Adjudication
from cw.review_convergence import SettledEntry, finding_identity
from cw.review_convergence._models import RoundInput
from cw.review_debt_filing import DEFERRED_FINDINGS_RELATIVE_PATH
from tests._cli_review_helpers import _isolated_cwd
from tests._review_convergence_helpers import fresh_ledger, round_finding
from tests._review_debt_helpers import stub_run_debt_filing, write_deferred_findings

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger

_TARGET = "cw.cli.review.converge_confirm.run_debt_filing"
_NOW = "2026-11-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _off_the_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A dispatch worktree is a cw session; run outside it so "no session" holds.
    _isolated_cwd(monkeypatch, tmp_path)


def _payload() -> RoundInput:
    # A loop round never reaches the engine-confirm check, which reads only kind.
    return cast("RoundInput", SimpleNamespace(kind="loop"))


def _ledger_settling(summary: str) -> ConvergenceLedger:
    settled = SettledEntry(
        identity=finding_identity("SHOULD_FIX", "src/cw/foo.py", summary),
        outcome="defer",
        rationale="old",
        round=1,
    )
    return fresh_ledger().model_copy(update={"settled": [settled]})


def _artifact_entry(summary: str) -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/foo.py",
        line_start=None,
        line_end=None,
        evidence="",
        summary=summary,
        outcome="defer",
        rationale="filed earlier",
        round=1,
        recorded_at=_NOW,
    )


def test_new_defer_is_filed_with_its_own_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding("SHOULD_FIX", "Opencode claim", outcome="defer", rationale="ok"),
        round_finding("MUST_FIX", "Open", outcome="fix"),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, "acme", "101", now=_NOW)

    assert len(calls) == 1
    assert calls[0].client == "acme"
    assert calls[0].ticket == "101"
    assert calls[0].artifact is None
    (applied,) = calls[0].applied
    assert applied.summary == "Opencode claim"
    assert applied.rationale == "ok"
    assert applied.round == 1


def test_settled_defer_is_not_refiled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding("SHOULD_FIX", "Old claim", outcome="defer", rationale="r")
    ]

    file_round_deferrals(
        _ledger_settling("Old claim"), _payload(), findings, "acme", "101", now=_NOW
    )

    assert calls == []


def test_silent_outside_a_session_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [round_finding("SHOULD_FIX", "Claim", outcome="defer", rationale="r")]

    file_round_deferrals(fresh_ledger(), _payload(), findings, None, None, now=_NOW)

    assert calls == []


def test_key_already_in_session_artifact_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_deferred_findings(tmp_path, _artifact_entry("Adjudicated claim"))
    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding(
            "SHOULD_FIX", "Adjudicated claim", outcome="defer", rationale="x"
        ),
        round_finding("SHOULD_FIX", "Fresh claim", outcome="defer", rationale="y"),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, None, None, now=_NOW)

    assert len(calls) == 1
    assert [a.summary for a in calls[0].applied] == ["Fresh claim"]
    assert calls[0].artifact is None


def test_only_already_filed_defers_make_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_deferred_findings(tmp_path, _artifact_entry("Adjudicated claim"))
    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding(
            "SHOULD_FIX", "Adjudicated claim", outcome="defer", rationale="x"
        ),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, None, None, now=_NOW)

    assert calls == []


def test_unreadable_artifact_does_not_abort_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A corrupt session artifact must not raise before the ledger commit (#3137):
    # the round still records, and the failure is one friction line.
    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"\xff\xfe\x00 not a deferred-findings file")
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding("SHOULD_FIX", "Fresh claim", outcome="defer", rationale="y")
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, None, None, now=_NOW)

    assert calls == []
    assert "friction: debt_filing_failed: 1" in capsys.readouterr().err
