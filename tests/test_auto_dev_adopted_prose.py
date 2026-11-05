"""Prose-sync pins for the RFC 0016 adopted-PR paths (#2943).

The repo's ``read_text`` + literal-substring convention: an adopted row enters
review with no ``.cw/plan.md`` (R9), and finalize flips an adopted draft PR to
ready immediately before arming auto-merge (R11).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cw.auto_dev_result import AutoDevResult

_COMMANDS = Path(__file__).resolve().parents[1] / ".claude" / "commands"
_REVIEW = (_COMMANDS / "auto-dev-review.md").read_text(encoding="utf-8")
_FINALIZE = (_COMMANDS / "auto-dev-finalize.md").read_text(encoding="utf-8")


class TestReviewWithNoPlan:
    def test_orientation_names_the_no_plan_case(self) -> None:
        assert "**No plan (an adopted PR, RFC 0016, #2943).**" in _REVIEW
        assert "test -f .cw/plan.md" in _REVIEW
        assert "never synthesize an empty plan" in _REVIEW

    def test_planned_file_set_block_is_conditional(self) -> None:
        assert "only when `.cw/plan.md` exists (skip the whole block" in _REVIEW

    def test_plan_flag_is_conditional_for_both_consolidate_calls(self) -> None:
        guard = "if [[ -f .cw/plan.md ]]; then PLAN_ARGS=(--plan .cw/plan.md); fi"
        assert _REVIEW.count(guard) == 3
        assert '"${PLAN_ARGS[@]}"' in _REVIEW
        assert _REVIEW.count("cw review consolidate --documents-from") == 2

    def test_plan_citing_rationale_rejected_when_absent(self) -> None:
        assert "reject any adjudication rationale that cites the plan" in _REVIEW

    def test_scope_tier_falls_back_without_a_plan(self) -> None:
        assert "skip steps 1 and 1.5 and fall through to the `scope_hint`" in _REVIEW
        assert "then the diff stat" in _REVIEW

    def test_plan_source_none_is_recorded(self) -> None:
        assert 'record `plan_source: "none"`' in _REVIEW


class TestFinalizeAdoptedDraft:
    def test_ready_step_keys_adoption_on_queue_metadata_branch(self) -> None:
        assert "**Adopted draft PR (RFC 0016, #2943).**" in _FINALIZE
        assert "non-null `queue_metadata.branch`" in _FINALIZE

    def test_ready_runs_after_the_step_4_0_recheck_and_before_the_arm(self) -> None:
        ready = _FINALIZE.index('"$FINALIZE" ready <pr-number> --repo-path <worktree>')
        recheck = _FINALIZE.index("3. **Enable auto-merge:** **Headless:** immediately")
        arm = _FINALIZE.index(
            '"$FINALIZE" arm-automerge <pr-number> --repo-path <worktree>', ready
        )
        assert recheck < ready < arm
        assert "after the Step 4.0 re-check has passed" in _FINALIZE

    def test_ready_failure_is_agent_block_with_details(self) -> None:
        assert "On a non-zero `ready` exit, do NOT arm" in _FINALIZE
        assert 'blocker.reason: "agent_block"' in _FINALIZE
        assert "adopted draft PR #<N> could not be marked ready" in _FINALIZE

    def test_flip_is_recorded_in_friction_highlights(self) -> None:
        assert '`"draft_flipped_ready"` to `friction_highlights`' in _FINALIZE

    def test_self_heal_arm_applies_the_same_step(self) -> None:
        assert (
            "run the Step 4d item 3 **Adopted draft PR** `ready` step first"
            in _FINALIZE
        )


_READY_BLOCKED_PROGRAM = re.compile(
    r"READY_BLOCKED_JSON=\$\(printf '%s' \"\$SENTINEL_JSON\" \| jq -c \\\n"
    r"\s*--arg details \"\$READY_BLOCKER_DETAILS\" --argjson pr_number <[a-z_-]+> \\\n"
    r"\s*'(?P<program>.*?)'\) \\",
    re.DOTALL,
)

_BASE_SHIPPED: dict[str, object] = {
    "schema_version": 4,
    "ticket_id": "2943",
    "status": "shipped",
    "stage_reached": "stage5_post_create",
    "scope": {
        "tier": "small",
        "files": 3,
        "lines_estimate": 10,
        "lines_actual": 12,
        "forbidden_touched": False,
    },
    "plan_source": "generated",
    "branch": "dev/2943",
    "worktree_path": "/wt",
    "fork_point_sha": "a" * 40,
    "commits": ["b" * 40],
    "pr": {
        "number": 7,
        "url": "https://github.com/acme/widgets/pull/7",
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
    "ambiguities": [],
    "blocker": None,
    "prior_pr_warnings": [],
    "next_actions": ["wait_for_ci"],
}


class TestReadyFailureConstructsItsBlockedSentinel:
    """The draft-ready failure snippets build the sentinel they emit (#2943)."""

    def test_both_sites_construct_before_emitting(self) -> None:
        assert len(_READY_BLOCKED_PROGRAM.findall(_FINALIZE)) == 2
        assert (
            _FINALIZE.count("printf '%s' \"$READY_BLOCKED_JSON\" | cw result emit -")
            == 2
        )
        # The unmodified base sentinel is never the emitted payload there.
        ready_blocks = _FINALIZE.split('"$FINALIZE" ready <pr')[1:]
        for block in ready_blocks:
            failure = block.split("draft_flipped_ready")[0]
            assert '$SENTINEL_JSON" | cw result emit -' not in failure

    @pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
    @pytest.mark.parametrize("site", [0, 1])
    def test_constructed_sentinel_is_a_valid_blocked_result(self, site: int) -> None:
        program = _READY_BLOCKED_PROGRAM.findall(_FINALIZE)[site]
        details = 'adopted draft PR #7 could not be marked ready: "boom"'
        out = subprocess.run(
            [
                "jq",
                "-c",
                "--arg",
                "details",
                details,
                "--argjson",
                "pr_number",
                "7",
                program,
            ],
            input=json.dumps(_BASE_SHIPPED),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        result = AutoDevResult.model_validate_json(out)
        assert result.status == "blocked"
        assert result.pr is None
        # ``pr_info`` is the unmodeled raw field cw reads the PR url from.
        pr_info = json.loads(out)["pr_info"]
        assert pr_info["number"] == 7
        assert pr_info["url"] == "https://github.com/acme/widgets/pull/7"
        assert pr_info["auto_merge"] is False
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.details == details
        assert result.next_actions == []
