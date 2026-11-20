"""Tests for cw.codex_fix_loop.seed — seeding cycle 0 with confirm findings (#2807)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result.schema._vocab import CODEX_MUST_FIX_FINDINGS
from cw.codex_fix_loop.seed import _should_seed, seed_verdict
from cw.codex_review import make_codex_blocked
from cw.review_findings import (
    AcceptedFinding,
    Finding,
    ReviewVerdict,
    consolidate_verdict,
    stamp_held_findings,
)
from tests._review_convergence_helpers import consolidated_verdict, stamp_dispositions
from tests._shared.review_builders import (
    _make_diff,
    _make_finding,
    _make_gap_finding,
    _make_reviewer_doc,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

_OPERATOR_ACTIONABLE = "review_operator_actionable"
_CONTEST_QUOTE = "Non-goal: keep the legacy retry path."


def _seed(summary: str, *, sha: str = "sha-claude") -> AcceptedFinding:
    """One accepted MUST_FIX finding, as a Claude confirm round recorded it."""
    (item,) = consolidated_verdict(accepted=[("MUST_FIX", summary)], sha=sha).accepted
    return item


def test_empty_seeds_return_the_same_verdict() -> None:
    verdict = consolidated_verdict(accepted=[("MUST_FIX", "codex finding")])
    assert seed_verdict(verdict, ()) is verdict


def test_a_seed_on_a_clean_verdict_makes_it_blocking() -> None:
    verdict = consolidated_verdict()
    assert verdict.blocking is False
    seed = _seed("claude finding")

    seeded = seed_verdict(verdict, [seed])

    assert seeded.blocking is True
    assert [f.summary for f in seeded.must_fix] == ["claude finding"]
    assert seeded.accepted[-1].finding == seed.finding
    assert seeded.accepted[-1].disposition == "fixed"
    assert seeded.review.must_fix_initial == verdict.review.must_fix_initial + 1
    assert seeded.reviewed_sha == verdict.reviewed_sha


def test_seeds_append_after_the_codex_findings() -> None:
    verdict = consolidated_verdict(accepted=[("MUST_FIX", "codex finding")])
    seeded = seed_verdict(verdict, [_seed("claude finding")])
    assert [f.summary for f in seeded.must_fix] == ["codex finding", "claude finding"]
    assert seeded.review.must_fix_initial == verdict.review.must_fix_initial + 1


def test_a_seed_the_codex_review_already_raised_is_not_duplicated() -> None:
    verdict = consolidated_verdict(accepted=[("MUST_FIX", "same finding")])
    seeded = seed_verdict(verdict, [_seed("same finding")])
    assert seeded is verdict


def test_a_seed_matching_an_operator_settled_codex_finding_is_not_reopened() -> None:
    """Dedupe runs against every accepted entry, not only the open ones."""
    verdict = stamp_dispositions(
        consolidated_verdict(accepted=[("MUST_FIX", "settled finding")]),
        {"settled finding": ("rejected", "operator settled")},
    )
    seeded = seed_verdict(verdict, [_seed("settled finding")])
    assert seeded is verdict


def test_duplicate_seeds_are_added_once() -> None:
    verdict = consolidated_verdict()
    seeded = seed_verdict(verdict, [_seed("claude finding"), _seed("claude finding")])
    assert len(seeded.must_fix) == 1
    assert seeded.review.must_fix_initial == verdict.review.must_fix_initial + 1


def test_a_dropped_seed_is_admitted_as_fixed() -> None:
    """A confirm MUST_FIX nobody fixed is open work for the loop."""
    seed = _seed("claude finding").model_copy(update={"disposition": "unresolved"})
    seeded = seed_verdict(consolidated_verdict(), [seed])
    assert seeded.accepted[-1].disposition == "fixed"


def test_the_input_verdict_is_not_mutated() -> None:
    verdict = consolidated_verdict()
    before = verdict.model_dump(mode="json")
    seed_verdict(verdict, [_seed("claude finding")])
    assert verdict.model_dump(mode="json") == before


# ---------------------------------------------------------------------------
# _should_seed: when cycle 0 merges the Claude confirm seeds (#2807, #3793)
# ---------------------------------------------------------------------------


def _held_verdict(*findings: Finding) -> ReviewVerdict:
    """Real consolidation plus the held stamp, as the codex synthesis builds it."""
    verdict = consolidate_verdict(
        [_make_reviewer_doc(*findings)], _make_diff(), reviewed_sha="sha"
    )
    return stamp_held_findings(verdict)


def _parked(tmp_path: Path, reason: str = _OPERATOR_ACTIONABLE) -> AutoDevResult:
    return make_codex_blocked(
        ticket_id="T-1", worktree=tmp_path, reason=reason, details="parked"
    )


class TestShouldSeed:
    def test_a_gap_only_cycle0_still_seeds(self, tmp_path: Path) -> None:
        verdict = _held_verdict(_make_gap_finding())

        assert verdict.blocking is False
        assert _should_seed(verdict, _parked(tmp_path), [_seed("seed")], True) is True

    @pytest.mark.parametrize(
        "finding",
        [
            _make_finding(contests_decision=_CONTEST_QUOTE),
            _make_gap_finding(contests_decision=_CONTEST_QUOTE),
        ],
        ids=["contest", "contest_and_gap_on_one_finding"],
    )
    def test_a_contest_cycle0_keeps_its_park(
        self, tmp_path: Path, finding: Finding
    ) -> None:
        verdict = _held_verdict(finding)

        assert _should_seed(verdict, _parked(tmp_path), [_seed("seed")], True) is False

    def test_no_seeds_never_seed(self, tmp_path: Path) -> None:
        verdict = _held_verdict(_make_gap_finding())

        assert _should_seed(verdict, _parked(tmp_path), [], True) is False

    def test_a_disabled_loop_never_seeds(self, tmp_path: Path) -> None:
        verdict = _held_verdict(_make_gap_finding())

        assert _should_seed(verdict, _parked(tmp_path), [_seed("seed")], False) is False

    def test_a_blocking_verdict_seeds(self, tmp_path: Path) -> None:
        verdict = _held_verdict(_make_finding())
        parked = _parked(tmp_path, CODEX_MUST_FIX_FINDINGS)

        assert verdict.blocking is True
        assert _should_seed(verdict, parked, [_seed("seed")], True) is True

    def test_no_verdict_never_seeds(self, tmp_path: Path) -> None:
        assert _should_seed(None, _parked(tmp_path), [_seed("seed")], True) is False

    def test_a_result_with_no_blocker_seeds(self, tmp_path: Path) -> None:
        verdict = _held_verdict()
        unparked = _parked(tmp_path).model_copy(update={"blocker": None})

        assert _should_seed(verdict, unparked, [_seed("seed")], True) is True

    def test_a_non_blocking_verdict_parked_on_something_else_keeps_its_park(
        self, tmp_path: Path
    ) -> None:
        verdict = _held_verdict()
        parked = _parked(tmp_path, "codex_review_partial")

        assert _should_seed(verdict, parked, [_seed("seed")], True) is False
