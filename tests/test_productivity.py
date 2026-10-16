"""Tests for cw.dispatch.productivity — the claim-evidence classifier (#1750).

Mirrors ``src/cw/dispatch/productivity.py`` 1:1. The module is the single
schema-owned extractor: both routing.py's raw ``last_result`` dicts and the
reconcile paths' ``AutoDevResult.model_dump(mode="json")`` payloads go through
the same code path, so every shape either producer can emit is covered here.
"""

from __future__ import annotations

import itertools
from typing import ClassVar

import pytest

from cw.auto_dev_result import (
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
)
from cw.dispatch.productivity import (
    ClaimEvidence,
    extract_claim_evidence,
    is_unproductive,
)


class TestExtractClaimEvidence:
    """The single schema-owned extractor, over every payload shape."""

    def test_none_payload_yields_all_false(self) -> None:
        assert extract_claim_evidence(None) == ClaimEvidence(
            had_commits=False,
            had_findings=False,
            resolution_consumed=False,
            human_input_park=False,
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
            had_commits=False,
            had_findings=False,
            resolution_consumed=False,
            human_input_park=False,
        )

    def test_empty_dict_yields_all_false(self) -> None:
        assert extract_claim_evidence({}) == ClaimEvidence(
            had_commits=False,
            had_findings=False,
            resolution_consumed=False,
            human_input_park=False,
        )

    def test_empty_commits_list_is_not_evidence(self) -> None:
        assert extract_claim_evidence({"commits": []}).had_commits is False

    def test_non_empty_commits_list_is_evidence(self) -> None:
        assert extract_claim_evidence({"commits": ["abc123"]}).had_commits is True

    def test_non_list_commits_is_not_evidence(self) -> None:
        assert extract_claim_evidence({"commits": "abc123"}).had_commits is False

    def test_zeroed_review_counts_are_not_findings(self) -> None:
        payload: dict[str, object] = {
            "review": {"must_fix_initial": 0, "should_fix": 0}
        }
        assert extract_claim_evidence(payload).had_findings is False

    def test_must_fix_initial_is_findings(self) -> None:
        # The #1727 case: a review claim that parked with real MUST_FIX
        # findings did productive work and must not be charged.
        payload: dict[str, object] = {
            "review": {"must_fix_initial": 2, "should_fix": 0}
        }
        assert extract_claim_evidence(payload).had_findings is True

    def test_should_fix_alone_is_findings(self) -> None:
        payload: dict[str, object] = {
            "review": {"must_fix_initial": 0, "should_fix": 3}
        }
        assert extract_claim_evidence(payload).had_findings is True

    def test_missing_review_key_is_not_findings(self) -> None:
        assert extract_claim_evidence({"commits": []}).had_findings is False

    def test_non_dict_review_is_not_findings(self) -> None:
        assert extract_claim_evidence({"review": "clean"}).had_findings is False

    def test_non_int_review_counts_are_not_findings(self) -> None:
        payload: dict[str, object] = {
            "review": {"must_fix_initial": "2", "should_fix": None}
        }
        assert extract_claim_evidence(payload).had_findings is False

    def test_bare_resolution_consumed_without_evidence_is_false(self) -> None:
        # R1 STRICT: the boolean alone carries no provenance a later reader can
        # check, so it does NOT credit the claim as productive.
        payload: dict[str, object] = {"resolution_consumed": True}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_resolution_consumed_with_empty_evidence_is_false(self) -> None:
        payload: dict[str, object] = {
            "resolution_consumed": True,
            "resolution_evidence": {},
        }
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_resolution_consumed_with_evidence_is_true(self) -> None:
        payload: dict[str, object] = {
            "resolution_consumed": True,
            "resolution_evidence": {"comment_id": "12345", "question": "which cap?"},
        }
        assert extract_claim_evidence(payload).resolution_consumed is True

    def test_resolution_evidence_without_the_boolean_is_false(self) -> None:
        payload: dict[str, object] = {"resolution_evidence": {"comment_id": "12345"}}
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_non_dict_resolution_evidence_is_false(self) -> None:
        payload: dict[str, object] = {
            "resolution_consumed": True,
            "resolution_evidence": "yes",
        }
        assert extract_claim_evidence(payload).resolution_consumed is False

    def test_blocked_result_shaped_payload_yields_all_false(self) -> None:
        # A BlockedResult carries no commits/review keys at all; plain .get()
        # defaults must make it read as zero evidence rather than raising.
        payload: dict[str, object] = {
            "status": "blocked",
            "blocker": {"reason": "merge_gate_blocked", "detail": "CI red"},
        }
        assert extract_claim_evidence(payload) == ClaimEvidence(
            had_commits=False,
            had_findings=False,
            resolution_consumed=False,
            human_input_park=False,
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
            had_commits=True,
            had_findings=True,
            resolution_consumed=True,
            human_input_park=False,
        )

    def test_producer_shaped_autodevresult_dump_is_read_as_consumed(self) -> None:
        """#1896: a real producer payload round-tripped through the schema.

        Builds an actual ``AutoDevResult`` (mirroring how ``session.last_result``
        is persisted — ``model_dump(mode="json")`` of a validated model, per
        ``src/cw/result.py``), then feeds that dump straight to the extractor,
        proving the producer-shaped payload — not a hand-built dict — is read
        as consumed.
        """
        from cw.auto_dev_result import AutoDevResult

        payload: dict[str, object] = {
            "schema_version": 4,
            "ticket_id": "UP-1896",
            "status": "ambiguities_pending_resolution",
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": None,
                "files": 0,
                "lines_estimate": 0,
                "lines_actual": None,
                "forbidden_touched": False,
            },
            "plan_source": "generated",
            "branch": None,
            "worktree_path": "/tmp/wt",
            "fork_point_sha": None,
            "commits": [],
            "pr": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": None,
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "ambiguities": [{"question": "which cap?"}],
            "blocker": None,
            "next_actions": ["user_resolve_cap"],
            "resolution_consumed": True,
            "resolution_evidence": {"comment_id": "123", "items": ["R1"]},
        }
        result = AutoDevResult.model_validate(payload)
        dumped = result.model_dump(mode="json")

        assert extract_claim_evidence(dumped).resolution_consumed is True

    def test_blocked_status_with_resolution_consumed_is_credited(self) -> None:
        """The extractor is status-agnostic for resolution_consumed (#2154).

        The extractor reads ``resolution_consumed``/``resolution_evidence`` off
        the payload with no status branch at all, so a ``status: "blocked"``
        sentinel (here, a plan-stage cap-check hard-exit shape — ``blocker.
        reason: "ambiguity_scan_unconverged"``) is credited exactly like a
        ``status: "ambiguities_pending_resolution"`` pause. This is a generic
        regression-lock on the consumer's shape, independent of which markdown
        exit actually emits these two keys — the cap/stub hard-exits do not,
        per the operator's #2154 review disposition (see
        ``auto-dev-plan.md``'s emission rule), but nothing here depends on
        that; only the payload shape matters to this consumer.
        """
        payload: dict[str, object] = {
            "status": "blocked",
            "blocker": {
                "stage": "stage1_plan",
                "reason": "ambiguity_scan_unconverged",
                "details": "A2 still open after 2 rounds",
            },
            "resolution_consumed": True,
            "resolution_evidence": {"comment_id": "1", "items": ["A2"]},
        }
        assert extract_claim_evidence(payload) == ClaimEvidence(
            had_commits=False,
            had_findings=False,
            resolution_consumed=True,
            human_input_park=False,
        )


class TestHumanInputPark:
    """#2695: human-input parks do not charge, except ``plan_pending_approval``."""

    _EXPECTED_PARK_CHARGE: ClassVar[dict[str, bool]] = {
        "ambiguities_pending_resolution": True,
        "premises_pending_verification": True,
        "review_pending_approval": True,
        # R5: auto_adopt_clean_plan can re-claim this park with no human, so
        # the ceiling stays its bound.
        "plan_pending_approval": False,
    }

    def test_expected_map_covers_the_whole_vocabulary(self) -> None:
        # A vocabulary addition must force an intentional charged/uncharged
        # decision here rather than silently inheriting one.
        assert set(self._EXPECTED_PARK_CHARGE) == PAUSED_FOR_USER_INPUT_STATUSES

    @pytest.mark.parametrize("status", sorted(PAUSED_FOR_USER_INPUT_STATUSES))
    def test_paused_status_classification(self, status: str) -> None:
        payload: dict[str, object] = {"status": status, "commits": []}
        assert (
            extract_claim_evidence(payload).human_input_park
            is self._EXPECTED_PARK_CHARGE[status]
        )

    def test_plan_pending_approval_still_charges(self) -> None:
        payload: dict[str, object] = {"status": "plan_pending_approval"}
        evidence = extract_claim_evidence(payload)
        assert evidence.human_input_park is False
        assert is_unproductive(evidence) is True

    @pytest.mark.parametrize(
        "status", ["blocked", "shipped", "no_op", "stage_complete"]
    )
    def test_non_park_status_is_not_a_park(self, status: str) -> None:
        payload: dict[str, object] = {"status": status}
        assert extract_claim_evidence(payload).human_input_park is False

    @pytest.mark.parametrize("reason", sorted(OPERATOR_UNAVAILABLE_BLOCKER_REASONS))
    def test_operator_unavailable_blocker_is_a_park(self, reason: str) -> None:
        payload: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "stage4_finalize", "reason": reason},
        }
        evidence = extract_claim_evidence(payload)
        assert evidence.human_input_park is True
        assert is_unproductive(evidence) is False

    @pytest.mark.parametrize(
        "reason",
        ["agent_block", "ambiguity_scan_unconverged", "deferred_stub_unresolved"],
    )
    def test_chargeable_blocker_is_not_a_park(self, reason: str) -> None:
        # The #2154 hard-exits keep a bound on a non-converging plan stage.
        payload: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "stage1_plan", "reason": reason},
        }
        evidence = extract_claim_evidence(payload)
        assert evidence.human_input_park is False
        assert is_unproductive(evidence) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": ["ambiguities_pending_resolution"]},
            {"status": 7},
            {"status": "blocked", "blocker": {"reason": ["push_auth_failed"]}},
            {"status": "blocked", "blocker": {"reason": None}},
            {"status": "blocked", "blocker": "push_auth_failed"},
            {"status": "blocked", "blocker": ["push_auth_failed"]},
        ],
    )
    def test_malformed_values_degrade_without_raising(
        self, payload: dict[str, object]
    ) -> None:
        assert extract_claim_evidence(payload).human_input_park is False


class TestIsUnproductive:
    """OR-combination truth table across the four evidence fields."""

    @pytest.mark.parametrize(
        (
            "had_commits",
            "had_findings",
            "resolution_consumed",
            "human_input_park",
        ),
        list(itertools.product([False, True], repeat=4)),
    )
    def test_truth_table(
        self,
        had_commits: bool,
        had_findings: bool,
        resolution_consumed: bool,
        human_input_park: bool,
    ) -> None:
        evidence = ClaimEvidence(
            had_commits=had_commits,
            had_findings=had_findings,
            resolution_consumed=resolution_consumed,
            human_input_park=human_input_park,
        )
        # Only the all-False row (no signal at all) is unproductive.
        all_false = (had_commits, had_findings, resolution_consumed, human_input_park)
        assert is_unproductive(evidence) is (all_false == (False, False, False, False))

    def test_no_payload_at_all_is_unproductive(self) -> None:
        # The #1653 crashloop shape: a dead session leaves no sentinel, so the
        # claim is unproductive and must be charged.
        assert is_unproductive(extract_claim_evidence(None)) is True
