"""Tests for cw.codex_impl._result: strict parse, stamping, blocked results (#3061)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.parse import extract_block
from cw.auto_dev_result.schema._vocab import (
    EMPTY_DIFF_BLOCKER_REASON,
    KNOWN_BLOCKER_REASONS,
)
from cw.codex_impl._result import (
    IMPL_COMPLETE_TRAILER,
    IMPL_FAILED,
    IMPL_NOT_PUSHED,
    IMPL_STATUSES,
    PLAN_MISSING,
    ImplTurnDocument,
    ImplTurnFailure,
    impl_blocked,
    impl_commit_message,
    impl_empty_diff_result,
    parse_impl_turn,
    stamp_impl_result,
)
from cw.codex_review import _CODEX_IMPL_BLOCKED_NEXT_ACTIONS
from cw.contract_manifest._render import _IMPL_CODEX_ADDENDUM, _stage_addendum
from cw.local_runner import GitFacts
from cw.models import TicketTask
from tests._tool_fakes import impl_sentinel

if TYPE_CHECKING:
    from pathlib import Path


def _task(**updates: Any) -> TicketTask:
    return TicketTask(ticket_id="T-9", client="c", **updates)


def _facts(**overrides: Any) -> GitFacts:
    facts: dict[str, Any] = {
        "branch": "dev/T-9",
        "fork_point": "a" * 40,
        "commits": ["b" * 40],
        "files": 2,
        "lines_actual": 31,
    }
    facts.update(overrides)
    return GitFacts(**facts)


def _payload(**overrides: Any) -> dict[str, Any]:
    block = extract_block(impl_sentinel(**overrides))
    assert block is not None
    payload: dict[str, Any] = json.loads(block)
    return payload


def _stamp(tmp_path: Path, payload: dict[str, Any], **kw: Any) -> AutoDevResult:
    args: dict[str, Any] = {
        "task": _task(),
        "worktree": tmp_path,
        "facts": _facts(),
        "scope_tier": "small",
        "plan_source": "generated",
        "extra_friction": (),
    }
    args.update(kw)
    return stamp_impl_result(payload, **args)


def _doc(sentinel: str | None = None) -> str:
    return json.dumps({"sentinel": sentinel or impl_sentinel()})


def test_impl_turn_document_forbids_extra_and_requires_a_string_sentinel() -> None:
    assert ImplTurnDocument.model_validate_json(_doc()).sentinel
    with pytest.raises(ValidationError):
        ImplTurnDocument.model_validate_json('{"sentinel": "x", "extra": 1}')
    with pytest.raises(ValidationError):
        ImplTurnDocument.model_validate_json('{"sentinel": 3}')
    with pytest.raises(ValidationError):
        ImplTurnDocument.model_validate_json("{}")


class TestParse:
    def test_accepts_one_framed_block(self) -> None:
        parsed = parse_impl_turn(_doc())
        assert isinstance(parsed, dict)
        assert parsed["status"] == "stage_complete"

    @pytest.mark.parametrize("content", [None, "", "  \n", '{"sentinel": 1}', "nope"])
    def test_missing_or_malformed_document_is_no_result(
        self, content: str | None
    ) -> None:
        parsed = parse_impl_turn(content)
        assert isinstance(parsed, ImplTurnFailure)
        assert parsed.reason == "no_result_emitted"

    @pytest.mark.parametrize(
        "sentinel",
        [
            "no block here",
            impl_sentinel() + impl_sentinel(),
            "<<<AUTO_DEV_RESULT\n{}\n",
            "<<<AUTO_DEV_RESULT\nnot json\nAUTO_DEV_RESULT>>>",
            "<<<AUTO_DEV_RESULT\n[1]\nAUTO_DEV_RESULT>>>",
            impl_sentinel(status="shipped"),
            "<<<AUTO_DEV_RESULT\n{}\nAUTO_DEV_RESULT>>>",
        ],
    )
    def test_bad_sentinel_is_validation_failed(self, sentinel: str) -> None:
        parsed = parse_impl_turn(_doc(sentinel))
        assert isinstance(parsed, ImplTurnFailure)
        assert parsed.reason == "validation_failed"

    def test_blocked_status_is_accepted(self) -> None:
        sentinel = impl_sentinel(status="blocked")
        parsed = parse_impl_turn(_doc(sentinel))
        assert isinstance(parsed, dict)
        assert parsed["status"] == "blocked"


class TestStamp:
    def test_overwrites_every_cw_owned_field(self, tmp_path: Path) -> None:
        payload = _payload(
            scope={
                "tier": "large",
                "files": 99,
                "lines_estimate": 20,
                "lines_actual": 999,
                "forbidden_touched": True,
            },
            friction_highlights=["model note"],
            next_actions=["something"],
            review={"must_fix_initial": 5, "should_fix": 5, "fix_cycles_used": 5},
        )
        result = _stamp(tmp_path, payload, extra_friction=["cw note"])
        assert result.stage_reached == "stage2_impl"
        assert result.branch == "dev/T-9"
        assert result.fork_point_sha == "a" * 40
        assert result.commits == ["b" * 40]
        assert result.worktree_path == str(tmp_path)
        assert result.plan_source == "generated"
        assert result.pr is None
        assert result.pr_created is None
        assert result.review.must_fix_initial == 0
        assert result.scope.files == 2
        assert result.scope.lines_actual == 31
        assert result.scope.tier == "small"
        assert result.scope.forbidden_touched is True
        assert result.scope.lines_estimate == 20
        assert result.friction_highlights == ["model note", "cw note"]
        assert result.next_actions == ["something"]
        assert result.health.recommendation == "PROCEED"
        assert result.ticket_id == "T-9"

    def test_missing_fork_point_stamps_none(self, tmp_path: Path) -> None:
        result = _stamp(tmp_path, _payload(), facts=_facts(fork_point=""))
        assert result.fork_point_sha is None

    def test_plan_source_none_token_is_friction(self, tmp_path: Path) -> None:
        result = _stamp(
            tmp_path,
            _payload(),
            plan_source="none",
            extra_friction=["codex_plan_source_none: x"],
        )
        assert result.plan_source == "none"
        assert "codex_plan_source_none: x" in result.friction_highlights

    def test_model_blocked_gets_the_impl_label_and_stage(self, tmp_path: Path) -> None:
        payload = _payload(
            status="blocked",
            blocker={
                "stage": "stage3_review",
                "reason": "impl_failed",
                "details": "nope",
                "retry_eligible": False,
                "retry_delay_seconds": None,
            },
        )
        result = _stamp(tmp_path, payload)
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.stage == "stage2_impl"
        assert result.blocker.reason == "impl_failed"
        assert result.next_actions == _CODEX_IMPL_BLOCKED_NEXT_ACTIONS

    def test_invalid_payload_raises_validation_error(self, tmp_path: Path) -> None:
        payload = _payload()
        del payload["health"]
        with pytest.raises(ValidationError):
            _stamp(tmp_path, payload)

    def test_scope_missing_raises_validation_error(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["scope"] = "nope"
        with pytest.raises(ValidationError):
            _stamp(tmp_path, payload)

    def test_non_list_friction_is_left_for_validation(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["friction_highlights"] = "nope"
        with pytest.raises(ValidationError):
            _stamp(tmp_path, payload)

    def test_does_not_use_the_review_fix_cycle_commit_filter(
        self, tmp_path: Path
    ) -> None:
        facts = _facts(commits=["c" * 40, "d" * 40])
        result = _stamp(tmp_path, _payload(), facts=facts)
        assert result.commits == ["c" * 40, "d" * 40]


class TestBlocked:
    def test_impl_blocked_uses_stage2_impl_label_and_impl_next_actions(
        self, tmp_path: Path
    ) -> None:
        result = impl_blocked(
            ticket_id="T-9", worktree=tmp_path, reason=IMPL_FAILED, details="boom"
        )
        assert result.status == "blocked"
        assert result.stage_reached == "stage2_impl"
        assert result.blocker is not None
        assert result.blocker.stage == "stage2_impl"
        assert result.blocker.reason == IMPL_FAILED
        assert result.next_actions == ["user_resolve_codex_impl_failure"]
        assert result.scope.lines_actual == 0

    def test_impl_blocked_stamps_measurement_when_given(self, tmp_path: Path) -> None:
        result = impl_blocked(
            ticket_id="T-9",
            worktree=tmp_path,
            reason=IMPL_FAILED,
            details="boom",
            retry_eligible=True,
            measured=(2, 31),
        )
        assert result.scope.files == 2
        assert result.scope.lines_actual == 31
        assert result.blocker is not None
        assert result.blocker.retry_eligible is True

    def test_empty_diff_result_is_empty_diff_blocked_with_the_reason_constant(
        self, tmp_path: Path
    ) -> None:
        result = impl_empty_diff_result(
            ticket_id="T-9", worktree=tmp_path, details="nothing changed", measured=None
        )
        assert result.status == "empty_diff_blocked"
        assert result.stage_reached == "stage2_impl"
        assert result.blocker is not None
        assert result.blocker.reason == EMPTY_DIFF_BLOCKER_REASON
        assert result.blocker.stage == "stage2_impl"
        assert result.next_actions == []
        assert AutoDevResult.model_validate(result.model_dump(mode="json"))


class TestCommitMessage:
    @pytest.mark.parametrize("repair", [False, True])
    def test_subject_blank_line_trailer(self, repair: bool) -> None:
        message = impl_commit_message(_task(), repair=repair)
        subject, blank, trailer = message.rstrip("\n").split("\n")
        assert subject.split("(")[0].split(":")[0] in {"feat", "fix"}
        assert "T-9" in subject
        assert blank == ""
        assert trailer == "Auto-Dev-Stage: impl-complete"

    def test_repair_subject_differs_from_fresh(self) -> None:
        fresh = impl_commit_message(_task(), repair=False)
        repair = impl_commit_message(_task(), repair=True)
        assert fresh.splitlines()[0] != repair.splitlines()[0]


class TestConstants:
    def test_trailer_constant_is_shared(self) -> None:
        assert IMPL_COMPLETE_TRAILER == "Auto-Dev-Stage: impl-complete"
        rendered = _stage_addendum("impl")
        assert IMPL_COMPLETE_TRAILER in rendered
        assert "{trailer}" not in rendered
        assert _IMPL_CODEX_ADDENDUM.count("{trailer}") == 1

    @pytest.mark.parametrize("reason", [IMPL_FAILED, PLAN_MISSING, IMPL_NOT_PUSHED])
    def test_named_reasons_are_known(self, reason: str) -> None:
        assert reason in KNOWN_BLOCKER_REASONS

    def test_statuses(self) -> None:
        assert frozenset({"stage_complete", "blocked"}) == IMPL_STATUSES
