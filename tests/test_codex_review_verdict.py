"""Tests for cw.codex_review._verdict — verdict synthesis and review-comment
rendering (#1236, #1239)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _format_failures_detail,
    render_verdict_comment,
    synthesize_codex_review_result,
)
from cw.executor_diagnostics import diagnostics_bundle_dir
from cw.review_findings import ReviewerRunFailure, consolidate_verdict
from tests._codex_review_helpers import _task
from tests.conftest import _make_diff, _make_finding, _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — disposition table
# ---------------------------------------------------------------------------


class TestSynthesizeCodexReviewResult:
    def test_zero_documents_blocked_unparseable(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-zero")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[],
            failures=[ReviewerRunFailure(role="R", reason="crash")],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None

    @pytest.mark.parametrize(
        ("reason", "expect_retry"),
        [
            (CODEX_BUDGET_EXHAUSTED, True),
            (CODEX_TIMEOUT, True),
            (CODEX_ERROR, None),
        ],
    )
    def test_zero_documents_retry_eligible_by_reason(
        self,
        make_git_repo: Callable[[str], Path],
        reason: str,
        expect_retry: bool | None,
    ) -> None:
        # MUST_FIX 2 (#1236): retry_eligible tracks whether the failure(s) are
        # transient (timeout/budget_exhausted self-heal via reconcile); a hard
        # codex_error is not retried automatically. failures must also survive
        # into details rather than being dropped.
        worktree = make_git_repo(f"wt-synth-zero-{reason}")
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[],
            failures=[ReviewerRunFailure(role="Code Quality Reviewer", reason=reason)],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.blocker is not None
        assert result.blocker.retry_eligible == expect_retry
        assert "Code Quality Reviewer" in result.blocker.details
        assert reason in result.blocker.details

    def test_blocking_must_fix(self, make_git_repo: Callable[[str], Path]) -> None:
        worktree = make_git_repo("wt-synth-block")
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert result.review.must_fix_initial == 1
        assert verdict is not None
        assert verdict.blocking is True
        assert result.blocker.details != ""
        assert "Bug here" in result.blocker.details
        assert "src/cw/foo.py:10" in result.blocker.details

    @pytest.mark.parametrize(
        "reason", [CODEX_BUDGET_EXHAUSTED, CODEX_TIMEOUT, CODEX_ERROR]
    )
    def test_partial_review_blocked(
        self, make_git_repo: Callable[[str], Path], reason: str
    ) -> None:
        # Decision 7 (#1236): a non-blocking verdict (no MUST_FIX among the
        # roles that DID run) still blocks when at least one selected role
        # skipped or errored without a document, regardless of reason — an
        # incomplete review must not silently ship as stage_complete.
        worktree = make_git_repo(f"wt-synth-partial-{reason}")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[ReviewerRunFailure(role="Performance Reviewer", reason=reason)],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_PARTIAL
        assert "Performance Reviewer" in result.blocker.details
        assert reason in result.blocker.details
        # The review counts derived from the roles that DID run still survive
        # onto the blocked sentinel — same "don't drop the parsed data"
        # discipline as the zero-documents and must-fix paths.
        assert result.review.should_fix == 1
        assert verdict is not None
        assert verdict.blocking is False

    def test_must_fix_takes_priority_over_partial(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # When a role that DID run reports a real MUST_FIX finding, that is
        # the more actionable/specific block reason even if another role also
        # failed to run — CODEX_MUST_FIX_FINDINGS wins over CODEX_REVIEW_PARTIAL.
        worktree = make_git_repo("wt-synth-mf-and-partial")
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[
                ReviewerRunFailure(role="Performance Reviewer", reason=CODEX_TIMEOUT)
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert verdict is not None
        assert verdict.blocking is True

    def test_stage_complete_non_blocking(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-ok")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "stage_complete"
        assert result.stage_reached == "stage3_review"
        assert result.health.recommendation == "PROCEED"
        assert result.review.should_fix == 1
        # Fully-documented review (no failures) → unchanged, and agents_run
        # counts exactly the one role that produced a document.
        assert result.review.agents_run == 1
        assert verdict is not None
        assert verdict.blocking is False


# ---------------------------------------------------------------------------
# render_verdict_comment
# ---------------------------------------------------------------------------


class TestRenderVerdictComment:
    def test_blocking_lists_must_fix(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "BLOCKING" in body
        assert "MUST_FIX" in body
        assert "bad thing" in body
        assert "src/cw/foo.py:10" in body

    def test_non_blocking_header(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "Non-blocking" in body

    def test_mixed_must_fix_and_should_fix_both_render(self) -> None:
        # Test Reviewer SHOULD_FIX 10 (#1236): the old test suite never
        # rendered an actual SHOULD_FIX finding — assert both headings and
        # both findings' summaries appear, MUST_FIX first.
        diff = _make_diff(
            "def broken():",
            "second_line = 2",
            files={"src/cw/foo.py": [10, 11]},
        )
        must_fix = _make_finding(severity="MUST_FIX", summary="bad thing")
        should_fix = _make_finding(
            severity="SHOULD_FIX",
            summary="minor nit",
            line_start=11,
            line_end=11,
            evidence="second_line = 2",
        )
        doc = _make_reviewer_doc(must_fix, should_fix)
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "BLOCKING" in body
        assert "### MUST_FIX" in body
        assert "### SHOULD_FIX" in body
        assert "bad thing" in body
        assert "minor nit" in body
        assert body.index("### MUST_FIX") < body.index("### SHOULD_FIX")

    def test_low_confidence_finding_renders_confidence_label(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="LOW", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "LOW confidence" in body
        assert "bad thing" in body

    def test_high_confidence_finding_renders_no_confidence_label(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="HIGH", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "confidence" not in body.lower()

    def test_medium_confidence_finding_renders_confidence_label(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="MEDIUM", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "MEDIUM confidence" in body

    def test_confidence_does_not_affect_blocking_or_partition(self) -> None:
        # Regression (#1555): confidence is display-only. Two SEPARATE
        # verdicts, not two findings in one doc — _dedup_key excludes
        # confidence and would merge same-key findings.
        diff = _make_diff()
        doc_high = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="HIGH")
        )
        doc_low = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="LOW")
        )
        verdict_high = consolidate_verdict([doc_high], diff, reviewed_sha="sha")
        verdict_low = consolidate_verdict([doc_low], diff, reviewed_sha="sha")
        assert verdict_high.blocking is True
        assert verdict_low.blocking is True
        assert len(verdict_high.must_fix) == len(verdict_low.must_fix) == 1
        assert "### MUST_FIX" in render_verdict_comment(verdict_low)


def test_format_failures_detail_includes_diagnostics_path() -> None:
    failures = [ReviewerRunFailure(role="Code Quality Reviewer", reason=CODEX_TIMEOUT)]
    detail = _format_failures_detail(failures, session_id="sess-fmt")
    assert "Code Quality Reviewer (codex_timeout)" in detail
    # tmp_config_dir relocates state_dir() away from the real home, so
    # _render_bundle_path takes its absolute-fallback branch: the rendered
    # pointer is exactly "[diagnostics: <absolute bundle dir>]".
    bundle = diagnostics_bundle_dir("sess-fmt")
    assert detail == f"Code Quality Reviewer (codex_timeout) [diagnostics: {bundle}]"
