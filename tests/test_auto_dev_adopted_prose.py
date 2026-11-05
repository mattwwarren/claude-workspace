"""Prose-sync pins for the RFC 0016 adopted-PR paths (#2943).

The repo's ``read_text`` + literal-substring convention: an adopted row enters
review with no ``.cw/plan.md`` (R9), and finalize flips an adopted draft PR to
ready immediately before arming auto-merge (R11).
"""

from __future__ import annotations

from pathlib import Path

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
        assert "Pass `--plan .cw/plan.md` only when `.cw/plan.md` exists" in _REVIEW
        assert "drop the `--plan .cw/plan.md` argument from both invocations" in _REVIEW
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
