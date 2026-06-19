"""Tests for cw.auto_dev_result - sentinel block parser + invariants."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    AutoDevResult,
    BlockedResult,
    extract_block,
    is_documented_example,
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


def _ambiguities_pending_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "GEN-ambig",
        "status": "ambiguities_pending_resolution",
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 80,
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
        "ambiguities": [
            {
                "question": "Should the enum be open or closed?",
                "plan_assumption": "closed",
                "alternatives": ["open"],
                "why_it_matters": "affects consumer contract",
                "ticket_evidence": "option 1 in the ticket",
            }
        ],
        "premises": [],
        "next_actions": ["user_resolve_ambiguities"],
    }


def _premises_pending_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "GEN-premise",
        "status": "premises_pending_verification",
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "small",
            "files": 2,
            "lines_estimate": 40,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
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
        "ambiguities": [],
        "premises": [
            {
                "claim": "The existing PR #198 codified a deliberate decision",
                "plan_depends_on_it_for": "deciding whether to override §4.4",
                "how_to_verify": "read PR #198 body",
            }
        ],
        "next_actions": ["user_verify_premises"],
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
        _ambiguities_pending_payload,
        _premises_pending_payload,
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
# Code-fence stripping
# ---------------------------------------------------------------------------


class TestCodeFenceStripping:
    """parse_stdout must handle code-fence-wrapped JSON payloads."""

    def test_json_fence_parses_same_as_raw(self) -> None:
        """```json\n...\n``` wrapper produces same AutoDevResult as raw JSON."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = (
            f"narrative\n<<<AUTO_DEV_RESULT\n```json\n{body}\n```\nAUTO_DEV_RESULT>>>\n"
        )
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_plain_fence_parses_same_as_raw(self) -> None:
        """```\n...\n``` (no language specifier) also parses."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = f"narrative\n<<<AUTO_DEV_RESULT\n```\n{body}\n```\nAUTO_DEV_RESULT>>>\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_misformed_fence_wrong_language_fails(self) -> None:
        """Unknown language spec (typescript) is NOT stripped; json.loads rejects it."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        sentinel = "<<<AUTO_DEV_RESULT\n```typescript\n"
        text = f"narrative\n{sentinel}{body}\n```\nAUTO_DEV_RESULT>>>\n"
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"

    def test_misformed_fence_no_closing_fails(self) -> None:
        """```json\n... without closing ``` is NOT stripped — json.loads rejects it."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = f"narrative\n<<<AUTO_DEV_RESULT\n```json\n{body}\nAUTO_DEV_RESULT>>>\n"
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"


# ---------------------------------------------------------------------------
# Loose fallback: code-fenced JSON without AUTO_DEV_RESULT markers (GH #337)
# ---------------------------------------------------------------------------


class TestLooseFallback:
    """parse_stdout must recover a valid payload from bare code-fenced JSON."""

    def test_json_fence_without_markers_parses(self) -> None:
        """```json\\n{...}\\n``` without markers is accepted as a loose fallback."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = f"narrative\n\n```json\n{body}\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_plain_fence_without_markers_parses(self) -> None:
        """```\\n{...}\\n``` (no language tag) is also accepted."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = f"All done:\n\n```\n{body}\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_last_fence_wins_when_multiple(self) -> None:
        """When multiple code fences exist, the last auto-dev-shaped one is used."""
        shipped = _shipped_payload()
        plan = _plan_pending_payload()
        shipped_body = json.dumps(shipped)
        plan_body = json.dumps(plan)
        text = f"```json\n{shipped_body}\n```\nmore\n```json\n{plan_body}\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "plan_pending_approval"

    def test_non_auto_dev_fence_not_accepted(self) -> None:
        """A code fence without schema_version+status is not treated as a sentinel."""
        text = '```json\n{"foo": "bar"}\n```\n'
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "no_result_emitted"

    def test_opening_sentinel_present_takes_precedence(self) -> None:
        """If the opening sentinel IS present (but close is missing), the
        crash-mid-emit path fires — loose fallback does NOT apply."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        # Has the opening marker but no close; also has a bare code fence.
        text = f"<<<AUTO_DEV_RESULT\n```json\n{body}\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, BlockedResult)
        assert "opening sentinel present" in result.blocker.details

    def test_invalid_json_fence_skipped_valid_earlier_one_used(self) -> None:
        """Invalid-JSON fence at end is skipped; earlier valid one is used.

        _extract_loose_sentinel_json iterates reversed (last fence first).  The
        last fence here contains unparseable text — exercises the JSONDecodeError
        branch — then falls back to the earlier valid fence.
        """
        payload = _shipped_payload()
        body = json.dumps(payload)
        # Valid fence FIRST; invalid fence LAST (scanned first in reversed order).
        text = f"```json\n{body}\n```\n\n```json\nnot parseable at all\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"

    def test_loose_fallback_preserves_all_fields(self) -> None:
        """Loose-parsed result round-trips all required fields correctly."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        text = f"worker output:\n\n```json\n{body}\n```\n"
        result = parse_stdout(text)
        assert isinstance(result, AutoDevResult)
        assert result.ticket_id == payload["ticket_id"]
        assert result.pr is not None
        assert result.pr.number == payload["pr"]["number"]


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

    def test_merge_gate_blocked_accepts_large_tier(self) -> None:
        # Issue #430 case 3: tier constraint on merge_gate_blocked relaxed.
        # A large ticket that hit a merge gate is no longer rejected.
        p = _merge_gate_payload()
        p["scope"]["tier"] = "large"
        result = AutoDevResult.model_validate(p)
        assert result.status == "merge_gate_blocked"
        assert result.scope.tier == "large"

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

    def test_no_op_with_stray_pr_coerced_to_clean_no_op(self) -> None:
        """parse_stdout coerces no_op+non-null pr to a clean no_op (issue #367).

        A producer bug emitted status=no_op alongside a non-null pr field,
        causing the §3.3 invariant to reject it as validation_failed/blocked.
        The strict AutoDevResult.model_validate still rejects the shape
        (see test_no_op_rejects_pr); leniency applies only at the parse boundary.
        """
        p = _no_op_payload()
        p["pr"] = {
            "number": 42,
            "url": "https://github.com/x/y/pull/42",
            "auto_merge": True,
            "base": "main",
        }
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.pr is None
        assert result.next_actions == ["close_issue_as_completed"]

    def test_no_op_with_stray_branch_and_commits_coerced(self) -> None:
        """parse_stdout strips stray branch/commits from no_op sentinels."""
        p = _no_op_payload()
        p["branch"] = "dev/gen-327-no-op"
        p["commits"] = ["abc1234", "def5678"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.branch is None
        assert result.commits == []

    def test_shipped_with_null_pr_still_fails_loudly(self) -> None:
        """Coerce logic does NOT apply to shipped; shipped+null pr still rejects."""
        p = _no_op_payload()
        p["status"] = "shipped"
        p["pr"] = None
        p["stage_reached"] = "stage5_post_create"
        p["scope"]["lines_actual"] = 10
        p["next_actions"] = ["wait_for_ci"]
        with pytest.raises(ValidationError, match="pr must be non-null"):
            AutoDevResult.model_validate(p)

    def test_stage1_pre_flight_rejects_incompatible_status(self) -> None:
        """Pre-flight exit only allows {no_op, blocked} — shipped et al are rejected.

        The `blocked` admission was added for #226 (Origin Sync block); see
        TestStage1PreFlightBlocked for the positive cases. Any other status
        at pre-flight is still a contract violation.
        """
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
# no_op + stray scope.lines_actual coerce (issue #399)
# A producer bug emitted status=no_op with stage_reached=stage1_pre_flight
# carrying a non-null scope.lines_actual.  The §3.3 cross-field invariant
# rejects this, routing a legitimate no_op to validation_failed → retry cap
# → failed (session 8043fe9f re-running already-satisfied ticket #143).
# parse_stdout coerces scope.lines_actual to null for no_op sentinels where
# stage_reached is stage1_pre_flight or stage1_plan.
# ---------------------------------------------------------------------------


class TestNoOpStrayLinesActualCoerce:
    def test_no_op_stray_lines_actual_coerced_at_stage1_pre_flight(self) -> None:
        """parse_stdout coerces no_op + stray scope.lines_actual to clean no_op.

        Regression for issue #399: the exact failure mode from session 8043fe9f
        re-running already-satisfied ticket #143 — a no_op sentinel carrying a
        non-null scope.lines_actual triggered the §3.3 pre-impl invariant,
        routing to validation_failed instead of cleanly closing the issue.
        """
        p = _no_op_payload()
        p["scope"]["lines_actual"] = 42  # stray non-null value from producer
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.scope.lines_actual is None

    def test_no_op_stray_lines_actual_coerced_at_stage1_plan(self) -> None:
        """Coercion also fires when stage_reached is stage1_plan (pre-impl)."""
        p = _no_op_payload()
        p["stage_reached"] = "stage1_plan"
        p["scope"]["lines_actual"] = 10
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.scope.lines_actual is None

    def test_no_op_stray_lines_actual_coerced_via_alias(self) -> None:
        """Coercion fires when stage_reached uses a short-form alias (pre_flight)."""
        p = _no_op_payload()
        p["stage_reached"] = "pre_flight"  # alias for stage1_pre_flight
        p["scope"]["lines_actual"] = 7
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.scope.lines_actual is None

    def test_no_op_null_lines_actual_unchanged(self) -> None:
        """null scope.lines_actual is the correct shape — coerce must not touch it."""
        p = _no_op_payload()
        assert p["scope"]["lines_actual"] is None
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.scope.lines_actual is None

    def test_post_impl_non_null_lines_actual_not_coerced(self) -> None:
        """Coerce does not fire for post-impl stages — non-null passes through."""
        p = _no_op_payload()
        # stage2_impl is outside {stage1_pre_flight, stage1_plan}, so the
        # coerce guard does NOT fire. lines_actual=30 is valid for a post-impl
        # stage and passes through to the parsed result unchanged.
        p["stage_reached"] = "stage2_impl"
        p["scope"]["lines_actual"] = 30
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "no_op"
        assert result.scope.lines_actual == 30  # not zeroed by coerce

    def test_regression_issue_399_session_8043fe9f_payload(self) -> None:
        """Exact payload shape from session 8043fe9f that triggered the #399 bug.

        The headless re-run of #143 emitted schema_version=4, status=no_op,
        stage_reached=stage1_pre_flight with a stray non-null scope.lines_actual.
        This caused BlockedResult(reason='validation_failed') → PENDING → retry
        cap → 'failed', burning 3 sessions on already-satisfied work.
        """
        payload = {
            "schema_version": 4,
            "ticket_id": "GEN-143",
            "status": "no_op",
            "stage_reached": "stage1_pre_flight",
            "scope": {
                "tier": "small",
                "files": 0,
                "lines_estimate": 0,
                "lines_actual": 0,  # stray non-null: the producer emitted 0, not null
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
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult), (
            f"Expected AutoDevResult, got BlockedResult: {result}"
        )
        assert result.status == "no_op"
        assert result.stage_reached == "stage1_pre_flight"
        assert result.scope.lines_actual is None


# ---------------------------------------------------------------------------
# Issue #416 — pre-impl blocked sentinels with null tier/confidence
# scope.tier and health.lowest_agent_confidence are Optional for pre-impl exits.
# When auto-dev exits before scope classification (Stage 0/1), these are
# legitimately null, but the model previously rejected them with validation_failed.
# ---------------------------------------------------------------------------


class TestPreImplNullTierAndConfidence:
    """scope.tier and health.lowest_agent_confidence are Optional for pre-impl exits.

    Issue #416: pre-impl blocked sentinels legitimately carry null tier/confidence
    (no scope was classified). Previously these failed as validation_failed.
    """

    def test_pre_impl_blocked_null_tier_and_confidence_parses(self) -> None:
        """blocked at stage1_plan with null tier + confidence parses cleanly."""
        p = _blocked_payload()
        p["stage_reached"] = "stage1_plan"
        p["scope"]["tier"] = None
        p["scope"]["lines_actual"] = None
        p["health"]["lowest_agent_confidence"] = None
        p["branch"] = None
        p["worktree_path"] = None
        p["fork_point_sha"] = None
        p["commits"] = []
        result = AutoDevResult.model_validate(p)
        assert result.scope.tier is None
        assert result.health.lowest_agent_confidence is None

    def test_pre_impl_ambiguities_null_tier_and_confidence_parses(self) -> None:
        """ambiguities_pending_resolution with null tier/confidence parses."""
        p = _ambiguities_pending_payload()
        p["scope"]["tier"] = None
        result = AutoDevResult.model_validate(p)
        assert result.scope.tier is None

    def test_post_impl_null_tier_rejected(self) -> None:
        """scope.tier must be non-null at post-impl stages (stage2_impl+)."""
        p = _blocked_payload()  # stage2_impl
        p["scope"]["tier"] = None
        with pytest.raises(ValidationError, match=r"scope\.tier"):
            AutoDevResult.model_validate(p)

    def test_post_impl_null_confidence_rejected(self) -> None:
        """lowest_agent_confidence must be non-null at post-impl stages."""
        p = _blocked_payload()  # stage2_impl
        p["health"]["lowest_agent_confidence"] = None
        with pytest.raises(ValidationError, match="lowest_agent_confidence"):
            AutoDevResult.model_validate(p)

    def test_regression_gc22_pre_impl_blocked_payload(self) -> None:
        """Regression: gc#22 shape that triggered validation_failed on valid blocked."""
        payload = {
            "schema_version": 4,
            "ticket_id": "22",
            "status": "blocked",
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": None,
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
                "lowest_agent_confidence": None,
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "EXIT_FOR_HUMAN_REVIEW",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": {
                "stage": "stage1_plan",
                "reason": "plan_unsound",
                "details": "plan direction contradicts architecture",
            },
            "next_actions": [],
        }
        sentinel = f"<<<AUTO_DEV_RESULT\n{json.dumps(payload)}\nAUTO_DEV_RESULT>>>"
        result = parse_stdout(sentinel)
        assert isinstance(result, AutoDevResult), (
            f"Expected AutoDevResult, got {result}"
        )
        assert result.status == "blocked"
        assert result.scope.tier is None
        assert result.health.lowest_agent_confidence is None


class TestShippedWaitForCiInject:
    """parse_stdout injects wait_for_ci for shipped sentinels missing it.

    Issue #417: an already-auto-merged PR legitimately omits wait_for_ci.
    """

    def test_parse_stdout_shipped_missing_wait_for_ci_injected(self) -> None:
        """parse_stdout injects wait_for_ci when shipped + empty next_actions."""
        p = _shipped_payload()
        p["next_actions"] = []
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"
        assert "wait_for_ci" in result.next_actions

    def test_strict_model_validate_shipped_missing_wait_for_ci_still_rejects(
        self,
    ) -> None:
        """Strict model_validate still rejects shipped without wait_for_ci."""
        p = _shipped_payload()
        p["next_actions"] = []
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_parse_stdout_shipped_with_wait_for_ci_not_duplicated(self) -> None:
        """wait_for_ci already present must not be duplicated."""
        p = _shipped_payload()
        assert "wait_for_ci" in p["next_actions"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.next_actions.count("wait_for_ci") == 1


class TestPreImplLinesActualZeroCoerce:
    """parse_stdout coerces lines_actual=0 to None at pre-impl stages (all statuses).

    Issue #416: workers emit lines_actual=0 instead of null at pre-impl stages.
    """

    def test_blocked_stage1_plan_lines_actual_zero_coerced(self) -> None:
        """Regression: #411 fanout — ambiguities_pending with lines_actual=0."""
        payload = {
            "schema_version": 4,
            "ticket_id": "411",
            "status": "ambiguities_pending_resolution",
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": "small",
                "files": 2,
                "lines_estimate": 80,
                "lines_actual": 0,
                "forbidden_touched": False,
            },
            "plan_source": "github_issue_existing",
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
            "ambiguities": [{"question": "Should this be closed-enum?"}],
            "premises": [],
            "next_actions": ["user_resolve_ambiguities"],
        }
        sentinel = f"<<<AUTO_DEV_RESULT\n{json.dumps(payload)}\nAUTO_DEV_RESULT>>>"
        result = parse_stdout(sentinel)
        assert isinstance(result, AutoDevResult), (
            f"Expected AutoDevResult, got {result}"
        )
        assert result.status == "ambiguities_pending_resolution"
        assert result.scope.lines_actual is None

    def test_pre_impl_lines_actual_nonzero_still_rejects(self) -> None:
        """lines_actual=50 at stage1_plan → BlockedResult (hard error)."""
        p = _ambiguities_pending_payload()
        p["scope"]["lines_actual"] = 50
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, BlockedResult)

    def test_post_impl_lines_actual_zero_not_coerced(self) -> None:
        """lines_actual=0 at stage2_impl is NOT coerced — post-impl, passes as-is."""
        p = _blocked_payload()  # stage2_impl
        p["scope"]["lines_actual"] = 0  # 0 at post-impl is valid (just zero lines)
        result = parse_stdout(_wrap_sentinel(p))
        # Either parses or fails, but NOT because of the pre-impl coerce
        # (the coerce guard should not fire at stage2_impl)
        # In fact lines_actual=0 is valid at post-impl (non-null, just zero changed)
        assert isinstance(result, AutoDevResult)
        assert result.scope.lines_actual == 0


# ---------------------------------------------------------------------------
# blocked + stray next_actions coerce (issue #371 — follow-up to #367/#370)
# A producer bug emits status=blocked alongside next_actions=['redispatch_ticket']
# (or other non-user-directed verbs). The §4.3 terminal-reject invariant then
# rejects the whole sentinel as validation_failed, masking the real blocker.
# parse_stdout coerces by dropping next_actions and preserving the blocker.
# Two exempt shapes are NOT coerced: pre-flight blocked and user-directed blocked.
# ---------------------------------------------------------------------------


class TestBlockedWithStrayNextActionsCoerce:
    def test_blocked_with_stray_next_actions_coerced(self) -> None:
        """parse_stdout coerces blocked+stray next_actions to clean blocked.

        Regression for issue #371: the exact failure mode from #367's first
        attempt — worker emitted blocked+next_actions=['redispatch_ticket'],
        causing validation_failed and masking the underlying impl_failed blocker.
        """
        p = _blocked_payload()
        p["next_actions"] = ["redispatch_ticket"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        assert result.next_actions == []

    def test_blocked_coerce_preserves_blocker(self) -> None:
        """Coercing stray next_actions must preserve the original blocker intact."""
        p = _blocked_payload()
        p["next_actions"] = ["redispatch_ticket"]
        p["blocker"]["retry_eligible"] = True
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.blocker is not None
        assert result.blocker.reason == "impl_failed"
        assert result.blocker.retry_eligible is True

    def test_blocked_strict_construction_still_rejects_stray_next_actions(
        self,
    ) -> None:
        """Strict AutoDevResult.model_validate still rejects blocked+next_actions."""
        p = _blocked_payload()
        p["next_actions"] = ["redispatch_ticket"]
        with pytest.raises(ValidationError, match="next_actions must be empty"):
            AutoDevResult.model_validate(p)

    def test_blocked_pre_flight_not_coerced(self) -> None:
        """Pre-flight blocked with sync_local_main is NOT coerced (legitimate shape)."""
        p = _blocked_payload()
        p["status"] = "blocked"
        p["stage_reached"] = "stage1_pre_flight"
        p["next_actions"] = ["sync_local_main"]
        p["scope"]["lines_actual"] = None
        p["branch"] = None
        p["worktree_path"] = None
        p["fork_point_sha"] = None
        p["commits"] = []
        p["blocker"]["stage"] = "stage1_pre_flight"
        p["blocker"]["reason"] = "local_main_diverged_from_origin"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        assert result.next_actions == ["sync_local_main"]

    def test_blocked_user_directed_not_coerced(self) -> None:
        """User-directed blocked (user_resolve_ prefix) is NOT coerced."""
        p = _blocked_payload()
        p["next_actions"] = ["user_resolve_ambiguity"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        assert result.next_actions == ["user_resolve_ambiguity"]


# ---------------------------------------------------------------------------
# stage1_pre_flight + blocked (added per #226 — Origin Sync block emits this
# combo legitimately; the consumer previously rejected it as a §4.3 violation,
# so every Origin-Sync-blocked sentinel became validation_failed). When status
# is blocked at pre-flight, next_actions is constrained to retry/escalation
# verbs rather than empty (which is required for the generic terminal-reject
# `blocked` shape at later stages).
# ---------------------------------------------------------------------------


def _pre_flight_blocked_payload() -> dict[str, Any]:
    """Origin-Sync-shaped sentinel: stage1_pre_flight + blocked + sync_local_main.

    Matches the producer's emit shape from commit 97c92b3 (cf #178). The
    blocker.reason is open-enum (§4.2) — `origin_sync_required` is the
    canonical value but any string is allowed.
    """
    return {
        "schema_version": 3,
        "ticket_id": "GEN-pre-flight-blocked",
        "status": "blocked",
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
            "recommendation": "EXIT_FOR_HUMAN_REVIEW",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": {
            "stage": "stage1_pre_flight",
            "reason": "origin_sync_required",
            "details": "local main behind origin/main; sync before dispatch",
        },
        "next_actions": ["sync_local_main"],
    }


class TestStage1PreFlightBlocked:
    def test_origin_sync_shaped_sentinel_parses_cleanly(self) -> None:
        """The exact shape the producer emits for Origin Sync block (#226)."""
        p = _pre_flight_blocked_payload()
        result = AutoDevResult.model_validate(p)
        assert result.status == "blocked"
        assert result.stage_reached == "stage1_pre_flight"
        assert result.next_actions == ["sync_local_main"]
        assert result.blocker is not None
        assert result.blocker.reason == "origin_sync_required"

    def test_manual_intervention_next_action_allowed(self) -> None:
        """Escalation path: blocker that needs human action, not retry."""
        p = _pre_flight_blocked_payload()
        p["next_actions"] = ["manual_intervention"]
        result = AutoDevResult.model_validate(p)
        assert result.next_actions == ["manual_intervention"]

    def test_empty_next_actions_rejected_when_blocked_at_pre_flight(self) -> None:
        """Pre-flight blocked must signal a recovery path — empty list is a bug.

        A producer emitting `blocked` with no next_actions at pre-flight
        gives the orchestrator nothing to route on. Force the producer to
        emit at least one of the allowed verbs.
        """
        p = _pre_flight_blocked_payload()
        p["next_actions"] = []
        with pytest.raises(ValidationError, match="next_actions"):
            AutoDevResult.model_validate(p)

    def test_invalid_next_action_rejected_at_pre_flight_blocked(self) -> None:
        """next_actions for pre-flight blocked is a closed set, not free-form.

        ``wait_for_ci`` would short-circuit on the earlier §4.3 ``wait_for_ci
        iff shipped`` rule, so use a verb that only the pre-flight closed-set
        check can reject. Asserts the rejection message points at the
        closed-set so we know the right branch fired.
        """
        p = _pre_flight_blocked_payload()
        p["next_actions"] = ["close_issue_as_completed"]  # valid for no_op, not blocked
        with pytest.raises(ValidationError, match="expected subset of"):
            AutoDevResult.model_validate(p)

    def test_mixed_valid_and_invalid_next_actions_rejected(self) -> None:
        """If any entry is outside the allowed set, the whole list is invalid.

        Producer must emit a pure list of allowed verbs — partial validity
        doesn't pass.
        """
        p = _pre_flight_blocked_payload()
        p["next_actions"] = ["sync_local_main", "free_text_recovery"]
        with pytest.raises(ValidationError, match="free_text_recovery"):
            AutoDevResult.model_validate(p)

    def test_pre_flight_blocked_through_parse_stdout(self) -> None:
        """End-to-end: a captured sentinel block round-trips via parse_stdout.

        The wrapper path (`_parse_sentinel_from_transcript` in signal_stop)
        uses parse_stdout, so the round-trip must succeed without falling
        into the BlockedResult fail-open path.
        """
        p = _pre_flight_blocked_payload()
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        assert result.stage_reached == "stage1_pre_flight"

    def test_blocked_at_later_stage_still_requires_empty_next_actions(self) -> None:
        """Regression: non-user-directed next_actions still rejected at stage2_impl.

        A `blocked` exit at stage2_impl with `sync_local_main` (a pre-flight
        recovery verb, not a user-directed prefix) must still fail — widening
        for pre-flight and for user-directed actions must NOT carry over here.
        """
        p = _blocked_payload()  # blocked at stage2_impl
        p["next_actions"] = ["sync_local_main"]
        with pytest.raises(ValidationError, match="next_actions"):
            AutoDevResult.model_validate(p)


# ---------------------------------------------------------------------------
# blocked + user-directed next_actions (issue #328 — schema must accept
# `status=blocked` payloads whose next_actions all start with a user_*
# prefix, so _is_paused_for_user_input can be tested against real instances).
# ---------------------------------------------------------------------------


def _user_directed_blocked_payload(next_actions: list[str]) -> dict[str, Any]:
    """blocked at stage2_impl with user-directed next_actions."""
    return {
        "schema_version": 4,
        "ticket_id": "GEN-user-blocked",
        "status": "blocked",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": 5,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "branch": "auto-dev/GEN-user-blocked",
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
        "blocker": {
            "stage": "stage2_impl",
            "reason": "awaiting_user_input",
            "details": "blocked pending user action",
        },
        "next_actions": next_actions,
    }


class TestBlockedWithUserDirectedNextActions:
    """Schema must accept blocked payloads whose next_actions are all user_*-prefixed.

    These payloads signal that the session is paused for human input rather
    than being a terminal-reject — _is_paused_for_user_input reads them at
    runtime. See issue #328.
    """

    def test_user_resolve_prefix_allowed(self) -> None:
        p = _user_directed_blocked_payload(["user_resolve_ambiguities"])
        result = AutoDevResult.model_validate(p)
        assert result.next_actions == ["user_resolve_ambiguities"]

    def test_user_decide_prefix_allowed(self) -> None:
        p = _user_directed_blocked_payload(["user_decide_approach"])
        result = AutoDevResult.model_validate(p)
        assert result.next_actions == ["user_decide_approach"]

    def test_user_verify_prefix_allowed(self) -> None:
        p = _user_directed_blocked_payload(["user_verify_something"])
        result = AutoDevResult.model_validate(p)
        assert result.next_actions == ["user_verify_something"]

    def test_mixed_user_directed_prefixes_allowed(self) -> None:
        """Multiple user_* entries in next_actions are all valid."""
        p = _user_directed_blocked_payload(
            ["user_resolve_ambiguities", "user_verify_premises"]
        )
        result = AutoDevResult.model_validate(p)
        assert len(result.next_actions) == 2

    def test_non_user_prefix_still_rejected(self) -> None:
        """A non-user-directed action mixed with user_* must still fail."""
        p = _user_directed_blocked_payload(
            ["user_resolve_ambiguities", "sync_local_main"]
        )
        with pytest.raises(ValidationError, match="next_actions"):
            AutoDevResult.model_validate(p)

    def test_empty_next_actions_still_valid_for_blocked(self) -> None:
        """Generic terminal-reject blocked shape (empty next_actions) unchanged."""
        p = _user_directed_blocked_payload([])
        result = AutoDevResult.model_validate(p)
        assert result.next_actions == []


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


class TestV4StatusPromotion:
    def test_ambiguities_pending_parses_at_v4(self) -> None:
        result = parse_stdout(_wrap_sentinel(_ambiguities_pending_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "ambiguities_pending_resolution"
        assert result.schema_version == 4

    def test_premises_pending_parses_at_v4(self) -> None:
        result = parse_stdout(_wrap_sentinel(_premises_pending_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "premises_pending_verification"
        assert result.schema_version == 4

    def test_ambiguities_pending_accepted_at_v2(self) -> None:
        """ambiguities_pending_resolution parses at schema_version=2.

        Rollout exception: producer emits v2 today (issue #316).
        """
        payload = {**_ambiguities_pending_payload(), "schema_version": 2}
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        assert result.status == "ambiguities_pending_resolution"

    def test_premises_pending_accepted_at_v2(self) -> None:
        """premises_pending_verification parses at schema_version=2.

        Rollout exception: producer emits v2 today (issue #316).
        """
        payload = {**_premises_pending_payload(), "schema_version": 2}
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        assert result.status == "premises_pending_verification"

    def test_ambiguities_pending_accepted_at_v3(self) -> None:
        """ambiguities_pending_resolution parses at schema_version=3.

        Rollout exception: accept under v3 as well (issue #316).
        """
        payload = {**_ambiguities_pending_payload(), "schema_version": 3}
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        assert result.status == "ambiguities_pending_resolution"

    def test_premises_pending_accepted_at_v3(self) -> None:
        """premises_pending_verification parses at schema_version=3.

        Rollout exception: accept under v3 as well (issue #316).
        """
        payload = {**_premises_pending_payload(), "schema_version": 3}
        result = parse_stdout(_wrap_sentinel(payload))
        assert isinstance(result, AutoDevResult)
        assert result.status == "premises_pending_verification"

    def test_v4_schema_accepted(self) -> None:
        p = _ambiguities_pending_payload()
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.schema_version == 4

    def test_v5_schema_rejected(self) -> None:
        p = _ambiguities_pending_payload()
        p["schema_version"] = 5
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, BlockedResult)
        assert result.blocker.reason == "schema_version_unsupported"

    # A5 — empty arrays rejected via cross-field validator
    def test_empty_ambiguities_rejected(self) -> None:
        p = _ambiguities_pending_payload()
        p["ambiguities"] = []
        with pytest.raises(ValidationError, match="ambiguities"):
            AutoDevResult.model_validate(p)

    def test_empty_premises_rejected(self) -> None:
        p = _premises_pending_payload()
        p["premises"] = []
        with pytest.raises(ValidationError, match="premises"):
            AutoDevResult.model_validate(p)

    # A2 — next_actions must be non-empty
    def test_ambiguities_empty_next_actions_rejected(self) -> None:
        p = _ambiguities_pending_payload()
        p["next_actions"] = []
        with pytest.raises(ValidationError, match="next_actions"):
            AutoDevResult.model_validate(p)

    def test_premises_empty_next_actions_rejected(self) -> None:
        p = _premises_pending_payload()
        p["next_actions"] = []
        with pytest.raises(ValidationError, match="next_actions"):
            AutoDevResult.model_validate(p)

    # A4 — pre-branch status: branch must be null
    def test_ambiguities_rejects_branch(self) -> None:
        p = _ambiguities_pending_payload()
        p["branch"] = "dev/sneak"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_premises_rejects_branch(self) -> None:
        p = _premises_pending_payload()
        p["branch"] = "dev/sneak"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    # A4 — lines_actual must be null (pre-impl exit)
    def test_ambiguities_rejects_lines_actual(self) -> None:
        p = _ambiguities_pending_payload()
        p["scope"]["lines_actual"] = 50
        with pytest.raises(ValidationError, match="lines_actual"):
            AutoDevResult.model_validate(p)

    def test_premises_rejects_lines_actual(self) -> None:
        p = _premises_pending_payload()
        p["scope"]["lines_actual"] = 30
        with pytest.raises(ValidationError, match="lines_actual"):
            AutoDevResult.model_validate(p)

    # A3 — entry fields are all optional (best-effort)
    def test_ambiguities_entry_with_minimal_keys_accepted(self) -> None:
        p = _ambiguities_pending_payload()
        p["ambiguities"] = [{"question": "only question provided"}]
        result = AutoDevResult.model_validate(p)
        assert len(result.ambiguities) == 1

    def test_premises_entry_with_minimal_keys_accepted(self) -> None:
        p = _premises_pending_payload()
        p["premises"] = [{"claim": "minimal premise"}]
        result = AutoDevResult.model_validate(p)
        assert len(result.premises) == 1

    def test_premises_entry_with_alternate_key_shapes_accepted(self) -> None:
        # Producer uses various key names; parser must tolerate any shape
        p = _premises_pending_payload()
        p["premises"] = [
            {
                "premise": "alternate key",
                "verify_by": "read the doc",
                "verified": False,
            }
        ]
        result = AutoDevResult.model_validate(p)
        assert len(result.premises) == 1

    def test_round_trip_preserves_ambiguities(self) -> None:
        p = _ambiguities_pending_payload()
        result = AutoDevResult.model_validate(p)
        dumped = result.model_dump(mode="json")
        assert dumped["status"] == "ambiguities_pending_resolution"
        assert len(dumped["ambiguities"]) == 1

    def test_round_trip_preserves_premises(self) -> None:
        p = _premises_pending_payload()
        result = AutoDevResult.model_validate(p)
        dumped = result.model_dump(mode="json")
        assert dumped["status"] == "premises_pending_verification"
        assert len(dumped["premises"]) == 1

    def test_existing_statuses_unaffected_by_v4_addition(self) -> None:
        # Regression guard: shipped (v1 payload) still parses under v4-aware parser
        p = _shipped_payload()
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"


class TestStageReachedAliases:
    """Short-form stage_reached aliases from the producer map to full-form values.

    Issue #292: producer occasionally emits resume-detection substates
    (``s5_ci_pending``, ``merged``, etc.) instead of canonical full-form
    values. The ``_normalize_stage_reached`` field_validator maps them so
    otherwise-valid sentinels don't fail with ``validation_failed``.
    """

    # Each tuple: (alias, expected_canonical, payload_factory)
    # The payload_factory must return a payload whose status/scope are
    # compatible with the expected_canonical stage.
    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            # pre-flight
            ("pre_flight", "stage1_pre_flight"),
            # Stage 1 substates
            ("s1_drafting", "stage1_plan"),
            ("s1_pending_ambiguity_resolution", "stage1_plan"),
            ("s1_pending_human_approval", "stage1_plan"),
            ("s1_plan_approved", "stage1_plan"),
            # Stage 2
            ("s2_implementing", "stage2_impl"),
            # Stage 3
            ("s3_review_pending", "stage3_review"),
            ("s3_fix_loop", "stage3_review"),
            # Stage 4 / 5 (PR created or later)
            ("s4_pr_open", "stage5_post_create"),
            ("s5_ci_pending", "stage5_post_create"),
            ("s5_ci_passed", "stage5_post_create"),
            ("s5_ci_failed", "stage5_post_create"),
            ("merged", "stage5_post_create"),
        ],
    )
    def test_short_form_aliases_normalize(self, alias: str, expected: str) -> None:
        payload_for: dict[str, Any] = {
            "stage1_pre_flight": _no_op_payload(),
            "stage1_plan": _plan_pending_payload(),
            "stage2_impl": _blocked_payload(),
            "stage3_review": _review_pending_payload(),
            "stage5_post_create": _shipped_payload(),
        }
        p = payload_for[expected]
        p["stage_reached"] = alias
        result = AutoDevResult.model_validate(p)
        assert result.stage_reached == expected

    @pytest.mark.parametrize(
        ("near_miss", "expected"),
        [
            ("stage4_pr_creation", "stage4b_pr_create"),  # the #630/#748 case
            ("stage4_creation", "stage4b_pr_create"),
            ("stage5_done", "stage5_post_create"),
            ("stage2_coding", "stage2_impl"),
            ("stage3_reviewing", "stage3_review"),
        ],
    )
    def test_near_miss_stage_coerces_by_prefix(
        self, near_miss: str, expected: str
    ) -> None:
        """#748: a near-miss stage_reached within a known stage number coerces
        to that stage's canonical value (informational field) instead of failing
        the whole sentinel and discarding completed work.
        """
        p = _shipped_payload()
        p["stage_reached"] = near_miss
        result = AutoDevResult.model_validate(p)
        assert result.stage_reached == expected

    def test_non_string_stage_passes_through_to_reject(self) -> None:
        # The normalizer leaves a non-str stage_reached untouched (no crash);
        # the Literal check then rejects it.
        p = _shipped_payload()
        p["stage_reached"] = 4
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    @pytest.mark.parametrize("bad", ["stagee5", "", "stage_1", "s6_unknown", "MERGED"])
    def test_misspelled_stage_still_rejects(self, bad: str) -> None:
        # Genuine garbage with no stage<1-5> prefix still rejects (#748 coerces
        # only near-misses within a known stage number, not arbitrary strings).
        p = _shipped_payload()
        p["stage_reached"] = bad
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_full_form_values_pass_through_unchanged(self) -> None:
        # Existing canonical values must not be mangled by the normalizer
        for full_form, payload_fn in [
            ("stage1_pre_flight", _no_op_payload),
            ("stage1_plan", _plan_pending_payload),
            ("stage2_impl", _blocked_payload),
            ("stage3_review", _review_pending_payload),
            ("stage4a_merge_gate", _merge_gate_payload),
            ("stage5_post_create", _shipped_payload),
        ]:
            p = payload_fn()
            result = AutoDevResult.model_validate(p)
            assert result.stage_reached == full_form

    def test_parse_stdout_shipped_with_s5_ci_pending(self) -> None:
        # Regression for issue #292: producer emitted s5_ci_pending in a shipped
        # sentinel; the task was left PENDING despite the PR being merged.
        p = _shipped_payload()
        p["stage_reached"] = "s5_ci_pending"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"
        assert result.stage_reached == "stage5_post_create"

    def test_parse_stdout_shipped_with_merged(self) -> None:
        p = _shipped_payload()
        p["stage_reached"] = "merged"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.stage_reached == "stage5_post_create"


# ---------------------------------------------------------------------------
# Phase C — Agent health aggregation (#174)
# ---------------------------------------------------------------------------


class TestPhaseC:
    def test_old_payload_without_agent_health_summary_parses(self) -> None:
        """Back-compat: payloads without agent_health_summary parse cleanly."""
        p = _shipped_payload()
        assert "agent_health_summary" not in p["health"]
        result = AutoDevResult.model_validate(p)
        assert result.health.agent_health_summary == []

    def test_agent_health_summary_round_trip(self) -> None:
        p = _shipped_payload()
        p["health"]["agent_health_summary"] = [
            {"agent_id": "plan-reviewer-xyz", "confidence": "HIGH", "scope": "small"},
            {"agent_id": "impl-agent-abc", "confidence": "MEDIUM", "scope": "large"},
        ]
        result = AutoDevResult.model_validate(p)
        assert len(result.health.agent_health_summary) == 2
        assert result.health.agent_health_summary[0].agent_id == "plan-reviewer-xyz"
        assert result.health.agent_health_summary[0].confidence == "HIGH"
        assert result.health.agent_health_summary[0].scope == "small"
        assert result.health.agent_health_summary[1].agent_id == "impl-agent-abc"
        assert result.health.agent_health_summary[1].confidence == "MEDIUM"
        assert result.health.agent_health_summary[1].scope == "large"

    def test_agent_health_entry_scope_optional(self) -> None:
        """scope is optional — agents without a scope concept omit it."""
        p = _shipped_payload()
        p["health"]["agent_health_summary"] = [
            {"agent_id": "plan-reviewer-xyz", "confidence": "HIGH"},
        ]
        result = AutoDevResult.model_validate(p)
        entry = result.health.agent_health_summary[0]
        assert entry.scope is None

    def test_agent_health_entry_scope_accepts_unknown_string(self) -> None:
        """scope is a free-form string; unknown values are tolerated."""
        p = _shipped_payload()
        p["health"]["agent_health_summary"] = [
            {"agent_id": "plan-reviewer-xyz", "confidence": "HIGH", "scope": "medium"},
        ]
        result = AutoDevResult.model_validate(p)
        assert result.health.agent_health_summary[0].scope == "medium"

    def test_phase_c_on_v3_shipped_block(self) -> None:
        """Full round-trip through parse_stdout with v3 agent_health_summary."""
        p = _shipped_payload()
        p["schema_version"] = 3
        p["health"]["agent_health_summary"] = [
            {"agent_id": "impl-agent-abc", "confidence": "LOW", "scope": "small"},
        ]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.health.agent_health_summary[0].confidence == "LOW"
        assert result.health.agent_health_summary[0].agent_id == "impl-agent-abc"


# ---------------------------------------------------------------------------
# Phase D — Pre-merge PR visibility (#174)
# ---------------------------------------------------------------------------


class TestPhaseD:
    def test_shipped_without_pr_created_still_valid(self) -> None:
        """Back-compat: pr_created absent on payloads from older producers."""
        p = _shipped_payload()
        assert "pr_created" not in p
        result = AutoDevResult.model_validate(p)
        assert result.pr_created is None

    def test_pr_created_round_trip(self) -> None:
        p = _shipped_payload()
        p["pr_created"] = {
            "number": 171,
            "url": "https://github.com/mattwwarren/claude-workspace/pull/171",
            "ci_status_at_creation": "pending",
            "auto_merge_enabled": True,
        }
        result = AutoDevResult.model_validate(p)
        assert result.pr_created is not None
        assert result.pr_created.number == 171
        assert "171" in result.pr_created.url
        assert result.pr_created.ci_status_at_creation == "pending"
        assert result.pr_created.auto_merge_enabled is True

    def test_pr_created_ci_status_at_creation_is_open_enum(self) -> None:
        """ci_status_at_creation accepts unknown strings — open-ish enum."""
        p = _shipped_payload()
        p["pr_created"] = {
            "number": 172,
            "url": "https://github.com/x/y/pull/172",
            "ci_status_at_creation": "action_required",
            "auto_merge_enabled": False,
        }
        result = AutoDevResult.model_validate(p)
        assert result.pr_created is not None
        assert result.pr_created.ci_status_at_creation == "action_required"

    def test_phase_d_on_v3_shipped_block_via_parse_stdout(self) -> None:
        p = _shipped_payload()
        p["schema_version"] = 3
        p["pr_created"] = {
            "number": 42,
            "url": "https://github.com/foo/bar/pull/42",
            "ci_status_at_creation": "passing",
            "auto_merge_enabled": True,
        }
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.pr_created is not None
        assert result.pr_created.number == 42
        assert result.pr_created.ci_status_at_creation == "passing"


# ---------------------------------------------------------------------------
# Combined Phase B + E + C + D — all four phases together (issue #174)
# ---------------------------------------------------------------------------


class TestAllPhasesBECDCombined:
    def test_all_phases_on_shipped_v3_block(self) -> None:
        """Fixture exercising all four #174 phases in a single payload."""
        p = _shipped_payload()
        p["schema_version"] = 3
        # Phase C: agent health summary
        p["health"]["agent_health_summary"] = [
            {"agent_id": "plan-reviewer-abc", "confidence": "HIGH", "scope": "small"},
            {"agent_id": "impl-agent-def", "confidence": "MEDIUM", "scope": "small"},
        ]
        # Phase D: pre-merge PR snapshot
        p["pr_created"] = {
            "number": 99,
            "url": "https://github.com/mattwwarren/claude-workspace/pull/99",
            "ci_status_at_creation": "pending",
            "auto_merge_enabled": True,
        }
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "shipped"
        # Phase C assertions
        assert len(result.health.agent_health_summary) == 2
        assert result.health.agent_health_summary[1].confidence == "MEDIUM"
        # Phase D assertions
        assert result.pr_created is not None
        assert result.pr_created.number == 99
        assert result.pr_created.ci_status_at_creation == "pending"

    def test_all_phases_on_blocked_v3_block(self) -> None:
        """Phase B+E on a blocked payload; Phase C on the health struct."""
        p = _blocked_payload()
        p["schema_version"] = 3
        # Phase B + E on the blocker
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
        # Phase C on the health struct
        p["health"]["agent_health_summary"] = [
            {"agent_id": "impl-agent-ghi", "confidence": "LOW"},
        ]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        # Phase B+E
        assert result.blocker is not None
        assert result.blocker.exception_type == "CITimeoutError"
        assert result.blocker.retry_eligible is True
        assert result.blocker.retry_delay_seconds == 120
        # Phase C
        assert result.health.agent_health_summary[0].confidence == "LOW"
        assert result.health.agent_health_summary[0].scope is None


class TestCostUsdField:
    def _make_shipped_payload(self, **extra: object) -> dict[str, object]:
        """Minimal valid shipped payload for building test AutoDevResults."""
        base: dict[str, object] = {
            "schema_version": 2,
            "ticket_id": "T-1",
            "status": "shipped",
            "stage_reached": "stage5_post_create",
            "scope": {
                "tier": "small",
                "files": 1,
                "lines_estimate": 5,
                "lines_actual": 5,
                "forbidden_touched": False,
            },
            "plan_source": "linear_existing",
            "branch": "dev/t-1",
            "worktree_path": "/tmp/wt",
            "fork_point_sha": "abc",
            "commits": ["c1"],
            "pr": {
                "number": 1,
                "url": "https://example.com",
                "auto_merge": True,
                "base": "main",
            },
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
            "next_actions": ["wait_for_ci"],
        }
        base.update(extra)
        return base

    def test_cost_usd_defaults_to_none(self) -> None:
        payload = self._make_shipped_payload()
        result = AutoDevResult.model_validate(payload)
        assert result.cost_usd is None

    def test_cost_usd_accepts_float(self) -> None:
        payload = self._make_shipped_payload(cost_usd=1.23)
        result = AutoDevResult.model_validate(payload)
        assert result.cost_usd == 1.23

    def test_cost_usd_must_be_non_negative(self) -> None:
        payload = self._make_shipped_payload(cost_usd=-0.01)
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(payload)

    def test_cost_usd_round_trip(self) -> None:
        payload = self._make_shipped_payload(cost_usd=2.5)
        result = AutoDevResult.model_validate(payload)
        dumped = result.model_dump(mode="json")
        restored = AutoDevResult.model_validate(dumped)
        assert restored.cost_usd == 2.5


# ---------------------------------------------------------------------------
# Issue #430 — coerce legitimate-but-sparse sentinels
#
# Case 1: ambiguities_pending_resolution with empty ambiguities (omitted key →
# []) and premises_pending_verification with empty premises both hard-fail.
# Accept empty with a warning; don't fail.
# ---------------------------------------------------------------------------


class TestCase1EmptyAmbiguitiesAndPremisesCoerce:
    """parse_stdout coerces sparse v4 sentinels with empty ambiguities/premises.

    A producer that emits ambiguities_pending_resolution without the
    `ambiguities` key (omitted → defaults to []) previously hit the
    §4.4 A5 invariant and became validation_failed -> retry x3 -> FAILED.
    Same for premises_pending_verification + missing premises.

    Strict AutoDevResult.model_validate still rejects the shape (negative
    tests below); leniency applies only at the parse boundary.
    """

    def _ambiguities_empty_payload(self) -> dict[str, Any]:
        p = _ambiguities_pending_payload()
        p["ambiguities"] = []  # omitted key defaults to []
        return p

    def _premises_empty_payload(self) -> dict[str, Any]:
        p = _premises_pending_payload()
        p["premises"] = []  # omitted key defaults to []
        return p

    @pytest.mark.parametrize(
        ("payload_fn", "expected_status"),
        [
            (
                lambda: {**_ambiguities_pending_payload(), "ambiguities": []},
                "ambiguities_pending_resolution",
            ),
            (
                lambda: {**_premises_pending_payload(), "premises": []},
                "premises_pending_verification",
            ),
        ],
    )
    def test_empty_array_coerced_to_parse_success(
        self, payload_fn: Any, expected_status: str
    ) -> None:
        """parse_stdout accepts sparse sentinels with empty ambiguities/premises."""
        result = parse_stdout(_wrap_sentinel(payload_fn()))
        assert isinstance(result, AutoDevResult)
        assert result.status == expected_status

    def test_empty_ambiguities_strict_still_rejects(self) -> None:
        """Strict model_validate still rejects empty ambiguities (negative test)."""
        p = _ambiguities_pending_payload()
        p["ambiguities"] = []
        with pytest.raises(ValidationError, match="ambiguities"):
            AutoDevResult.model_validate(p)

    def test_empty_premises_strict_still_rejects(self) -> None:
        """Strict model_validate still rejects empty premises (negative test)."""
        p = _premises_pending_payload()
        p["premises"] = []
        with pytest.raises(ValidationError, match="premises"):
            AutoDevResult.model_validate(p)

    def test_non_empty_ambiguities_unchanged(self) -> None:
        """Coerce must not touch non-empty ambiguities arrays."""
        result = parse_stdout(_wrap_sentinel(_ambiguities_pending_payload()))
        assert isinstance(result, AutoDevResult)
        assert len(result.ambiguities) == 1

    def test_non_empty_premises_unchanged(self) -> None:
        """Coerce must not touch non-empty premises arrays."""
        result = parse_stdout(_wrap_sentinel(_premises_pending_payload()))
        assert isinstance(result, AutoDevResult)
        assert len(result.premises) == 1

    def test_missing_ambiguities_key_same_as_empty(self) -> None:
        """Omitting the ambiguities key entirely (omit → []) also parses."""
        p = _ambiguities_pending_payload()
        del p["ambiguities"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "ambiguities_pending_resolution"

    def test_missing_premises_key_same_as_empty(self) -> None:
        """Omitting the premises key entirely (omit → []) also parses."""
        p = _premises_pending_payload()
        del p["premises"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "premises_pending_verification"


# ---------------------------------------------------------------------------
# Issue #430 — Case 2: downgrade_applied=True requires scope.tier='small' —
# relax to require only the status, not tier (a producer reporting original
# tier='large' is now accepted).
# ---------------------------------------------------------------------------


class TestCase2DowngradeAppliedLargeTierCoerce:
    """downgrade_applied=True with scope.tier='large' now parses.

    The original invariant rejected any downgrade=True + tier!='small'
    combination. A producer that reports the original tier='large' would
    fail as validation_failed. Relax the invariant: require only
    status='review_pending_approval'; accept any tier value.

    The genuinely-malformed case (downgrade=True on a non-review_pending
    status) must still fail.
    """

    def _downgrade_large_payload(self) -> dict[str, Any]:
        p = _review_pending_payload()
        p["health"]["downgrade_applied"] = True
        # tier remains 'large' — the producer reported the original tier
        assert p["scope"]["tier"] == "large"
        return p

    @pytest.mark.parametrize(
        "tier",
        ["large", "small"],
    )
    def test_downgrade_applied_accepted_with_any_tier(self, tier: str) -> None:
        """downgrade_applied=True parses regardless of scope.tier."""
        p = _review_pending_payload()
        p["scope"]["tier"] = tier
        if tier == "large":
            # large post-impl stage requires lines_actual set
            p["scope"]["lines_actual"] = 580
        p["health"]["downgrade_applied"] = True
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "review_pending_approval"
        assert result.health.downgrade_applied is True

    def test_downgrade_applied_wrong_status_still_rejects(self) -> None:
        """downgrade=True on shipped still fails (genuinely malformed)."""
        p = _shipped_payload()
        p["health"]["downgrade_applied"] = True
        with pytest.raises(ValidationError, match="downgrade_applied"):
            AutoDevResult.model_validate(p)

    def test_downgrade_applied_blocked_still_rejects(self) -> None:
        """downgrade=True on blocked still fails (genuinely malformed)."""
        p = _blocked_payload()
        p["health"]["downgrade_applied"] = True
        with pytest.raises(ValidationError, match="downgrade_applied"):
            AutoDevResult.model_validate(p)


# ---------------------------------------------------------------------------
# Issue #430 — Case 3: merge_gate_blocked requires scope.tier='small' —
# relax so a large ticket that hit a merge gate is accepted.
# ---------------------------------------------------------------------------


class TestCase3MergeGateBlockedLargeTierCoerce:
    """merge_gate_blocked now parses regardless of scope.tier.

    The original §4.1 invariant rejected merge_gate_blocked + tier!='small'.
    A large ticket hitting a merge gate was rejected as validation_failed.
    Relax: accept any tier value for merge_gate_blocked status.

    A genuinely-malformed sentinel (wrong status) still fails.
    """

    @pytest.mark.parametrize(
        "tier",
        ["large", "small"],
    )
    def test_merge_gate_blocked_accepted_with_any_tier(self, tier: str) -> None:
        """merge_gate_blocked parses regardless of scope.tier."""
        p = _merge_gate_payload()
        p["scope"]["tier"] = tier
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "merge_gate_blocked"

    def test_merge_gate_blocked_large_tier_strict_also_parses(self) -> None:
        """model_validate also accepts merge_gate_blocked + tier='large'."""
        p = _merge_gate_payload()
        p["scope"]["tier"] = "large"
        result = AutoDevResult.model_validate(p)
        assert result.status == "merge_gate_blocked"
        assert result.scope.tier == "large"

    def test_shipped_with_large_tier_still_valid(self) -> None:
        """Relaxing merge_gate_blocked tier check must not affect other statuses."""
        p = _shipped_payload()
        p["scope"]["tier"] = "large"
        # shipped+large is valid (no tier constraint on shipped)
        result = AutoDevResult.model_validate(p)
        assert result.status == "shipped"


# ---------------------------------------------------------------------------
# Issue #430 — Case 4: scope_exceeded / forbidden_area emitted at/after
# stage2_impl carry non-null branch and/or lines_actual. Extend the no_op
# stray-branch/lines coerce to scope_exceeded and forbidden_area.
# ---------------------------------------------------------------------------


class TestCase4ScopeExceededForbiddenAreaStrayFieldsCoerce:
    """parse_stdout coerces stray branch/lines_actual on scope_exceeded/forbidden_area.

    A producer that exits with scope_exceeded or forbidden_area at/after
    stage2_impl may emit a non-null branch or lines_actual. The §3.3 pre-branch
    and lines_actual invariants previously rejected these sentinels as
    validation_failed. Extend the no_op coerce to these statuses.

    Strict model_validate still rejects the shapes (negative tests below).
    """

    def _scope_exceeded_at_stage2_payload(self) -> dict[str, Any]:
        """scope_exceeded at stage2_impl with stray branch + lines_actual."""
        p = _scope_exceeded_payload()
        p["stage_reached"] = "stage2_impl"
        p["branch"] = "dev/gen-5-scope-exceeded"
        p["scope"]["lines_actual"] = 120
        return p

    def _forbidden_area_at_stage2_payload(self) -> dict[str, Any]:
        """forbidden_area at stage2_impl with stray branch + lines_actual."""
        p = _forbidden_area_payload()
        p["stage_reached"] = "stage2_impl"
        p["branch"] = "dev/gen-6-forbidden"
        p["scope"]["lines_actual"] = 55
        return p

    @pytest.mark.parametrize(
        ("payload_fn", "expected_status"),
        [
            (
                lambda: {
                    **_scope_exceeded_payload(),
                    "stage_reached": "stage2_impl",
                    "branch": "dev/gen-5-scope",
                    "scope": {
                        **_scope_exceeded_payload()["scope"],
                        "lines_actual": 120,
                    },
                },
                "scope_exceeded",
            ),
            (
                lambda: {
                    **_forbidden_area_payload(),
                    "stage_reached": "stage2_impl",
                    "branch": "dev/gen-6-forbidden",
                    "scope": {
                        **_forbidden_area_payload()["scope"],
                        "lines_actual": 55,
                    },
                },
                "forbidden_area",
            ),
        ],
    )
    def test_stray_branch_and_lines_actual_coerced(
        self, payload_fn: Any, expected_status: str
    ) -> None:
        """parse_stdout coerces stray branch/lines_actual on terminal statuses."""
        result = parse_stdout(_wrap_sentinel(payload_fn()))
        assert isinstance(result, AutoDevResult)
        assert result.status == expected_status
        assert result.branch is None

    def test_scope_exceeded_stray_branch_only_coerced(self) -> None:
        """Stray branch alone (lines_actual already null) is also coerced."""
        p = _scope_exceeded_payload()
        p["stage_reached"] = "stage1_plan"  # pre-impl exit — branch still stray
        p["branch"] = "dev/gen-5-stray"
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "scope_exceeded"
        assert result.branch is None

    def test_forbidden_area_post_impl_lines_actual_preserved(self) -> None:
        """At post-impl stages, lines_actual is valid and must NOT be coerced.

        A producer exiting forbidden_area at stage2_impl after doing some impl
        work emits a non-null lines_actual. This is correct per §3.3. The coerce
        must NOT null it out, and the payload must parse cleanly.
        """
        p = _forbidden_area_payload()
        p["stage_reached"] = "stage2_impl"
        p["scope"]["lines_actual"] = 88
        # branch is already None in _forbidden_area_payload
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "forbidden_area"
        # lines_actual is preserved (not coerced) at post-impl stage
        assert result.scope.lines_actual == 88

    def test_scope_exceeded_pre_impl_stray_lines_actual_coerced(self) -> None:
        """At pre-impl stage, stray lines_actual IS coerced to null."""
        p = _scope_exceeded_payload()
        # stage1_plan is pre-impl; stray lines_actual triggers coerce
        assert p["stage_reached"] == "stage1_plan"
        p["scope"]["lines_actual"] = 20
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "scope_exceeded"
        assert result.scope.lines_actual is None

    def test_scope_exceeded_strict_rejects_stray_branch(self) -> None:
        """Strict model_validate still rejects scope_exceeded + non-null branch."""
        p = _scope_exceeded_payload()
        p["branch"] = "dev/gen-5-stray"
        with pytest.raises(ValidationError):
            AutoDevResult.model_validate(p)

    def test_forbidden_area_strict_rejects_stray_lines_actual(self) -> None:
        """Strict model_validate still rejects forbidden_area + non-null lines_actual
        at a pre-impl stage."""
        p = _forbidden_area_payload()
        # stage1_plan is pre-impl — lines_actual must be null
        p["scope"]["lines_actual"] = 50
        with pytest.raises(ValidationError, match="lines_actual"):
            AutoDevResult.model_validate(p)

    def test_scope_exceeded_stray_commits_coerced(self) -> None:
        """Stray commits are also coerced to empty for scope_exceeded."""
        p = _scope_exceeded_payload()
        p["commits"] = ["sha1", "sha2"]
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "scope_exceeded"
        assert result.commits == []

    def test_clean_scope_exceeded_unchanged(self) -> None:
        """Clean scope_exceeded (already null branch + lines_actual) is untouched."""
        result = parse_stdout(_wrap_sentinel(_scope_exceeded_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "scope_exceeded"
        assert result.branch is None

    def test_clean_forbidden_area_unchanged(self) -> None:
        """Clean forbidden_area (already null branch + lines_actual) is untouched."""
        result = parse_stdout(_wrap_sentinel(_forbidden_area_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "forbidden_area"
        assert result.branch is None


# ---------------------------------------------------------------------------
# Issue #430 — Case 5: Blocker rejects retry_delay_seconds set without
# retry_eligible=True; an older producer omitting retry_eligible (→ None)
# fails the whole sentinel. Treat None as "not set" (legitimate) or coerce
# to True when retry_delay_seconds is present.
# ---------------------------------------------------------------------------


class TestCase5BlockerRetryEligibleNoneWithDelay:
    """Blocker now accepts retry_eligible=None with a non-null retry_delay_seconds.

    An older producer omitting retry_eligible (defaults to None) paired with
    a non-null retry_delay_seconds previously failed the _check_retry_invariants
    validator. Coerce: when retry_delay_seconds is set and retry_eligible is
    None, treat it as retry_eligible=True (the producer implied retryability
    by supplying a delay).

    Genuinely malformed cases (retry_eligible=False with a delay) still fail.
    """

    @pytest.mark.parametrize(
        ("retry_eligible", "retry_delay_seconds"),
        [
            (None, 60),  # older producer omits retry_eligible
            (None, 0),  # delay of 0 is also valid
            (True, 120),  # explicit True still works
        ],
    )
    def test_retry_delay_with_none_or_true_eligible_parses(
        self, retry_eligible: bool | None, retry_delay_seconds: int
    ) -> None:
        """retry_delay_seconds with retry_eligible=None or True now parses."""
        p = _blocked_payload()
        p["blocker"]["retry_eligible"] = retry_eligible
        p["blocker"]["retry_delay_seconds"] = retry_delay_seconds
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.retry_delay_seconds == retry_delay_seconds

    def test_retry_eligible_false_with_delay_still_rejects(self) -> None:
        """retry_eligible=False with a delay is genuinely malformed — still fails."""
        p = _blocked_payload()
        p["blocker"]["retry_eligible"] = False
        p["blocker"]["retry_delay_seconds"] = 30
        with pytest.raises(ValidationError, match="retry_delay_seconds"):
            AutoDevResult.model_validate(p)

    def test_retry_eligible_none_no_delay_still_valid(self) -> None:
        """retry_eligible=None without a delay is the legacy shape — still valid."""
        p = _blocked_payload()
        assert p["blocker"].get("retry_eligible") is None
        assert p["blocker"].get("retry_delay_seconds") is None
        result = AutoDevResult.model_validate(p)
        assert result.blocker is not None
        assert result.blocker.retry_eligible is None
        assert result.blocker.retry_delay_seconds is None

    def test_retry_eligible_none_with_delay_coerced_to_true_via_parse_stdout(
        self,
    ) -> None:
        """End-to-end: parse_stdout coerces retry_eligible=None+delay to eligible."""
        p = _blocked_payload()
        p["blocker"]["retry_eligible"] = None
        p["blocker"]["retry_delay_seconds"] = 45
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.blocker is not None
        assert result.blocker.retry_delay_seconds == 45
        # After coerce, retry_eligible is True
        assert result.blocker.retry_eligible is True

    def test_negative_delay_still_rejected_even_with_none_eligible(self) -> None:
        """Negative delay is always malformed regardless of retry_eligible."""
        p = _blocked_payload()
        p["blocker"]["retry_eligible"] = None
        p["blocker"]["retry_delay_seconds"] = -1
        with pytest.raises(ValidationError, match="retry_delay_seconds"):
            AutoDevResult.model_validate(p)


# ---------------------------------------------------------------------------
# TestB2StatusSets — RFC 0005 B2 frozensets and constants
# ---------------------------------------------------------------------------


class TestB2StatusSets:
    """RFC 0005 B2 stage-advance status sets and constants."""

    def test_stage_success_statuses(self) -> None:
        assert frozenset({"shipped", "stage_complete"}) == STAGE_SUCCESS_STATUSES

    def test_stage_complete_in_stage_success_statuses(self) -> None:
        """stage_complete (#699) in STAGE_SUCCESS_STATUSES — Rule 3 routes it."""
        assert "stage_complete" in STAGE_SUCCESS_STATUSES

    def test_stage_failure_statuses(self) -> None:
        assert (
            frozenset(
                {"blocked", "merge_gate_blocked", "scope_exceeded", "forbidden_area"}
            )
            == STAGE_FAILURE_STATUSES
        )

    def test_scope_gated_approval_statuses(self) -> None:
        assert (
            frozenset({"plan_pending_approval", "review_pending_approval"})
            == SCOPE_GATED_APPROVAL_STATUSES
        )

    def test_scope_tier_small(self) -> None:
        assert SCOPE_TIER_SMALL == "small"

    def test_paused_is_superset_of_scope_gated(self) -> None:
        """DRY composition: PAUSED_FOR_USER_INPUT_STATUSES includes scope-gated."""
        assert SCOPE_GATED_APPROVAL_STATUSES.issubset(PAUSED_FOR_USER_INPUT_STATUSES)

    def test_paused_composed_from_scope_gated(self) -> None:
        """SCOPE_GATED_APPROVAL_STATUSES compose into PAUSED (no duplication)."""
        # Both plan_pending_approval and review_pending_approval must be in PAUSED
        assert "plan_pending_approval" in PAUSED_FOR_USER_INPUT_STATUSES
        assert "review_pending_approval" in PAUSED_FOR_USER_INPUT_STATUSES


# ---------------------------------------------------------------------------
# RFC 0005 B2 — stage_complete (#699)
#
# IMPL pushes a branch but does NOT create a PR (FINALIZE does). The old
# sentinel used "shipped" which requires a non-null pr and wait_for_ci —
# both absent from IMPL's output — causing validation_failed → retry cap →
# FAILED. stage_complete is the PR-less intermediate stage-success status;
# existing validators already enforce pr=null + no wait_for_ci for any
# status != "shipped" and blocker=null for any status != "blocked".
# ---------------------------------------------------------------------------


def _stage_complete_payload() -> dict[str, Any]:
    """Minimal valid stage_complete payload (RFC 0005 B2 IMPL completion, #699)."""
    return {
        "schema_version": 4,
        "ticket_id": "GEN-stage-complete",
        "status": "stage_complete",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 60,
            "lines_actual": 55,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
        "branch": "dev/gen-stage-complete",
        "worktree_path": "/tmp/wt/gen-stage-complete",
        "fork_point_sha": "deadbeef",
        "commits": ["sha-a", "sha-b"],
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


class TestStageComplete:
    """RFC 0005 B2 stage_complete status (#699) — PR-less intermediate stage success.

    Existing validators already constrain stage_complete to (pr=null,
    blocker=null, no wait_for_ci) because those checks key on status != "shipped"
    and status != "blocked".  No new validator code is required.
    """

    def test_stage_complete_validates_ok(self) -> None:
        """stage_complete with pr=None, blocker=None, no wait_for_ci passes."""
        result = AutoDevResult.model_validate(_stage_complete_payload())
        assert result.status == "stage_complete"
        assert result.pr is None
        assert result.blocker is None
        assert "wait_for_ci" not in result.next_actions

    def test_stage_complete_parses_via_parse_stdout(self) -> None:
        """parse_stdout accepts stage_complete sentinel end-to-end."""
        result = parse_stdout(_wrap_sentinel(_stage_complete_payload()))
        assert isinstance(result, AutoDevResult)
        assert result.status == "stage_complete"

    def test_stage_complete_with_pr_set_raises(self) -> None:
        """stage_complete + non-null pr violates the §3.3 pr-iff-shipped invariant."""
        p = _stage_complete_payload()
        p["pr"] = {
            "number": 42,
            "url": "https://github.com/foo/bar/pull/42",
            "auto_merge": True,
            "base": "main",
        }
        with pytest.raises(ValidationError, match="pr must be null"):
            AutoDevResult.model_validate(p)

    def test_stage_complete_with_wait_for_ci_raises(self) -> None:
        """stage_complete + wait_for_ci violates §4.3 wait_for_ci-iff-shipped."""
        p = _stage_complete_payload()
        p["next_actions"] = ["wait_for_ci"]
        with pytest.raises(ValidationError, match="wait_for_ci"):
            AutoDevResult.model_validate(p)

    def test_stage_complete_in_stage_success_statuses(self) -> None:
        """stage_complete must be in STAGE_SUCCESS_STATUSES so Rule 3 triggers."""
        assert "stage_complete" in STAGE_SUCCESS_STATUSES

    def test_stage_complete_accepted_at_v2(self) -> None:
        """stage_complete accepted under all supported schema versions (#699)."""
        p = {**_stage_complete_payload(), "schema_version": 2}
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "stage_complete"

    def test_stage_complete_accepted_at_v3(self) -> None:
        """stage_complete is accepted under schema_version=3."""
        p = {**_stage_complete_payload(), "schema_version": 3}
        result = parse_stdout(_wrap_sentinel(p))
        assert isinstance(result, AutoDevResult)
        assert result.status == "stage_complete"


# ---------------------------------------------------------------------------
# is_documented_example — placeholder detection (GitHub #591)
# ---------------------------------------------------------------------------


def _documented_example_payload() -> dict[str, Any]:
    """Payload matching the illustrative example in the /auto-dev skill prompt."""
    return {
        "schema_version": 4,
        "ticket_id": "PROJ-1234",
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
        "branch": "dev/proj-1234-fix-login",
        "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
        "fork_point_sha": "abc1234",
        "commits": ["sha1", "sha2"],
        "pr": {
            "number": 42,
            "url": "https://github.com/.../pull/42",
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


def test_is_documented_example_returns_true_for_placeholder() -> None:
    """The documented example payload (pr=42, PROJ-1234, dev/proj-1234-...) → True."""
    result = AutoDevResult.model_validate(_documented_example_payload())
    assert is_documented_example(result)


def test_is_documented_example_returns_false_for_real_result_with_pr42() -> None:
    """Real result with pr=42 but different ticket_id/branch → False."""
    # _shipped_payload: pr.number=42 but ticket_id="GEN-1234", branch="dev/gen-1234-..."
    result = AutoDevResult.model_validate(_shipped_payload())
    assert not is_documented_example(result)


def test_is_documented_example_returns_false_for_non_example() -> None:
    """A normal shipped result with unrelated ticket/branch/pr → False."""
    p = {
        **_documented_example_payload(),
        "ticket_id": "MYPROJ-99",
        "branch": "dev/myproj-99-some-feature",
        "pr": {
            "number": 99,
            "url": "https://github.com/org/repo/pull/99",
            "auto_merge": False,
            "base": "main",
        },
    }
    result = AutoDevResult.model_validate(p)
    assert not is_documented_example(result)
