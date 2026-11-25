"""Tests for cw.stage_verification.impl_merge_adopt: merge adoption (#3529).

Real conflicting merges left in a worktree by the shared
``diverge_origin_default`` and ``leave_conflicting_merge`` builders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.merge_probe import MergeTreeResult
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_merge_adopt import (
    AdoptedMerge,
    adopt_unconcluded_merge,
    conflict_fingerprints,
    merge_unconcluded_block,
    merge_unresolved_block,
    unresolved_conflict_files,
    untouched_conflicts,
)
from tests._codex_review_helpers import fresh_impl_repo
from tests._shared.git_repos import (
    commit_tracked_file,
    diverge_origin_default,
    git_in,
    leave_conflicting_merge,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BRANCH = "dev/T-1"
_MARKERS = "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> origin/main\n"


@pytest.fixture
def repo_origin(make_git_repo: Callable[..., Path]) -> tuple[Path, Path]:
    return fresh_impl_repo(make_git_repo, "adopt-wt", _BRANCH)


@pytest.fixture
def conflicted(repo_origin: tuple[Path, Path], tmp_path: Path) -> Path:
    """A worktree mid-merge, conflicted in ``clash.py``."""
    repo, origin = repo_origin
    diverge_origin_default(repo, origin, _BRANCH, tmp_path / "side")
    leave_conflicting_merge(repo)
    return repo


def _binary_conflict(repo_origin: tuple[Path, Path], tmp_path: Path) -> Path:
    repo, origin = repo_origin
    diverge_origin_default(
        repo,
        origin,
        _BRANCH,
        tmp_path / "side",
        "blob.bin",
        mine="a\x00\n",
        theirs="b\x00\n",
    )
    leave_conflicting_merge(repo)
    return repo


def _modify_delete_conflict(repo_origin: tuple[Path, Path], tmp_path: Path) -> Path:
    """Our branch modifies ``md.py``; origin/main deletes it."""
    repo, origin = repo_origin
    side = tmp_path / "side"
    push_commit_to_origin(origin, "main", side, "md.py", "base\n")
    git_in(repo, "fetch", "origin")
    git_in(repo, "merge", "origin/main")
    commit_tracked_file(repo, "md.py", "mine\n")
    git_in(repo, "push", "origin", _BRANCH)
    git_in(side, "rm", "md.py")
    git_in(side, "commit", "-m", "delete md.py")
    git_in(side, "push", "origin", "main")
    leave_conflicting_merge(repo)
    return repo


class TestAdoptUnconcludedMerge:
    def test_a_default_branch_merge_is_adopted(self, conflicted: Path) -> None:
        adopted = adopt_unconcluded_merge(conflicted, base_branch="main")
        assert isinstance(adopted, AdoptedMerge)
        assert adopted.merge_head == git_in(conflicted, "rev-parse", "MERGE_HEAD")
        assert adopted.outcome.merge_files == frozenset({"clash.py"})
        assert adopted.outcome.conflicted == frozenset({"clash.py"})
        assert len(adopted.outcome.growth_tree) == 40

    def test_a_merge_head_off_the_base_is_refused(self, conflicted: Path) -> None:
        fork = git_in(conflicted, "merge-base", "HEAD", "MERGE_HEAD")
        git_in(conflicted, "update-ref", "refs/remotes/origin/main", fork)
        block = adopt_unconcluded_merge(conflicted, base_branch="main")
        assert isinstance(block, ImplBlock)
        assert block.reason == IMPL_FAILED
        assert block.details.startswith("merge_in_progress: MERGE_HEAD ")
        assert "is not on origin/main" in block.details

    def test_an_unreadable_ancestry_check_is_refused(self, conflicted: Path) -> None:
        git_in(conflicted, "update-ref", "-d", "refs/remotes/origin/main")
        block = adopt_unconcluded_merge(conflicted, base_branch="main")
        assert isinstance(block, ImplBlock)
        assert "could not check MERGE_HEAD against origin/main" in block.details

    def test_no_merge_head_is_refused(self, repo_origin: tuple[Path, Path]) -> None:
        block = adopt_unconcluded_merge(repo_origin[0], base_branch="main")
        assert isinstance(block, ImplBlock)
        assert "MERGE_HEAD could not be read" in block.details

    def test_an_unmeasurable_merge_tree_is_refused(self, conflicted: Path) -> None:
        unavailable = MergeTreeResult("unavailable", None, (), "tree failed")
        with patch(
            "cw.stage_verification.impl_merge_adopt.merge_tree",
            return_value=unavailable,
        ):
            block = adopt_unconcluded_merge(conflicted, base_branch="main")
        assert isinstance(block, ImplBlock)
        assert "cannot measure the unconcluded merge: tree failed" in block.details


class TestUnresolvedConflictFiles:
    def test_finds_the_marker_lines_in_conflicted_paths(self, conflicted: Path) -> None:
        assert unresolved_conflict_files(conflicted, {"clash.py"}) == ("clash.py",)

    def test_a_resolved_conflicted_file_is_not_listed(self, conflicted: Path) -> None:
        (conflicted / "clash.py").write_text("merged\n", encoding="utf-8")
        assert unresolved_conflict_files(conflicted, {"clash.py"}) == ()

    def test_a_non_conflicted_file_holding_the_literals_is_not_flagged(
        self, conflicted: Path
    ) -> None:
        (conflicted / "clash.py").write_text("merged\n", encoding="utf-8")
        (conflicted / "doc.md").write_text(_MARKERS, encoding="utf-8")
        assert unresolved_conflict_files(conflicted, {"clash.py"}) == ()
        assert unresolved_conflict_files(conflicted, {"doc.md"}) == ("doc.md",)
        assert unresolved_conflict_files(conflicted, set()) == ()

    def test_only_a_closing_marker_line_is_enough(self, conflicted: Path) -> None:
        (conflicted / "clash.py").write_text(
            "x\n>>>>>>> origin/main\n", encoding="utf-8"
        )
        assert unresolved_conflict_files(conflicted, {"clash.py"}) == ("clash.py",)

    def test_a_marker_in_the_middle_of_a_line_is_not_a_conflict(
        self, conflicted: Path
    ) -> None:
        (conflicted / "clash.py").write_text("x <<<<<<< y\n", encoding="utf-8")
        assert unresolved_conflict_files(conflicted, {"clash.py"}) == ()

    def test_unreadable_and_absent_paths_are_skipped(self, conflicted: Path) -> None:
        (conflicted / "bad.bin").write_bytes(b"\xff\xfe\x00")
        got = unresolved_conflict_files(conflicted, {"bad.bin", "gone.py"})
        assert got == ()


class TestUntouchedConflicts:
    def test_a_text_conflict_the_turn_rewrote_is_not_listed(
        self, conflicted: Path
    ) -> None:
        before = conflict_fingerprints(conflicted, {"clash.py"})
        (conflicted / "clash.py").write_text("merged\n", encoding="utf-8")
        assert untouched_conflicts(conflicted, before) == ()

    def test_a_text_conflict_left_alone_is_listed(self, conflicted: Path) -> None:
        before = conflict_fingerprints(conflicted, {"clash.py"})
        assert untouched_conflicts(conflicted, before) == ("clash.py",)

    def test_a_binary_conflict_left_identical_is_listed(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo = _binary_conflict(repo_origin, tmp_path)
        before = conflict_fingerprints(repo, {"blob.bin"})
        assert untouched_conflicts(repo, before) == ("blob.bin",)

    def test_a_binary_conflict_the_turn_changed_is_not_listed(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo = _binary_conflict(repo_origin, tmp_path)
        before = conflict_fingerprints(repo, {"blob.bin"})
        (repo / "blob.bin").write_bytes(b"resolved\x00\n")
        assert untouched_conflicts(repo, before) == ()

    def test_a_modify_delete_conflict_left_as_is_is_listed(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo = _modify_delete_conflict(repo_origin, tmp_path)
        before = conflict_fingerprints(repo, {"md.py"})
        assert before == {"md.py": git_in(repo, "hash-object", "md.py")}
        assert untouched_conflicts(repo, before) == ("md.py",)

    def test_a_modify_delete_conflict_the_turn_deleted_is_not_listed(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo = _modify_delete_conflict(repo_origin, tmp_path)
        before = conflict_fingerprints(repo, {"md.py"})
        (repo / "md.py").unlink()
        assert untouched_conflicts(repo, before) == ()

    def test_an_absent_file_fingerprints_as_empty(self, conflicted: Path) -> None:
        assert conflict_fingerprints(conflicted, {"gone.py"}) == {"gone.py": ""}

    def test_a_path_resolved_in_the_index_is_not_listed(self, conflicted: Path) -> None:
        before = conflict_fingerprints(conflicted, {"clash.py"})
        git_in(conflicted, "add", "clash.py")
        assert untouched_conflicts(conflicted, before) == ()

    def test_a_path_outside_the_recorded_conflicts_is_never_listed(
        self, conflicted: Path
    ) -> None:
        assert untouched_conflicts(conflicted, {}) == ()


class TestMergeUnresolvedBlock:
    def test_markers_give_a_block_naming_the_files(self, conflicted: Path) -> None:
        before = conflict_fingerprints(conflicted, {"clash.py"})
        (conflicted / "clash.py").write_text(_MARKERS, encoding="utf-8")
        block = merge_unresolved_block(conflicted, {"clash.py"}, before)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert block.details.startswith("merge_in_progress: ")
        assert "conflict markers remain in clash.py" in block.details

    def test_an_untouched_path_gives_a_block(self, conflicted: Path) -> None:
        (conflicted / "clash.py").write_text("no markers\n", encoding="utf-8")
        before = conflict_fingerprints(conflicted, {"clash.py"})
        block = merge_unresolved_block(conflicted, {"clash.py"}, before)
        assert block is not None
        assert block.details.startswith("merge_in_progress: ")
        assert "clash.py is still unmerged and was not changed" in block.details

    def test_a_resolved_conflict_gives_none(self, conflicted: Path) -> None:
        before = conflict_fingerprints(conflicted, {"clash.py"})
        (conflicted / "clash.py").write_text("merged\n", encoding="utf-8")
        assert merge_unresolved_block(conflicted, {"clash.py"}, before) is None

    def test_a_long_listing_is_capped(self, conflicted: Path) -> None:
        names = {f"f{i:02d}.py" for i in range(25)}
        for name in names:
            (conflicted / name).write_text(_MARKERS, encoding="utf-8")
        block = merge_unresolved_block(conflicted, names, {})
        assert block is not None
        assert "(and 5 more)" in block.details


class TestMergeUnconcludedBlock:
    def test_no_merge_in_progress_is_none(self, repo_origin: tuple[Path, Path]) -> None:
        assert merge_unconcluded_block(repo_origin[0]) is None

    def test_a_merge_still_in_progress_blocks(self, conflicted: Path) -> None:
        block = merge_unconcluded_block(conflicted)
        assert block is not None
        assert block.reason == IMPL_FAILED
        assert block.details.startswith("merge_in_progress: MERGE_HEAD is still set")

    def test_an_unmeasurable_state_blocks(self, conflicted: Path) -> None:
        with patch(
            "cw.stage_verification.impl_merge_adopt.merge_in_progress",
            return_value=None,
        ):
            block = merge_unconcluded_block(conflicted)
        assert block is not None
        assert "could not verify that the merge concluded" in block.details
