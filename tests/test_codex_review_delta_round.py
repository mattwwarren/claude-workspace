"""The conflict-resolution round's review input (#2980).

``_prepare_review_pass(resolution=...)`` reviews only what the resolver did:
the diff from the tree ``git merge-tree`` writes for the merge's parents
(conflict markers left in) to the merge commit's own tree. Every prompt shows
the branch's own, already-reviewed hunks in the touched files as context. The
shard planner counts that context, so an oversized input fails loud and is
never truncated. Every test builds its :class:`ResolutionSpec` directly over a
real repository with a resolved merge; no ledger is involved.
"""

from __future__ import annotations

import hashlib
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
from cw.merge_probe import merge_tree
from cw.review_convergence import ResolutionSpec
from cw.review_findings import CapturedDiff
from tests import _git_sandbox
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

#: Two of the four files conflict; the other two changed on main only.
_RESOLVED = ("pkg_a/a1.py", "pkg_b/b1.py")
_MAIN_ONLY = ("pkg_a/a2.py", "pkg_c/c1.py")
#: The line main edits cleanly in every file, away from the conflict on line 0.
_MAIN_LINE = 10
#: The branch edits every line from here on in its two files: a large context.
_BRANCH_FROM = 30


class _Resolved(NamedTuple):
    repo: Path
    spec: ResolutionSpec


def _tag(relpath: str) -> str:
    return relpath.replace("/", "_").replace(".", "_")


def _edited(relpath: str, lines: int, edits: dict[int, str]) -> str:
    body = _stem_content(relpath, lines).splitlines(keepends=True)
    for index, text in edits.items():
        body[index] = text
    return "".join(body)


def _branch_edits(relpath: str, lines: int) -> dict[int, str]:
    edits = {0: f"branch_{_tag(relpath)} = 0\n"}
    edits.update(
        {
            n: f"branch_ctx_{_tag(relpath)}_{n} = {n}\n"
            for n in range(_BRANCH_FROM, lines)
        }
    )
    return edits


def _resolved_repo(
    make_git_repo: Callable[[str], Path], name: str, *, lines: int = 40
) -> _Resolved:
    """Main adds four files; the branch edits two; main edits all four; resolve."""
    repo = make_git_repo(name)
    for relpath in _SHARD_FILES:
        commit_tracked_file(repo, relpath, _stem_content(relpath, lines))
    git_in(repo, "checkout", "-b", "feature")
    for relpath in _RESOLVED:
        commit_tracked_file(
            repo, relpath, _edited(relpath, lines, _branch_edits(relpath, lines))
        )
    git_in(repo, "checkout", "main")
    for relpath in _SHARD_FILES:
        edits = {_MAIN_LINE: "main_edit = 1\n"}
        if relpath in _RESOLVED:
            edits[0] = f"main_{_tag(relpath)} = 0\n"
        commit_tracked_file(repo, relpath, _edited(relpath, lines, edits))
    git_in(repo, "checkout", "feature")
    _merge_and_resolve(repo, lines)
    merge = git_in(repo, "rev-parse", "HEAD")
    written = merge_tree(repo, f"{merge}^1", f"{merge}^2")
    assert written.status == "conflict"
    assert written.tree is not None
    spec = ResolutionSpec(
        merge=merge,
        conflicted_tree=written.tree,
        resolved_tree=git_in(repo, "rev-parse", f"{merge}^{{tree}}"),
        reviewed_sha=merge,
        branch_base=git_in(repo, "merge-base", f"{merge}^1", f"{merge}^2"),
        branch_tip=git_in(repo, "rev-parse", f"{merge}^1"),
        paths=_RESOLVED,
    )
    return _Resolved(repo, spec)


def _merge_and_resolve(repo: Path, lines: int) -> None:
    """Merge main, stop on the two conflicts, resolve each to its own line."""
    stopped = _git_sandbox.run(
        "merge", "--no-edit", "main", cwd=str(repo), text=False, check=False
    )
    assert stopped.returncode != 0
    for relpath in _RESOLVED:
        edits = _branch_edits(relpath, lines)
        edits[0] = f"resolved_{_tag(relpath)} = 0\n"
        edits[_MAIN_LINE] = "main_edit = 1\n"
        (repo / relpath).write_text(_edited(relpath, lines, edits), encoding="utf-8")
        git_in(repo, "add", relpath)
    git_in(repo, "commit", "--no-edit")


def _prepared(resolved: _Resolved, session_id: str) -> _ReviewPassInputs:
    return _prepare_review_pass(
        _task(),
        resolved.repo,
        "main",
        config_branch="main",
        runner=FakeCodexRunner(),
        session_id=session_id,
        resolution=resolved.spec,
    )


class TestPrepareReviewPass:
    def test_the_diff_is_exactly_the_tree_diff(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "tree-diff")
        spec = resolved.spec

        prepared = _prepared(resolved, "resolution-diff")

        expected = git_in(
            resolved.repo,
            "diff",
            "--no-color",
            "--no-renames",
            spec.conflicted_tree,
            spec.resolved_tree,
        )
        assert prepared.diff.text.strip() == expected
        assert prepared.reviewed_sha == spec.reviewed_sha
        assert sorted(prepared.changed_files) == sorted(_RESOLVED)
        assert set(split_diff_by_file(prepared.diff.text)) == set(_RESOLVED)
        for relpath in _MAIN_ONLY:
            assert relpath not in prepared.diff.text
        assert "main_edit = 1" not in prepared.diff.text
        assert "branch_ctx_" not in prepared.diff.text
        assert "resolved_pkg_a_a1_py = 0" in prepared.diff.text

    def test_the_branch_hunks_are_the_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "context")

        prepared = _prepared(resolved, "resolution-context")

        assert set(prepared.branch_context) == set(_RESOLVED)
        for relpath in _RESOLVED:
            context = prepared.branch_context[relpath]
            assert f"branch_ctx_{_tag(relpath)}_{_BRANCH_FROM}" in context
            assert "main_edit" not in context

    def test_every_prompt_carries_the_instructions_then_the_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "prompt")

        prepared = _prepared(resolved, "resolution-prompt")

        for prompt in prepared.prompts_by_role.values():
            diff_at = prompt.index("## Diff\n")
            instructions_at = prompt.index(_MERGE_DELTA_INSTRUCTIONS)
            context_at = prompt.index(_BRANCH_CONTEXT_HEADER)
            assert diff_at < instructions_at < context_at
            for relpath in _RESOLVED:
                assert f"### {relpath}\n" in prompt

    def test_a_prompt_names_only_its_own_files_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "own-files")
        prepared = _prepared(resolved, "resolution-own-files")
        only = _RESOLVED[0]

        prompts = prepared.render_prompts(CapturedDiff(text=""), [only])

        for prompt in prompts.values():
            assert f"### {only}\n" in prompt
            assert f"### {_RESOLVED[1]}\n" not in prompt

    def test_a_resolution_and_a_fix_loop_delta_are_mutually_exclusive(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "exclusive")
        runner = FakeCodexRunner()

        with pytest.raises(ValueError, match="mutually exclusive"):
            _prepare_review_pass(
                _task(),
                resolved.repo,
                "main",
                config_branch="main",
                runner=runner,
                session_id="resolution-exclusive",
                delta_from_sha=resolved.spec.branch_tip,
                resolution=resolved.spec,
            )

        assert runner.calls == []

    def test_no_spec_leaves_no_context(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "no-spec")

        prepared = _prepare_review_pass(
            _task(),
            resolved.repo,
            "main",
            config_branch="main",
            runner=FakeCodexRunner(),
            session_id="resolution-no-spec",
        )

        assert dict(prepared.branch_context) == {}
        for prompt in prepared.prompts_by_role.values():
            assert _BRANCH_CONTEXT_HEADER not in prompt


def _prompt(
    changed: list[str],
    branch_context: dict[str, str] | None | object = ...,
    *,
    backend: str = "codex",
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
        "backend": backend,
    }
    if branch_context is not ...:
        kwargs["branch_context"] = branch_context
    return _build_reviewer_prompt("Code Quality Reviewer", **kwargs)


#: ``(UTF-8 byte length, sha256)`` of :func:`_prompt` ``(["x.py"])`` as
#: rendered by the code BEFORE #2980 (captured from e7ff0bd8 with
#: ``PYTHONPATH`` at that tree's ``src``), per backend. Re-capture only on an
#: intended prompt change.
_GOLDEN = {
    "codex": (
        3395,
        "92f82ddfbd76f407280dd2592ce3c3af84589229709db554c8085668df9907b6",
    ),
    "claude": (
        3918,
        "dd90035311b0b296ecb49a04c355da1ecfbecc1c80f3141dade966037b2c17c1",
    ),
}


def _digest(text: str) -> tuple[int, str]:
    """``(byte length, sha256)`` of *text*'s UTF-8 encoding."""
    encoded = text.encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


class TestPromptRegression:
    @pytest.mark.parametrize("backend", sorted(_GOLDEN))
    def test_a_prompt_without_context_matches_the_pre_change_golden(
        self, backend: str
    ) -> None:
        assert _digest(_prompt(["x.py"], backend=backend)) == _GOLDEN[backend]

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
        resolved = _resolved_repo(make_git_repo, "shards", lines=60)
        prepared = _prepared(resolved, "resolution-shards")
        overhead, costs = _shard_limit_inputs(prepared)
        extra = {
            path: branch_context_cost(path, text)
            for path, text in prepared.branch_context.items()
        }
        biggest = max(costs[path] + extra[path] for path in costs)
        limit = overhead + branch_context_overhead() + biggest
        assert max(len(p) for p in prepared.prompts_by_role.values()) > limit

        plans = plan_prompt_shards(prepared, limit)

        assert len(plans) == len(_RESOLVED)
        for plan in plans:
            assert plan.prompt_chars <= limit
            for prompt in plan.prompts_by_role.values():
                assert _prompt_files(prompt) == list(plan.files)
                (own,) = plan.files
                assert f"### {own}\n" in prompt
                others = [path for path in _RESOLVED if path != own]
                assert all(f"### {path}\n" not in prompt for path in others)

    def test_an_input_the_context_pushes_over_fails_loud(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A limit that fits the chunks alone but not with their context."""
        resolved = _resolved_repo(make_git_repo, "too-large", lines=60)
        prepared = _prepared(resolved, "resolution-too-large")
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


class TestClaudePromptLimit:
    """The Claude path cannot shard, so an oversized resolution prompt fails loud."""

    @staticmethod
    def _longest(resolved: _Resolved) -> int:
        prepared = _prepare_review_pass(
            _task(),
            resolved.repo,
            "main",
            config_branch="main",
            runner=None,
            session_id="resolution-claude-limit",
            capability=_CLAUDE_CAPABILITY,
            backend="claude",
            resolution=resolved.spec,
        )
        for prompt in prepared.prompts_by_role.values():
            assert _prompt_files(prompt) == list(_RESOLVED)
            assert _BRANCH_CONTEXT_HEADER in prompt
        return max(len(prompt) for prompt in prepared.prompts_by_role.values())

    @staticmethod
    def _review(
        resolved: _Resolved, runner: FakeClaudeRunner, limit: int | None
    ) -> object:
        return run_claude_review(
            runner=runner,
            task=_task(),
            worktree=resolved.repo,
            default_branch="main",
            base_ref=None,
            model=None,
            wall_clock_budget_seconds=None,
            session_id="resolution-claude-limit",
            resolution=resolved.spec,
            max_prompt_chars=limit,
        )

    def test_a_prompt_over_the_limit_raises_before_any_call(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "claude-over")
        longest = self._longest(resolved)
        runner = FakeClaudeRunner()

        with pytest.raises(ReviewInputTooLargeError) as raised:
            self._review(resolved, runner, longest - 1)

        assert raised.value.actual_chars == longest
        assert raised.value.max_chars == longest - 1
        assert raised.value.cause == "input_too_large"
        assert runner.calls == []

    def test_a_prompt_at_the_limit_runs(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        resolved = _resolved_repo(make_git_repo, "claude-at")
        runner = FakeClaudeRunner()

        self._review(resolved, runner, self._longest(resolved))

        assert runner.calls

    def test_no_limit_never_raises(self, make_git_repo: Callable[[str], Path]) -> None:
        resolved = _resolved_repo(make_git_repo, "claude-none")
        runner = FakeClaudeRunner()

        self._review(resolved, runner, None)

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


_SPEC = ResolutionSpec(
    merge="m",
    conflicted_tree="t1",
    resolved_tree="t2",
    reviewed_sha="h",
    branch_base="b",
    branch_tip="c",
    paths=("a.py",),
)


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
                resolution=_SPEC,
            )
        assert seen["resolution"] == _SPEC
        assert seen["config_branch"] == "main"

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
                resolution=_SPEC,
            )
        assert seen["resolution"] == _SPEC
        # #3038: the real name travels separately from the pinned diff base.
        assert seen["config_branch"] == "main"
        assert seen["backend"] == "claude"
