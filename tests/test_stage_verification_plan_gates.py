"""Tests for cw.stage_verification.plan_gates: gate set B and its named reasons (#3060
R6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._result import parse_plan_turn, stamp_plan_result
from cw.stage_verification.plan_gates import (
    APPROVAL_NEXT_ACTIONS,
    CODEX_PLAN_ARTIFACT_MISSING,
    CODEX_PLAN_GATE_FAILED,
    CommentApproval,
    check_artifact,
    check_large_tier_approval,
    check_plan_files,
    check_scope_tier,
    has_approval_evidence,
)
from tests._shared.session_seeding import _make_ticket_task
from tests._tool_fakes import plan_sentinel

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult

_FILES = "## Files Modified\n\n- src/cw/x.py\n"
_SMALL = "## Summary\n\n**Scope tier:** small\n\n" + _FILES
_LARGE = "## Summary\n\n**Scope tier:** large\n\n" + _FILES
_FP = "b" * 64
_OTHER_FP = "c" * 64
_PARKED_FP = "d" * 64
_CONSUMED = (
    f"plan_comment_approval_consumed: comment=c9 parked={_PARKED_FP} current={_FP}"
)


def _result(tmp_path: Path, **overrides: Any) -> AutoDevResult:
    block = extract_block(plan_sentinel(**overrides))
    assert block is not None
    return stamp_plan_result(json.loads(block), ticket_id="T-1", worktree=tmp_path)


class TestArtifact:
    def test_empty_plan_text_is_missing(self) -> None:
        failure = check_artifact("  \n")
        assert failure is not None
        assert failure.reason == CODEX_PLAN_ARTIFACT_MISSING

    def test_non_empty_plan_text_passes(self) -> None:
        assert check_artifact(_SMALL) is None


def test_invalid_sentinel_is_validation_failed(tmp_path: Path) -> None:
    content = json.dumps(
        {"plan_markdown": _SMALL, "sentinel": "garbage", "settlements": []}
    )
    parsed = parse_plan_turn(content, ticket_id="T-1", worktree=tmp_path)
    assert getattr(parsed, "reason", None) == "validation_failed"


class TestFilesModified:
    def test_absent_section_fails(self) -> None:
        failure = check_plan_files("## Summary\n\n**Scope tier:** small\n")
        assert failure is not None
        assert failure.reason == CODEX_PLAN_GATE_FAILED
        assert "files_modified" in failure.details

    def test_empty_section_fails(self) -> None:
        failure = check_plan_files("## Files Modified\n\nNothing yet.\n")
        assert failure is not None
        assert "files_modified" in failure.details

    def test_listed_paths_pass(self) -> None:
        assert check_plan_files(_SMALL) is None


class TestScopeTier:
    def test_zero_tier_lines_fail(self) -> None:
        failure = check_scope_tier(_FILES, "small")
        assert failure is not None
        assert failure.reason == CODEX_PLAN_GATE_FAILED
        assert "found 0" in failure.details

    def test_two_tier_lines_fail(self) -> None:
        failure = check_scope_tier(_SMALL + "\n**Scope tier:** large\n", "small")
        assert failure is not None
        assert "found 2" in failure.details

    def test_a_fenced_tier_line_is_not_counted(self) -> None:
        fenced = _SMALL + "\n```\n**Scope tier:** large\n```\n"
        assert check_scope_tier(fenced, "small") is None

    def test_sentinel_and_plan_mismatch_fails(self) -> None:
        failure = check_scope_tier(_SMALL, "large")
        assert failure is not None
        assert "scope_tier_match" in failure.details

    def test_matching_tier_passes(self) -> None:
        assert check_scope_tier(_LARGE, "large") is None


class TestLargeTierApproval:
    def test_small_is_unchanged(self, tmp_path: Path) -> None:
        result = _result(tmp_path)
        check = check_large_tier_approval(result, _make_ticket_task(), _FP)
        assert check.result is result
        assert check.mismatch is None

    def test_large_without_evidence_parks_for_approval(self, tmp_path: Path) -> None:
        result = _result(tmp_path, tier="large")
        check = check_large_tier_approval(result, _make_ticket_task(), _FP)
        assert check.result.status == "plan_pending_approval"
        assert check.result.next_actions == list(APPROVAL_NEXT_ACTIONS)
        assert check.mismatch is None

    def test_matching_evidence_keeps_stage_complete(self, tmp_path: Path) -> None:
        task = _make_ticket_task(
            plan_approved_at=datetime.now(UTC), plan_approved_fingerprint=_FP
        )
        assert has_approval_evidence(task)
        result = _result(tmp_path, tier="large")
        check = check_large_tier_approval(result, task, _FP)
        assert check.result.status == "stage_complete"

    def test_differing_fingerprint_reparks_quoting_both(self, tmp_path: Path) -> None:
        approved = "c" * 64
        task = _make_ticket_task(
            plan_approved_at=datetime.now(UTC), plan_approved_fingerprint=approved
        )
        result = _result(tmp_path, tier="large")
        check = check_large_tier_approval(result, task, _FP)
        assert check.result.status == "plan_pending_approval"
        assert check.mismatch == (approved, _FP)
        (token,) = check.result.friction_highlights
        assert approved in token
        assert _FP in token

    def _approved(self, fingerprint: str) -> Any:
        return _make_ticket_task(
            plan_approved_at=datetime.now(UTC), plan_approved_fingerprint=fingerprint
        )

    @staticmethod
    def _consumed() -> CommentApproval:
        """A comment approval for a plan other than the one being gated."""
        return CommentApproval("c9", parked=_PARKED_FP, current=_FP)

    def test_a_consumed_comment_with_a_matching_row_fingerprint_stays_complete(
        self, tmp_path: Path
    ) -> None:
        """The row approval still covers this plan; the dead comment is noted."""
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"),
            self._approved(_FP),
            _FP,
            comment=self._consumed(),
        )
        assert check.result.status == "stage_complete"
        assert check.mismatch is None
        assert check.result.friction_highlights == [_CONSUMED]

    def test_a_consumed_comment_with_the_alternate_stays_complete(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"),
            self._approved(_OTHER_FP),
            _FP,
            alternate=True,
            comment=self._consumed(),
        )
        assert check.result.status == "stage_complete"
        assert check.mismatch is None
        assert check.result.friction_highlights == [_CONSUMED]

    def test_a_consumed_comment_over_a_mismatched_row_reparks_with_both_notes(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"),
            self._approved(_OTHER_FP),
            _FP,
            comment=self._consumed(),
        )
        assert check.result.status == "plan_pending_approval"
        assert check.mismatch == (_OTHER_FP, _FP)
        consumed, mismatch = check.result.friction_highlights
        assert consumed == _CONSUMED
        assert mismatch.startswith("plan_approval_fingerprint_mismatch:")

    def test_the_alternate_alone_never_approves_without_row_evidence(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"), _make_ticket_task(), _FP, alternate=True
        )
        assert check.result.status == "plan_pending_approval"
        assert check.mismatch is None

    def test_a_consumed_comment_without_row_evidence_parks_with_the_note(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"),
            _make_ticket_task(),
            _FP,
            comment=self._consumed(),
        )
        assert check.result.status == "plan_pending_approval"
        assert check.mismatch is None
        assert check.result.friction_highlights == [_CONSUMED]

    def test_a_comment_for_this_plan_approves_without_row_evidence(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _result(tmp_path, tier="large"),
            _make_ticket_task(),
            _FP,
            comment=CommentApproval("c9", parked=_FP, current=_FP),
        )
        assert check.result.status == "stage_complete"
        assert check.result.friction_highlights == []

    def test_a_comment_never_changes_a_small_plan(self, tmp_path: Path) -> None:
        result = _result(tmp_path)
        check = check_large_tier_approval(
            result, _make_ticket_task(), _FP, comment=self._consumed()
        )
        assert check.result is result

    def test_fingerprint_without_timestamp_is_not_evidence(self) -> None:
        task = _make_ticket_task(plan_approved_fingerprint=_FP)
        assert not has_approval_evidence(task)

    def test_park_status_is_left_alone(self, tmp_path: Path) -> None:
        result = _result(tmp_path, tier="large", status="plan_pending_approval")
        check = check_large_tier_approval(result, _make_ticket_task(), _FP)
        assert check.result is result


def test_every_gate_failure_is_not_retry_eligible(tmp_path: Path) -> None:
    from cw.codex_plan._result import plan_blocked

    for failure in (
        check_artifact(""),
        check_plan_files(""),
        check_scope_tier("", "small"),
    ):
        assert failure is not None
        blocked = plan_blocked(
            ticket_id="T-1",
            worktree=tmp_path,
            reason=failure.reason,
            details=failure.details,
        )
        assert blocked.blocker is not None
        assert blocked.blocker.retry_eligible is False
