"""Tests for cw.result — validate_payload helper."""

from __future__ import annotations

from typing import Any

from cw.result import validate_payload


def _valid_payload() -> dict[str, Any]:
    """Minimal valid shipped payload for testing."""
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


class TestValidatePayload:
    def test_valid_shipped_payload_returns_no_errors(self) -> None:
        errors = validate_payload(_valid_payload())
        assert errors == []

    def test_pr_non_null_with_blocked_status_returns_error(self) -> None:
        payload = _valid_payload()
        payload["status"] = "blocked"
        payload["pr"] = {"number": 1, "url": "...", "auto_merge": True, "base": "main"}
        payload["blocker"] = {"stage": "s2", "reason": "impl_failed", "details": "x"}
        payload["next_actions"] = []
        errors = validate_payload(payload)
        assert any("pr" in e for e in errors)

    def test_bad_stage_reached_returns_error(self) -> None:
        payload = _valid_payload()
        payload["stage_reached"] = "not_a_real_stage"
        errors = validate_payload(payload)
        assert len(errors) > 0

    def test_lines_actual_non_null_at_stage1_plan_returns_error(self) -> None:
        payload = _valid_payload()
        payload["status"] = "plan_pending_approval"
        payload["stage_reached"] = "stage1_plan"
        payload["scope"]["tier"] = "large"
        payload["scope"]["lines_actual"] = 99  # should be null at stage1_plan
        payload["branch"] = None
        payload["worktree_path"] = None
        payload["fork_point_sha"] = None
        payload["commits"] = []
        payload["pr"] = None
        payload["health"]["lowest_agent_confidence"] = "HIGH"
        payload["next_actions"] = []
        errors = validate_payload(payload)
        assert any("lines_actual" in e for e in errors)
