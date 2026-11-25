"""Approval evidence for the codex PLAN lane's resumed rounds (#3465).

Unit tests over :mod:`cw.codex_plan._approval`, the tracker's ordered
authority stream and the large-tier gate. Every comment is built with the
shared snapshot builders; the GitHub rule set is used throughout, with the
operator identity carried in ``operator_ids``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from cw.codex_plan._result import ResumeDraft, draft_body, stamp_plan_result
from cw.codex_plan._resume import (
    APPROVED_FINGERPRINT_MATCH,
    OPERATOR_APPROVAL_NO_DELTA,
    fast_path_reason,
)
from cw.codex_plan._tracker import TrackerObservation, observe_tracker
from cw.plan_fingerprint import (
    compute_body_sha,
    compute_plan_draft_fingerprint,
    format_last_evaluated,
)
from cw.stage_verification.plan_gates import CommentApproval, check_large_tier_approval
from cw.ticket_snapshot import SnapshotContext
from tests._shared.plan_approval_builders import (
    OPERATOR,
    PARK_AUTHOR,
    blocking_comment,
    park_comment,
    stream_snapshot,
    token_body,
    token_comment,
)
from tests._shared.ticket_context import make_comment
from tests._ticket_view_helpers import make_task
from tests._tool_fakes import plan_sentinel

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import TicketTask
    from cw.ticket_snapshot import SnapshotComment

STRANGER = "stranger"
_CTX = SnapshotContext(github=True, operator_ids=(OPERATOR,))
_NO_IDS = SnapshotContext(github=True, operator_ids=())
_APPROVED_AT = datetime(2026, 11, 10, tzinfo=UTC)
_OLD = "2026-11-01T12:00:00Z"
_NEW = "2026-11-18T12:00:00Z"
BODY = "## Files Modified\n- src/x.py\n\n**Scope tier:** large\n"


def _observe(
    *comments: SnapshotComment, ctx: SnapshotContext = _CTX, body: str = BODY
) -> TrackerObservation:
    return observe_tracker(stream_snapshot(comments, body=body), ctx)


def _approving(
    *comments: SnapshotComment,
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
        assert _approving(park_comment(), token_comment()) == "c9"

    def test_casefolded_operator_login_matches(self) -> None:
        ctx = SnapshotContext(github=True, operator_ids=("Operator-Login",))
        assert _approving(park_comment(), token_comment(), ctx=ctx) == "c9"

    def test_stranger_token_does_not_approve(self) -> None:
        assert _approving(park_comment(), token_comment(author=STRANGER)) is None

    def test_unknown_author_does_not_approve(self) -> None:
        comment = make_comment("c9", body=token_body(), author_id=None, created_at=_NEW)
        assert _approving(park_comment(), comment) is None

    def test_agent_marked_token_does_not_approve(self) -> None:
        comment = make_comment(
            "c9",
            body=token_body(),
            author_id=OPERATOR,
            created_at=_NEW,
            agent_authored=True,
        )
        assert _approving(park_comment(), comment) is None

    def test_author_conflict_token_does_not_approve(self) -> None:
        comment = make_comment(
            "c9",
            body=token_body(),
            author_id=OPERATOR,
            created_at=_NEW,
            author_conflict=True,
        )
        assert _approving(park_comment(), comment) is None

    def test_empty_operator_ids_on_github_never_approves(self) -> None:
        assert _approving(park_comment(), token_comment(), ctx=_NO_IDS) is None

    def test_an_unobserved_snapshot_never_approves(self) -> None:
        assert (
            approving_comment(
                observe_tracker(None, _CTX), settled_sources=(), resolutions_source=None
            )
            is None
        )


class TestOrdering:
    def test_token_before_the_park_does_not_approve(self) -> None:
        early_token = token_comment(created_at=_OLD)
        later_park = park_comment("p2", created_at=_NEW)
        assert _approving(early_token, later_park) is None
        stream = _observe(early_token, later_park).authority_stream
        assert [e.id for e in stream] == ["c9", "p2"]
        assert is_stale(stream, "c9") is True

    def test_nearest_header_must_be_a_park(self) -> None:
        blocking = blocking_comment()
        assert _approving(park_comment(), blocking, token_comment()) is None

    def test_a_token_alone_in_the_stream_has_no_park_to_follow(self) -> None:
        """Not stale, so ``follows_park`` is what refuses it."""
        stream = _observe(token_comment()).authority_stream
        assert [e.id for e in stream] == ["c9"]
        assert is_stale(stream, "c9") is False
        assert follows_park(stream, "c9") is False
        assert _approving(token_comment()) is None

    def test_a_non_park_header_ahead_of_the_token_is_not_a_park(self) -> None:
        blocking = blocking_comment()
        stream = _observe(blocking, token_comment()).authority_stream
        assert [e.id for e in stream] == ["b1", "c9"]
        assert is_stale(stream, "c9") is False
        assert follows_park(stream, "c9") is False
        assert _approving(blocking, token_comment()) is None

    def test_follows_park_of_an_unknown_comment_is_false(self) -> None:
        stream = _observe(park_comment(), token_comment()).authority_stream
        assert follows_park(stream, "missing") is False

    def test_later_operator_comment_makes_the_token_stale(self) -> None:
        later = make_comment("c10", body="also", author_id=OPERATOR, created_at=_NEW)
        assert _approving(park_comment(), token_comment(), later) is None

    def test_later_park_header_makes_the_token_stale(self) -> None:
        newer_park = park_comment("p2", created_at=_NEW)
        assert _approving(park_comment(), token_comment(), newer_park) is None

    def test_later_agent_comment_without_a_header_does_not_make_it_stale(self) -> None:
        analysis = make_comment(
            "a1",
            body="analysis",
            author_id=PARK_AUTHOR,
            created_at=_NEW,
            agent_authored=True,
        )
        assert _approving(park_comment(), token_comment(), analysis) == "c9"

    def test_stream_flags_park_and_token(self) -> None:
        stream = _observe(park_comment(), token_comment()).authority_stream
        assert [(e.id, e.park, e.approver) for e in stream] == [
            ("p1", True, False),
            ("c9", False, True),
        ]
        assert is_stale(stream, "c9") is False
        assert follows_park(stream, "c9") is True
        assert is_stale(stream, "missing") is True


class TestDoubleDuty:
    def test_a_token_that_also_settles_items_does_not_approve(self) -> None:
        assert _approving(park_comment(), token_comment(), sources=("c9",)) is None

    def test_a_token_that_is_the_resolutions_source_does_not_approve(self) -> None:
        assert (
            _approving(park_comment(), token_comment(), resolutions="comment:c9")
            is None
        )

    def test_an_unrelated_source_does_not_block(self) -> None:
        assert (
            _approving(
                park_comment(),
                token_comment(),
                sources=("row:1:abc",),
                resolutions="none",
            )
            == "c9"
        )
        assert is_double_duty("c9", ("c8",), "comment:c8") is False


class TestAlternate:
    def _last_evaluated(self, body: str = BODY) -> str:
        return format_last_evaluated("none", compute_body_sha(body))

    def _task(self, **extra: object) -> TicketTask:
        fields: dict[str, object] = {
            "plan_approved_at": _APPROVED_AT,
            "plan_approved_fingerprint": compute_plan_draft_fingerprint(BODY),
        }
        fields.update(extra)
        return make_task(**fields)

    def test_holds_with_no_newer_operator_comment(self) -> None:
        observation = _observe(park_comment())
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
        observation = _observe(park_comment(), body=BODY + "\nedited\n")
        assert (
            alternate_holds(self._task(), observation, self._last_evaluated()) is False
        )

    def test_a_missing_or_malformed_anchor_ends_it(self) -> None:
        observation = _observe(park_comment())
        assert alternate_holds(self._task(), observation, None) is False
        assert alternate_holds(self._task(), observation, "body_sha=zz") is False
        assert persisted_body_sha(None) is None

    def test_the_producers_fingerprint_round_trips_to_its_body_sha(self) -> None:
        # The producer (TrackerObservation.fingerprint) and the reader
        # (persisted_body_sha) share one grammar owner; a format change in
        # either must not leave the alternate silently never holding.
        observation = _observe(park_comment())
        assert observation.fingerprint is not None
        assert persisted_body_sha(observation.fingerprint) == compute_body_sha(BODY)

    def test_a_row_delta_never_takes_it(self) -> None:
        observation = _observe(park_comment())
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
            alternate_holds(no_row, _observe(park_comment()), self._last_evaluated())
            is False
        )

    @pytest.mark.parametrize(
        ("created", "holds"),
        [
            ("2026-11-09T23:59:59Z", True),
            ("2026-11-10T00:00:00Z", True),
            ("2026-11-10T00:00:01Z", False),
        ],
        ids=["older", "equal-is-not-newer", "after"],
    )
    def test_only_a_strictly_newer_operator_comment_ends_the_delta(
        self, created: str, holds: bool
    ) -> None:
        comment = make_comment("c10", body="hi", author_id=OPERATOR, created_at=created)
        assert no_authority_delta(_APPROVED_AT, _observe(comment)) is holds

    @pytest.mark.parametrize(
        ("created", "holds"),
        [("2026-11-09T12:00:00Z", True), ("2026-11-10T12:00:00Z", False)],
    )
    def test_a_naive_approval_time_is_compared_as_utc_with_a_real_comment(
        self, created: str, holds: bool
    ) -> None:
        naive = datetime(2026, 11, 10, 6, 0, tzinfo=UTC).replace(tzinfo=None)
        comment = make_comment("c10", body="hi", author_id=OPERATOR, created_at=created)
        assert no_authority_delta(naive, _observe(comment)) is holds

    def test_a_non_string_comment_time_fails_closed(self) -> None:
        observation = TrackerObservation(
            observed=True, comments=({"id": "c10", "createdAt": 1_700_000_000},)
        )
        assert no_authority_delta(_APPROVED_AT, observation) is False

    def test_a_missing_approval_time_fails_closed(self) -> None:
        assert no_authority_delta(None, _observe(park_comment())) is False

    def test_unparsable_comment_time_counts_as_newer(self) -> None:
        bad = make_comment(
            "c10", body="hi", author_id=OPERATOR, created_at="not-a-time"
        )
        assert no_authority_delta(_APPROVED_AT, _observe(bad)) is False

    def test_naive_approval_time_reads_as_utc(self) -> None:
        naive = _APPROVED_AT.replace(tzinfo=None)
        assert no_authority_delta(naive, _observe(park_comment())) is True

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
            _observe(park_comment(), token_comment()),
            None,
            row_delta=False,
            settlements=(),
            resolutions_source=None,
            resume=None,
        )
        assert facts is NO_APPROVAL

    def test_the_parked_fingerprint_is_the_resume_draft_fingerprint(self) -> None:
        resume = ResumeDraft(text="", leading="", body=BODY, fingerprint="FP-PARKED")
        facts = approval_facts(
            make_task(),
            _observe(park_comment(), token_comment()),
            None,
            row_delta=False,
            settlements=(),
            resolutions_source=None,
            resume=resume,
        )
        assert facts.parked_fingerprint == "FP-PARKED"
        assert facts.comment_id == "c9"

    def test_a_row_delta_keeps_the_comment_fact(self) -> None:
        resume = ResumeDraft(text="", leading="", body=BODY, fingerprint="FP")
        facts = approval_facts(
            make_task(),
            _observe(park_comment(), token_comment()),
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


def _large(tmp_path: Path) -> AutoDevResult:
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
