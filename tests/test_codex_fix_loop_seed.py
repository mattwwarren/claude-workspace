"""Tests for cw.codex_fix_loop.seed — seeding cycle 0 with confirm findings (#2807)."""

from __future__ import annotations

from cw.codex_fix_loop.seed import seed_verdict
from cw.review_findings import AcceptedFinding
from tests._review_convergence_helpers import consolidated_verdict, stamp_dispositions


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
