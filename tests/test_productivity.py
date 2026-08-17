"""Tests for cw.dispatch.productivity — the claim-evidence classifier (#1750).

Mirrors ``src/cw/dispatch/productivity.py`` 1:1. The module is the single
schema-owned extractor: both routing.py's raw ``last_result`` dicts and the
reconcile paths' ``AutoDevResult.model_dump(mode="json")`` payloads go through
the same code path, so every shape either producer can emit is covered here.
"""

from __future__ import annotations

import pytest

from cw.dispatch.productivity import (
    ClaimEvidence,
    extract_claim_evidence,
    is_unproductive,
)


class TestExtractClaimEvidence:
    """The single schema-owned extractor, over every payload shape."""

    def test_none_payload_yields_all_false(self) -> None:
        assert extract_claim_evidence(None) == ClaimEvidence(
            had_commits=False, had_findings=False, resolution_consumed=False
        )

    @pytest.mark.parametrize(
        "payload",
        [
            "not-a-dict",
            123,
            ["commits"],
        ],
    )
    def test_non_dict_payload_yields_all_false(self, payload: object) -> None:
        # Defensive: last_result is typed dict|None but reaches us from a
        # persisted JSON blob, so a scalar must degrade, not raise.
        assert extract_claim_evidence(payload) == ClaimEvidence(  # type: ignore[arg-type]
            had_commits=False, had_findings=False, resolution_consumed=False
        )

    def test_empty_dict_yields_all_false(self) -> None:
        assert extract_claim_evidence({}) == ClaimEvidence(
            had_commits=False, had_findings=False, resolution_consumed=False
        )

    def test_empty_commits_list_is_not_evidence(self) -> None:
        assert extract_claim_evidence({"commits": []}).had_commits is False

    def test_non_empty_commits_list_is_evidence(self) -> None:
        assert extract_claim_evidence({"commits": ["abc123"]}).had_commits is True

    def test_non_list_commits_is_not_evidence(self) -> None:
        assert extract_claim_evidence({"commits": "abc123"}).had_commits is False

    def test_zeroed_review_counts_are_not_findings(self) -> None:
        payload = {"review": {"must_fix_initial": 0, "should_fix": 0}}
        assert extract_claim_evidence(payload).had_findings is False

    def test_must_fix_initial_is_findings(self) -> None:
        # The #1727 case: a review claim that parked with real MUST_FIX
        # findings did productive work and must not be charged.
        payload = {"review": {"must_fix_initial": 2, "should_fix": 0}}
        assert extract_claim_evidence(payload).had_findings is True

    def test_should_fix_alone_is_findings(self) -> None:
        payload = {"review": {"must_fix_initial": 0, "should_fix": 3}}
        assert extract_claim_evidence(payload).had_findings is True

    def test_missing_review_key_is_not_findings(self) -> None:
        assert extract_claim_evidence({"commits": []}).had_findings is False

    def test_non_dict_review_is_not_findings(self) -> None:
        assert extract_claim_evidence({"review": "clean"}).had_findings is False

    def test_non_int_review_counts_are_not_findings(self) -> None:
        payload = {"review": {"must_fix_initial": "2", "should_fix": None}}
        assert extract_claim_evidence(payload).had_findings is False

    def test_bare_resolution_consumed_without_evidence_is_false(self) -> None:
        # R1 STRICT: the boolean alone carries no provenance a later reader can
        # check, so it does NOT credit the claim as productive.
        payload = {"resolution_consumed": True}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_resolution_consumed_with_empty_evidence_is_false(self) -> None:
        payload = {"resolution_consumed": True, "resolution_evidence": {}}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_resolution_consumed_with_evidence_is_true(self) -> None:
        payload = {
            "resolution_consumed": True,
            "resolution_evidence": {"comment_id": "12345", "question": "which cap?"},
        }
        assert extract_claim_evidence(payload).resolution_consumed is True

    def test_resolution_evidence_without_the_boolean_is_false(self) -> None:
        payload = {"resolution_evidence": {"comment_id": "12345"}}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_non_dict_resolution_evidence_is_false(self) -> None:
        payload = {"resolution_consumed": True, "resolution_evidence": "yes"}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_blocked_result_shaped_payload_yields_all_false(self) -> None:
        # A BlockedResult carries no commits/review keys at all; plain .get()
        # defaults must make it read as zero evidence rather than raising.
        payload = {
            "status": "blocked",
            "blocker": {"reason": "merge_gate_blocked", "detail": "CI red"},
        }
        assert extract_claim_evidence(payload) == ClaimEvidence(
            had_commits=False, had_findings=False, resolution_consumed=False
        )

    def test_full_productive_sentinel(self) -> None:
        payload = {
            "status": "shipped",
            "commits": ["deadbee"],
            "review": {"must_fix_initial": 1, "should_fix": 2},
            "resolution_consumed": True,
            "resolution_evidence": {"comment_id": "1"},
        }
        assert extract_claim_evidence(payload) == ClaimEvidence(
            had_commits=True, had_findings=True, resolution_consumed=True
        )


class TestIsUnproductive:
    """OR-combination truth table across the three evidence fields."""

    @pytest.mark.parametrize(
        ("had_commits", "had_findings", "resolution_consumed", "expected"),
        [
            (False, False, False, True),
            (True, False, False, False),
            (False, True, False, False),
            (False, False, True, False),
            (True, True, False, False),
            (True, False, True, False),
            (False, True, True, False),
            (True, True, True, False),
        ],
    )
    def test_truth_table(
        self,
        had_commits: bool,
        had_findings: bool,
        resolution_consumed: bool,
        expected: bool,
    ) -> None:
        evidence = ClaimEvidence(
            had_commits=had_commits,
            had_findings=had_findings,
            resolution_consumed=resolution_consumed,
        )
        assert is_unproductive(evidence) is expected

    def test_no_payload_at_all_is_unproductive(self) -> None:
        # The #1653 crashloop shape: a dead session leaves no sentinel, so the
        # claim is unproductive and must be charged.
        assert is_unproductive(extract_claim_evidence(None)) is True
