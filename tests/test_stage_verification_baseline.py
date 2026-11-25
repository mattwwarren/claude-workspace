"""Tests for cw.stage_verification.baseline — the per-cycle clean-start baseline
    (#2633).

Every capture test also asserts that capture never stages: the index and the
unmerged entries are the same afterwards as before.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import codex_review_persist
from cw.codex_review import _parse_unified_diff
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH
from cw.stage_verification.baseline import (
    _CW_REVIEW_ARTIFACTS,
    CycleBaseline,
    DirtyStart,
    best_effort_dirty_paths,
    capture_cycle_baseline,
    cycle_diff,
    cycle_touched_paths,
    diff_since,
    stage_cycle,
    staged_cycle_tree,
    unmerged_paths,
)
from tests import _git_sandbox
from tests._codex_review_helpers import (
    _head_baseline,
    _seed_conflicting_cherry_pick,
    _stage_merge_from_other_branch,
    _write,
)
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ARTIFACTS = (
    ".claude/review-verdict.md",
    ".claude/review-verdict.json",
    ".claude/review-verdict-unparseable.md",
)


_KINDS = ("modified", "unstaged-new", "deleted")


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
    @pytest.mark.parametrize("state", ["untracked", "modified", "staged", "both"])
    def test_review_verdict_artifacts_do_not_make_the_tree_dirty(
        self, make_git_repo: Callable[..., Path], artifact: str, state: str
    ) -> None:
        repo = _repo(make_git_repo)
        if state == "untracked":
            _write(repo / artifact, "verdict\n")
        else:
            commit_tracked_file(repo, artifact, "old\n")
            _write(repo / artifact, "rewritten by review\n")
            if state in {"staged", "both"}:
                git_in(repo, "add", artifact)
            if state == "both":
                _write(repo / artifact, "rewritten again\n")

        result = _capture_unchanged(repo)

        assert isinstance(result, CycleBaseline)
        assert result.review_artifacts == frozenset({artifact})

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    def test_staged_new_review_artifact_is_still_dirty(
        self, make_git_repo: Callable[..., Path], artifact: str
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / artifact, "verdict\n")
        git_in(repo, "add", "-f", artifact)

        # A staged-new artifact (`A `) is not in HEAD, so it is real work.
        assert f"A  {artifact}" in _dirty(repo).dirty

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    @pytest.mark.parametrize("change", ["delete", "git-rm", "git-mv"])
    def test_deleted_or_renamed_tracked_review_artifact_is_still_dirty(
        self, make_git_repo: Callable[..., Path], artifact: str, change: str
    ) -> None:
        repo = _repo(make_git_repo)
        commit_tracked_file(repo, artifact, "old\n")
        if change == "delete":
            (repo / artifact).unlink()
        elif change == "git-rm":
            git_in(repo, "rm", artifact)
        else:
            git_in(repo, "mv", artifact, ".claude/moved.md")

        dirty = _dirty(repo).dirty

        assert dirty
        assert any(artifact in line for line in dirty)

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    def test_tracked_artifact_plus_other_modified_file_names_only_the_other(
        self, make_git_repo: Callable[..., Path], artifact: str
    ) -> None:
        repo = _repo(make_git_repo)
        commit_tracked_file(repo, artifact, "old\n")
        _write(repo / artifact, "rewritten by review\n")
        _write(repo / "a.py", "a = 2\n")

        assert _dirty(repo).dirty == (" M a.py",)

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


class TestAdoptedMergeBaseline:
    """``merge_files`` adopts one unconcluded merge and nothing else (#3529)."""

    def _merge(self, repo: Path) -> None:
        _stage_merge_from_other_branch(repo, {"other.py": "o = 1\n"}, squash=False)

    def test_merge_head_is_tolerated_only_when_merge_files_is_given(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        self._merge(repo)
        refused = capture_cycle_baseline(repo)
        assert isinstance(refused, DirtyStart)
        assert refused.markers == ("MERGE_HEAD",)
        baseline = capture_cycle_baseline(repo, merge_files=frozenset({"other.py"}))
        assert isinstance(baseline, CycleBaseline)
        assert baseline.tree_sha == git_in(repo, "rev-parse", "HEAD^{tree}")

    def test_dirty_paths_outside_merge_files_still_refuse(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        self._merge(repo)
        _write(repo / "stray.py", "s = 1\n")
        start = capture_cycle_baseline(repo, merge_files=frozenset({"other.py"}))
        assert isinstance(start, DirtyStart)
        assert start.markers == ()
        assert start.dirty == ("?? stray.py",)

    def test_unmerged_paths_outside_merge_files_still_refuse(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _seed_conflicting_cherry_pick(repo)
        start = capture_cycle_baseline(repo, merge_files=frozenset())
        assert isinstance(start, DirtyStart)
        assert start.unmerged == ("pyproject.toml",)

    def test_unmerged_paths_inside_merge_files_are_tolerated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        git_in(repo, "checkout", "-b", "side", "main")
        _write(repo / "a.py", "a = 'theirs'\n")
        git_in(repo, "commit", "-am", "theirs")
        git_in(repo, "checkout", "feature")
        _write(repo / "a.py", "a = 'mine'\n")
        git_in(repo, "commit", "-am", "mine")
        merged = _git_sandbox.run(
            "merge", "--no-commit", "--no-ff", "side", cwd=repo, check=False
        )
        assert merged.returncode == 1
        assert unmerged_paths(repo) == ("a.py",)
        baseline = capture_cycle_baseline(repo, merge_files=frozenset({"a.py"}))
        assert isinstance(baseline, CycleBaseline)

    def test_the_other_operation_markers_still_refuse(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _seed_conflicting_cherry_pick(repo)
        start = capture_cycle_baseline(repo, merge_files=frozenset({"pyproject.toml"}))
        assert isinstance(start, DirtyStart)
        assert start.markers == ("CHERRY_PICK_HEAD",)

    def test_unmerged_paths_is_public_and_sorted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        assert unmerged_paths(repo) == ()
        _seed_conflicting_cherry_pick(repo)
        assert unmerged_paths(repo) == ("pyproject.toml",)


class TestCwReviewArtifacts:
    def test_allowlist_matches_the_real_constants(self) -> None:
        assert {
            str(codex_review_persist.REVIEW_VERDICT_COMMENT_RELATIVE_PATH),
            str(REVIEW_VERDICT_JSON_RELATIVE_PATH),
            str(codex_review_persist.REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH),
        } == _CW_REVIEW_ARTIFACTS

    def test_allowlist_is_the_three_expected_paths(self) -> None:
        assert {
            ".claude/review-verdict.md",
            ".claude/review-verdict.json",
            ".claude/review-verdict-unparseable.md",
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

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    @pytest.mark.parametrize("state", ["modified", "staged", "both"])
    def test_tracked_artifact_at_start_is_unstaged_and_left_modified(
        self, make_git_repo: Callable[..., Path], artifact: str, state: str
    ) -> None:
        repo = _repo(make_git_repo)
        commit_tracked_file(repo, artifact, "old\n")
        _write(repo / artifact, "rewritten by review\n")
        if state in {"staged", "both"}:
            git_in(repo, "add", artifact)
        if state == "both":
            _write(repo / artifact, "rewritten again\n")
        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)
        _write(repo / "a.py", "a = 2\n")

        assert cycle_touched_paths(repo, baseline) == {"a.py"}
        assert git_in(repo, "diff", "--cached", "--name-only") == "a.py"
        assert artifact in git_in(repo, "diff", "--name-only").split()

    def test_cycle_that_writes_nothing_with_tracked_artifact_is_empty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        commit_tracked_file(repo, _ARTIFACTS[0], "old\n")
        _write(repo / _ARTIFACTS[0], "rewritten by review\n")
        baseline = capture_cycle_baseline(repo)
        assert isinstance(baseline, CycleBaseline)

        assert cycle_touched_paths(repo, baseline) == set()

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

    def test_diff_since_baseline_tree_matches_cycle_diff(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        baseline = _head_baseline(repo)
        _write(repo / "a.py", "a = 2\nb = 3\n")
        cycle_touched_paths(repo, baseline)

        assert diff_since(repo, baseline.tree_sha) == cycle_diff(repo, baseline)

    def test_diff_since_older_commit_covers_committed_and_staged_work(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2648: the loop-wide diff spans earlier cycles' commits plus this one."""
        repo = _repo(make_git_repo)
        start = git_in(repo, "rev-parse", "HEAD")
        _write(repo / "a.py", "a = 1\nearlier = 1\n")
        git_in(repo, "commit", "-am", "earlier cycle")
        baseline = _head_baseline(repo)
        _write(repo / "a.py", "a = 1\nearlier = 1\ncurrent = 1\n")
        cycle_touched_paths(repo, baseline)

        diff = diff_since(repo, start)

        assert "+earlier = 1" in diff
        assert "+current = 1" in diff
        assert "+earlier = 1" not in cycle_diff(repo, baseline)


class TestStagedCycleTree:
    def test_clean_tree_equals_head_tree(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)

        assert staged_cycle_tree(repo, frozenset()) == git_in(
            repo, "rev-parse", "HEAD^{tree}"
        )

    def test_an_edit_changes_it(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        clean = staged_cycle_tree(repo, frozenset())
        _write(repo / "a.py", "a = 2\n")

        assert staged_cycle_tree(repo, frozenset()) != clean

    def test_ignored_files_and_review_artifacts_do_not(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / ".gitignore", ".coverage\n")
        git_in(repo, "add", ".gitignore")
        git_in(repo, "commit", "-m", "ignore")
        clean = staged_cycle_tree(repo, frozenset())
        _write(repo / ".coverage", "data\n")
        _write(repo / _ARTIFACTS[0], "verdict\n")

        assert staged_cycle_tree(repo, frozenset({_ARTIFACTS[0]})) == clean


class TestBestEffortDirtyPaths:
    def test_untracked_modified_and_deleted_paths_are_sorted_and_unique(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "bdp-basic")
        (repo / "keep.py").write_text("k = 1\n", encoding="utf-8")
        (repo / "gone.py").write_text("g = 1\n", encoding="utf-8")
        git_in(repo, "add", "keep.py", "gone.py")
        git_in(repo, "commit", "-m", "add keep and gone")
        (repo / "keep.py").write_text("k = 2\n", encoding="utf-8")
        (repo / "gone.py").unlink()
        (repo / "new.py").write_text("n = 1\n", encoding="utf-8")

        assert best_effort_dirty_paths(repo) == ("gone.py", "keep.py", "new.py")

    def test_staged_rename_yields_both_paths(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "bdp-rename")
        git_in(repo, "mv", "a.py", "b.py")

        assert best_effort_dirty_paths(repo) == ("a.py", "b.py")

    def test_clean_tree_returns_empty(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo, "bdp-clean")
        assert best_effort_dirty_paths(repo) == ()

    @pytest.mark.parametrize(
        "error", [subprocess.CalledProcessError(1, "git status"), OSError("gone")]
    )
    def test_failing_status_returns_empty(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        error: BaseException,
    ) -> None:
        from cw.stage_verification import baseline

        repo = _repo(make_git_repo, "bdp-fail")

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise error

        monkeypatch.setattr(baseline, "git_output", _raise)
        assert best_effort_dirty_paths(repo) == ()


class TestToleratedDirtyStart:
    """#3693: an operator-tolerated exact path does not refuse the cycle start."""

    @staticmethod
    def _dirty_tracked(repo: Path, relpath: str) -> None:
        commit_tracked_file(repo, relpath, "a = 1\n")
        (repo / relpath).write_text("a = 2\n")

    @classmethod
    def _dirty_as(cls, repo: Path, kind: str) -> None:
        """Leave ``t.py`` as `` M`` (modified), ``??`` (unstaged-new) or `` D``."""
        if kind == "modified":
            cls._dirty_tracked(repo, "t.py")
        elif kind == "unstaged-new":
            (repo / "t.py").write_text("x = 1\n")
        else:
            commit_tracked_file(repo, "t.py", "a = 1\n")
            (repo / "t.py").unlink()

    @pytest.mark.parametrize("kind", _KINDS)
    def test_tolerated_dirty_path_is_a_clean_baseline(
        self, make_git_repo: Callable[..., Path], kind: str
    ) -> None:
        repo = _repo(make_git_repo, f"tol-{kind}")
        self._dirty_as(repo, kind)
        before = _index_state(repo)
        got = capture_cycle_baseline(repo, tolerated=frozenset({"t.py"}))
        assert isinstance(got, CycleBaseline)
        assert "t.py" in got.review_artifacts
        assert _index_state(repo) == before

    def test_unlisted_dirty_path_still_refuses(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "tol-unlisted")
        self._dirty_tracked(repo, "t.py")
        got = capture_cycle_baseline(repo, tolerated=frozenset({"other.py"}))
        assert isinstance(got, DirtyStart)
        assert any("t.py" in line for line in got.dirty)

    def test_tolerated_plus_unlisted_refuses_without_tolerated_line(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "tol-mixed")
        self._dirty_tracked(repo, "t.py")
        self._dirty_tracked(repo, "junk.py")
        got = capture_cycle_baseline(repo, tolerated=frozenset({"t.py"}))
        assert isinstance(got, DirtyStart)
        assert not any("t.py" in line for line in got.dirty)
        assert any("junk.py" in line for line in got.dirty)

    def test_empty_tolerated_is_the_default(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "tol-default")
        self._dirty_tracked(repo, "t.py")
        assert isinstance(capture_cycle_baseline(repo), DirtyStart)
        assert isinstance(
            capture_cycle_baseline(repo, tolerated=frozenset()), DirtyStart
        )

    @pytest.mark.parametrize("kind", _KINDS)
    def test_staging_leaves_tolerated_path_out_of_cycle(
        self, make_git_repo: Callable[..., Path], kind: str
    ) -> None:
        repo = _repo(make_git_repo, f"tol-stage-{kind}")
        self._dirty_as(repo, kind)
        worktree_before = (
            (repo / "t.py").read_text() if (repo / "t.py").exists() else None
        )
        baseline = capture_cycle_baseline(repo, tolerated=frozenset({"t.py"}))
        assert isinstance(baseline, CycleBaseline)
        (repo / "fix.py").write_text("f = 1\n")
        stage_cycle(repo, baseline.review_artifacts)
        staged = git_in(repo, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["fix.py"]
        # The operator's edit is left alone in the worktree, never staged.
        after = (repo / "t.py").read_text() if (repo / "t.py").exists() else None
        assert after == worktree_before

    @pytest.mark.parametrize("kind", _KINDS)
    def test_cycle_touched_paths_omits_the_tolerated_path(
        self, make_git_repo: Callable[..., Path], kind: str
    ) -> None:
        repo = _repo(make_git_repo, f"tol-touched-{kind}")
        self._dirty_as(repo, kind)
        baseline = capture_cycle_baseline(repo, tolerated=frozenset({"t.py"}))
        assert isinstance(baseline, CycleBaseline)
        (repo / "fix.py").write_text("f = 1\n")
        assert cycle_touched_paths(repo, baseline) == {"fix.py"}

    def test_an_unmerged_tolerated_path_still_refuses(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "tol-unmerged")
        _seed_conflicting_cherry_pick(repo)
        before = _index_state(repo)

        got = capture_cycle_baseline(repo, tolerated=frozenset({"pyproject.toml"}))

        assert isinstance(got, DirtyStart)
        assert got.unmerged == ("pyproject.toml",)
        assert got.markers == ("CHERRY_PICK_HEAD",)
        assert _index_state(repo) == before

    def test_unmerged_entries_alone_still_refuse_a_tolerated_path(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """No marker is set: the unmerged probe is what refuses."""
        repo = _repo(make_git_repo, "tol-unmerged-bare")
        _seed_conflicting_cherry_pick(repo)
        git_in(repo, "cherry-pick", "--quit")

        got = capture_cycle_baseline(repo, tolerated=frozenset({"pyproject.toml"}))

        assert isinstance(got, DirtyStart)
        assert got.markers == ()
        assert got.unmerged == ("pyproject.toml",)

    def test_an_operation_marker_still_refuses_with_a_tolerated_path(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "tol-marker")
        _stage_merge_from_other_branch(repo, {"other.py": "o = 1\n"})

        got = capture_cycle_baseline(repo, tolerated=frozenset({"other.py"}))

        assert isinstance(got, DirtyStart)
        assert got.markers == ("MERGE_HEAD",)

    @pytest.mark.parametrize("artifact", _ARTIFACTS)
    @pytest.mark.parametrize("state", ["untracked", "modified"])
    def test_a_tolerated_path_that_is_also_a_review_artifact_is_one_entry(
        self, make_git_repo: Callable[..., Path], artifact: str, state: str
    ) -> None:
        repo = _repo(make_git_repo, "tol-artifact")
        if state == "untracked":
            _write(repo / artifact, "verdict\n")
        else:
            commit_tracked_file(repo, artifact, "old\n")
            _write(repo / artifact, "rewritten by review\n")
        before = _index_state(repo)

        got = capture_cycle_baseline(repo, tolerated=frozenset({artifact}))

        assert isinstance(got, CycleBaseline)
        assert got.review_artifacts == frozenset({artifact})
        assert _index_state(repo) == before

    def test_the_dirty_start_warning_is_logged_once_per_capture(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _repo(make_git_repo, "tol-warn")
        self._dirty_tracked(repo, "t.py")
        tolerated = frozenset({"t.py"})
        with caplog.at_level(logging.WARNING, logger="cw.tolerated_paths"):
            capture_cycle_baseline(repo, tolerated=tolerated)
            first = [r.args for r in caplog.records if r.name == "cw.tolerated_paths"]
            capture_cycle_baseline(repo, tolerated=tolerated)
        records = [r.args for r in caplog.records if r.name == "cw.tolerated_paths"]
        assert first == [("t.py", "dirty_start")]
        assert records == [("t.py", "dirty_start")] * 2

    def test_no_warning_is_logged_when_nothing_tolerated_is_dirty(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _repo(make_git_repo, "tol-quiet")
        with caplog.at_level(logging.WARNING, logger="cw.tolerated_paths"):
            capture_cycle_baseline(repo, tolerated=frozenset({"t.py"}))
        assert [r for r in caplog.records if r.name == "cw.tolerated_paths"] == []
