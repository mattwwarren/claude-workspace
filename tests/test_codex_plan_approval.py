"""Approval evidence for the codex PLAN lane's resumed rounds (#3465).

Unit tests over :mod:`cw.codex_plan._approval`, the tracker's ordered
authority stream and the large-tier gate. Every comment is built with the
shared snapshot builders; the GitHub rule set is used throughout, with the
operator identity carried in ``operator_ids``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw.auto_dev_result.parse import extract_block
from cw.codex_plan._approval import (
    APPROVAL_LINE,
    NO_APPROVAL,
    ApprovalFacts,
    alternate_holds,
    approval_facts,
    approving_comment,
    follows_park,
    has_approval_token,
    is_double_duty,
    is_stale,
    no_authority_delta,
    persisted_body_sha,
)
from cw.codex_plan._post import PARK_HEADER
from cw.codex_plan._result import draft_body, stamp_plan_result
from cw.codex_plan._resume import (
    APPROVED_FINGERPRINT_MATCH,
    OPERATOR_APPROVAL_NO_DELTA,
    fast_path_reason,
)
from cw.codex_plan._tracker import observe_tracker
from cw.gh import BLOCKING_FINDINGS_HEADER
from cw.plan_fingerprint import compute_body_sha, compute_plan_draft_fingerprint
from cw.stage_verification.plan_gates import CommentApproval, check_large_tier_approval
from cw.ticket_snapshot import SnapshotContext
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._ticket_view_helpers import make_task
from tests._tool_fakes import plan_sentinel

if TYPE_CHECKING:
    from pathlib import Path

OPERATOR = "operator-login"
STRANGER = "stranger"
_CTX = SnapshotContext(github=True, operator_ids=(OPERATOR,))
_NO_IDS = SnapshotContext(github=True, operator_ids=())
_APPROVED_AT = datetime(2026, 11, 10, tzinfo=UTC)
_OLD = "2026-11-01T12:00:00Z"
_NEW = "2026-11-18T12:00:00Z"
BODY = "## Files Modified\n- src/x.py\n\n**Scope tier:** large\n"
PARK_BODY = f"{PARK_HEADER}\n\n1. Which store?"


def _token_body() -> str:
    return f"Approved.\n\n{APPROVAL_LINE}\n"


def _park(comment_id: str = "p1") -> Any:
    return make_comment(
        comment_id,
        body=PARK_BODY,
        author_id="cw-bot",
        created_at=_OLD,
        agent_authored=True,
    )


def _token(
    comment_id: str = "c9", *, author: str = OPERATOR, created: str = _NEW
) -> Any:
    return make_comment(
        comment_id, body=_token_body(), author_id=author, created_at=created
    )


def _observe(*comments: Any, ctx: SnapshotContext = _CTX, body: str = BODY) -> Any:
    return observe_tracker(make_snapshot_from(comments, body=body), ctx)


def _approving(
    *comments: Any,
    ctx: SnapshotContext = _CTX,
    sources: tuple[str, ...] = (),
    resolutions: str | None = None,
) -> str | None:
    return approving_comment(
        _observe(*comments, ctx=ctx),
        settled_sources=sources,
        resolutions_source=resolutions,
    )


class TestTokenLine:
    @pytest.mark.parametrize(
        "body",
        [
            APPROVAL_LINE,
            f"  {APPROVAL_LINE}  \n",
            f"Text\r\n{APPROVAL_LINE}\r\n",
        ],
    )
    def test_the_exact_line_passes(self, body: str) -> None:
        assert has_approval_token(body)

    @pytest.mark.parametrize(
        "body",
        [
            "approved",
            "LGTM",
            "go ahead",
            "<!-- auto-dev-plan-approved -->",
            f"> {APPROVAL_LINE}",
            f"see {APPROVAL_LINE} here",
            f"```\n{APPROVAL_LINE}\n```",
            "<!-- auto-dev-comment-approval --",
        ],
    )
    def test_near_misses_and_quoted_or_fenced_tokens_fail(self, body: str) -> None:
        assert not has_approval_token(body)


class TestIdentity:
    def test_operator_token_after_a_park_approves(self) -> None:
        assert _approving(_park(), _token()) == "c9"

    def test_casefolded_operator_login_matches(self) -> None:
        ctx = SnapshotContext(github=True, operator_ids=("Operator-Login",))
        assert _approving(_park(), _token(), ctx=ctx) == "c9"

    def test_stranger_token_does_not_approve(self) -> None:
        assert _approving(_park(), _token(author=STRANGER)) is None

    def test_unknown_author_does_not_approve(self) -> None:
        comment = make_comment(
            "c9", body=_token_body(), author_id=None, created_at=_NEW
        )
        assert _approving(_park(), comment) is None

    def test_agent_marked_token_does_not_approve(self) -> None:
        comment = make_comment(
            "c9",
            body=_token_body(),
            author_id=OPERATOR,
            created_at=_NEW,
            agent_authored=True,
        )
        assert _approving(_park(), comment) is None

    def test_author_conflict_token_does_not_approve(self) -> None:
        comment = make_comment(
            "c9",
            body=_token_body(),
            author_id=OPERATOR,
            created_at=_NEW,
            author_conflict=True,
        )
        assert _approving(_park(), comment) is None

    def test_empty_operator_ids_on_github_never_approves(self) -> None:
        assert _approving(_park(), _token(), ctx=_NO_IDS) is None

    def test_an_unobserved_snapshot_never_approves(self) -> None:
        assert (
            approving_comment(
                observe_tracker(None, _CTX), settled_sources=(), resolutions_source=None
            )
            is None
        )


class TestOrdering:
    def test_token_before_the_park_does_not_approve(self) -> None:
        assert _approving(_token(created=_OLD), _park("p2")) is None

    def test_nearest_header_must_be_a_park(self) -> None:
        blocking = make_comment(
            "b1",
            body=f"{BLOCKING_FINDINGS_HEADER}\n\nfix it",
            author_id="cw-bot",
            created_at=_OLD,
            agent_authored=True,
        )
        assert _approving(_park(), blocking, _token()) is None

    def test_later_operator_comment_makes_the_token_stale(self) -> None:
        later = make_comment("c10", body="also", author_id=OPERATOR, created_at=_NEW)
        assert _approving(_park(), _token(), later) is None

    def test_later_park_header_makes_the_token_stale(self) -> None:
        newer_park = make_comment(
            "p2",
            body=PARK_BODY,
            author_id="cw-bot",
            created_at=_NEW,
            agent_authored=True,
        )
        assert _approving(_park(), _token(), newer_park) is None

    def test_later_agent_comment_without_a_header_does_not_make_it_stale(self) -> None:
        analysis = make_comment(
            "a1",
            body="analysis",
            author_id="cw-bot",
            created_at=_NEW,
            agent_authored=True,
        )
        assert _approving(_park(), _token(), analysis) == "c9"

    def test_stream_flags_park_and_token(self) -> None:
        stream = _observe(_park(), _token()).authority_stream
        assert [(e.id, e.park, e.approver) for e in stream] == [
            ("p1", True, False),
            ("c9", False, True),
        ]
        assert is_stale(stream, "c9") is False
        assert follows_park(stream, "c9") is True
        assert is_stale(stream, "missing") is True


class TestDoubleDuty:
    def test_a_token_that_also_settles_items_does_not_approve(self) -> None:
        assert _approving(_park(), _token(), sources=("c9",)) is None

    def test_a_token_that_is_the_resolutions_source_does_not_approve(self) -> None:
        assert _approving(_park(), _token(), resolutions="comment:c9") is None

    def test_an_unrelated_source_does_not_block(self) -> None:
        assert (
            _approving(_park(), _token(), sources=("row:1:abc",), resolutions="none")
            == "c9"
        )
        assert is_double_duty("c9", ("c8",), "comment:c8") is False


class TestAlternate:
    def _last_evaluated(self, body: str = BODY) -> str:
        return f"operator_comment=none|body_sha={compute_body_sha(body)}"

    def _task(self, **extra: object) -> Any:
        fields: dict[str, object] = {
            "plan_approved_at": _APPROVED_AT,
            "plan_approved_fingerprint": compute_plan_draft_fingerprint(BODY),
        }
        fields.update(extra)
        return make_task(**fields)

    def test_holds_with_no_newer_operator_comment(self) -> None:
        observation = _observe(_park())
        assert (
            alternate_holds(self._task(), observation, self._last_evaluated()) is True
        )

    def test_a_newer_operator_comment_ends_it(self) -> None:
        observation = _observe(
            make_comment("c10", body="hi", author_id=OPERATOR, created_at=_NEW)
        )
        assert (
            alternate_holds(self._task(), observation, self._last_evaluated()) is False
        )

    def test_a_post_marker_audit_comment_ends_it(self) -> None:
        marker = (
            f"<!-- auto-dev-plan-approved: {compute_plan_draft_fingerprint(BODY)} -->"
        )
        audit = make_comment("m1", body=marker, author_id=OPERATOR, created_at=_NEW)
        observation = _observe(audit)
        assert (
            alternate_holds(self._task(), observation, self._last_evaluated()) is False
        )
        assert fast_path_reason(self._task(), BODY) == "approved_fingerprint_match"

    def test_a_body_change_since_the_persisted_anchor_ends_it(self) -> None:
        observation = _observe(_park(), body=BODY + "\nedited\n")
        assert (
            alternate_holds(self._task(), observation, self._last_evaluated()) is False
        )

    def test_a_missing_or_malformed_anchor_ends_it(self) -> None:
        observation = _observe(_park())
        assert alternate_holds(self._task(), observation, None) is False
        assert alternate_holds(self._task(), observation, "body_sha=zz") is False
        assert persisted_body_sha(None) is None

    def test_a_row_delta_never_takes_it(self) -> None:
        observation = _observe(_park())
        assert (
            alternate_holds(
                self._task(), observation, self._last_evaluated(), row_delta=True
            )
            is False
        )

    def test_an_unobserved_snapshot_or_missing_row_approval_ends_it(self) -> None:
        assert (
            alternate_holds(
                self._task(), observe_tracker(None, _CTX), self._last_evaluated()
            )
            is False
        )
        no_row = make_task()
        assert (
            alternate_holds(no_row, _observe(_park()), self._last_evaluated()) is False
        )

    def test_unparsable_comment_time_counts_as_newer(self) -> None:
        bad = make_comment(
            "c10", body="hi", author_id=OPERATOR, created_at="not-a-time"
        )
        assert no_authority_delta(_APPROVED_AT, _observe(bad)) is False

    def test_naive_approval_time_reads_as_utc(self) -> None:
        naive = _APPROVED_AT.replace(tzinfo=None)
        assert no_authority_delta(naive, _observe(_park())) is True

    def test_fast_path_reason_names_the_alternate(self) -> None:
        mismatched = self._task(plan_approved_fingerprint="sha256:other")
        assert fast_path_reason(mismatched, BODY) == "approved_fingerprint_mismatch"
        assert (
            fast_path_reason(mismatched, BODY, alternate=True)
            == OPERATOR_APPROVAL_NO_DELTA
        )
        assert (
            fast_path_reason(self._task(), BODY, alternate=True)
            == APPROVED_FINGERPRINT_MATCH
        )


class TestFacts:
    def test_a_fresh_run_has_no_facts(self) -> None:
        facts = approval_facts(
            make_task(),
            _observe(_park(), _token()),
            None,
            row_delta=False,
            settlements=(),
            resolutions_source=None,
            resume=None,
        )
        assert facts is NO_APPROVAL

    def test_the_parked_fingerprint_is_the_resume_draft_fingerprint(
        self, tmp_path: Path
    ) -> None:
        from cw.codex_plan._result import ResumeDraft

        resume = ResumeDraft(text="", leading="", body=BODY, fingerprint="FP-PARKED")
        facts = approval_facts(
            make_task(),
            _observe(_park(), _token()),
            None,
            row_delta=False,
            settlements=(),
            resolutions_source=None,
            resume=resume,
        )
        assert facts.parked_fingerprint == "FP-PARKED"
        assert facts.comment_id == "c9"

    def test_a_row_delta_keeps_the_comment_fact(self) -> None:
        from cw.codex_plan._result import ResumeDraft

        resume = ResumeDraft(text="", leading="", body=BODY, fingerprint="FP")
        facts = approval_facts(
            make_task(),
            _observe(_park(), _token()),
            None,
            row_delta=True,
            settlements=(),
            resolutions_source=None,
            resume=resume,
        )
        assert facts.alternate is False
        assert facts.comment_id == "c9"

    def test_comment_approval_compares_the_draft_form(self) -> None:
        facts = ApprovalFacts(alternate=False, comment_id="c9", parked_fingerprint="FP")
        approval = facts.comment_approval(BODY.rstrip("\n"))
        assert approval == CommentApproval(
            comment_id="c9",
            parked="FP",
            current=compute_plan_draft_fingerprint(draft_body(BODY)),
        )
        assert ApprovalFacts().comment_approval(BODY) is None


def _large(tmp_path: Path) -> Any:
    block = extract_block(plan_sentinel(tier="large"))
    assert block is not None
    return stamp_plan_result(json.loads(block), ticket_id="T-1", worktree=tmp_path)


class TestGate:
    def test_matching_comment_approval_completes_without_row_evidence(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _large(tmp_path),
            make_task(),
            "FP",
            comment=CommentApproval("c9", parked="FP", current="FP"),
        )
        assert check.result.status == "stage_complete"
        assert check.mismatch is None

    def test_a_changed_plan_consumes_the_comment_and_parks(
        self, tmp_path: Path
    ) -> None:
        check = check_large_tier_approval(
            _large(tmp_path),
            make_task(),
            "FP2",
            comment=CommentApproval("c9", parked="FP", current="FP2"),
        )
        assert check.result.status == "plan_pending_approval"
        assert check.mismatch is None
        assert any(
            line.startswith(
                "plan_comment_approval_consumed: comment=c9 parked=FP current=FP2"
            )
            for line in check.result.friction_highlights
        )

    def test_no_keywords_keeps_the_row_only_park(self, tmp_path: Path) -> None:
        check = check_large_tier_approval(_large(tmp_path), make_task(), "FP")
        assert check.result.status == "plan_pending_approval"

    def test_alternate_keeps_stage_complete_on_a_mismatched_row(
        self, tmp_path: Path
    ) -> None:
        task = make_task(
            plan_approved_at=_APPROVED_AT, plan_approved_fingerprint="OTHER"
        )
        kept = check_large_tier_approval(_large(tmp_path), task, "FP", alternate=True)
        assert kept.result.status == "stage_complete"
        parked = check_large_tier_approval(_large(tmp_path), task, "FP")
        assert parked.result.status == "plan_pending_approval"
        assert parked.mismatch == ("OTHER", "FP")
