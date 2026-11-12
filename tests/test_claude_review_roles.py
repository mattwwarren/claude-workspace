"""Tests for the per-role Claude loop ``cw.claude_review._roles`` (#2791).

Every role runs against a real git worktree (the G4 check reads its state) and
a ``FakeClaudeRunner`` replaying envelopes from ``tests/_claude_review_helpers``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from cw.claude_review import (
    ClaudeRunResult,
    FakeClaudeRunner,
    WorktreeMutation,
    build_claude_review_argv,
    run_claude_roles,
)
from cw.claude_review import _roles as claude_roles
from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEW_WORKTREE_MUTATED,
    CODEX_TIMEOUT,
)
from cw.codex_review import _roles as codex_roles
from cw.executor_diagnostics import ExecutorFailure, diagnostics_bundle_dir
from cw.review_findings import ReviewerFindingsDocument
from tests._claude_review_helpers import (
    auth_failure_envelope,
    error_envelope,
    non_document_envelope,
    review_envelope,
    truncated_envelope,
    usage_envelope,
)
from tests._codex_review_helpers import _Clock, _doc_json, _finding_payload
from tests._review_convergence_helpers import structurally_invalid_doc_json
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.claude_review import ClaudeRolesOutcome
    from cw.review_findings import ReviewerRunMetrics

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

    def test_document_persist_failure_is_logged_as_claude_and_never_blocks(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#2808: the shared persist helper names the Claude backend in its log."""

        def _boom(*_a: object, **_k: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_review._roles.atomic_write_text", _boom)
        runner = FakeClaudeRunner(stdout=review_envelope(_doc_json(role=_ROLE)))
        with caplog.at_level(logging.WARNING):
            outcome = _run(wt, runner)
        assert [d.reviewer_role for d in outcome.documents] == [_ROLE]
        assert outcome.failures == []
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "claude review role" in m and "document persist failed" in m
            for m in messages
        )
        assert not any("codex review role" in m for m in messages)


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

    def test_metrics_carry_duration_and_envelope_usage(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.claude_review._roles.time.monotonic", _Clock([100.0, 107.5])
        )
        outcome = _run(wt, FakeClaudeRunner(stdout=review_envelope()))
        metrics = outcome.metrics_by_role["Code Quality Reviewer"]
        assert metrics["duration_seconds"] == pytest.approx(7.5)
        # input = 4 base + 8140 cache creation + 8040 cache read.
        assert metrics["input_tokens"] == 16184
        assert metrics["cached_input_tokens"] == 8040
        assert metrics["output_tokens"] == 331
        assert metrics["reasoning_tokens"] == 0
        assert metrics["effective_model"] == "claude-sonnet-5-5"
        assert metrics["thread_id"] is None
        assert metrics["terminal_event"] is None
        assert metrics["tool_call_counts"] == {}

    def test_budget_helpers_are_shared_with_the_codex_lane(self) -> None:
        """#2817: one deadline/budget implementation, never a re-duplicated copy."""
        assert claude_roles._role_budget is codex_roles._role_budget
        assert claude_roles._role_deadline is codex_roles._role_deadline


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


_USAGE_LOGGER = "cw.claude_review._roles"


def _metrics(wt: Path, stdout: str) -> ReviewerRunMetrics:
    outcome = _run(wt, FakeClaudeRunner(stdout=stdout))
    return outcome.metrics_by_role[_ROLE]


def _usage(envelope: dict[str, object]) -> dict[str, Any]:
    usage = envelope["usage"]
    assert isinstance(usage, dict)
    return usage


class TestEnvelopeUsage:
    """#3063: the confirm lane records tokens and model on the run metrics."""

    def test_a_complete_usage_block_does_not_warn(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_USAGE_LOGGER):
            _metrics(wt, review_envelope())

        assert not [m for m in caplog.messages if "usage absent" in m]

    def test_a_missing_usage_block_warns_with_role_and_session(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stdout = usage_envelope(lambda e: e.pop("usage"))

        with caplog.at_level(logging.WARNING, logger=_USAGE_LOGGER):
            metrics = _metrics(wt, stdout)

        assert metrics["input_tokens"] is None
        assert metrics["output_tokens"] is None
        (message,) = [m for m in caplog.messages if "usage absent" in m]
        assert f"role={_ROLE!r}" in message
        assert f"session={_SESSION!r}" in message

    def test_a_non_dict_usage_is_absent(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stdout = usage_envelope(lambda e: e.update(usage=7))

        with caplog.at_level(logging.WARNING, logger=_USAGE_LOGGER):
            metrics = _metrics(wt, stdout)

        assert metrics["input_tokens"] is None
        assert [m for m in caplog.messages if "usage absent" in m]

    def test_bool_and_str_token_values_are_none(self, wt: Path) -> None:
        def mutate(envelope: dict[str, object]) -> None:
            usage = _usage(envelope)
            usage["input_tokens"] = True
            usage["output_tokens"] = "331"
            usage["cache_read_input_tokens"] = False

        metrics = _metrics(wt, usage_envelope(mutate))

        assert metrics["input_tokens"] is None
        assert metrics["output_tokens"] is None
        assert metrics["cached_input_tokens"] is None

    def test_missing_cache_fields_count_as_zero(self, wt: Path) -> None:
        def mutate(envelope: dict[str, object]) -> None:
            usage = _usage(envelope)
            del usage["cache_read_input_tokens"]
            del usage["cache_creation_input_tokens"]

        metrics = _metrics(wt, usage_envelope(mutate))

        assert metrics["input_tokens"] == 4
        assert metrics["cached_input_tokens"] is None
        assert metrics["output_tokens"] == 331

    def test_missing_output_tokens_details_leaves_reasoning_none(
        self, wt: Path
    ) -> None:
        stdout = usage_envelope(lambda e: _usage(e).pop("output_tokens_details"))

        assert _metrics(wt, stdout)["reasoning_tokens"] is None

    def test_thinking_tokens_are_reasoning(self, wt: Path) -> None:
        def mutate(envelope: dict[str, object]) -> None:
            _usage(envelope)["output_tokens_details"] = {"thinking_tokens": 12}

        assert _metrics(wt, usage_envelope(mutate))["reasoning_tokens"] == 12

    def test_two_model_usage_keys_leave_the_model_none(self, wt: Path) -> None:
        def mutate(envelope: dict[str, object]) -> None:
            model_usage = envelope["modelUsage"]
            assert isinstance(model_usage, dict)
            model_usage["claude-haiku-5-5"] = {}

        assert _metrics(wt, usage_envelope(mutate))["effective_model"] is None

    @pytest.mark.parametrize("model_usage", [None, [], {}, "x"])
    def test_absent_or_non_dict_model_usage_leaves_the_model_none(
        self, wt: Path, model_usage: object
    ) -> None:
        stdout = usage_envelope(lambda e: e.update(modelUsage=model_usage))

        assert _metrics(wt, stdout)["effective_model"] is None

    def test_an_error_envelope_keeps_its_tokens(self, wt: Path) -> None:
        stdout = usage_envelope(lambda e: e.update(is_error=True))
        outcome = _run(wt, FakeClaudeRunner(stdout=stdout))

        assert outcome.failures[0].reason == CODEX_ERROR
        assert outcome.metrics_by_role[_ROLE]["input_tokens"] == 16184

    def test_a_mutated_worktree_role_keeps_its_tokens(self, wt: Path) -> None:
        runner = FakeClaudeRunner(stdout=review_envelope(), side_effect=_write_file)

        outcome = _run(wt, runner)

        assert outcome.mutation is not None
        assert outcome.metrics_by_role[_ROLE]["output_tokens"] == 331

    def test_a_process_failure_has_none_tokens_and_no_usage_warning(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = FakeClaudeRunner(returncode=1, stdout="", stderr="boom")

        with caplog.at_level(logging.WARNING, logger=_USAGE_LOGGER):
            outcome = _run(wt, runner)

        assert outcome.metrics_by_role[_ROLE]["input_tokens"] is None
        assert not [m for m in caplog.messages if "usage absent" in m]

    def test_cost_is_still_logged_and_never_persisted(
        self, wt: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_USAGE_LOGGER):
            metrics = _metrics(wt, review_envelope())

        assert "total_cost_usd=0.036682" in caplog.text
        assert "cost_usd" not in metrics
