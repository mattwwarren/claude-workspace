"""Tests for cw.codex_review._verdict — verdict synthesis and review-comment
rendering (#1236, #1239)."""

from __future__ import annotations

import logging
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
from tests._reconcile_helpers import (
    SCOPE_GUARD_BRANCH,
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _make_stale_base_repo,
)
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
            default_branch="main",
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
            default_branch="main",
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
            default_branch="main",
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
            default_branch="main",
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
            default_branch="main",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert verdict is not None
        assert verdict.blocking is True

    def test_unanchored_must_fix_finding_blocks_through_codex_path(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # #1632: synthesize_codex_review_result always threads its own
        # already-available `worktree` param straight through to
        # consolidate_verdict — a MUST_FIX finding citing a file that is
        # real on disk but not part of the diff still blocks, via the new
        # "unanchored" routing rather than being silently dropped.
        worktree = make_git_repo("wt-synth-unanchored")
        (worktree / "docs.md").write_text("real file, not in the diff")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth-unanchored",
            default_branch="main",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert verdict is not None
        assert verdict.blocking is True
        assert verdict.rejected == []

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
            default_branch="main",
        )
        assert result.status == "stage_complete"
        assert result.stage_reached == "stage3_review"
        assert result.health.recommendation == "PROCEED"
        # Pinned against _derive_health (#1551): a single status="ok" document
        # (the default from _make_reviewer_doc) is the "nothing degraded"
        # input, so health must derive to the strongest signal, not just
        # happen to match a hardcoded literal.
        assert result.health.lowest_agent_confidence == "HIGH"
        assert result.health.any_incomplete_risk is False
        assert result.review.should_fix == 1
        # Fully-documented review (no failures) → unchanged, and agents_run
        # counts exactly the one role that produced a document.
        assert result.review.agents_run == 1
        assert verdict is not None
        assert verdict.blocking is False


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — health derivation from document status
# ---------------------------------------------------------------------------


class TestSynthesizeCodexReviewResultHealth:
    @pytest.mark.parametrize("status", ["degraded", "failed"])
    def test_non_ok_document_status_downgrades_health(
        self, make_git_repo: Callable[[str], Path], status: str
    ) -> None:
        # #1551: a reviewer document that is not status="ok" means that
        # role's coverage was reduced (degraded) or it self-reported failure
        # (failed) even though it still parsed into a document — neither case
        # produced a MUST_FIX finding or a ReviewerRunFailure, so the old
        # hardcoded HIGH/PROCEED silently reported full confidence over a
        # review that wasn't actually clean. status="failed" requires empty
        # findings (_check_failed_has_no_findings), so both branches share
        # the same no-findings, no-failures shape here.
        worktree = make_git_repo(f"wt-synth-health-{status}")
        doc = _make_reviewer_doc(status=status)
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "MEDIUM"
        assert result.health.any_incomplete_risk is True
        assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
        assert verdict is not None

    def test_multiple_ok_documents_stay_high_confidence(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # Document *count* alone is not a valid degraded-health signal: both
        # real call sites run roles via run_codex_roles, whose contract
        # guarantees exactly one of {document, ReviewerRunFailure} per
        # selected role. So at those call sites, failures == [] already means
        # every selected role produced a document — the document list's
        # length carries no additional information about coverage. What
        # actually degrades health is a document's own `status`, independent
        # of how many documents are present.
        worktree = make_git_repo("wt-synth-health-two-ok")
        doc_a = _make_reviewer_doc(reviewer_role="Role A")
        doc_b = _make_reviewer_doc(reviewer_role="Role B")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc_a, doc_b],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "HIGH"
        assert result.health.any_incomplete_risk is False
        assert result.health.recommendation == "PROCEED"
        assert verdict is not None

    @pytest.mark.parametrize("status", ["ok", "degraded"])
    def test_health_unaffected_by_malformed_or_absent_metrics(
        self, make_git_repo: Callable[[str], Path], status: str
    ) -> None:
        # R2 (#1710): audit metrics feed ReviewerRunRecord's new fields only.
        # A malformed stream (no terminal event, an unexpected tool attempt)
        # must produce exactly the same health/disposition as no metrics at all.
        worktree = make_git_repo(f"wt-synth-health-metrics-{status}")
        doc = _make_reviewer_doc(status=status)
        baseline, _v = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
        )
        with_metrics, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            metrics_by_role={
                doc.reviewer_role: {
                    "terminal_event": None,
                    "unexpected_tool_attempts": ["mcp_tool_call"],
                    "had_command_evidence": False,
                }
            },
        )
        assert with_metrics.status == baseline.status
        assert with_metrics.health.model_dump() == baseline.health.model_dump()
        assert verdict is not None
        assert verdict.agents_run[0].unexpected_tool_attempts == ["mcp_tool_call"]


class TestSynthesizeCodexReviewResultMetrics:
    """#1710: metrics_by_role threads onto verdict.agents_run."""

    def test_metrics_land_on_agents_run_records(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-metrics")
        doc = _make_reviewer_doc(reviewer_role="Role A")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            metrics_by_role={
                "Role A": {
                    "thread_id": "thr-a",
                    "duration_seconds": 4.0,
                    "input_tokens": 11,
                    "terminal_event": "turn.completed",
                    "tool_call_counts": {"command_execution": 3},
                    "had_command_evidence": True,
                }
            },
        )
        assert verdict is not None
        record = verdict.agents_run[0]
        assert record.reviewer_role == "Role A"
        assert record.thread_id == "thr-a"
        assert record.duration_seconds == pytest.approx(4.0)
        assert record.input_tokens == 11
        assert record.terminal_event == "turn.completed"
        assert record.tool_call_counts == {"command_execution": 3}
        assert record.had_command_evidence is True

    def test_default_none_leaves_records_at_defaults(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-metrics-none")
        doc = _make_reviewer_doc(reviewer_role="Role A")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
        )
        assert verdict is not None
        assert verdict.agents_run[0].thread_id is None
        assert verdict.agents_run[0].tool_call_counts == {}

    def test_zero_documents_path_is_metrics_free(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # Adopted assumption 5 (#1710): no ReviewVerdict is built when every
        # role failed, so metrics have nowhere to attach — and passing them
        # must not perturb the blocked result.
        worktree = make_git_repo("wt-synth-metrics-zero")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[],
            failures=[ReviewerRunFailure(role="R", reason="crash")],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            metrics_by_role={"R": {"thread_id": "thr-r"}},
        )
        assert verdict is None
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE


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


# ---------------------------------------------------------------------------
# #1487 — clean-review scope is measured, not hardcoded zero
# ---------------------------------------------------------------------------


class TestCleanReviewScopeMeasurement:
    def test_clean_review_reports_measured_scope(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """stage_complete now carries the branch's real files/lines, not 0/0."""
        worktree = _make_stale_base_repo(make_git_repo, "wt-verdict-scope")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-scope",
            default_branch="main",
        )

        assert result.status == "stage_complete"
        assert result.scope.files == SCOPE_GUARD_FILES
        assert result.scope.lines_actual == SCOPE_GUARD_LINES
        # lines_estimate stays 0 — out of scope for #1487.
        assert result.scope.lines_estimate == 0
        assert verdict is not None

    def test_clean_review_honours_default_branch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """The caller's default_branch selects the base ref for the measurement."""
        worktree = _make_stale_base_repo(
            make_git_repo, "wt-verdict-trunk", default_branch="trunk"
        )
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-scope-trunk",
            default_branch="trunk",
        )

        assert result.scope.files == SCOPE_GUARD_FILES
        assert result.scope.lines_actual == SCOPE_GUARD_LINES

    def test_unmeasurable_worktree_falls_back_to_zero(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """No origin/<default_branch> ref → today's 0/0 behavior is preserved."""
        worktree = make_git_repo("wt-verdict-noorigin")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-scope-none",
            default_branch="main",
        )

        assert result.scope.files == 0
        assert result.scope.lines_actual == 0

    def test_unmeasurable_worktree_logs_scope_verification_unavailable(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """#1487 fix loop: the 0/0 fallback now warns, distinguishing it from a
        genuine zero-diff clean review — previously silent, unlike the other
        two ingestion families' scope_verification_unavailable WARNING."""
        worktree = make_git_repo("wt-verdict-noorigin-warns")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        with caplog.at_level(logging.WARNING, logger="cw.codex_review._verdict"):
            result, _verdict = synthesize_codex_review_result(
                task=_task(),
                worktree=worktree,
                documents=[doc],
                failures=[],
                diff=_make_diff(),
                reviewed_sha="sha",
                session_id="s-scope-warns",
                default_branch="main",
            )

        assert result.scope.files == 0
        assert result.scope.lines_actual == 0
        assert "scope_verification_unavailable" in caplog.text

    def test_health_and_branch_are_untouched_by_the_scope_change(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Regression pin for #1551/#1580: health and branch stay as derived."""
        worktree = _make_stale_base_repo(make_git_repo, "wt-verdict-health")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-scope-health",
            default_branch="main",
        )

        assert result.branch == SCOPE_GUARD_BRANCH
        assert result.health.lowest_agent_confidence == "HIGH"
        assert result.health.any_incomplete_risk is False
        assert result.health.recommendation == "PROCEED"
