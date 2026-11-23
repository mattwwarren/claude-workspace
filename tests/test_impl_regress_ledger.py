"""The clean-mode ledger verdict reader (#3862), hermetic on tmp worktrees.

Every ledger comes from ``write_held_ledger`` and every round verdict is built by
``build_held_verdict``, through the shared helpers. No git repository is needed:
the reader never consults HEAD.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import LEDGER_RELATIVE_PATH, round_verdict_path
from cw.review_findings import has_held_findings, held_findings
from cw.stage_verification.impl_regress_ledger import read_held_round_verdict
from tests._review_convergence_helpers import fresh_ledger, write_ledger
from tests._review_history_helpers import write_held_ledger, write_round_verdict
from tests._shared.review_builders import (
    _make_gap_finding,
    build_held_verdict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings import Finding

_T = "T-1"


def _gap(summary: str = "A") -> Finding:
    return _make_gap_finding(summary=summary)


def _contest(summary: str = "C") -> Finding:
    return _make_gap_finding(summary=summary, contests_decision="we decided X")


def test_held_in_early_round_later_rounds_clean_is_delivered(tmp_path: Path) -> None:
    """The observed #3786/#3840 shape: held in round 1, rounds 2-4 clean."""
    write_held_ledger(tmp_path, [[_gap("A")], [], [], []])
    read = read_held_round_verdict(tmp_path, _T)
    assert read.refusal is None
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"
    assert has_held_findings(read.verdict, "evidence_gap")


def test_latest_round_with_held_finding_wins(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], [], [_gap("B")]])
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0003"
    assert [af.finding.summary for af in held_findings(read.verdict)] == ["B"]


def test_contest_in_earlier_round_withholds_later_round_gap(tmp_path: Path) -> None:
    """An unsettled contest in round 1 withholds the round-3 gap (the scan)."""
    write_held_ledger(tmp_path, [[_contest()], [], [_gap("A")]])
    read = read_held_round_verdict(tmp_path, _T)
    assert read.refusal is None
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"
    assert has_held_findings(read.verdict, "decision_contest")


def test_settled_contest_in_earlier_round_delivers_later_round_gap(
    tmp_path: Path,
) -> None:
    write_held_ledger(tmp_path, [[_contest()], [], [_gap("A")]], settle=[_contest()])
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0003"
    assert not has_held_findings(read.verdict, "decision_contest")


def test_unreadable_round_between_contest_and_gap_is_skipped(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_contest()], [], [_gap("A")]])
    round_verdict_path(tmp_path / LEDGER_RELATIVE_PATH, 2).write_text("{", "utf-8")
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"


def test_contest_round_head_mismatch_still_withholds(tmp_path: Path) -> None:
    """The head pin applies to the delivered candidate only, not a contest round."""
    write_held_ledger(tmp_path, [[_contest()], [], [_gap("A")]])
    write_round_verdict(
        tmp_path, 1, build_held_verdict([_contest()], reviewed_sha="sha-other")
    )
    read = read_held_round_verdict(tmp_path, _T)
    assert read.refusal is None
    assert read.verdict is not None
    assert has_held_findings(read.verdict, "decision_contest")


@pytest.mark.parametrize(
    ("settle", "expected"),
    [
        ([_gap("B")], ("sha-0001", None)),
        ([_gap("A"), _gap("B")], (None, "not_blocking")),
    ],
    ids=["fallback-to-earlier-round", "all-settled"],
)
def test_settled_gap_in_latest_round_falls_back_or_refuses(
    tmp_path: Path,
    settle: list[Finding],
    expected: tuple[str | None, str | None],
) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], [], [_gap("B")]], settle=settle)
    read = read_held_round_verdict(tmp_path, _T)
    if expected[0] is None:
        assert read.verdict is None
        assert read.refusal == expected[1]
    else:
        assert read.verdict is not None
        assert read.verdict.reviewed_sha == expected[0]


def test_no_round_holds_a_held_finding_is_not_blocking(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[], [], []])
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is None
    assert read.refusal == "not_blocking"


def test_orphan_file_past_last_ledger_round_is_never_read(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], []])
    write_round_verdict(
        tmp_path, 3, build_held_verdict([_gap("B")], reviewed_sha="sha-0003")
    )
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"


def test_orphan_contest_past_last_round_does_not_withhold(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], []])
    write_round_verdict(
        tmp_path, 3, build_held_verdict([_contest()], reviewed_sha="sha-0003")
    )
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"


def test_missing_ledger_is_artifact_missing(tmp_path: Path) -> None:
    read = read_held_round_verdict(tmp_path, _T)
    assert read.refusal == "artifact_missing"


def test_unreadable_ledger_is_artifact_missing(tmp_path: Path) -> None:
    (tmp_path / LEDGER_RELATIVE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / LEDGER_RELATIVE_PATH).write_text("{", encoding="utf-8")
    assert read_held_round_verdict(tmp_path, _T).refusal == "artifact_missing"


def test_empty_rounds_is_artifact_missing(tmp_path: Path) -> None:
    write_ledger(tmp_path, fresh_ledger(ticket_id=_T))
    assert read_held_round_verdict(tmp_path, _T).refusal == "artifact_missing"


def test_round_without_verdict_file_is_skipped(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], [], []], skip_verdict={3})
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"


def test_no_round_verdict_readable_is_artifact_missing(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], []], skip_verdict={1, 2})
    assert read_held_round_verdict(tmp_path, _T).refusal == "artifact_missing"


def test_foreign_ticket_is_not_owned(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")]], ticket_id="OTHER-1")
    assert read_held_round_verdict(tmp_path, _T).refusal == "not_owned"


def test_selected_round_head_mismatch_is_not_owned(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], [], []])
    write_round_verdict(
        tmp_path, 1, build_held_verdict([_gap("A")], reviewed_sha="sha-other")
    )
    assert read_held_round_verdict(tmp_path, _T).refusal == "not_owned"


def test_head_pin_ignores_rounds_scanned_past(tmp_path: Path) -> None:
    write_held_ledger(tmp_path, [[_gap("A")], [], []])
    write_round_verdict(
        tmp_path,
        3,
        build_held_verdict([], reviewed_sha="sha-other"),
    )
    read = read_held_round_verdict(tmp_path, _T)
    assert read.verdict is not None
    assert read.verdict.reviewed_sha == "sha-0001"


def test_no_git_and_no_head_read(tmp_path: Path) -> None:
    """``tmp_path`` is not a git repo, and delivery still happens."""
    write_held_ledger(tmp_path, [[_gap("A")]])
    assert read_held_round_verdict(tmp_path, _T).verdict is not None


def test_review_feedback_present_beats_ledger(tmp_path: Path) -> None:
    """A PR row with review_feedback is refused before any ledger read."""
    from datetime import UTC, datetime

    from cw.models.review_feedback import ReviewFeedback
    from cw.stage_verification.impl_regress_findings import regress_review_findings
    from tests._shared.review_builders import regressed_review_row

    write_held_ledger(tmp_path, [[_gap("A")]], ticket_id=_T)
    feedback = ReviewFeedback(
        repo="o/r",
        pr_number=1,
        reviewed_sha="a" * 40,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )
    row = regressed_review_row(
        _T,
        pr_url="https://example/pr/1",
        review_feedback=feedback,
        blocked_reason="review_operator_actionable",
    )
    outcome = regress_review_findings(row, tmp_path)
    assert outcome is not None
    assert outcome.reason == "review_feedback_present"
