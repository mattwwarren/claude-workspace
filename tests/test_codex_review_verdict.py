"""Tests for cw.codex_review._verdict — verdict synthesis and review-comment
rendering (#1236, #1239)."""

from __future__ import annotations

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
    CODEX_TIMEOUT,
    _format_failures_detail,
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
from cw.review_findings import (
    AcceptedFinding,
    AgentSpecSource,
    AgentSpecStatus,
    ReviewerRunFailure,
    ReviewerRunRecord,
    ReviewVerdict,
    consolidate_verdict,
)
from tests._codex_review_helpers import _task
from tests._reconcile_helpers import (
    SCOPE_GUARD_BRANCH,
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _make_stale_base_repo,
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
