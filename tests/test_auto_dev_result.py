"""Tests for cw.auto_dev_result - sentinel block parser + invariants."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from cw.auto_dev_result import (
    AutoDevResult,
    BlockedResult,
    extract_block,
    parse_stdout,
)

# ---------------------------------------------------------------------------
# Payload helpers — keep round-trippable shapes for each terminal status.
# ---------------------------------------------------------------------------


def _shipped_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-1234",
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 42,
            "lines_actual": 47,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/gen-1234-fix-login",
        "worktree_path": "/tmp/wt/gen-1234",
        "fork_point_sha": "abc1234",
        "commits": ["sha1", "sha2"],
        "pr": {
            "number": 42,
            "url": "https://github.com/foo/bar/pull/42",
            "auto_merge": True,
            "base": "main",
        },
        "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "MEDIUM",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["wait_for_ci"],
    }


def _plan_pending_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-2",
        "status": "plan_pending_approval",
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "large",
            "files": 25,
            "lines_estimate": 1200,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["user_approve_plan"],
    }


def _review_pending_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-3",
        "status": "review_pending_approval",
        "stage_reached": "stage3_review",
        "scope": {
            "tier": "large",
            "files": 12,
            "lines_estimate": 600,
            "lines_actual": 580,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/gen-3",
        "worktree_path": "/tmp/wt/gen-3",
        "fork_point_sha": "def456",
        "commits": ["c1"],
        "pr": None,
        "review": {"must_fix_initial": 2, "should_fix": 0, "fix_cycles_used": 1},
        "health": {
            "lowest_agent_confidence": "MEDIUM",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["user_approve_review"],
    }


def _merge_gate_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-4",
        "status": "merge_gate_blocked",
        "stage_reached": "stage4a_merge_gate",
        "scope": {
            "tier": "small",
            "files": 2,
            "lines_estimate": 20,
            "lines_actual": 18,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/gen-4",
        "worktree_path": "/tmp/wt/gen-4",
        "fork_point_sha": "fff",
        "commits": ["c1"],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["resolve_merge_gate"],
    }


def _scope_exceeded_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-5",
        "status": "scope_exceeded",
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "large",
            "files": 30,
            "lines_estimate": 2000,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": [],
    }


def _forbidden_area_payload() -> dict[str, Any]:
    p = _scope_exceeded_payload()
    p["ticket_id"] = "GEN-6"
    p["status"] = "forbidden_area"
    p["scope"]["tier"] = "small"
    p["scope"]["files"] = 5
    p["scope"]["lines_estimate"] = 100
    p["scope"]["forbidden_touched"] = True
    return p


def _no_op_payload() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "ticket_id": "GEN-no-op",
        "status": "no_op",
        "stage_reached": "stage1_pre_flight",
        "scope": {
            "tier": "small",
            "files": 0,
            "lines_estimate": 0,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "none",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["close_issue_as_completed"],
    }


def _blocked_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ticket_id": "GEN-7",
        "status": "blocked",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 4,
            "lines_estimate": 80,
            "lines_actual": 60,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/gen-7",
        "worktree_path": "/tmp/wt/gen-7",
        "fork_point_sha": "aaa",
        "commits": ["c1"],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "LOW",
            "any_incomplete_risk": True,
            "shortcuts": ["skipped lint"],
            "recommendation": "EXIT_FOR_HUMAN_REVIEW",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": ["impl agent reported BLOCK"],
        "blocker": {
            "stage": "stage2_impl",
            "reason": "impl_failed",
            "details": "tests failed twice",
        },
        "next_actions": [],
    }


def _wrap_sentinel(payload: dict[str, Any]) -> str:
    body = json.dumps(payload)
    return f"some narrative\n<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"


# ---------------------------------------------------------------------------
# Status round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload_factory",
    [
        _shipped_payload,
        _plan_pending_payload,
        _review_pending_payload,
        _merge_gate_payload,
        _scope_exceeded_payload,
        _forbidden_area_payload,
        _blocked_payload,
        _no_op_payload,
    ],
)
class TestStatusRoundTrips:
    def test_parses_clean(self, payload_factory: Any) -> None:
        result = parse_stdout(_wrap_sentinel(payload_factory()))
        assert isinstance(result, AutoDevResult)
        assert result.status == payload_factory()["status"]

    def test_round_trip_preserves_status(self, payload_factory: Any) -> None:
        payload = payload_factory()
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        # model_dump round-trip preserves the status enum
        dumped = result.model_dump(mode="json")
        assert dumped["status"] == payload["status"]


# ---------------------------------------------------------------------------
# §6 Failure modes
# ---------------------------------------------------------------------------


class TestSentinelFailureModes:
    def test_no_sentinel_at_all(self) -> None:
        result = parse_stdout("just narrative, no block here\n")
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"

    def test_open_sentinel_no_close(self) -> None:
        text = '<<<AUTO_DEV_RESULT\n{"schema_version": 1\n... crashed mid-emit'
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"
        assert "opening sentinel present" in result.blocker.details

    def test_malformed_json(self) -> None:
        text = "<<<AUTO_DEV_RESULT\n{not json,,,}\nAUTO_DEV_RESULT>>>\n"
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"
        assert "JSON parse failed" in result.blocker.details

    def test_unsupported_schema_version(self) -> None:
        payload = _shipped_payload()
        payload["schema_version"] = 99
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "schema_version_unsupported"

    def test_schema_v2_accepted(self) -> None:
        # A v1-shape payload should round-trip cleanly under schema_version=2;
        # the only v2-exclusive surface is the no_op status, but every other
        # field is unchanged.
        payload = _shipped_payload()
        payload["schema_version"] = 2
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        assert result.schema_version == 2

    def test_missing_schema_version(self) -> None:
        payload = _shipped_payload()
        del payload["schema_version"]
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "schema_version_unsupported"

    def test_unknown_status(self) -> None:
        payload = _shipped_payload()
        payload["status"] = "exploded"
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "status_unknown"
        assert "exploded" in result.blocker.details

    def test_multiple_blocks(self) -> None:
        text = (
            _wrap_sentinel(_shipped_payload())
            + "extra noise\n"
            + _wrap_sentinel(_plan_pending_payload())
        )
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "multiple_result_blocks"
        assert "count=2" in result.blocker.details

    def test_top_level_not_an_object(self) -> None:
        text = "<<<AUTO_DEV_RESULT\n[1, 2, 3]\nAUTO_DEV_RESULT>>>\n"
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"

    def test_validation_failure_surfaces_blocked(self) -> None:
        # Invariant violation: shipped without pr → ValidationError → blocked.
        payload = _shipped_payload()
        payload["pr"] = None
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "validation_failed"


# ---------------------------------------------------------------------------
# Sentinel framing edge cases
# ---------------------------------------------------------------------------


class TestSentinelFraming:
    def test_extra_noise_before_and_after(self) -> None:
        text = (
            "lots of pre-noise\n"
            "more lines\n" + _wrap_sentinel(_shipped_payload()) + "trailing CI url\n"
        )
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_extract_block_returns_none_when_absent(self) -> None:
        assert extract_block("nothing here") is None

    def test_extract_block_returns_inner_text(self) -> None:
        text = _wrap_sentinel(_shipped_payload())
        inner = extract_block(text)
        assert inner is not None
        assert json.loads(inner)["status"] == "shipped"

    def test_narrative_quoting_sentinel_doesnt_break_parse(self) -> None:
        # If narrative happens to mention the literal sentinel string but
        # without a complete pair, it must not be extracted.
        text = "the agent said: <<<AUTO_DEV_RESULT is the sentinel\n" + _wrap_sentinel(
            _shipped_payload()
        )
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)


# ---------------------------------------------------------------------------
# Cross-field invariants (per the owner's comment + §3-§5)
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_shipped_requires_pr(self) -> None:
        p = _shipped_payload()
        p["pr"] = None
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_non_shipped_rejects_pr(self) -> None:
        p = _plan_pending_payload()
        p["pr"] = {
            "number": 1,
            "url": "x",
            "auto_merge": True,
            "base": "main",
        }
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_blocked_requires_blocker(self) -> None:
        p = _blocked_payload()
        p["blocker"] = None
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_non_blocked_rejects_blocker(self) -> None:
        p = _shipped_payload()
        p["blocker"] = {"stage": "x", "reason": "y", "details": ""}
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_shipped_requires_wait_for_ci(self) -> None:
        p = _shipped_payload()
        p["next_actions"] = []
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_non_shipped_rejects_wait_for_ci(self) -> None:
        p = _plan_pending_payload()
        p["next_actions"] = ["wait_for_ci"]
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_downgrade_applied_requires_review_pending_small(self) -> None:
        # downgrade=True on a shipped payload (which is also small) is
        # rejected because the status must be review_pending_approval.
        p = _shipped_payload()
        p["health"]["downgrade_applied"] = True
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_downgrade_applied_accepted_on_review_pending_small(self) -> None:
        p = _review_pending_payload()
        p["scope"]["tier"] = "small"
        p["health"]["downgrade_applied"] = True
        # large→small swap also needs lines_actual present; already set
        result = AutoDevResult.model_validate(p)
        assert result.health.downgrade_applied is True

    def test_merge_gate_blocked_requires_small(self) -> None:
        p = _merge_gate_payload()
        p["scope"]["tier"] = "large"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_pre_branch_status_rejects_branch(self) -> None:
        p = _plan_pending_payload()
        p["branch"] = "dev/sneak"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_stage1_plan_requires_lines_actual_null(self) -> None:
        p = _plan_pending_payload()
        p["scope"]["lines_actual"] = 50
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_post_impl_requires_lines_actual_present(self) -> None:
        p = _shipped_payload()
        p["scope"]["lines_actual"] = None
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_terminal_reject_rejects_next_actions(self) -> None:
        p = _scope_exceeded_payload()
        p["next_actions"] = ["something"]
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_no_op_rejected_at_schema_v1(self) -> None:
        # Per §8: new status values only valid at the version that introduced
        # them. A v1-tagged payload with status=no_op is a producer bug —
        # surface as validation_failed rather than silently accepting.
        p = _no_op_payload()
        p["schema_version"] = 1
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "validation_failed"

    def test_no_op_rejects_branch(self) -> None:
        # no_op is a pre-branch status — emitting a branch is a contract
        # violation.
        p = _no_op_payload()
        p["branch"] = "dev/sneak"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_no_op_rejects_pr(self) -> None:
        p = _no_op_payload()
        p["pr"] = {"number": 1, "url": "x", "auto_merge": True, "base": "main"}
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_blocker_reason_is_open_enum(self) -> None:
        # The producer can emit reasons we've never seen; consumers must
        # accept them rather than forcing a closed Literal.
        p = _blocked_payload()
        p["blocker"]["reason"] = "future_unknown_reason"
        result = AutoDevResult.model_validate(p)
        assert result.blocker is not None
        assert result.blocker.reason == "future_unknown_reason"


# ---------------------------------------------------------------------------
# Phase B + E — Blocker context and retry semantics (#174)
# ---------------------------------------------------------------------------


class TestBlockerPhaseBE:
    def test_v2_block_without_new_fields_still_parses(self) -> None:
        """Back-compat: producers on v1/v2 emit no new fields; parser accepts."""
        p = _blocked_payload()
        result = AutoDevResult.model_validate(p)
        assert isinstance(result.blocker, type(result.blocker))
        assert result.blocker is not None
        assert result.blocker.exception_type is None
        assert result.blocker.message is None
        assert result.blocker.recovery_hint is None
        assert result.blocker.retry_eligible is None
        assert result.blocker.retry_delay_seconds is None

    def test_phase_b_fields_round_trip(self) -> None:
        p = _blocked_payload()
        p["blocker"].update(
            {
                "exception_type": "PlanValidationError",
                "message": (
                    "Plan contains MUST_FIX findings that persist after revision"
                ),
                "recovery_hint": "Manual review required; update plan and retry",
            },
        )
        result = AutoDevResult.model_validate(p)
        assert result.blocker is not None
        assert result.blocker.exception_type == "PlanValidationError"
        assert "MUST_FIX" in (result.blocker.message or "")
        assert "Manual review" in (result.blocker.recovery_hint or "")

    def test_phase_e_retry_eligible_with_delay(self) -> None:
        p = _blocked_payload()
        p["blocker"].update(
            {
                "reason": "ci_timeout",
                "retry_eligible": True,
                "retry_delay_seconds": 120,
            },
        )
        result = AutoDevResult.model_validate(p)
        assert result.blocker is not None
        assert result.blocker.retry_eligible is True
        assert result.blocker.retry_delay_seconds == 120

    def test_phase_e_retry_ineligible_no_delay(self) -> None:
        p = _blocked_payload()
        p["blocker"].update(
            {
                "reason": "plan_unreviewable",
                "retry_eligible": False,
                "retry_delay_seconds": None,
            },
        )
        result = AutoDevResult.model_validate(p)
        assert result.blocker is not None
        assert result.blocker.retry_eligible is False
        assert result.blocker.retry_delay_seconds is None

    def test_retry_delay_without_eligible_rejected(self) -> None:
        # A delay only makes sense when the orchestrator is allowed to retry.
        p = _blocked_payload()
        p["blocker"].update(
            {"retry_eligible": False, "retry_delay_seconds": 30},
        )
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_retry_delay_negative_rejected(self) -> None:
        p = _blocked_payload()
        p["blocker"].update(
            {"retry_eligible": True, "retry_delay_seconds": -5},
        )
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_phase_b_and_e_together_on_v3_block(self) -> None:
        p = _blocked_payload()
        p["schema_version"] = 3
        p["blocker"].update(
            {
                "reason": "ci_timeout",
                "exception_type": "CITimeoutError",
                "message": "CI did not complete within 30 minutes",
                "recovery_hint": "Re-dispatch with a 2-minute delay",
                "retry_eligible": True,
                "retry_delay_seconds": 120,
            },
        )
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.blocker is not None
        assert result.blocker.exception_type == "CITimeoutError"
        assert result.blocker.retry_eligible is True
        assert result.blocker.retry_delay_seconds == 120


# ---------------------------------------------------------------------------
# stage1_pre_flight + plan_source=none (v3, with v2 backward-compat exception)
# ---------------------------------------------------------------------------


class TestPreFlightNoOp:
    def test_no_op_payload_round_trips(self) -> None:
        """v3 pre-flight no-op payload serializes and parses correctly."""
        result = parse_stdout(_wrap_sentinel(_no_op_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.stage_reached == "stage1_pre_flight"
        assert result.plan_source == "none"

    def test_no_op_payload_v2_backward_compat(self) -> None:
        """Pre-flight shape accepted under schema_version=2 (rollout exception)."""
        p = _no_op_payload()
        p["schema_version"] = 2
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.stage_reached == "stage1_pre_flight"
        assert result.plan_source == "none"
        assert result.schema_version == 2

    def test_stage1_pre_flight_requires_no_op_status(self) -> None:
        # stage_reached=stage1_pre_flight with any non-no_op status is rejected.
        p = _no_op_payload()
        p["status"] = "shipped"
        p["pr"] = {
            "number": 1,
            "url": "https://github.com/x/y/pull/1",
            "auto_merge": True,
            "base": "main",
        }
        p["next_actions"] = ["wait_for_ci"]
        p["scope"]["lines_actual"] = None  # still pre-impl stage
        with pytest.raises(ValidationError, match="stage1_pre_flight"):
            AutoDevResult.model_validate(p)

    def test_lines_actual_allowed_none_at_stage1_pre_flight(self) -> None:
        """lines_actual=None is valid at stage1_pre_flight (pre-impl exit)."""
        p = _no_op_payload()
        assert p["scope"]["lines_actual"] is None
        result = AutoDevResult.model_validate(p)
        assert result.scope.lines_actual is None

    def test_lines_actual_non_null_rejected_at_stage1_pre_flight(self) -> None:
        """lines_actual non-null at stage1_pre_flight violates pre-impl invariant."""
        p = _no_op_payload()
        p["scope"]["lines_actual"] = 10
        with pytest.raises(ValidationError, match="lines_actual"):
            AutoDevResult.model_validate(p)


# ---------------------------------------------------------------------------
# plan_source="github_issue_existing" (added per #190 — producer-side rename
# of "linear_existing" for GitHub-sourced runs; treated identically).
# ---------------------------------------------------------------------------


class TestPlanSourceGitHubIssueExisting:
    def test_shipped_run_with_github_issue_existing_parses(self) -> None:
        """A real shipped payload (#149 captured) emitted under v2."""
        p = _shipped_payload()
        p["schema_version"] = 2
        p["plan_source"] = "github_issue_existing"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.plan_source == "github_issue_existing"
        assert result.status == "shipped"
        assert result.schema_version == 2

    def test_no_op_run_with_github_issue_existing_parses(self) -> None:
        """A pre-flight no_op payload (#136 captured) with the GitHub plan_source."""
        p = _no_op_payload()
        p["schema_version"] = 2
        p["plan_source"] = "github_issue_existing"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.plan_source == "github_issue_existing"
        assert result.status == "no_op"
