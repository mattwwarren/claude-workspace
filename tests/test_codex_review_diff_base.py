"""The codex lane diffs from ``origin/<default>``'s merge-base, not local main (#2823).

``_capture_diff``, the fix loop's pre-loop divergence baseline and the fix
fence all used to diff against the bare local default branch. After a merge of
``origin/main`` into a branch whose local ``main`` is stale, that range carries
every commit the merge brought in, while the confirm round (#2800) diffed from
``git merge-base origin/main HEAD``. These tests drive real git through the
shared ``branch_that_merged_main`` builder: local ``main`` stays at the fork
point, ``origin/main`` advances, and the feature branch merges it.

Expected values come from ``cw._git.git_output`` (unstripped, like
``_capture_diff``), never ``git_in`` (stripped), so byte-identity claims are
byte-exact.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw._git import git_output
from cw.codex_fix_loop.divergence import pre_loop_diff_lines
from cw.codex_fix_loop.fence import check_fix_fence
from cw.codex_review import CODEX_FIX_REVERTED_BRANCH, _capture_diff
from cw.codex_review._capability import _CodexFilesystemCapability, _CodexFingerprint
from cw.codex_review._context import _prepare_review_pass
from cw.worktree import _parse_numstat_totals
from tests._codex_review_helpers import _measured_from_head, _task
from tests._review_convergence_helpers import (
    MergedMainBranch,
    branch_that_merged_main,
    live_merge_base,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_DIFF_BASE_LOGGER = "cw.review_convergence._diff_base"


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    worktree, _origin = make_git_repo_with_origin("codex-diff-base")
    return worktree


@pytest.fixture
def merged(repo: Path, tmp_path: Path) -> MergedMainBranch:
    """Feature branch that merged ``origin/main``; local ``main`` is stale."""
    result = branch_that_merged_main(repo, tmp_path / "main-clone")
    assert git_in(repo, "rev-parse", "main") == result.fork_point
    assert git_in(repo, "rev-parse", "origin/main") != result.fork_point
    return result


def _diff_from(repo: Path, base: str) -> str:
    """``git diff --no-color <base>...HEAD``, unstripped like ``_capture_diff``."""
    return git_output(["diff", "--no-color", f"{base}...HEAD"], cwd=repo)


def _numstat_lines(repo: Path, base: str) -> int:
    numstat = git_output(["diff", "--numstat", f"{base}...HEAD"], cwd=repo)
    return _parse_numstat_totals(numstat)[1]


class TestCaptureDiff:
    def test_a_stale_local_main_does_not_widen_the_diff(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        diff, _sha, changed = _capture_diff(repo, "main")

        assert merged.main_only_file not in diff.text
        assert diff.text == _diff_from(repo, live_merge_base(repo))
        assert merged.main_only_file not in changed
        assert merged.main_only_file not in diff.files

    def test_the_old_expression_did_carry_the_main_only_file(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        """Contrast pin: the fixture reproduces the bug the fix removes."""
        assert merged.main_only_file in _diff_from(repo, "main")

    def test_a_current_local_main_is_byte_identical(self, repo: Path) -> None:
        diff, _sha, _changed = _capture_diff(repo, "main")

        assert diff.text == _diff_from(repo, "main")
        assert diff.text

    def test_no_origin_falls_back_to_the_local_branch_with_a_warning(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("codex-diff-base-no-origin")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "feature.py")

        with caplog.at_level(logging.WARNING, logger=_DIFF_BASE_LOGGER):
            diff, _sha, _changed = _capture_diff(repo, "main")

        assert diff.text == _diff_from(repo, "main")
        assert diff.text
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "origin/main does not resolve" in warnings[0].getMessage()

    def test_a_commit_sha_passes_through_unchanged(
        self,
        repo: Path,
        merged: MergedMainBranch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``cw review confirm`` hands ``_capture_diff`` the ledger's pinned SHA."""
        base = live_merge_base(repo)

        with caplog.at_level(logging.WARNING, logger=_DIFF_BASE_LOGGER):
            diff, _sha, _changed = _capture_diff(repo, base)

        assert diff.text == _diff_from(repo, base)
        assert merged.main_only_file not in diff.text
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_a_deleted_local_main_still_diffs(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        git_in(repo, "branch", "-D", "main")

        diff, _sha, _changed = _capture_diff(repo, "main")

        assert diff.text == _diff_from(repo, live_merge_base(repo))
        assert merged.main_only_file not in diff.text


class TestPreLoopDiffLines:
    def test_a_stale_local_main_does_not_inflate_the_baseline(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        expected = _numstat_lines(repo, live_merge_base(repo))
        stale = _numstat_lines(repo, "main")
        assert stale > expected  # the main-only file's lines

        assert pre_loop_diff_lines(repo, "main") == expected

    def test_no_origin_matches_the_local_branch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("codex-diff-base-lines-no-origin")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "feature.py")

        assert pre_loop_diff_lines(repo, "main") == _numstat_lines(repo, "main")
        assert pre_loop_diff_lines(repo, "main") > 0


class TestFenceBase:
    def _revert_whole_branch(self, repo: Path) -> None:
        """Stage the removal of every file the branch added on top of the merge."""
        git_in(repo, "rm", "-q", "feature.py", "after_merge.py")

    def _breach(self, repo: Path) -> tuple[str, tuple[str, ...], str]:
        breach = check_fix_fence(
            repo,
            default_branch="main",
            allowed_files=None,
            finding_files=frozenset(),
            cycle=2,
            touched=set(_measured_from_head(repo)),
        )
        assert breach is not None
        return breach.reason, breach.paths, breach.details

    def test_a_stale_local_main_reads_the_same_as_a_current_one(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        self._revert_whole_branch(repo)
        stale = self._breach(repo)

        git_in(repo, "branch", "-f", "main", "origin/main")
        current = self._breach(repo)

        assert stale == current
        reason, paths, details = stale
        assert reason == CODEX_FIX_REVERTED_BRANCH
        assert paths == ("after_merge.py", "feature.py")
        # The merge's main-only file is not part of the branch diff, so emptying
        # the branch is read as emptying it, not as "2 of 3".
        assert "empty the branch diff" in details
        assert merged.main_only_file not in details


class TestEndToEnd:
    def test_the_review_pass_diff_is_the_confirm_round_diff(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        capability = _CodexFilesystemCapability(
            True, None, _CodexFingerprint(None, "claude", "claude", "claude-tools")
        )

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=None,
            session_id="s-diff-base",
            capability=capability,
        )

        assert prepared.diff.text == _diff_from(repo, live_merge_base(repo))
        assert merged.main_only_file not in prepared.diff.text
