"""record-round records the non-engine SHOULD_FIX defers in the artifact (#3137)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from cw.cli.review import converge_confirm
from cw.cli.review.converge_confirm import file_round_deferrals
from cw.review_adjudication import Adjudication, read_deferred_findings_md
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


def _in_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    return artifact


def test_new_defer_is_written_to_the_artifact_and_never_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _in_session(tmp_path, monkeypatch)
    calls = stub_run_debt_filing(monkeypatch, target=_TARGET)
    findings = [
        round_finding("SHOULD_FIX", "Opencode claim", outcome="defer", rationale="ok"),
        round_finding("MUST_FIX", "Open", outcome="fix"),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, now=_NOW)

    (entry,) = read_deferred_findings_md(artifact)
    assert entry.summary == "Opencode claim"
    assert entry.rationale == "ok"
    assert entry.round == 1
    assert not entry.ticket
    assert calls == []


def test_settled_defer_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _in_session(tmp_path, monkeypatch)
    findings = [
        round_finding("SHOULD_FIX", "Old claim", outcome="defer", rationale="r")
    ]

    file_round_deferrals(_ledger_settling("Old claim"), _payload(), findings, now=_NOW)

    assert not artifact.exists()


def test_silent_outside_a_session() -> None:
    findings = [round_finding("SHOULD_FIX", "Claim", outcome="defer", rationale="r")]

    file_round_deferrals(fresh_ledger(), _payload(), findings, now=_NOW)


def test_key_already_in_session_artifact_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = write_deferred_findings(tmp_path, _artifact_entry("Adjudicated claim"))
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    findings = [
        round_finding(
            "SHOULD_FIX", "Adjudicated claim", outcome="defer", rationale="x"
        ),
        round_finding("SHOULD_FIX", "Fresh claim", outcome="defer", rationale="y"),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, now=_NOW)

    written = read_deferred_findings_md(artifact)
    assert [entry.summary for entry in written] == ["Adjudicated claim", "Fresh claim"]
    assert written[0].rationale == "filed earlier"


def test_only_already_recorded_defers_leave_the_artifact_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = write_deferred_findings(tmp_path, _artifact_entry("Adjudicated claim"))
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    before = artifact.read_text(encoding="utf-8")
    findings = [
        round_finding(
            "SHOULD_FIX", "Adjudicated claim", outcome="defer", rationale="x"
        ),
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, now=_NOW)

    assert artifact.read_text(encoding="utf-8") == before


def test_unreadable_artifact_does_not_abort_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A corrupt session artifact must not raise before the ledger commit (#3137):
    # the round still records, and the failure is one friction line.
    artifact = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"\xff\xfe\x00 not a deferred-findings file")
    monkeypatch.setattr(converge_confirm, "default_artifact", lambda: artifact)
    findings = [
        round_finding("SHOULD_FIX", "Fresh claim", outcome="defer", rationale="y")
    ]

    file_round_deferrals(fresh_ledger(), _payload(), findings, now=_NOW)

    assert "friction: debt_filing_failed: 1" in capsys.readouterr().err
