"""Tests for cw.codex_review._verdict — verdict synthesis and review-comment
rendering (#1236, #1239)."""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import Review
from cw.codex_review import (
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS,
    CODEX_TIMEOUT,
    _format_failures_detail,
    make_codex_blocked,
    render_verdict_comment,
    synthesize_codex_review_result,
)
from cw.codex_review._capability import (
    _CodexFilesystemCapability,
    _CodexFingerprint,
)
from cw.events import read_events
from cw.executor_diagnostics import diagnostics_bundle_dir
from cw.models.enums import OrchestratorEventType
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_findings import (
    AcceptedFinding,
    AgentSpecSource,
    AgentSpecStatus,
    RejectedFinding,
    ReviewerRunFailure,
    ReviewerRunRecord,
    ReviewVerdict,
    consolidate_verdict,
)
from cw.review_findings._consolidate import _count_rejected_by_severity
from tests._codex_review_helpers import _task
from tests._reconcile_helpers import (
    SCOPE_GUARD_BRANCH,
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _make_stale_base_repo,
    _scope_guard_git,
)
from tests.conftest import (
    _make_debt_record,
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
)
from tests.test_review_adjudication import _make_voided_finding

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_findings import Finding, ReviewerFindingsDocument


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
            fix_loop_enabled=False,
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None
        # #1835: must carry codex_review's own next_actions label, never the
        # LocalExecutor one make_blocked defaults to.
        assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
        assert "local_executor" not in result.next_actions[0]

    @pytest.mark.parametrize(
        ("reason", "expect_retry"),
        [
            (CODEX_BUDGET_EXHAUSTED, True),
            (CODEX_TIMEOUT, True),
            # #1836: a provider-capacity blip is transient too.
            (CODEX_MODEL_CAPACITY, True),
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
        # #1835: must carry codex_review's own next_actions label, never the
        # LocalExecutor one make_blocked defaults to.
        assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
        assert "local_executor" not in result.next_actions[0]

    @pytest.mark.parametrize(
        ("reason", "expect_retry"),
        [
            (CODEX_BUDGET_EXHAUSTED, True),
            (CODEX_TIMEOUT, True),
            # #1836 review finding: the partial-review branch must derive
            # retry_eligible the same way the zero-documents branch does — a
            # capacity blip hitting one role among several must not silently
            # fall back to non-retry-eligible just because other roles still
            # produced documents.
            (CODEX_MODEL_CAPACITY, True),
            (CODEX_ERROR, None),
        ],
    )
    def test_partial_review_blocked(
        self,
        make_git_repo: Callable[[str], Path],
        reason: str,
        expect_retry: bool | None,
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
            fix_loop_enabled=False,
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_PARTIAL
        assert result.blocker.retry_eligible == expect_retry
        assert "Performance Reviewer" in result.blocker.details
        assert reason in result.blocker.details
        # The review counts derived from the roles that DID run still survive
        # onto the blocked sentinel — same "don't drop the parsed data"
        # discipline as the zero-documents and must-fix paths.
        assert result.review.should_fix == 1
        assert verdict is not None
        assert verdict.blocking is False
        # #1835: must carry codex_review's own next_actions label, never the
        # LocalExecutor one make_blocked defaults to.
        assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
        assert "local_executor" not in result.next_actions[0]

    def test_partial_review_retry_eligible_true_when_any_failure_transient(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # #1836 review finding: test_partial_review_blocked above only ever
        # supplies a single failing role per case, so it can't distinguish
        # any(...) from "check failures[0] only". Pin the actual motivating
        # shape — one role capacity-blipped while another failed for an
        # unrelated, non-transient reason — and confirm retry_eligible is
        # still True.
        worktree = make_git_repo("wt-synth-partial-mixed-reasons")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[
                ReviewerRunFailure(role="Performance Reviewer", reason=CODEX_ERROR),
                ReviewerRunFailure(
                    role="Architecture Reviewer", reason=CODEX_MODEL_CAPACITY
                ),
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.blocker is not None
        assert result.blocker.retry_eligible is True

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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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

    @pytest.mark.parametrize(
        "kind", ["zero_documents", "must_fix", "mechanically_rejected", "partial"]
    )
    def test_blocked_next_actions_never_borrows_local_executor_label(
        self, make_git_repo: Callable[[str], Path], kind: str
    ) -> None:
        """Regression guard for #1835: none of the four blocked-producing
        branches of synthesize_codex_review_result may carry local_runner's
        LocalExecutor-specific next_actions label — a Codex CLI subprocess
        failure is not a LocalExecutor/aider failure."""
        worktree = make_git_repo(f"wt-synth-regression-{kind}")
        documents: list[ReviewerFindingsDocument] = []
        failures: list[ReviewerRunFailure] = []
        if kind == "zero_documents":
            failures = [ReviewerRunFailure(role="R", reason="crash")]
        elif kind == "must_fix":
            documents = [_make_reviewer_doc(_make_finding(severity="MUST_FIX"))]
        elif kind == "mechanically_rejected":
            documents = [_mechanically_rejected_must_fix_doc()]
        else:  # partial
            documents = [_make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))]
            failures = [
                ReviewerRunFailure(role="Performance Reviewer", reason=CODEX_TIMEOUT)
            ]
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=documents,
            failures=failures,
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth-regression",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status == "blocked"
        assert result.next_actions != ["user_resolve_local_executor_failure"]


# ---------------------------------------------------------------------------
# make_codex_blocked — shared Codex-subsystem blocked-result constructor
# ---------------------------------------------------------------------------


class TestMakeCodexBlocked:
    def test_make_codex_blocked_next_actions_is_baked_in(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-make-codex-blocked-next-actions")
        result = make_codex_blocked(
            ticket_id="T-1", worktree=worktree, reason="whatever"
        )
        assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
        assert result.next_actions != ["user_resolve_local_executor_failure"]

    def test_make_codex_blocked_defaults_stage3_review(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-make-codex-blocked-stage-default")
        result = make_codex_blocked(
            ticket_id="T-1", worktree=worktree, reason="whatever"
        )
        assert result.stage_reached == "stage3_review"
        assert result.blocker is not None
        assert result.blocker.stage == "stage3_review"

    def test_make_codex_blocked_stage_reached_override_still_works(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-make-codex-blocked-stage-override")
        result = make_codex_blocked(
            ticket_id="T-1",
            worktree=worktree,
            reason="whatever",
            stage_reached="stage2_impl",
        )
        assert result.stage_reached == "stage2_impl"
        assert result.blocker is not None
        assert result.blocker.stage == "stage2_impl"

    def test_make_codex_blocked_forwards_details_retry_fields(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-make-codex-blocked-retry-fields")
        result = make_codex_blocked(
            ticket_id="T-1",
            worktree=worktree,
            reason="whatever",
            details="some detail",
            retry_eligible=True,
            retry_delay_seconds=30,
        )
        assert result.blocker is not None
        assert result.blocker.details == "some detail"
        assert result.blocker.retry_eligible is True
        assert result.blocker.retry_delay_seconds == 30

    def test_make_codex_blocked_has_no_next_actions_parameter(self) -> None:
        params = inspect.signature(make_codex_blocked).parameters
        assert "next_actions" not in params


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — health derivation from document status
# ---------------------------------------------------------------------------


class TestSynthesizeCodexReviewResultHealth:
    @pytest.mark.parametrize(
        ("status", "reviewer_role"),
        [("degraded", "Architecture Reviewer"), ("failed", "Test Reviewer")],
    )
    def test_non_ok_document_status_downgrades_health(
        self,
        make_git_repo: Callable[[str], Path],
        status: str,
        reviewer_role: str,
    ) -> None:
        # #1551: a reviewer document that is not status="ok" means that
        # role's coverage was reduced (degraded) or it self-reported failure
        # (failed) even though it still parsed into a document — neither case
        # produced a MUST_FIX finding or a ReviewerRunFailure, so the old
        # hardcoded HIGH/PROCEED silently reported full confidence over a
        # review that wasn't actually clean. status="failed" requires empty
        # findings (_check_failed_has_no_findings), so both branches share
        # the same no-findings, no-failures shape here.
        #
        # #1856: "degraded" is pinned to a non-Test-Reviewer role because
        # ("Test Reviewer", "degraded") is now specifically carved out of
        # this downgrade (see TestTestReviewerDegradedCarveOut below) --
        # this test still proves the *general* claim that a degraded/failed
        # reviewer of some role downgrades health. "failed" stays on the
        # fixture-default Test Reviewer role to prove a self-reported
        # failure still downgrades health regardless of role.
        worktree = make_git_repo(f"wt-synth-health-{status}")
        doc = _make_reviewer_doc(status=status, reviewer_role=reviewer_role)
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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

    def test_degraded_document_detail_survives_full_synthesis_pipeline(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # #1775: proves the detail-copy fix reaches the real call path (not
        # just consolidate_verdict in isolation), and that existing health
        # behavior for a degraded document is unchanged by the addition.
        # #1856: reviewer_role pinned off "Test Reviewer" so this keeps
        # testing the general degraded-detail-copy path rather than
        # colliding with the new Test-Reviewer-degraded carve-out.
        worktree = make_git_repo("wt-synth-health-detail")
        doc = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="Architecture Reviewer",
        )
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "MEDIUM"
        assert result.health.any_incomplete_risk is True
        assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
        assert verdict is not None
        assert verdict.agents_run[0].detail == "sandbox lacked filesystem access"


class TestTestReviewerDegradedCarveOut:
    """#1856: Test Reviewer's read-only-sandbox ``status="degraded"`` is a
    structurally-forced signal (it was never capable of running pytest under
    the codex review sandbox, on any ticket, ever — see
    ``src/cw/codex_review/_roles.py``'s read-only posture), not a substantive
    coverage gap, so it is excluded from ``_derive_health``'s confidence
    computation. The carve-out is narrow: (role, status) == ("Test Reviewer",
    "degraded") only.
    """

    def test_test_reviewer_degraded_status_excluded_from_health(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-1856-test-reviewer-degraded")
        doc = _make_reviewer_doc(status="degraded")  # default role: Test Reviewer
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "HIGH"
        assert result.health.any_incomplete_risk is False
        assert result.health.recommendation == "PROCEED"
        assert verdict is not None

    def test_test_reviewer_failed_status_still_downgrades_health(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # The carve-out is status-scoped, not role-blanket: a Test Reviewer
        # document that self-reports "failed" (not "degraded") still
        # downgrades health.
        worktree = make_git_repo("wt-1856-test-reviewer-failed")
        doc = _make_reviewer_doc(status="failed")  # default role: Test Reviewer
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "MEDIUM"
        assert result.health.any_incomplete_risk is True
        assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
        assert verdict is not None

    def test_test_reviewer_degraded_with_another_degraded_role_still_downgrades_health(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # A substantive degradation on another role must still gate, even
        # when it coexists with the sandbox-caused Test Reviewer one.
        worktree = make_git_repo("wt-1856-mixed-degraded")
        documents = [
            _make_reviewer_doc(status="degraded"),  # Test Reviewer, carved out
            _make_reviewer_doc(
                status="degraded", reviewer_role="Architecture Reviewer"
            ),
        ]
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=documents,
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status == "stage_complete"
        assert result.health.lowest_agent_confidence == "MEDIUM"
        assert result.health.any_incomplete_risk is True
        assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
        assert verdict is not None


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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
            metrics_by_role={"R": {"thread_id": "thr-r"}},
        )
        assert verdict is None
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — mechanically-rejected MUST_FIX (#1714)
# ---------------------------------------------------------------------------


def _mechanically_rejected_must_fix_doc() -> ReviewerFindingsDocument:
    """A doc whose single MUST_FIX finding cites a file absent from the diff.

    ``_make_diff()`` only knows ``src/cw/foo.py``, so this finding is rejected
    ``unknown_file`` before adjudication — a *mechanical* rejection, the exact
    shape #1714 is about.
    """
    return _make_reviewer_doc(
        _make_finding(
            severity="MUST_FIX",
            file="src/cw/never_in_the_diff.py",
            line_start=None,
            line_end=None,
            summary="dropped before adjudication",
        )
    )


def _evidence_not_in_diff_must_fix_doc() -> ReviewerFindingsDocument:
    """A doc whose MUST_FIX finding resolves to a real diff line window but
    whose evidence text is absent from it (#1792).

    Sibling of :func:`_mechanically_rejected_must_fix_doc`: that fixture is
    rejected ``unknown_file`` (a reason ``RejectedFinding.detail`` is never
    populated for); this one is rejected ``evidence_not_in_diff``, the one
    reason #1792 populates ``detail`` for.
    """
    return _make_reviewer_doc(
        _make_finding(
            severity="MUST_FIX",
            file="src/cw/foo.py",
            line_start=10,
            line_end=10,
            evidence="not present anywhere",
            summary="evidence mismatch",
        )
    )


class TestSynthesizeCodexReviewResultMechanicalRejection:
    def test_mechanically_rejected_must_fix_blocks(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # #1714 fleet regression: this used to fall through to stage_complete.
        worktree = make_git_repo("wt-synth-mech-reject")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_mechanically_rejected_must_fix_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth-mech",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.status != "stage_complete"
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
        assert verdict is not None
        # R4: the signal is carried by rejected_must_fix, NOT by flipping
        # `blocking` — a mechanically-rejected finding's anchor is unreliable
        # and must never enter the autofix loop.
        assert verdict.blocking is False
        assert len(verdict.rejected_must_fix) == 1
        assert "dropped before adjudication" in result.blocker.details
        # #1835: must carry codex_review's own next_actions label, never the
        # LocalExecutor one make_blocked defaults to.
        assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
        assert "local_executor" not in result.next_actions[0]

    def test_partial_vs_mechanical_rejection_precedence(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # Pins the branch ordering: the mechanical-rejection branch sits
        # between the `blocking` branch and the `failures` (partial) branch, so
        # a run that would otherwise report CODEX_REVIEW_PARTIAL reports the
        # dropped MUST_FIX instead — the stronger, more specific signal.
        worktree = make_git_repo("wt-synth-mech-partial")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_mechanically_rejected_must_fix_doc()],
            failures=[ReviewerRunFailure(role="Performance Reviewer", reason="crash")],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth-mech-partial",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
        assert result.blocker.reason != CODEX_REVIEW_PARTIAL
        assert verdict is not None

    def test_accepted_must_fix_still_reports_the_original_reason(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # Regression guard: the new branch must not steal the blocking path.
        worktree = make_git_repo("wt-synth-mech-mixed")
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX"),
            _make_finding(
                severity="MUST_FIX",
                file="src/cw/never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped one",
            ),
        )
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth-mech-mixed",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert verdict is not None
        assert verdict.blocking is True
        assert len(verdict.rejected_must_fix) == 1


def _sub_must_fix_rejected_doc(**overrides: object) -> ReviewerFindingsDocument:
    """A doc whose single SHOULD_FIX finding cites a file absent from the diff.

    The #2000 shape: mechanically rejected like
    :func:`_mechanically_rejected_must_fix_doc`, but BELOW MUST_FIX, so #1714's
    force-block deliberately does not fire and the finding used to vanish
    without a counter, a log line, or a rendered section.
    """
    return _make_reviewer_doc(
        _make_finding(
            severity="SHOULD_FIX",
            file="src/cw/never_in_the_diff.py",
            line_start=None,
            line_end=None,
            summary="dropped below must_fix",
        ),
        **overrides,
    )


class TestSynthesizeCodexReviewResultSubMustFixRejection:
    """#2000 — a rejection below MUST_FIX is counted and surfaced, not gating."""

    def test_sub_must_fix_rejection_does_not_report_unqualified_proceed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _make_stale_base_repo(make_git_repo, "wt-sub-mf-reject")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_sub_must_fix_rejected_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-sub-mf-reject",
            default_branch="main",
            fix_loop_enabled=False,
        )

        # Non-gating by operator resolution (A1): the run still completes.
        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.rejected_must_fix == []
        assert verdict.rejected_count == 1
        assert verdict.rejected_count_by_severity == {"SHOULD_FIX": 1}
        # The terminal sentinel an unattended orchestrator reads carries the
        # same count -- not just the rendered comment a human reads.
        assert result.review.rejected_count == 1
        assert result.review.rejected_count_by_severity == {"SHOULD_FIX": 1}
        # ...and the headline is qualified, never the bare clean one.
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "PROCEED (1 finding(s) mechanically rejected)" in body
        assert "**Non-blocking** — no MUST_FIX findings" not in body
        # A1 (#2000, round-1 operator resolution): informational-only. The
        # counter deliberately does NOT flip Health.recommendation, so
        # _should_gate_for_review_health keeps meaning "no reviewer ran /
        # coverage degraded" rather than "one matcher miss occurred".
        assert result.health.recommendation == "PROCEED"

    def test_clean_review_with_zero_rejections_still_reports_proceed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # Regression guard against over-triggering: nothing was rejected, so
        # the ordinary clean headline must survive untouched.
        worktree = _make_stale_base_repo(make_git_repo, "wt-sub-mf-clean")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-sub-mf-clean",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "stage_complete"
        assert result.health.recommendation == "PROCEED"
        assert verdict is not None
        assert verdict.rejected_count == 0
        assert result.review.rejected_count == 0
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "**Non-blocking** — no MUST_FIX findings" in body
        assert "mechanically rejected" not in body

    def test_1714_force_block_still_fires_unchanged(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # R4: #2000 must not weaken #1714. A mixed pass (one MUST_FIX plus one
        # SHOULD_FIX rejection) still takes the MUST_FIX force-block path with
        # its original blocker reason.
        worktree = _make_stale_base_repo(make_git_repo, "wt-1714-unchanged")
        doc = _make_reviewer_doc(
            _make_finding(
                severity="MUST_FIX",
                file="src/cw/never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped before adjudication",
            ),
            _make_finding(
                severity="SHOULD_FIX",
                file="src/cw/also_never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped below must_fix",
            ),
        )
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-1714-unchanged",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
        assert verdict is not None
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_count == 2
        assert verdict.rejected_count_by_severity == {
            "MUST_FIX": 1,
            "SHOULD_FIX": 1,
        }


# ---------------------------------------------------------------------------
# render_verdict_comment
# ---------------------------------------------------------------------------


class TestSynthesizeCodexReviewResultVoidedSuppression:
    """#1814: an operator-voided finding cannot re-park the codex backend."""

    def _doc(self, *findings: Finding) -> ReviewerFindingsDocument:
        return _make_reviewer_doc(*findings)

    def test_voided_must_fix_no_longer_blocks(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Acceptance criterion (b), codex path: an identical re-derived
        finding is suppressed rather than re-parked."""
        worktree = make_git_repo("wt-synth-voided")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(_make_finding(severity="MUST_FIX"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-voided",
            default_branch="main",
            fix_loop_enabled=False,
            voided_findings=[_make_voided_finding()],
        )

        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert verdict.accepted[0].disposition == "rejected"

    def test_non_matching_must_fix_still_blocks_alongside_a_void(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-voided-mixed")
        diff = _make_diff(
            "def broken():", "return 1", files={"src/cw/foo.py": [10, 11]}
        )
        live = _make_finding(
            severity="MUST_FIX",
            line_start=11,
            line_end=11,
            summary="A genuinely new bug",
            evidence="return 1",
        )
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(_make_finding(severity="MUST_FIX"), live)],
            failures=[],
            diff=diff,
            reviewed_sha="sha",
            session_id="s-voided-mixed",
            default_branch="main",
            fix_loop_enabled=False,
            voided_findings=[_make_voided_finding()],
        )

        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert verdict is not None
        assert verdict.blocking is True
        assert [f.summary for f in verdict.must_fix] == ["A genuinely new bug"]
        assert [af.disposition for af in verdict.accepted] == ["rejected", "fixed"]

    def test_suppression_emits_event_correlated_to_the_ticket(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-voided-event")
        synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(_make_finding(severity="MUST_FIX"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-voided-event",
            default_branch="main",
            fix_loop_enabled=False,
            voided_findings=[_make_voided_finding()],
        )

        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _task().ticket_id
        assert events[0].payload["file"] == "src/cw/foo.py"
        assert events[0].payload["severity"] == "MUST_FIX"
        assert events[0].payload["operator_comment_id"]

    def test_no_voided_findings_leaves_the_blocking_path_intact(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-voided-none")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(_make_finding(severity="MUST_FIX"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-voided-none",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "blocked"
        assert verdict is not None
        assert verdict.blocking is True
        assert (
            read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED]) == []
        )


class TestSynthesizeCodexReviewResultFindingDispositionSuppression:
    """#1838: an operator-adjudicated finding cannot re-park a later round."""

    def _doc(self, *findings: Finding) -> ReviewerFindingsDocument:
        return _make_reviewer_doc(*findings)

    def _ledger(self, finding: Finding, **overrides: object) -> dict[str, object]:
        key = _disposition_key(finding.file, finding.summary)
        assert key is not None
        payload: dict[str, object] = {
            "outcome": "REJECTED",
            "rationale": "settled by the operator in an earlier round",
            "recorded_at": "2026-08-16T00:00:00Z",
        }
        payload.update(overrides)
        return {key: FindingDisposition.model_validate(payload)}

    def test_rejected_disposition_suppresses_the_rederived_must_fix(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-disposition")
        finding = _make_finding(severity="MUST_FIX")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(finding)],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-disposition",
            default_branch="main",
            fix_loop_enabled=False,
            finding_dispositions=self._ledger(finding),
        )

        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert verdict.accepted[0].disposition == "rejected"

    def test_suppression_carries_the_visibility_signal_onto_the_comment(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Operator-mandated (#1838 Decisions): a suppression must be visible.

        The signal rides ``AcceptedFinding.disposition_detail``, which
        ``_disposition_annotation``/``_render_findings`` already surface on the
        posted comment — asserted end to end here, not just on the field.
        """
        worktree = make_git_repo("wt-synth-disposition-visible")
        finding = _make_finding(severity="MUST_FIX")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(finding)],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-disposition-visible",
            default_branch="main",
            fix_loop_enabled=False,
            finding_dispositions=self._ledger(finding),
        )

        assert verdict is not None
        detail = verdict.accepted[0].disposition_detail
        assert "suppressed by prior REJECTED adjudication" in detail
        assert "2026-08-16T00:00:00Z" in detail
        assert "re-adjudicate if the code at this location has changed" in detail
        comment = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert detail in comment

    def test_composes_with_voided_suppression_without_interfering(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-disposition-compose")
        diff = _make_diff(
            "def broken():", "return 1", files={"src/cw/foo.py": [10, 11]}
        )
        voided = _make_finding(severity="MUST_FIX")
        adjudicated = _make_finding(
            severity="MUST_FIX",
            line_start=11,
            line_end=11,
            summary="A separately adjudicated bug",
            evidence="return 1",
        )
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(voided, adjudicated)],
            failures=[],
            diff=diff,
            reviewed_sha="sha",
            session_id="s-disposition-compose",
            default_branch="main",
            fix_loop_enabled=False,
            voided_findings=[_make_voided_finding()],
            finding_dispositions=self._ledger(adjudicated),
        )

        assert verdict is not None
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert [af.disposition for af in verdict.accepted] == ["rejected", "rejected"]

    def test_accepted_outcome_does_not_suppress(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-disposition-accepted")
        finding = _make_finding(severity="MUST_FIX")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(finding)],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-disposition-accepted",
            default_branch="main",
            fix_loop_enabled=False,
            finding_dispositions=self._ledger(finding, outcome="ACCEPTED"),
        )

        assert result.status == "blocked"
        assert verdict is not None
        assert verdict.blocking is True

    def test_no_ledger_leaves_the_blocking_path_intact(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-disposition-none")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[self._doc(_make_finding(severity="MUST_FIX"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-disposition-none",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "blocked"
        assert verdict is not None
        assert verdict.blocking is True
        assert (
            read_events(
                event_types=[
                    OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED
                ]
            )
            == []
        )


class TestRenderFindingsIsDispositionAware:
    """#1814/A1: a suppressed finding must not render as a live one.

    ``_render_findings`` filtered ``verdict.accepted`` on ``severity`` alone,
    discarding ``disposition`` before the loop body ran — so a voided
    (``"rejected"``) or #1805-dropped MUST_FIX rendered byte-identically to a
    still-blocking one. Annotation only: nothing is filtered or reordered.
    """

    def _verdict(
        self, *accepted: AcceptedFinding, **overrides: object
    ) -> ReviewVerdict:
        must_fix = [
            af.finding
            for af in accepted
            if af.finding.severity == "MUST_FIX" and af.disposition == "fixed"
        ]
        payload: dict[str, object] = {
            "blocking": bool(must_fix),
            "must_fix": must_fix,
            "reviewed_sha": "sha",
            "accepted": list(accepted),
            "review": Review(
                must_fix_initial=len(must_fix),
                should_fix=sum(
                    1 for af in accepted if af.finding.severity == "SHOULD_FIX"
                ),
                fix_cycles_used=0,
                deferred=0,
                agents_run=1,
            ),
        }
        payload.update(overrides)
        return ReviewVerdict.model_validate(payload)

    def _accepted(self, finding: Finding, **overrides: object) -> AcceptedFinding:
        payload: dict[str, object] = {"finding": finding, "reviewers": ["R"]}
        payload.update(overrides)
        return AcceptedFinding.model_validate(payload)

    def _bullet(self, body: str, summary: str) -> str:
        return next(
            ln for ln in body.splitlines() if ln.startswith("- ") and summary in ln
        )

    @pytest.mark.parametrize("severity", ["MUST_FIX", "SHOULD_FIX"])
    def test_suppressed_finding_line_differs_from_a_live_one(
        self, severity: str
    ) -> None:
        live = _make_finding(severity=severity, summary="still live")
        suppressed = _make_finding(
            severity=severity, summary="voided by the operator", evidence="return 1"
        )
        body = render_verdict_comment(
            self._verdict(
                self._accepted(live),
                self._accepted(
                    suppressed,
                    disposition="rejected",
                    disposition_detail="voided by operator comment alice@2026-08-11",
                ),
            ),
            fix_loop_enabled=False,
        )

        assert f"### {severity}" in body
        live_line = self._bullet(body, "still live")
        suppressed_line = self._bullet(body, "voided by the operator")
        assert "suppressed" not in live_line
        assert "suppressed" in suppressed_line
        assert "rejected" in suppressed_line
        assert "voided by operator comment alice@2026-08-11" in suppressed_line

    def test_dropped_disposition_gets_the_identical_treatment(self) -> None:
        """#1805's ``"dropped"`` exposure is closed by the same change."""
        dropped = _make_finding(severity="MUST_FIX", summary="nobody decided this")
        body = render_verdict_comment(
            self._verdict(
                self._accepted(
                    dropped,
                    disposition="dropped",
                    disposition_detail="no adjudication entry recorded",
                )
            ),
            fix_loop_enabled=False,
        )

        line = self._bullet(body, "nobody decided this")
        assert "suppressed" in line
        assert "dropped" in line

    def test_blank_disposition_detail_still_annotates(self) -> None:
        finding = _make_finding(severity="SHOULD_FIX", summary="deferred one")
        body = render_verdict_comment(
            self._verdict(self._accepted(finding, disposition="deferred")),
            fix_loop_enabled=False,
        )

        line = self._bullet(body, "deferred one")
        assert "_(suppressed — deferred)_" in line

    def test_headline_count_and_finding_list_never_disagree(self) -> None:
        """The BLOCKING count reflects only live findings; the list shows both."""
        live = _make_finding(severity="MUST_FIX", summary="still live")
        suppressed = _make_finding(
            severity="MUST_FIX", summary="voided one", evidence="return 1"
        )
        body = render_verdict_comment(
            self._verdict(
                self._accepted(live),
                self._accepted(
                    suppressed, disposition="rejected", disposition_detail="voided"
                ),
            ),
            fix_loop_enabled=False,
        )

        assert "**BLOCKING** — 1 MUST_FIX finding(s)" in body
        assert "still live" in body
        assert "voided one" in body

    def test_all_fixed_verdict_renders_no_annotation(self) -> None:
        body = render_verdict_comment(
            self._verdict(self._accepted(_make_finding(severity="MUST_FIX"))),
            fix_loop_enabled=False,
        )

        assert "suppressed" not in body


class TestRenderVerdictComment:
    def test_render_verdict_comment_shows_mechanically_rejected_must_fix(self) -> None:
        # #1714's second silence: _render_findings iterates verdict.accepted
        # only, so a mechanically-rejected MUST_FIX was invisible on the posted
        # comment even once the sentinel blocked on it.
        verdict = consolidate_verdict(
            [_mechanically_rejected_must_fix_doc()], _make_diff(), reviewed_sha="sha"
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "Non-blocking" not in body
        assert "MUST_FIX REJECTED" in body
        assert "dropped before adjudication" in body
        assert "src/cw/never_in_the_diff.py" in body
        assert "unknown_file" in body

    def test_rejected_must_fix_section_renders_alongside_blocking(self) -> None:
        # The section is rendered unconditionally, so the (rarer) mixed case
        # surfaces both the blocking findings and the dropped one.
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", summary="bad thing"),
            _make_finding(
                severity="MUST_FIX",
                file="src/cw/never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped one",
            ),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "BLOCKING" in body
        assert "bad thing" in body
        assert "dropped one" in body

    def test_render_verdict_comment_includes_discrepancy_detail(self) -> None:
        # #1792: an evidence_not_in_diff rejection's populated `detail`
        # (unlike a mechanically-rejected unknown_file's blank one, covered
        # above) surfaces on the rendered comment.
        verdict = consolidate_verdict(
            [_evidence_not_in_diff_must_fix_doc()], _make_diff(), reviewed_sha="sha"
        )
        assert verdict.rejected_must_fix[0].detail != ""
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert verdict.rejected_must_fix[0].detail in body

    def test_render_verdict_comment_shows_sub_must_fix_rejected_section(self) -> None:
        # #2000: below MUST_FIX there was no section at all -- the reader saw a
        # clean comment over a review that had deleted a finding.
        verdict = consolidate_verdict(
            [_sub_must_fix_rejected_doc()], _make_diff(), reviewed_sha="sha"
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "### Below MUST_FIX — mechanically rejected (not adjudicated)" in body
        # Distinct from the #1714 MUST_FIX heading, which must not appear here.
        assert "### MUST_FIX — mechanically rejected (not adjudicated)" not in body
        assert "<details>" in body
        assert "src/cw/never_in_the_diff.py" in body
        assert "dropped below must_fix" in body
        assert "unknown_file" in body

    def test_render_verdict_comment_groups_rejected_by_reviewer_and_reason(
        self,
    ) -> None:
        # R5 (noise design): same reviewer, same reason collapses into ONE
        # <details> group carrying a count, not N loose bullets.
        doc = _make_reviewer_doc(
            _make_finding(
                severity="SHOULD_FIX",
                file="src/cw/gone_a.py",
                line_start=None,
                line_end=None,
                summary="first drop",
            ),
            _make_finding(
                severity="NIT",
                file="src/cw/gone_b.py",
                line_start=None,
                line_end=None,
                summary="second drop",
            ),
            reviewer_role="Code Quality Reviewer",
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "<summary>Code Quality Reviewer — unknown_file (2)</summary>" in body
        assert body.count("<details>") == 1
        assert "first drop" in body
        assert "second drop" in body

    def test_sub_must_fix_rejected_line_renders_anchor_and_detail(self) -> None:
        # A line-anchored SHOULD_FIX rejected `evidence_not_in_diff` is the one
        # reason that populates RejectedFinding.detail (#1792) -- both the
        # `file:line` anchor and the indented discrepancy line must reach the
        # new section, not just the MUST_FIX one.
        doc = _make_reviewer_doc(
            _make_finding(
                severity="SHOULD_FIX",
                file="src/cw/foo.py",
                line_start=10,
                line_end=10,
                evidence="not present anywhere",
                summary="evidence mismatch",
            )
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected[0].detail != ""
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "- **src/cw/foo.py:10** — evidence mismatch" in body
        assert f"  - {verdict.rejected[0].detail}" in body

    def test_unrecognized_severity_rejection_sorts_last_and_still_renders(
        self,
    ) -> None:
        # A rejected payload is by definition one that failed validation, so
        # its claimed severity may be absent or not a Severity member at all.
        # It must sort last rather than raise -- and it must still be RENDERED:
        # an uncountable rejection is still a rejection, and dropping it here
        # would reintroduce the exact silence #2000 closes.
        strange = RejectedFinding.model_construct(
            raw={"file": "src/cw/strange.py", "summary": "no severity at all"},
            # A distinct role so it forms its own group -- and one that sorts
            # FIRST alphabetically, so a pass that ignored severity rank and
            # fell back to the raw key tuple would order it ahead of the
            # SHOULD_FIX group and fail the ordering assertion below.
            reviewer_role="Aaa Reviewer",
            reason="unknown_file",
            detail="",
        )
        base = consolidate_verdict(
            [
                _make_reviewer_doc(
                    _make_finding(
                        severity="SHOULD_FIX",
                        file="src/cw/gone.py",
                        line_start=None,
                        line_end=None,
                        summary="ordinary drop",
                    )
                )
            ],
            _make_diff(),
            reviewed_sha="sha",
        )
        verdict = base.model_copy(
            update={
                "rejected": [strange, *base.rejected],
                "rejected_count": len(base.rejected) + 1,
                # Kept internally consistent with the injected `strange`
                # rejection -- a real `consolidate_verdict` call would tally
                # its severity-less `raw` under "unknown" (see
                # `_count_rejected_by_severity`), and a verdict no real call
                # could produce is exactly the drift #2000 exists to catch.
                "rejected_count_by_severity": {
                    **base.rejected_count_by_severity,
                    "unknown": 1,
                },
            }
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "no severity at all" in body
        assert "ordinary drop" in body
        # SHOULD_FIX is a known severity, so its group precedes the unranked one
        # regardless of the reversed input order above.
        assert body.index("ordinary drop") < body.index("no severity at all")

    def test_count_rejected_by_severity_tallies_unrecognized_as_unknown(
        self,
    ) -> None:
        # #2000: `_count_rejected_by_severity`'s documented "anything unusable
        # is tallied under 'unknown'" fallback (see its docstring) was never
        # directly exercised anywhere in this diff -- the test above sets
        # `rejected_count_by_severity` by hand rather than through the
        # function that actually computes it.
        strange = RejectedFinding.model_construct(
            raw={"file": "src/cw/strange.py", "summary": "no severity at all"},
            reviewer_role="Aaa Reviewer",
            reason="unknown_file",
            detail="",
        )
        known = RejectedFinding(
            raw=_make_finding(severity="SHOULD_FIX").model_dump(),
            reviewer_role="Reviewer",
            reason="unknown_file",
        )
        assert _count_rejected_by_severity([strange, known]) == {
            "unknown": 1,
            "SHOULD_FIX": 1,
        }

    def test_rejected_must_fix_section_unchanged_when_sub_must_fix_also_present(
        self,
    ) -> None:
        # R3/R4: the #1714 section's heading and per-finding line shape are
        # byte-identical whether or not the new sibling section renders.
        doc = _make_reviewer_doc(
            _make_finding(
                severity="MUST_FIX",
                file="src/cw/never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped before adjudication",
            ),
            _make_finding(
                severity="SHOULD_FIX",
                file="src/cw/also_never_in_the_diff.py",
                line_start=None,
                line_end=None,
                summary="dropped below must_fix",
            ),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert (
            "### MUST_FIX — mechanically rejected (not adjudicated)\n"
            "\n"
            "- **src/cw/never_in_the_diff.py** — dropped before adjudication "
            "(rejected: unknown_file)\n"
        ) in body
        assert "### Below MUST_FIX — mechanically rejected (not adjudicated)" in body
        assert "dropped below must_fix" in body
        # The headline stays #1714's -- the new branch sits strictly below it.
        assert "MUST_FIX REJECTED" in body
        assert "PROCEED (findings mechanically rejected)" not in body

    def test_clean_verdict_has_no_rejected_must_fix_section(self) -> None:
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "MUST_FIX REJECTED" not in body
        assert "Non-blocking" in body

    def test_blocking_lists_must_fix(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "BLOCKING" in body
        assert "MUST_FIX" in body
        assert "bad thing" in body
        assert "src/cw/foo.py:10" in body

    def test_non_blocking_header(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
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
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
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
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "LOW confidence" in body
        assert "bad thing" in body

    def test_high_confidence_finding_renders_no_confidence_label(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="HIGH", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "confidence" not in body.lower()

    def test_medium_confidence_finding_renders_confidence_label(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", confidence="MEDIUM", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
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
        assert "### MUST_FIX" in render_verdict_comment(
            verdict_low, fix_loop_enabled=False
        )

    # -----------------------------------------------------------------
    # #1705 — found-nothing vs found-and-fixed vs fix-loop-off histories
    # -----------------------------------------------------------------

    def test_clean_no_findings_fix_loop_off_states_single_pass(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "single-pass" in body.lower()
        assert "disabled" in body.lower()
        # R1: fix-loop-off must never be phrased as flaked/degraded.
        assert "flak" not in body.lower()
        assert "degrad" not in body.lower()

    def test_clean_no_findings_fix_loop_on_states_available_but_unneeded(
        self,
    ) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        off_body = render_verdict_comment(verdict, fix_loop_enabled=False)
        on_body = render_verdict_comment(verdict, fix_loop_enabled=True)
        assert "available" in on_body.lower()
        assert on_body != off_body

    def test_found_and_fixed_headline_differs_from_found_nothing(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        clean_verdict = consolidate_verdict(
            documents=[doc],
            diff=diff,
            reviewed_sha="sha",
        )
        fixed_verdict = clean_verdict.model_copy(
            update={
                "review": clean_verdict.review.model_copy(
                    update={"must_fix_initial": 2, "fix_cycles_used": 2}
                )
            }
        )
        clean_body = render_verdict_comment(clean_verdict, fix_loop_enabled=True)
        fixed_body = render_verdict_comment(fixed_verdict, fix_loop_enabled=True)
        assert fixed_body != clean_body
        assert "2" in fixed_body
        assert "UNVERIFIED" not in fixed_body

    def test_flaked_fix_loop_renders_unverified_not_resolved(self) -> None:
        """#1723 — a fix loop that converged without any real commit renders
        an UNVERIFIED headline instead of the resolved-N-of-M claim."""
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        clean_verdict = consolidate_verdict(
            documents=[doc],
            diff=diff,
            reviewed_sha="sha",
        )
        flaked_verdict = clean_verdict.model_copy(
            update={
                "review": clean_verdict.review.model_copy(
                    update={
                        "must_fix_initial": 2,
                        "fix_cycles_used": 2,
                        "had_real_commit": False,
                    }
                )
            }
        )
        body = render_verdict_comment(flaked_verdict, fix_loop_enabled=True)
        assert "UNVERIFIED" in body
        assert "commit outcomes not individually tracked" not in body
        assert "2" in body

    def test_had_real_commit_true_never_renders_unverified(self) -> None:
        """#1723 — a genuine fix-cycle commit never renders the UNVERIFIED
        headline, regardless of how many cycles it took."""
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        clean_verdict = consolidate_verdict(
            documents=[doc],
            diff=diff,
            reviewed_sha="sha",
        )
        fixed_verdict = clean_verdict.model_copy(
            update={
                "review": clean_verdict.review.model_copy(
                    update={
                        "must_fix_initial": 2,
                        "fix_cycles_used": 2,
                        "had_real_commit": True,
                    }
                )
            }
        )
        body = render_verdict_comment(fixed_verdict, fix_loop_enabled=True)
        assert "UNVERIFIED" not in body

    def test_unknown_real_commit_never_renders_unverified(self) -> None:
        """Legacy payloads with an unknown commit outcome retain prior prose."""
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        clean_verdict = consolidate_verdict(
            documents=[doc],
            diff=diff,
            reviewed_sha="sha",
        )
        legacy_verdict = clean_verdict.model_copy(
            update={
                "review": clean_verdict.review.model_copy(
                    update={"must_fix_initial": 2, "fix_cycles_used": 2}
                )
            }
        )
        body = render_verdict_comment(legacy_verdict, fix_loop_enabled=True)
        assert legacy_verdict.review.had_real_commit is None
        assert "UNVERIFIED" not in body

    def test_blocking_capped_exit_shows_resolved_vs_open_counts(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        verdict = verdict.model_copy(
            update={
                "review": verdict.review.model_copy(
                    update={
                        "must_fix_initial": 3,
                        "deferred": 1,
                        "fix_cycles_used": 5,
                    }
                )
            }
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=True)
        assert "2 of 3" in body
        assert "1" in body

    def test_failed_role_note_rendered_for_partial_coverage(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Performance Reviewer", reason="codex_timeout")
            ],
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "PARTIAL COVERAGE" in body
        assert "Performance Reviewer" in body

    def test_fix_loop_off_blocking_states_single_pass(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "BLOCKING" in body
        assert "single-pass" in body.lower()
        assert "disabled" in body.lower()

    def test_capability_note_renders_when_degraded(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        verdict = verdict.model_copy(
            update={"capability_mode": "degraded", "capability_reason": "unknown"}
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "degraded" in body.lower()
        assert "unknown" in body

    def test_capability_note_renders_when_capable(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        verdict = verdict.model_copy(
            update={"capability_mode": "capable", "capability_reason": None}
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "capable" in body.lower()

    def test_capability_note_absent_when_mode_unset(self) -> None:
        # capability_mode defaults to None (nobody probed) -- must not render
        # "unknown" or any placeholder, per the ticket's explicit instruction.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.capability_mode is None
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "capability" not in body.lower()
        assert "degraded" not in body.lower()


# ---------------------------------------------------------------------------
# #1775 — a degraded reviewer's stated reason (`detail`), rendered
# ---------------------------------------------------------------------------


class TestRenderDegradedRolesNote:
    # #1806: ReviewerFindingsDocument now rejects status="degraded" with a
    # blank `detail` at construction, so the two blank-detail cases below can
    # no longer go through _make_reviewer_doc()/consolidate_verdict() -- they
    # build a valid verdict first, then substitute a directly-constructed
    # ReviewerRunRecord (rendering stays reachable as defense-in-depth for
    # any run record not routed through the document contract).

    def test_degraded_role_with_detail_renders_role_and_reason(self) -> None:
        doc = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="Reviewer A",
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "**DEGRADED COVERAGE**" in body
        assert "Reviewer A" in body
        assert "sandbox lacked filesystem access" in body

    def test_degraded_role_with_no_detail_renders_distinct_no_reason_marker(
        self,
    ) -> None:
        doc = _make_reviewer_doc(reviewer_role="Reviewer A")
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        verdict = verdict.model_copy(
            update={
                "agents_run": [
                    ReviewerRunRecord(
                        reviewer_role="Reviewer A",
                        status="degraded",
                        detail="",
                        finding_count=0,
                    )
                ]
            }
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "**DEGRADED COVERAGE**" in body
        assert "Reviewer A: degraded (no reason given)" in body

    def test_two_degraded_roles_with_and_without_detail_both_render_distinguishably(
        self,
    ) -> None:
        # Mirrors the real #1754 repro: multiple degraded roles in one pass,
        # only some of which stated a reason.
        doc_a = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="Reviewer A",
        )
        verdict = consolidate_verdict([doc_a], _make_diff(), reviewed_sha="sha")
        verdict = verdict.model_copy(
            update={
                "agents_run": [
                    ReviewerRunRecord(
                        reviewer_role="Reviewer A",
                        status="degraded",
                        detail="sandbox lacked filesystem access",
                        finding_count=0,
                    ),
                    ReviewerRunRecord(
                        reviewer_role="Reviewer B",
                        status="degraded",
                        detail="",
                        finding_count=0,
                    ),
                ]
            }
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "Reviewer A: degraded — sandbox lacked filesystem access" in body
        assert "Reviewer B: degraded (no reason given)" in body

    def test_clean_verdict_has_no_degraded_roles_section(self) -> None:
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "DEGRADED COVERAGE" not in body

    def test_degraded_note_renders_alongside_failed_roles_note(self) -> None:
        doc = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="Reviewer A",
        )
        verdict = consolidate_verdict(
            [doc],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Perf Reviewer", reason="timeout")
            ],
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "**PARTIAL COVERAGE**" in body
        assert "Perf Reviewer" in body
        assert "**DEGRADED COVERAGE**" in body
        assert "Reviewer A: degraded — sandbox lacked filesystem access" in body


# ---------------------------------------------------------------------------
# #1773 — agent-spec resolution status on the verdict and its rendered note
# ---------------------------------------------------------------------------


_RECOVERED_NOTE = (
    "**NOTE:** Code Quality Reviewer's repo-tracked spec was present but "
    "empty — recovered via the global fallback; the repo-tracked file may be "
    "truncated or need attention."
)


def _spec_status(
    source: AgentSpecSource,
    empty: bool,
    empty_repo_file: bool,
    role: str = "Code Quality Reviewer",
) -> AgentSpecStatus:
    return AgentSpecStatus(
        role=role,
        source=source,
        empty=empty,
        empty_repo_file=empty_repo_file,
    )


def _body_with_specs(statuses: list[AgentSpecStatus]) -> str:
    diff = _make_diff()
    doc = _make_reviewer_doc(_make_finding(severity="NIT"))
    verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
    verdict = verdict.model_copy(update={"agent_spec_status": statuses})
    return render_verdict_comment(verdict, fix_loop_enabled=False)


class TestRenderAgentSpecNote:
    def test_repo_present_renders_fully_specified_headline(self) -> None:
        body = _body_with_specs(
            [_spec_status("repo", empty=False, empty_repo_file=False)]
        )
        assert "_Agent specs loaded for all 1 reviewer role(s)._" in body
        assert "UNSPECIFIED" not in body
        assert "**NOTE:**" not in body

    def test_repo_empty_with_gate_disabled_names_the_role(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("repo", empty=True, empty_repo_file=True),
                _spec_status(
                    "repo", empty=False, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "**AGENT SPEC(S) UNSPECIFIED** — 1 of 2 role(s) ran without a " in body
        assert "Code Quality Reviewer (present but empty, no usable fallback)." in body
        assert "**NOTE:**" not in body

    def test_global_present_renders_fully_specified_headline(self) -> None:
        body = _body_with_specs(
            [_spec_status("global", empty=False, empty_repo_file=False)]
        )
        assert "_Agent specs loaded for all 1 reviewer role(s)._" in body
        assert "**NOTE:**" not in body

    def test_recovered_empty_repo_file_appends_the_addendum(self) -> None:
        body = _body_with_specs(
            [_spec_status("global", empty=False, empty_repo_file=True)]
        )
        assert "_Agent specs loaded for all 1 reviewer role(s)._" in body
        assert _RECOVERED_NOTE in body

    def test_global_found_but_empty_label(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("global", empty=True, empty_repo_file=False),
                _spec_status(
                    "repo", empty=False, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "**AGENT SPEC(S) UNSPECIFIED** — 1 of 2 role(s) ran without a " in body
        assert "Code Quality Reviewer (global spec found but empty)." in body
        assert "**NOTE:**" not in body

    def test_global_empty_with_empty_repo_file_uses_no_usable_fallback_label(
        self,
    ) -> None:
        body = _body_with_specs(
            [
                _spec_status("global", empty=True, empty_repo_file=True),
                _spec_status(
                    "repo", empty=False, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "Code Quality Reviewer (present but empty, no usable fallback)." in body
        assert "**NOTE:**" not in body

    def test_absent_label(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("none", empty=True, empty_repo_file=False),
                _spec_status(
                    "repo", empty=False, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "Code Quality Reviewer (absent)." in body
        assert "**NOTE:**" not in body

    def test_none_with_empty_repo_file_uses_no_usable_fallback_label(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("none", empty=True, empty_repo_file=True),
                _spec_status(
                    "repo", empty=False, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "Code Quality Reviewer (present but empty, no usable fallback)." in body
        assert "**NOTE:**" not in body

    def test_recovered_addendum_renders_alongside_a_degraded_headline(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("global", empty=False, empty_repo_file=True),
                _spec_status(
                    "none", empty=True, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert "**AGENT SPEC(S) UNSPECIFIED** — 1 of 2 role(s) ran without a " in body
        assert "SysAdmin Reviewer (absent)." in body
        assert _RECOVERED_NOTE in body

    def test_all_roles_unspecified_renders_the_fail_open_headline(self) -> None:
        body = _body_with_specs(
            [
                _spec_status("none", empty=True, empty_repo_file=False),
                _spec_status(
                    "none", empty=True, empty_repo_file=False, role="SysAdmin Reviewer"
                ),
            ]
        )
        assert (
            "**ALL AGENT SPECS UNSPECIFIED** — no reviewer role in this pass "
            "had a loaded agent specification (repo or global); every prompt's "
            "`## Agent Specification` section was empty." in body
        )
        assert "**NOTE:**" not in body

    def test_unset_renders_nothing(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.agent_spec_status == []
        body = render_verdict_comment(verdict, fix_loop_enabled=False)
        assert "AGENT SPEC" not in body
        assert "Agent specs loaded" not in body


class TestVerdictRecordsAgentSpecStatus:
    def test_agent_spec_status_recorded_verbatim(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-spec-status")
        statuses = [_spec_status("global", empty=False, empty_repo_file=True)]
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-spec-status",
            default_branch="main",
            fix_loop_enabled=False,
            agent_spec_status=statuses,
        )
        assert verdict is not None
        assert verdict.agent_spec_status == statuses

    def test_omitted_kwarg_leaves_the_field_empty(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-spec-status-absent")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-spec-status-absent",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert verdict is not None
        assert verdict.agent_spec_status == []


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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
            fix_loop_enabled=False,
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
                fix_loop_enabled=False,
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
            fix_loop_enabled=False,
        )

        assert result.branch == SCOPE_GUARD_BRANCH
        assert result.health.lowest_agent_confidence == "HIGH"
        assert result.health.any_incomplete_risk is False
        assert result.health.recommendation == "PROCEED"


# ---------------------------------------------------------------------------
# #1870 — a measured-empty clean review exits empty_diff_blocked, not
# stage_complete
# ---------------------------------------------------------------------------


_EMPTY_DIFF_BRANCH = "dev/1870-empty"


def _make_empty_diff_repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    """Repo checked out on a branch carrying no commits of its own.

    Deliberately a real repo rather than a stubbed ``compute_branch_diff_scope``:
    the distinction this ticket turns on is "measured 0/0" versus "unmeasurable"
    (``None``), and only a real measurement proves the branch reaches the former.
    """
    repo = make_git_repo(name)
    _scope_guard_git(repo, "remote", "add", "origin", str(repo))
    _scope_guard_git(repo, "fetch", "origin", "main")
    _scope_guard_git(repo, "checkout", "-b", _EMPTY_DIFF_BRANCH)
    return repo


class TestEmptyDiffCleanReview:
    def test_measured_empty_diff_blocks_instead_of_completing(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#1870: a clean review over a branch with nothing on it is not a pass."""
        from cw.auto_dev_result import EMPTY_DIFF_BLOCKER_REASON

        worktree = _make_empty_diff_repo(make_git_repo, "wt-verdict-empty")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-empty-diff",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "empty_diff_blocked"
        assert result.stage_reached == "stage3_review"
        assert result.scope.files == 0
        assert result.scope.lines_actual == 0
        assert result.branch == _EMPTY_DIFF_BRANCH
        assert result.blocker is not None
        assert result.blocker.reason == EMPTY_DIFF_BLOCKER_REASON
        assert result.blocker.stage == "stage3_review"
        assert _EMPTY_DIFF_BRANCH in result.blocker.details
        # health must not vouch for a review that covered nothing
        assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
        # the real verdict is preserved for the caller's rendering path, and
        # review.agents_run keeps its true value rather than being zeroed
        assert verdict is not None
        assert result.review == verdict.review
        assert result.review.agents_run == 1

    def test_non_empty_clean_review_is_unaffected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Regression guard: a branch with real churn still exits stage_complete."""
        worktree = _make_stale_base_repo(make_git_repo, "wt-verdict-nonempty")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-nonempty-diff",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "stage_complete"
        assert result.blocker is None
        assert result.scope.files == SCOPE_GUARD_FILES

    def test_unmeasurable_worktree_is_not_treated_as_empty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """``None`` from compute_branch_diff_scope means "could not measure",
        never "measured zero" -- it must keep its pre-#1870 stage_complete
        fallback, warning included, rather than being parked."""
        worktree = make_git_repo("wt-verdict-unmeasurable-empty")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))

        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-unmeasurable-empty",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.status == "stage_complete"
        assert result.scope.files == 0
        assert result.scope.lines_actual == 0


# ---------------------------------------------------------------------------
# Filesystem-capability recording on the verdict (#1709)
# ---------------------------------------------------------------------------


def _capability(*, capable: bool, reason: str | None) -> _CodexFilesystemCapability:
    return _CodexFilesystemCapability(
        capable=capable,
        reason=reason,
        fingerprint=_CodexFingerprint(
            cli_version="0.147.0",
            platform="Linux",
            install_type="other",
            sandbox_mode="read-only",
        ),
    )


class TestVerdictRecordsCapabilityMode:
    """The selected capability mode must be recoverable from the artifact a
    human reads, not just from a log line that scrolled away (#1709)."""

    def test_degraded_capability_recorded_on_verdict(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-cap-degraded")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-cap-degraded",
            default_branch="main",
            fix_loop_enabled=False,
            capability=_capability(capable=False, reason="unknown"),
        )
        assert verdict is not None
        assert verdict.capability_mode == "degraded"
        assert verdict.capability_reason == "unknown"

    def test_capable_capability_recorded_on_verdict(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-cap-capable")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-cap-capable",
            default_branch="main",
            fix_loop_enabled=False,
            capability=_capability(capable=True, reason=None),
        )
        assert verdict is not None
        assert verdict.capability_mode == "capable"
        assert verdict.capability_reason is None

    def test_absent_capability_leaves_fields_unset(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """The kwarg is optional (additive): callers that do not probe — e.g.
        the direct-synthesis tests above — record nothing rather than a
        misleading default."""
        worktree = make_git_repo("wt-cap-absent")
        _result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc()],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-cap-absent",
            default_branch="main",
            fix_loop_enabled=False,
        )
        assert verdict is not None
        assert verdict.capability_mode is None
        assert verdict.capability_reason is None


# ---------------------------------------------------------------------------
# _render_debt_note / previous_reviewed_sha (#1837)
# ---------------------------------------------------------------------------


class TestRenderDebtNote:
    def _verdict(self, **overrides: object) -> ReviewVerdict:
        payload: dict[str, object] = {
            "blocking": False,
            "must_fix": [],
            "reviewed_sha": "sha",
            "review": Review(
                must_fix_initial=0,
                should_fix=0,
                fix_cycles_used=0,
                deferred=0,
                agents_run=1,
            ),
        }
        payload.update(overrides)
        return ReviewVerdict.model_validate(payload)

    def test_empty_debt_renders_nothing(self) -> None:
        body = render_verdict_comment(self._verdict(), fix_loop_enabled=False)
        assert "### Debt" not in body

    def test_debt_section_names_file_summary_and_disposition(self) -> None:
        record = _make_debt_record(
            file="src/cw/bar.py",
            summary="Duplicated helper",
            fingerprint=("src/cw/bar.py", "duplicated helper"),
        )
        body = render_verdict_comment(
            self._verdict(debt=[record]), fix_loop_enabled=True
        )

        # `###`, matching `_render_rejected_must_fix`'s sub-section convention.
        assert "### Debt" in body
        assert "## Debt" not in body.replace("### Debt", "")
        assert "src/cw/bar.py" in body
        assert "Duplicated helper" in body
        assert "NEEDS_FILING" in body
        assert "duplicated helper" in body

    def test_previous_reviewed_sha_renders_only_when_set(self) -> None:
        unset = render_verdict_comment(self._verdict(), fix_loop_enabled=True)
        assert "reviewed only what changed since" not in unset

        set_body = render_verdict_comment(
            self._verdict(previous_reviewed_sha="0ldsha1"), fix_loop_enabled=True
        )
        assert "0ldsha1" in set_body

    def test_run_failure_discard_section_names_role_count_and_severities(
        self,
    ) -> None:
        # #2029: a reviewer whose whole document was unusable still discarded
        # findings nobody read — the comment has to say so.
        verdict = self._verdict(
            run_failures_with_should_fix_discards=[
                ReviewerRunFailure(
                    role="Performance Reviewer",
                    reason="codex_review_unparseable",
                    discarded_finding_count=2,
                    discarded_finding_severities={"SHOULD_FIX": 1, "NIT": 1},
                )
            ]
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=False)

        assert "### Reviewer failures that discarded findings" in body
        assert "Performance Reviewer" in body
        assert "codex_review_unparseable" in body
        assert "SHOULD_FIX: 1" in body
        assert "NIT: 1" in body

    def test_empty_run_failure_discards_render_nothing(self) -> None:
        body = render_verdict_comment(self._verdict(), fix_loop_enabled=False)
        assert "### Reviewer failures that discarded findings" not in body


class TestSynthesizeCodexReviewResultRunFailureDiscards:
    """#2029 — a whole-document discard at SHOULD_FIX-or-above parks the run."""

    def _failure(self, **overrides: object) -> ReviewerRunFailure:
        payload: dict[str, object] = {
            "role": "Performance Reviewer",
            "reason": CODEX_REVIEW_UNPARSEABLE,
        }
        payload.update(overrides)
        return ReviewerRunFailure.model_validate(payload)

    def test_should_fix_discard_outranks_the_plain_partial_disposition(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _make_stale_base_repo(make_git_repo, "wt-2029-discard-order")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_sub_must_fix_rejected_doc()],
            failures=[
                self._failure(
                    discarded_finding_count=1,
                    discarded_finding_severities={"SHOULD_FIX": 1},
                )
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-2029-discard-order",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS
        assert result.blocker.reason != CODEX_REVIEW_PARTIAL
        assert verdict is not None
        assert len(verdict.run_failures_with_should_fix_discards) == 1

    def test_must_fix_discard_also_gates(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _make_stale_base_repo(make_git_repo, "wt-2029-discard-mf")
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_sub_must_fix_rejected_doc()],
            failures=[
                self._failure(
                    discarded_finding_count=1,
                    discarded_finding_severities={"MUST_FIX": 1},
                )
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-2029-discard-mf",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS

    def test_nit_only_discard_falls_through_to_partial(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # The threshold lock: "any discard at all" is NOT the gate.
        worktree = _make_stale_base_repo(make_git_repo, "wt-2029-discard-nit")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_sub_must_fix_rejected_doc()],
            failures=[
                self._failure(
                    discarded_finding_count=3,
                    discarded_finding_severities={"NIT": 2, "DEBT": 1},
                )
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-2029-discard-nit",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_PARTIAL
        assert verdict is not None
        assert verdict.run_failures_with_should_fix_discards == []

    def test_surviving_must_fix_still_outranks_a_discard(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        # Ordering guard in the other direction: the new branch sits BELOW the
        # blocking and rejected_must_fix branches, never above them.
        worktree = _make_stale_base_repo(make_git_repo, "wt-2029-discard-under")
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_mechanically_rejected_must_fix_doc()],
            failures=[
                self._failure(
                    discarded_finding_count=1,
                    discarded_finding_severities={"SHOULD_FIX": 1},
                )
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-2029-discard-under",
            default_branch="main",
            fix_loop_enabled=False,
        )

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED

    def test_pre_validation_rejected_threads_into_the_verdict(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        worktree = _make_stale_base_repo(make_git_repo, "wt-2029-prevalidation")
        rejected = RejectedFinding(
            raw={"severity": "MUST_FIX", "file": "src/cw/foo.py", "summary": "unread"},
            reviewer_role="Code Quality Reviewer",
            reason="schema_invalid",
            detail="evidence: Field required",
        )
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[_make_reviewer_doc(_make_finding(severity="NIT"))],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-2029-prevalidation",
            default_branch="main",
            fix_loop_enabled=False,
            pre_validation_rejected=[rejected],
        )

        assert verdict is not None
        assert verdict.rejected_must_fix == [rejected]
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
