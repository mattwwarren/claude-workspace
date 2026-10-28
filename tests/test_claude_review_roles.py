"""Tests for the per-role Claude loop ``cw.claude_review._roles`` (#2791).

Every role runs against a real git worktree (the G4 check reads its state) and
a ``FakeClaudeRunner`` replaying envelopes from ``tests/_claude_review_helpers``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.claude_review import (
    ClaudeRunResult,
    FakeClaudeRunner,
    WorktreeMutation,
    build_claude_review_argv,
    run_claude_roles,
)
from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEW_WORKTREE_MUTATED,
    CODEX_TIMEOUT,
)
from cw.executor_diagnostics import ExecutorFailure, diagnostics_bundle_dir
from cw.review_findings import ReviewerFindingsDocument
from tests._claude_review_helpers import (
    auth_failure_envelope,
    error_envelope,
    non_document_envelope,
    review_envelope,
    truncated_envelope,
)
from tests._codex_review_helpers import _Clock, _doc_json, _finding_payload
from tests._review_convergence_helpers import structurally_invalid_doc_json
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.claude_review import ClaudeRolesOutcome

_ROLE = "Code Quality Reviewer"
_SECOND = "SysAdmin Reviewer"
_SESSION = "confirm-T-1"


@pytest.fixture
def wt(make_git_repo: Callable[..., Path]) -> Path:
    return make_git_repo("claude-roles")


def _run(
    wt: Path,
    runner: FakeClaudeRunner,
    *,
    roles: tuple[str, ...] = (_ROLE,),
    budget: int | None = None,
) -> ClaudeRolesOutcome:
    return run_claude_roles(
        runner=runner,
        worktree=wt,
        roles=list(roles),
        prompts_by_role={role: f"prompt for {role}" for role in roles},
        model=None,
        wall_clock_budget_seconds=budget,
        session_id=_SESSION,
    )


def _ok(role: str = _ROLE) -> ClaudeRunResult:
    return ClaudeRunResult(
        returncode=0, stdout=review_envelope(_doc_json(role=role)), stderr=""
    )


class TestDocuments:
    def test_structured_output_becomes_a_document(self, wt: Path) -> None:
        payload = _finding_payload(severity="MUST_FIX", summary="real bug")
        runner = FakeClaudeRunner(
            stdout=review_envelope(_doc_json(role=_ROLE, findings=[payload]))
        )
        outcome = _run(wt, runner)
        assert outcome.failures == []
        assert outcome.mutation is None
        [doc] = outcome.documents
        assert isinstance(doc, ReviewerFindingsDocument)
        assert doc.findings[0].summary == "real bug"

    def test_result_string_is_the_fallback(self, wt: Path) -> None:
        runner = FakeClaudeRunner(
            stdout=review_envelope(_doc_json(role=_ROLE), structured_output=False)
        )
        outcome = _run(wt, runner)
        assert [d.reviewer_role for d in outcome.documents] == [_ROLE]

    def test_the_captured_document_parses(self, wt: Path) -> None:
        """The real capture's own ``correctness`` document, unmodified."""
        outcome = _run(wt, FakeClaudeRunner(stdout=review_envelope()))
        [doc] = outcome.documents
        assert doc.reviewer_role == "correctness"
        assert doc.findings[0].severity == "MUST_FIX"

    def test_prompt_travels_over_stdin_in_the_worktree(self, wt: Path) -> None:
        runner = FakeClaudeRunner(stdout=review_envelope(_doc_json(role=_ROLE)))
        _run(wt, runner)
        [call] = runner.calls
        assert call["stdin"] == f"prompt for {_ROLE}"
        assert call["cwd"] == wt
        assert call["timeout"] is None
        argv = call["argv"]
        assert isinstance(argv, list)
        assert f"prompt for {_ROLE}" not in argv
        # The whole confinement contract, on the argv run_claude_roles really
        # builds: an appended --add-dir or a widened --tools would differ here.
        assert argv == build_claude_review_argv(
            model=None,
            schema=json.dumps(ReviewerFindingsDocument.model_json_schema()),
        )


class TestFailures:
    @pytest.mark.parametrize(
        ("result", "reason"),
        [
            (ClaudeRunResult(returncode=1, stdout="", stderr="boom"), CODEX_ERROR),
            (
                ClaudeRunResult(
                    returncode=127, stdout="", stderr="claude: command not found"
                ),
                CODEX_ERROR,
            ),
            (
                ClaudeRunResult(returncode=0, stdout=error_envelope(), stderr=""),
                CODEX_ERROR,
            ),
            (
                ClaudeRunResult(
                    returncode=0, stdout=auth_failure_envelope(), stderr=""
                ),
                CODEX_ERROR,
            ),
            (
                ClaudeRunResult(returncode=-1, stdout="", stderr="", timed_out=True),
                CODEX_TIMEOUT,
            ),
            (
                ClaudeRunResult(returncode=0, stdout=truncated_envelope(), stderr=""),
                CODEX_REVIEW_UNPARSEABLE,
            ),
            (
                ClaudeRunResult(
                    returncode=0, stdout=non_document_envelope(), stderr=""
                ),
                CODEX_REVIEW_UNPARSEABLE,
            ),
            (
                ClaudeRunResult(returncode=0, stdout="", stderr=""),
                CODEX_REVIEW_UNPARSEABLE,
            ),
            (
                ClaudeRunResult(returncode=0, stdout="[1, 2]", stderr=""),
                CODEX_REVIEW_UNPARSEABLE,
            ),
            (
                ClaudeRunResult(
                    returncode=0,
                    stdout=json.dumps({"type": "result", "is_error": False}),
                    stderr="",
                ),
                CODEX_REVIEW_UNPARSEABLE,
            ),
        ],
        ids=[
            "nonzero_exit",
            "spawn_error",
            "is_error",
            "auth_failure",
            "timeout",
            "truncated",
            "non_document",
            "empty_stdout",
            "non_object",
            "no_document_key",
        ],
    )
    def test_failure_reason(
        self, wt: Path, result: ClaudeRunResult, reason: str
    ) -> None:
        outcome = _run(wt, FakeClaudeRunner(results=[result]))
        assert outcome.documents == []
        assert [(f.role, f.reason) for f in outcome.failures] == [(_ROLE, reason)]
        assert outcome.mutation is None

    def test_schema_mismatch_records_a_discarded_tally(self, wt: Path) -> None:
        runner = FakeClaudeRunner(
            stdout=review_envelope(structurally_invalid_doc_json("SHOULD_FIX"))
        )
        [failure] = _run(wt, runner).failures
        assert failure.reason == CODEX_REVIEW_UNPARSEABLE
        assert failure.discarded_finding_count == 1
        assert failure.discarded_finding_severities == {"SHOULD_FIX": 1}

    def test_a_diagnostics_bundle_is_written_per_failed_role(self, wt: Path) -> None:
        runner = FakeClaudeRunner(
            results=[
                ClaudeRunResult(returncode=1, stdout="", stderr="boom"),
                ClaudeRunResult(returncode=-1, stdout="", stderr="", timed_out=True),
            ]
        )
        _run(wt, runner, roles=(_ROLE, _SECOND))
        bundles = sorted(
            p
            for p in diagnostics_bundle_dir(_SESSION).glob("*.json")
            if "-document-" not in p.name
        )
        failures = [
            ExecutorFailure.model_validate_json(p.read_text(encoding="utf-8"))
            for p in bundles
        ]
        assert sorted((f.reviewer_role, f.category) for f in failures) == [
            (_ROLE, "nonzero_exit"),
            (_SECOND, "timeout"),
        ]
        assert {f.executor_name for f in failures} == {"claude"}


class TestBudgetAndMetrics:
    def test_an_exhausted_budget_skips_without_calling_the_runner(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.claude_review._roles.time.monotonic", _Clock([0.0, 1000.0])
        )
        runner = FakeClaudeRunner(stdout=review_envelope(_doc_json(role=_ROLE)))
        outcome = _run(wt, runner, roles=(_ROLE, _SECOND), budget=10)
        assert runner.calls == []
        assert [(f.role, f.reason) for f in outcome.failures] == [
            (_ROLE, CODEX_BUDGET_EXHAUSTED),
            (_SECOND, CODEX_BUDGET_EXHAUSTED),
        ]
        assert outcome.metrics_by_role == {}

    def test_a_live_budget_bounds_the_role_timeout(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.claude_review._roles.time.monotonic", _Clock([0.0, 100.0, 100.0, 110.0])
        )
        runner = FakeClaudeRunner(stdout=review_envelope(_doc_json(role=_ROLE)))
        _run(wt, runner, budget=600)
        assert runner.calls[0]["timeout"] == 500

    def test_metrics_carry_only_duration(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.claude_review._roles.time.monotonic", _Clock([100.0, 107.5])
        )
        outcome = _run(wt, FakeClaudeRunner(stdout=review_envelope()))
        metrics = outcome.metrics_by_role["Code Quality Reviewer"]
        assert metrics["duration_seconds"] == pytest.approx(7.5)
        assert metrics["input_tokens"] is None
        assert metrics["output_tokens"] is None
        assert metrics["terminal_event"] is None
        assert metrics["tool_call_counts"] == {}


def _write_file(worktree: Path) -> None:
    (worktree / "hook-wrote.txt").write_text("x\n", encoding="utf-8")


def _commit(worktree: Path) -> None:
    commit_tracked_file(worktree, "hook_commit.py")


def _touch_nothing(_worktree: Path) -> None:
    return None


class TestWorktreeMutation:
    """G4: a role that changes HEAD or ``git status`` fails and stops the loop."""

    @pytest.mark.parametrize("effect", [_write_file, _commit], ids=["write", "commit"])
    def test_a_mutating_role_fails_and_stops_later_roles(
        self, wt: Path, effect: Callable[[Path], None]
    ) -> None:
        runner = FakeClaudeRunner(
            stdout=review_envelope(_doc_json(role=_ROLE)), side_effect=effect
        )
        outcome = _run(wt, runner, roles=(_ROLE, _SECOND))
        assert len(runner.calls) == 1
        assert outcome.documents == []
        assert [(f.role, f.reason) for f in outcome.failures] == [
            (_ROLE, CODEX_REVIEW_WORKTREE_MUTATED)
        ]
        assert isinstance(outcome.mutation, WorktreeMutation)
        assert outcome.mutation.role == _ROLE
        assert _SECOND not in outcome.metrics_by_role

    def test_a_write_is_described_by_its_status_lines(self, wt: Path) -> None:
        runner = FakeClaudeRunner(stdout=review_envelope(), side_effect=_write_file)
        mutation = _run(wt, runner).mutation
        assert mutation is not None
        assert mutation.describe() == "git status changed: ?? hook-wrote.txt"

    def test_a_commit_is_described_by_its_heads(self, wt: Path) -> None:
        head = git_in(wt, "rev-parse", "HEAD")
        runner = FakeClaudeRunner(stdout=review_envelope(), side_effect=_commit)
        mutation = _run(wt, runner).mutation
        assert mutation is not None
        new_head = git_in(wt, "rev-parse", "HEAD")
        assert mutation.describe() == f"HEAD {head} -> {new_head}"

    def test_a_role_that_touches_nothing_passes(self, wt: Path) -> None:
        runner = FakeClaudeRunner(
            results=[_ok(_ROLE), _ok(_SECOND)], side_effect=_touch_nothing
        )
        outcome = _run(wt, runner, roles=(_ROLE, _SECOND))
        assert outcome.mutation is None
        assert [d.reviewer_role for d in outcome.documents] == [_ROLE, _SECOND]

    def test_an_already_dirty_tree_is_not_a_mutation(self, wt: Path) -> None:
        (wt / "operator-scratch.txt").write_text("dirty before\n", encoding="utf-8")
        runner = FakeClaudeRunner(stdout=review_envelope(_doc_json(role=_ROLE)))
        outcome = _run(wt, runner)
        assert outcome.mutation is None
        assert len(outcome.documents) == 1

    def test_the_status_description_quotes_at_most_five_lines(self) -> None:
        mutation = WorktreeMutation(
            "r",
            ("h", ""),
            ("h", "".join(f"?? f{i}.txt\n" for i in range(8))),
        )
        assert mutation.describe().count("??") == 5
