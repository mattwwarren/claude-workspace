"""Tests for cw.codex_fix_loop.baseline — the per-cycle clean-start baseline (#2633).

Every capture test also asserts that capture never stages: the index and the
unmerged entries are the same afterwards as before.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import codex_background
from cw.codex_fix_loop.baseline import (
    _CW_REVIEW_ARTIFACTS,
    CycleBaseline,
    DirtyStart,
    capture_cycle_baseline,
    cycle_diff,
    cycle_touched_paths,
)
from cw.codex_review import _parse_unified_diff
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH
from tests._codex_review_helpers import (
    _head_baseline,
    _seed_conflicting_cherry_pick,
    _stage_merge_from_other_branch,
    _write,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ARTIFACTS = (
    ".claude/review-verdict.md",
    ".claude/review-verdict.json",
    ".claude/review-verdict-unparseable.md",
)


def _repo(make_git_repo: Callable[..., Path], name: str = "bl") -> Path:
    """A repo on ``feature`` with ``a.py`` committed on ``main``."""
    repo = make_git_repo(name)
    _write(repo / "a.py", "a = 1\n")
    git_in(repo, "add", "a.py")
    git_in(repo, "commit", "-m", "a")
    git_in(repo, "checkout", "-b", "feature")
    return repo


def _index_state(repo: Path) -> tuple[str, str]:
    """The staged diff and the unmerged entries, to prove capture never stages."""
    return (
        git_in(repo, "diff", "--cached", "--name-status"),
        git_in(repo, "ls-files", "-u"),
    )


def _capture_unchanged(repo: Path) -> CycleBaseline | DirtyStart:
    before = _index_state(repo)
    result = capture_cycle_baseline(repo)
    assert _index_state(repo) == before
    return result


def _dirty(repo: Path) -> DirtyStart:
    result = _capture_unchanged(repo)
    assert isinstance(result, DirtyStart)
    return result


class TestCaptureCycleBaseline:
    def test_records_head_and_tree_of_clean_worktree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)

        result = _capture_unchanged(repo)

        assert isinstance(result, CycleBaseline)
        assert result.head_sha == git_in(repo, "rev-parse", "HEAD")
        # Clean-start invariant: the baseline tree IS HEAD's tree.
        assert result.tree_sha == git_in(repo, "rev-parse", "HEAD^{tree}")

    def test_prestaged_changes_are_refused(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "staged.py", "s = 1\n")
        git_in(repo, "add", "staged.py")

        start = _dirty(repo)

        assert start.markers == ()
        assert start.unmerged == ()
        assert any(line.endswith("staged.py") for line in start.dirty)
        assert "staged.py" in git_in(repo, "diff", "--cached", "--name-only")

    def test_unstaged_and_untracked_changes_are_refused(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "a.py", "a = 2\n")
        assert any(line.endswith("a.py") for line in _dirty(repo).dirty)

        git_in(repo, "checkout", "--", "a.py")
        _write(repo / "newdir" / "b.py", "b = 1\n")
        # --untracked-files=all: a new directory lists its files.
        assert "?? newdir/b.py" in _dirty(repo).dirty

    def test_unmerged_index_is_refused_without_staging(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _seed_conflicting_cherry_pick(repo)

        start = _dirty(repo)

        assert start.unmerged == ("pyproject.toml",)
        assert start.markers == ("CHERRY_PICK_HEAD",)
        # Regression for the `git add -A` hole: the conflict is still there.
        assert len(git_in(repo, "ls-files", "-u").splitlines()) == 3
        assert "<<<<<<<" in (repo / "pyproject.toml").read_text()

    @pytest.mark.parametrize(
        ("operation", "markers"),
        [
            ("merge", ("MERGE_HEAD",)),
            ("cherry-pick-conflict", ("CHERRY_PICK_HEAD",)),
            ("revert-conflict", ("REVERT_HEAD",)),
            ("squash", ()),
            ("cherry-pick-no-commit", ()),
        ],
    )
    def test_unfinished_operation_markers(
        self,
        make_git_repo: Callable[..., Path],
        operation: str,
        markers: tuple[str, ...],
    ) -> None:
        repo = _repo(make_git_repo)
        if operation in {"merge", "squash"}:
            _stage_merge_from_other_branch(
                repo, {"other.py": "o = 1\n"}, squash=operation == "squash"
            )
        elif operation == "cherry-pick-conflict":
            _seed_conflicting_cherry_pick(repo)
        elif operation == "revert-conflict":
            _write(repo / "a.py", "a = 2\n")
            git_in(repo, "commit", "-am", "two")
            _write(repo / "a.py", "a = 3\n")
            git_in(repo, "commit", "-am", "three")
            with pytest.raises(subprocess.CalledProcessError):
                git_in(repo, "revert", "--no-edit", "HEAD~1")
        else:
            git_in(repo, "checkout", "-b", "side")
            _write(repo / "side.py", "s = 1\n")
            git_in(repo, "add", "side.py")
            git_in(repo, "commit", "-m", "side")
            git_in(repo, "checkout", "feature")
            git_in(repo, "cherry-pick", "-n", "side")

        start = _dirty(repo)

        assert start.markers == markers
        assert start.dirty  # porcelain catches every one of them

    def test_cw_runtime_artifacts_do_not_make_the_tree_dirty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text(".cw/\n", encoding="utf-8")
        _write(repo / ".cw" / "codex_driver.log", "log\n")

        assert isinstance(_capture_unchanged(repo), CycleBaseline)

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    def test_untracked_review_verdict_artifacts_do_not_make_the_tree_dirty(
        self, make_git_repo: Callable[..., Path], artifact: str
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / artifact, "verdict\n")

        assert isinstance(_capture_unchanged(repo), CycleBaseline)

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    def test_modified_tracked_review_verdict_artifact_is_still_dirty(
        self, make_git_repo: Callable[..., Path], artifact: str
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / artifact, "verdict\n")
        git_in(repo, "add", "-f", artifact)
        # A staged-new artifact (`A `) is dirty: the allowlist is untracked-only.
        assert f"A  {artifact}" in _dirty(repo).dirty
        git_in(repo, "commit", "-m", "track the artifact")
        _write(repo / artifact, "edited\n")

        assert any(line.endswith(artifact) for line in _dirty(repo).dirty)

    def test_artifact_allowlist_does_not_hide_other_untracked_files(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / ".claude" / "review-verdict.md", "verdict\n")
        _write(repo / ".claude" / "other.md", "other\n")

        assert _dirty(repo).dirty == ("?? .claude/other.md",)

    def test_git_failure_propagates(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        with pytest.raises(subprocess.CalledProcessError):
            capture_cycle_baseline(not_a_repo)


class TestCwReviewArtifacts:
    def test_allowlist_matches_the_real_constants(self) -> None:
        assert {
            str(codex_background.REVIEW_VERDICT_COMMENT_RELATIVE_PATH),
            str(REVIEW_VERDICT_JSON_RELATIVE_PATH),
            str(codex_background.REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH),
        } == _CW_REVIEW_ARTIFACTS


class TestCycleTouchedPaths:
    def test_edit_counts(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        _write(repo / "a.py", "a = 2\n")

        assert cycle_touched_paths(repo, baseline) == {"a.py"}

    def test_untracked_new_file_counts(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        _write(repo / "pkg" / "new.py", "n = 1\n")

        assert cycle_touched_paths(repo, baseline) == {"pkg/new.py"}

    def test_deleted_file_counts(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        (repo / "a.py").unlink()

        assert cycle_touched_paths(repo, baseline) == {"a.py"}

    def test_rename_reports_both_sides(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        git_in(repo, "mv", "a.py", "b.py")

        assert cycle_touched_paths(repo, baseline) == {"a.py", "b.py"}

    def test_unchanged_cycle_is_empty(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)

        assert cycle_touched_paths(repo, _head_baseline(repo)) == set()

    def test_tracked_review_artifact_edit_is_measured_and_staged(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        artifact = repo / ".claude" / "review-verdict-unparseable.md"
        _write(artifact, "original\n")
        git_in(repo, "add", str(artifact.relative_to(repo)))
        git_in(repo, "commit", "-m", "track review artifact")

        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)
        _write(artifact, "edited\n")

        assert cycle_touched_paths(repo, baseline) == {
            ".claude/review-verdict-unparseable.md"
        }
        assert ".claude/review-verdict-unparseable.md" in git_in(
            repo, "diff", "--cached", "--name-only"
        )

    def test_paths_are_relative_to_the_baseline_tree_not_head(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        # The fix invocation commits its own change: HEAD moves, tree clean.
        _write(repo / "self.py", "s = 1\n")
        git_in(repo, "add", "self.py")
        git_in(repo, "commit", "-m", "fix invocation committed itself")

        assert cycle_touched_paths(repo, baseline) == {"self.py"}


class TestCycleDiff:
    def test_diff_is_relative_to_the_baseline_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        _write(repo / "a.py", "a = 2\n")
        cycle_touched_paths(repo, baseline)  # stages the cycle's changes

        diff = cycle_diff(repo, baseline)

        assert "-a = 1" in diff
        assert "+a = 2" in diff

    def test_diff_is_zero_context(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "a.py", "a = 1\nb = 2\nc = 3\nd = 4\n")
        git_in(repo, "commit", "-am", "four lines")
        baseline = _head_baseline(repo)
        _write(repo / "a.py", "a = 1\nb = 2\nc = 30\nd = 4\n")
        cycle_touched_paths(repo, baseline)

        diff = cycle_diff(repo, baseline)
        file_diffs, file_line_text, _window, changed = _parse_unified_diff(diff)

        assert changed == ["a.py"]
        assert file_line_text["a.py"] == {3: "c = 30"}
        # -U0: no context lines around the one-line change.
        assert not [
            line for line in file_diffs["a.py"].splitlines() if line.startswith(" ")
        ]
