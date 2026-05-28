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

    def test_invalid_json_fence_skipped_valid_later_one_used(self) -> None:
        """An invalid-JSON code fence is skipped; the last valid one is used."""
        payload = _shipped_payload()
        body = json.dumps(payload)
        # First fence has invalid JSON (not parseable); second has the real payload.
        text = f"```json\nnot parseable at all\n```\n\n```json\n{body}\n```\n"
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

    @pytest.mark.parametrize("bad", ["stagee5", "", "stage_1", "s6_unknown", "MERGED"])
    def test_misspelled_stage_still_rejects(self, bad: str) -> None:
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
