"""The merge delta confirm round's review input (#2918).

``_prepare_review_pass(merge_delta=...)`` reviews only what a default-branch
merge brought into the files it overlaps (``from_sha..to_sha`` limited to those
paths) and shows the branch's own, already-reviewed hunks in the same files as
context. The shard planner counts that context, so an oversized input fails
loud and is never truncated. Every test builds its :class:`DeltaSpec` directly
over a real repository with a merge; no ledger is involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

import pytest

from cw.claude_review import FakeClaudeRunner, run_claude_review
from cw.claude_review.core import _CLAUDE_CAPABILITY
from cw.codex_review import _prepare_review_pass, run_review
from cw.codex_review._context._prompt_render import (
    _build_reviewer_prompt,
    branch_context_cost,
    branch_context_overhead,
)
from cw.codex_review._context._prompt_text import (
    _BRANCH_CONTEXT_HEADER,
    _MERGE_DELTA_INSTRUCTIONS,
)
from cw.codex_review._shard import (
    ReviewInputTooLargeError,
    plan_prompt_shards,
    plan_shards,
    split_diff_by_file,
)
from cw.codex_runner import FakeCodexRunner
from cw.review_convergence import DeltaSpec
from cw.review_findings import CapturedDiff
from tests._codex_review_helpers import (
    _SHARD_FILES,
    _prompt_files,
    _shard_limit_inputs,
    _stem_content,
    _task,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_review import _ReviewPassInputs

#: Two of the four files overlap; the other two changed on main only.
_OVERLAP = ("pkg_a/a1.py", "pkg_b/b1.py")
_MAIN_ONLY = ("pkg_a/a2.py", "pkg_c/c1.py")


class _Merged(NamedTuple):
    repo: Path
    spec: DeltaSpec


def _branch_line(relpath: str) -> str:
    return f"branch_edit_{relpath.replace('/', '_').replace('.', '_')} = 1\n"


def _merged_repo(
    make_git_repo: Callable[[str], Path], name: str, *, lines: int = 20
) -> _Merged:
    """Main adds four files; the branch edits two; main edits all four; merge."""
    repo = make_git_repo(name)
    for relpath in _SHARD_FILES:
        commit_tracked_file(repo, relpath, _stem_content(relpath, lines))
    git_in(repo, "checkout", "-b", "feature")
    for relpath in _OVERLAP:
        body = _stem_content(relpath, lines).splitlines(keepends=True)
        commit_tracked_file(repo, relpath, "".join([_branch_line(relpath), *body[1:]]))
    clean = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "main")
    for relpath in _SHARD_FILES:
        body = _stem_content(relpath, lines).splitlines(keepends=True)
        commit_tracked_file(repo, relpath, "".join([*body[:-1], "main_edit = 1\n"]))
    git_in(repo, "checkout", "feature")
    git_in(repo, "merge", "--no-edit", "main")
    head = git_in(repo, "rev-parse", "HEAD")
    base = git_in(repo, "merge-base", clean, "main")
    spec = DeltaSpec(from_sha=clean, to_sha=head, paths=_OVERLAP, branch_base=base)
    return _Merged(repo, spec)


def _prepared(merged: _Merged, session_id: str) -> _ReviewPassInputs:
    return _prepare_review_pass(
        _task(),
        merged.repo,
        "main",
        runner=FakeCodexRunner(),
        session_id=session_id,
        merge_delta=merged.spec,
    )


class TestPrepareReviewPass:
    def test_the_diff_is_limited_to_the_overlap_at_head(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "limited")

        prepared = _prepared(merged, "delta-limited")

        assert prepared.reviewed_sha == merged.spec.to_sha
        assert prepared.reviewed_sha == git_in(merged.repo, "rev-parse", "HEAD")
        assert sorted(prepared.changed_files) == sorted(_OVERLAP)
        assert set(split_diff_by_file(prepared.diff.text)) == set(_OVERLAP)
        for relpath in _MAIN_ONLY:
            assert relpath not in prepared.diff.text
        assert "main_edit = 1" in prepared.diff.text
        assert _branch_line(_OVERLAP[0]).strip() not in prepared.diff.text

    def test_the_branch_hunks_are_the_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "context")

        prepared = _prepared(merged, "delta-context")

        assert set(prepared.branch_context) == set(_OVERLAP)
        for relpath in _OVERLAP:
            assert _branch_line(relpath).strip() in prepared.branch_context[relpath]
            assert "main_edit" not in prepared.branch_context[relpath]

    def test_every_prompt_carries_the_section_and_instructions(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "prompt")

        prepared = _prepared(merged, "delta-prompt")

        for prompt in prepared.prompts_by_role.values():
            assert _MERGE_DELTA_INSTRUCTIONS in prompt
            assert _BRANCH_CONTEXT_HEADER in prompt
            for relpath in _OVERLAP:
                assert f"### {relpath}\n" in prompt

    def test_a_prompt_names_only_its_own_files_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "own-files")
        prepared = _prepared(merged, "delta-own-files")
        only = _OVERLAP[0]

        prompts = prepared.render_prompts(CapturedDiff(text=""), [only])

        for prompt in prompts.values():
            assert f"### {only}\n" in prompt
            assert f"### {_OVERLAP[1]}\n" not in prompt

    def test_a_merge_delta_and_a_fix_loop_delta_are_mutually_exclusive(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Both would render the merge diff under the fix-loop re-review text."""
        merged = _merged_repo(make_git_repo, "exclusive")
        runner = FakeCodexRunner()

        with pytest.raises(ValueError, match="mutually exclusive"):
            _prepare_review_pass(
                _task(),
                merged.repo,
                "main",
                runner=runner,
                session_id="delta-exclusive",
                delta_from_sha=merged.spec.from_sha,
                merge_delta=merged.spec,
            )

        assert runner.calls == []

    def test_no_spec_leaves_no_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "no-spec")

        prepared = _prepare_review_pass(
            _task(),
            merged.repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="delta-no-spec",
        )

        assert dict(prepared.branch_context) == {}
        for prompt in prepared.prompts_by_role.values():
            assert _BRANCH_CONTEXT_HEADER not in prompt


def _prompt(
    changed: list[str], branch_context: dict[str, str] | None = ..., **_: object
) -> str:
    """One role prompt over a one-file diff; *branch_context* omitted by default."""
    kwargs: dict[str, Any] = {
        "agent_spec_text": "spec",
        "diff": CapturedDiff(text="diff --git a/x.py b/x.py\n"),
        "changed_files": changed,
        "plan_text": None,
        "ticket_text": None,
        "project_rubrics": None,
        "repo_policy_section": None,
        "sensitive_hits": [],
    }
    if branch_context is not ...:
        kwargs["branch_context"] = branch_context
    return _build_reviewer_prompt("Code Quality Reviewer", **kwargs)


class TestPromptRegression:
    def test_a_prompt_without_context_is_byte_identical(self) -> None:
        baseline = _prompt(["x.py"])
        assert _prompt(["x.py"], None) == baseline
        assert _prompt(["x.py"], {}) == baseline
        assert _prompt(["x.py"], {"other.py": "@@ hunk"}) == baseline
        assert _BRANCH_CONTEXT_HEADER not in baseline

    def test_context_is_rendered_after_the_diff(self) -> None:
        prompt = _prompt(["x.py"], {"x.py": "@@ -1 +1 @@\n-a\n+b"})
        diff_at = prompt.index("## Diff\n")
        assert prompt.index(_MERGE_DELTA_INSTRUCTIONS) > diff_at
        assert prompt.index(f"{_BRANCH_CONTEXT_HEADER}\n\n### x.py\n@@") > diff_at

    def test_the_section_costs_what_the_planner_counts(self) -> None:
        text = "@@ -1 +1 @@\n-a\n+b"
        without = _prompt(["x.py"])
        with_context = _prompt(["x.py"], {"x.py": text})
        assert len(with_context) - len(without) == (
            branch_context_overhead() + branch_context_cost("x.py", text)
        )


class TestSharding:
    def test_shard_planning_counts_the_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "shards", lines=60)
        prepared = _prepared(merged, "delta-shards")
        overhead, costs = _shard_limit_inputs(prepared)
        extra = {
            path: branch_context_cost(path, text)
            for path, text in prepared.branch_context.items()
        }
        biggest = max(costs[path] + extra[path] for path in costs)
        limit = overhead + branch_context_overhead() + biggest
        assert max(len(p) for p in prepared.prompts_by_role.values()) > limit

        plans = plan_prompt_shards(prepared, limit)

        assert len(plans) == len(_OVERLAP)
        for plan in plans:
            assert plan.prompt_chars <= limit
            for prompt in plan.prompts_by_role.values():
                assert _prompt_files(prompt) == list(plan.files)
                (own,) = plan.files
                assert f"### {own}\n" in prompt
                others = [path for path in _OVERLAP if path != own]
                assert all(f"### {path}\n" not in prompt for path in others)

    def test_an_input_the_context_pushes_over_fails_loud(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A limit that fits the chunks alone but not with their context."""
        merged = _merged_repo(make_git_repo, "too-large", lines=60)
        prepared = _prepared(merged, "delta-too-large")
        overhead, costs = _shard_limit_inputs(prepared)
        limit = overhead + branch_context_overhead() + max(costs.values())

        with pytest.raises(ReviewInputTooLargeError) as raised:
            plan_prompt_shards(prepared, limit)

        assert raised.value.cause == "file_too_large"

    def test_plan_shards_without_extra_costs_is_unchanged(self) -> None:
        chunks = {"a/x.py": "x" * 10, "b/y.py": "y" * 10}
        assert plan_shards(chunks, overhead=0, max_chars=100) == plan_shards(
            chunks, overhead=0, max_chars=100, extra_costs={}
        )
        with pytest.raises(ReviewInputTooLargeError):
            plan_shards(chunks, overhead=0, max_chars=20, extra_costs={"a/x.py": 50})


class TestClaudePath:
    def test_the_claude_prompt_is_unsharded_and_bounded(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "claude")

        prepared = _prepare_review_pass(
            _task(),
            merged.repo,
            "main",
            runner=None,
            session_id="delta-claude",
            capability=_CLAUDE_CAPABILITY,
            backend="claude",
            merge_delta=merged.spec,
        )

        for prompt in prepared.prompts_by_role.values():
            assert _prompt_files(prompt) == list(_OVERLAP)
            assert _BRANCH_CONTEXT_HEADER in prompt
            assert all(path not in prompt for path in _MAIN_ONLY)


class TestClaudePromptLimit:
    """The Claude path cannot shard, so an oversized delta prompt fails loud."""

    @staticmethod
    def _longest(merged: _Merged) -> int:
        prepared = _prepare_review_pass(
            _task(),
            merged.repo,
            "main",
            runner=None,
            session_id="delta-claude-limit",
            capability=_CLAUDE_CAPABILITY,
            backend="claude",
            merge_delta=merged.spec,
        )
        return max(len(prompt) for prompt in prepared.prompts_by_role.values())

    @staticmethod
    def _review(merged: _Merged, runner: FakeClaudeRunner, limit: int | None) -> object:
        return run_claude_review(
            runner=runner,
            task=_task(),
            worktree=merged.repo,
            default_branch="main",
            base_ref=None,
            model=None,
            wall_clock_budget_seconds=None,
            session_id="delta-claude-limit",
            merge_delta=merged.spec,
            max_prompt_chars=limit,
        )

    def test_a_prompt_over_the_limit_raises_before_any_call(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "claude-over")
        longest = self._longest(merged)
        runner = FakeClaudeRunner()

        with pytest.raises(ReviewInputTooLargeError) as raised:
            self._review(merged, runner, longest - 1)

        assert raised.value.actual_chars == longest
        assert raised.value.max_chars == longest - 1
        assert raised.value.cause == "input_too_large"
        assert runner.calls == []

    def test_a_prompt_at_the_limit_runs(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        merged = _merged_repo(make_git_repo, "claude-at")
        runner = FakeClaudeRunner()

        self._review(merged, runner, self._longest(merged))

        assert runner.calls

    def test_no_limit_never_raises(self, make_git_repo: Callable[[str], Path]) -> None:
        merged = _merged_repo(make_git_repo, "claude-none")
        runner = FakeClaudeRunner()

        self._review(merged, runner, None)

        assert runner.calls


class _PreparedError(Exception):
    """Raised by the spy after it captured ``_prepare_review_pass``'s arguments."""


def _spy_prepare(monkeypatch: pytest.MonkeyPatch, module: str) -> dict[str, object]:
    seen: dict[str, object] = {}

    def _spy(*_args: object, **kwargs: object) -> NoReturn:
        seen.update(kwargs)
        raise _PreparedError

    monkeypatch.setattr(f"{module}._prepare_review_pass", _spy)
    return seen


_SPEC = DeltaSpec(from_sha="c", to_sha="h", paths=("a.py",), branch_base="b")


class TestForwarding:
    def test_run_review_forwards_the_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _spy_prepare(monkeypatch, "cw.codex_review.core")
        with pytest.raises(_PreparedError):
            run_review(
                runner=FakeCodexRunner(),
                task=_task(),
                worktree=tmp_path,
                default_branch="main",
                model=None,
                reasoning_effort=None,
                wall_clock_budget_seconds=None,
                session_id="fwd",
                fix_loop_enabled=False,
                merge_delta=_SPEC,
            )
        assert seen["merge_delta"] == _SPEC

    def test_run_claude_review_forwards_the_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _spy_prepare(monkeypatch, "cw.claude_review.core")
        with pytest.raises(_PreparedError):
            run_claude_review(
                runner=FakeClaudeRunner(),
                task=_task(),
                worktree=tmp_path,
                default_branch="main",
                base_ref="base",
                model=None,
                wall_clock_budget_seconds=None,
                session_id="fwd",
                merge_delta=_SPEC,
            )
        assert seen["merge_delta"] == _SPEC
        assert seen["backend"] == "claude"
