"""Tests for cw.worktree_gc — GC worktrees for squash-merged/closed branches."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cw.models import (
    CwState,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.tracker import TRACKER_GITHUB_ISSUES
from cw.worktree_gc import (
    _GIT_BRANCH_DELETE_FLAG,
    GC_KEEP_VERDICTS,
    GC_REMOVE_VERDICTS,
    GC_SKIP_VERDICTS,
    GcVerdict,
    WorktreeEntry,
    WorktreeGcReport,
    WorktreeGcResult,
    _has_unpushed_commits,
    _live_worktree_paths,
    check_pr_state,
    classify_worktrees,
    list_repo_worktrees,
    remove_worktree_gc,
    run_worktree_gc,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PORCELAIN_TEMPLATE = (
    "worktree {main}\nHEAD abc1234\nbranch refs/heads/main\n\n"
    "worktree {wt1}\nHEAD def5678\nbranch refs/heads/dev/630\n\n"
    "worktree {wt2}\nHEAD 9abcdef\nbranch refs/heads/dev/629\nlocked\n\n"
    "worktree {wt3}\nHEAD 000aaaa\ndetached\n\n"
)


def _make_porcelain(main: Path, wt1: Path, wt2: Path, wt3: Path) -> str:
    return _PORCELAIN_TEMPLATE.format(main=main, wt1=wt1, wt2=wt2, wt3=wt3)


# ---------------------------------------------------------------------------
# list_repo_worktrees
# ---------------------------------------------------------------------------


class TestListRepoWorktrees:
    def test_returns_non_main_worktrees(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        wt2 = tmp_path / "wt" / "dev-629"
        wt3 = tmp_path / "wt" / "detached"
        porcelain = _make_porcelain(main, wt1, wt2, wt3)

        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        # main is excluded; wt1/wt2/wt3 included
        assert len(entries) == 3
        paths = {e.path for e in entries}
        assert wt1 in paths
        assert wt2 in paths
        assert wt3 in paths
        assert main not in paths

    def test_locked_flag_parsed(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        wt2 = tmp_path / "wt" / "dev-629"
        wt3 = tmp_path / "wt" / "detached"
        porcelain = _make_porcelain(main, wt1, wt2, wt3)

        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        entry_map = {e.path: e for e in entries}
        assert entry_map[wt2].locked is True
        assert entry_map[wt1].locked is False

    def test_detached_head_branch_is_none(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        wt2 = tmp_path / "wt" / "dev-629"
        wt3 = tmp_path / "wt" / "detached"
        porcelain = _make_porcelain(main, wt1, wt2, wt3)

        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        entry_map = {e.path: e for e in entries}
        assert entry_map[wt3].branch is None

    def test_branch_parsed(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        wt2 = tmp_path / "wt" / "dev-629"
        wt3 = tmp_path / "wt" / "detached"
        porcelain = _make_porcelain(main, wt1, wt2, wt3)

        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        entry_map = {e.path: e for e in entries}
        assert entry_map[wt1].branch == "dev/630"

    def test_git_failure_returns_empty(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            entries = list_repo_worktrees(main)

        assert entries == []

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        with patch("cw.worktree_gc._sp.run", side_effect=OSError("git not found")):
            entries = list_repo_worktrees(main)

        assert entries == []

    def test_locked_with_reason(self, tmp_path: Path) -> None:
        """locked line may have optional reason after space."""
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        porcelain = (
            f"worktree {main}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {wt1}\nHEAD def\nbranch refs/heads/dev/630\n"
            "locked manual hold\n\n"
        )
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        assert len(entries) == 1
        assert entries[0].locked is True

    def test_bare_flag_parsed(self, tmp_path: Path) -> None:
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "bare-clone"
        porcelain = (
            f"worktree {main}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {wt1}\nHEAD def\nbare\n\n"
        )
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        assert len(entries) == 1
        assert entries[0].is_bare is True
        assert entries[0].branch is None

    def test_no_trailing_newline_parsed(self, tmp_path: Path) -> None:
        """Porcelain output without trailing blank line still parses the last block."""
        main = tmp_path / "repo"
        wt1 = tmp_path / "wt" / "dev-630"
        # No trailing \n after the last block
        porcelain = (
            f"worktree {main}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {wt1}\nHEAD def\nbranch refs/heads/dev/630"
        )
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=porcelain, stderr="")
            entries = list_repo_worktrees(main)

        assert len(entries) == 1
        assert entries[0].branch == "dev/630"


# ---------------------------------------------------------------------------
# check_pr_state
# ---------------------------------------------------------------------------


class TestCheckPrState:
    def test_returns_merged(self) -> None:
        payload = json.dumps([{"state": "MERGED", "number": 735}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, pr_number, gh_available = check_pr_state("dev/630")

        assert state == "MERGED"
        assert pr_number == 735
        assert gh_available is True

    def test_returns_open(self) -> None:
        payload = json.dumps([{"state": "OPEN", "number": 736}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, pr_number, gh_available = check_pr_state("dev/631")

        assert state == "OPEN"
        assert pr_number == 736
        assert gh_available is True

    def test_returns_closed(self) -> None:
        payload = json.dumps([{"state": "CLOSED", "number": 734}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, pr_number, gh_available = check_pr_state("dev/629")

        assert state == "CLOSED"
        assert pr_number == 734
        assert gh_available is True

    def test_no_prs_returns_empty_string(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            state, pr_number, gh_available = check_pr_state("rfc/0006")

        assert state == ""
        assert pr_number is None
        assert gh_available is True

    def test_gh_not_found_returns_none_false(self) -> None:
        with patch("cw.worktree_gc._sp.run", side_effect=FileNotFoundError):
            state, pr_number, gh_available = check_pr_state("dev/630")

        assert state is None
        assert pr_number is None
        assert gh_available is False

    def test_timeout_returns_none_true(self) -> None:
        with patch(
            "cw.worktree_gc._sp.run",
            side_effect=subprocess.TimeoutExpired("gh", 10),
        ):
            state, pr_number, gh_available = check_pr_state("dev/630")

        assert state is None
        assert pr_number is None
        assert gh_available is True

    def test_non_zero_exit_returns_none_true(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            state, pr_number, gh_available = check_pr_state("dev/630")

        assert state is None
        assert pr_number is None
        assert gh_available is True

    def test_bad_json_returns_none_true(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not json", stderr=""
            )
            state, pr_number, gh_available = check_pr_state("dev/630")

        assert state is None
        assert pr_number is None
        assert gh_available is True

    def test_passes_state_all_flag(self) -> None:
        payload = json.dumps([{"state": "MERGED", "number": 735}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            check_pr_state("dev/630")

        cmd = mock_run.call_args[0][0]
        assert "--state" in cmd
        state_idx = cmd.index("--state")
        assert cmd[state_idx + 1] == "all"

    def test_passes_head_branch_arg(self) -> None:
        payload = json.dumps([{"state": "MERGED", "number": 735}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            check_pr_state("dev/630")

        cmd = mock_run.call_args[0][0]
        head_idx = cmd.index("--head")
        assert cmd[head_idx + 1] == "dev/630"


# ---------------------------------------------------------------------------
# classify_worktrees
# ---------------------------------------------------------------------------


class TestClassifyWorktrees:
    def _make_entry(
        self,
        tmp_path: Path,
        name: str,
        branch: str | None = "dev/630",
        locked: bool = False,
    ) -> WorktreeEntry:
        return WorktreeEntry(path=tmp_path / name, branch=branch, locked=locked)

    def test_locked_gets_skip_locked(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", locked=True)]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state") as mock_gh,
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_LOCKED
        mock_gh.assert_not_called()

    def test_detached_gets_skip_detached(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch=None)]
        with patch("cw.worktree_gc.list_repo_worktrees", return_value=entries):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_DETACHED

    def test_merged_pr_clean_gets_remove_merged(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 735, True)),
            patch("cw.worktree_gc._is_dirty", return_value=False),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.REMOVE_MERGED
        assert results[0].pr_number == 735

    def test_merged_pr_dirty_gets_skip_dirty(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 735, True)),
            patch("cw.worktree_gc._is_dirty", return_value=True),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_DIRTY

    def test_closed_pr_default_keeps(self, tmp_path: Path) -> None:
        """CLOSED PRs are kept by default (include_closed=False)."""
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/629")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("CLOSED", 734, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_CLOSED_PR

    def test_closed_pr_include_closed_gets_remove(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/629")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("CLOSED", 734, True)),
            patch("cw.worktree_gc._is_dirty", return_value=False),
        ):
            results = classify_worktrees(tmp_path / "repo", include_closed=True)

        assert results[0].verdict == GcVerdict.REMOVE_CLOSED

    def test_closed_pr_include_closed_dirty_keeps(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/629")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("CLOSED", 734, True)),
            patch("cw.worktree_gc._is_dirty", return_value=True),
        ):
            results = classify_worktrees(tmp_path / "repo", include_closed=True)

        assert results[0].verdict == GcVerdict.SKIP_DIRTY

    def test_open_pr_gets_keep_open_pr(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/631")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("OPEN", 736, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_OPEN_PR

    def test_no_pr_gets_keep_no_pr(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="rfc/0006")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("", None, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_NO_PR

    def test_gh_unavailable_gets_skip(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=(None, None, False)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_GH_UNAVAILABLE

    def test_transient_error_keeps(self, tmp_path: Path) -> None:
        """None state with gh_available=True is a transient error — keep."""
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=(None, None, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_NO_PR

    def test_pr_number_stored(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 735, True)),
            patch("cw.worktree_gc._is_dirty", return_value=False),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].pr_number == 735

    def test_bare_worktree_gets_skip_bare(self, tmp_path: Path) -> None:
        entries = [
            WorktreeEntry(
                path=tmp_path / "wt1", branch="main", locked=False, is_bare=True
            )
        ]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state") as mock_gh,
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_BARE
        mock_gh.assert_not_called()

    def test_closed_pr_keeps_with_keep_closed_verdict(self, tmp_path: Path) -> None:
        """CLOSED PR without --include-closed → KEEP_CLOSED_PR (not KEEP_NO_PR)."""
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/629")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("CLOSED", 734, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_CLOSED_PR
        assert results[0].pr_number == 734

    def test_worktree_bases_filters_out_of_scope(self, tmp_path: Path) -> None:
        """Worktrees outside worktree_bases are silently skipped."""
        in_scope = self._make_entry(tmp_path, "wt-base/dev-630", branch="dev/630")
        out_of_scope = self._make_entry(tmp_path, "other/dev-631", branch="dev/631")
        entries = [in_scope, out_of_scope]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("OPEN", 736, True)),
        ):
            results = classify_worktrees(
                tmp_path / "repo",
                worktree_bases=frozenset({tmp_path / "wt-base"}),
            )

        assert len(results) == 1
        assert results[0].entry.branch == "dev/630"

    def test_worktree_bases_accepts_multiple_bases(self, tmp_path: Path) -> None:
        """Worktrees under any of the given bases are included."""
        wt1 = self._make_entry(tmp_path, "base-a/dev-630", branch="dev/630")
        wt2 = self._make_entry(tmp_path, "base-b/dev-631", branch="dev/631")
        wt3 = self._make_entry(tmp_path, "other/dev-632", branch="dev/632")
        entries = [wt1, wt2, wt3]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("OPEN", 736, True)),
        ):
            results = classify_worktrees(
                tmp_path / "repo",
                worktree_bases=frozenset({tmp_path / "base-a", tmp_path / "base-b"}),
            )

        assert len(results) == 2
        branches = {r.entry.branch for r in results}
        assert branches == {"dev/630", "dev/631"}

    def test_gh_unavailable_short_circuits_subsequent_entries(
        self, tmp_path: Path
    ) -> None:
        """After first gh_available=False, remaining entries skip the gh call."""
        entries = [
            self._make_entry(tmp_path, "wt1", branch="dev/630"),
            self._make_entry(tmp_path, "wt2", branch="dev/631"),
            self._make_entry(tmp_path, "wt3", branch="dev/632"),
        ]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state", return_value=(None, None, False)
            ) as mock_gh,
        ):
            results = classify_worktrees(tmp_path / "repo")

        # gh was only called once — second and third entries short-circuited
        mock_gh.assert_called_once()
        assert all(r.verdict == GcVerdict.SKIP_GH_UNAVAILABLE for r in results)

    def test_check_pr_state_receives_cwd(self, tmp_path: Path) -> None:
        """classify_worktrees passes git_cwd to check_pr_state for gh repo context."""
        repo = tmp_path / "repo"
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state", return_value=("OPEN", 736, True)
            ) as mock_gh,
        ):
            classify_worktrees(repo)

        _, kwargs = mock_gh.call_args
        assert kwargs.get("cwd") == repo

    def test_check_pr_state_receives_timeout(self, tmp_path: Path) -> None:
        """classify_worktrees forwards timeout to check_pr_state."""
        repo = tmp_path / "repo"
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state", return_value=("OPEN", 736, True)
            ) as mock_gh,
        ):
            classify_worktrees(repo, timeout=30)

        args, kwargs = mock_gh.call_args
        assert kwargs.get("timeout") == 30 or (len(args) > 1 and args[1] == 30)


# ---------------------------------------------------------------------------
# _is_dirty
# ---------------------------------------------------------------------------


class TestIsDirty:
    def test_clean_worktree_returns_false(self, tmp_path: Path) -> None:
        from cw.worktree_gc import _is_dirty

        with (
            patch("cw.worktree_gc._sp.run") as mock_run,
            patch("cw.worktree_gc._has_unpushed_commits", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert _is_dirty(tmp_path, "dev/630") is False

    def test_dirty_worktree_returns_true(self, tmp_path: Path) -> None:
        from cw.worktree_gc import _is_dirty

        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=" M some/file.py\n", stderr=""
            )
            assert _is_dirty(tmp_path, "dev/630") is True

    def test_oserror_returns_true_conservative(self, tmp_path: Path) -> None:
        from cw.worktree_gc import _is_dirty

        with patch("cw.worktree_gc._sp.run", side_effect=OSError("no git")):
            assert _is_dirty(tmp_path, "dev/630") is True

    def test_unpushed_commits_returns_true(self, tmp_path: Path) -> None:
        """Clean working tree with unpushed commits → dirty."""
        from cw.worktree_gc import _is_dirty

        with (
            patch("cw.worktree_gc._sp.run") as mock_run,
            patch("cw.worktree_gc._has_unpushed_commits", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert _is_dirty(tmp_path, "dev/630") is True

    def test_strips_git_dir_from_env(self, tmp_path: Path) -> None:
        """GIT_DIR must be stripped so git uses the worktree, not the parent repo."""
        import os

        from cw.worktree_gc import _is_dirty

        captured_env: dict[str, str] = {}

        def _capture(cmd: list[str], **kwargs: object) -> MagicMock:
            env = kwargs.get("env")
            if isinstance(env, dict):
                captured_env.update(env)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch.dict(os.environ, {"GIT_DIR": "/some/other/.git"}),
            patch("cw.worktree_gc._sp.run", side_effect=_capture),
            patch("cw.worktree_gc._has_unpushed_commits", return_value=False),
        ):
            _is_dirty(tmp_path, "dev/630")

        assert "GIT_DIR" not in captured_env


class TestHasUnpushedCommits:
    def test_no_unpushed_returns_false(self, tmp_path: Path) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert _has_unpushed_commits(tmp_path, "dev/630") is False

    def test_unpushed_commits_returns_true(self, tmp_path: Path) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123 fix something\n", stderr=""
            )
            assert _has_unpushed_commits(tmp_path, "dev/630") is True

    def test_oserror_returns_true_conservative(self, tmp_path: Path) -> None:
        with patch("cw.worktree_gc._sp.run", side_effect=OSError("no git")):
            assert _has_unpushed_commits(tmp_path, "dev/630") is True

    def test_nonzero_exit_returns_true_conservative(self, tmp_path: Path) -> None:
        """Non-zero exit (e.g. remote ref absent) → conservative True."""
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="error")
            assert _has_unpushed_commits(tmp_path, "dev/630") is True

    def test_passes_branch_to_log_command(self, tmp_path: Path) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _has_unpushed_commits(tmp_path, "feature/xyz")

        cmd = mock_run.call_args[0][0]
        assert "origin/feature/xyz..HEAD" in cmd


# ---------------------------------------------------------------------------
# remove_worktree_gc
# ---------------------------------------------------------------------------


class TestRemoveWorktreeGc:
    def test_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = remove_worktree_gc(entry, tmp_path / "repo")

        assert result is True
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in cmd and "remove" in cmd for cmd in cmds)
        assert any("branch" in cmd and _GIT_BRANCH_DELETE_FLAG in cmd for cmd in cmds)

    def test_branch_delete_failure_does_not_raise(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)

        def _side_effect(cmd: list[str], **_kw: object) -> MagicMock:
            if "branch" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="not merged")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("cw.worktree_gc._sp.run", side_effect=_side_effect):
            # Branch delete failure does not affect return value (wt was removed)
            result = remove_worktree_gc(entry, tmp_path / "repo")
        assert result is True

    def test_skip_branch_delete_when_no_branch(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch=None, locked=False)
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            remove_worktree_gc(entry, tmp_path / "repo")

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any("branch" in cmd for cmd in cmds)

    def test_skip_branch_delete_kwarg(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            remove_worktree_gc(entry, tmp_path / "repo", delete_branch=False)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any("branch" in cmd for cmd in cmds)

    def test_worktree_remove_failure_skips_branch_delete(self, tmp_path: Path) -> None:
        """If worktree remove fails, branch delete is skipped to avoid inconsistency."""
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)

        def _side_effect(cmd: list[str], **_kw: object) -> MagicMock:
            if "worktree" in cmd and "remove" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("cw.worktree_gc._sp.run", side_effect=_side_effect) as mock_run:
            result = remove_worktree_gc(entry, tmp_path / "repo")

        assert result is False
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any("branch" in cmd for cmd in cmds)

    def test_branch_flag_is_force_delete(self, tmp_path: Path) -> None:
        """Branch deletion uses -D (force) not -d (safe), since squash-merged
        branches are never ancestors of main."""
        assert _GIT_BRANCH_DELETE_FLAG == "-D"

    def test_oserror_propagates(self, tmp_path: Path) -> None:
        """OSError from subprocess propagates — CLI handle_errors catches it."""
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        with (
            patch("cw.worktree_gc._sp.run", side_effect=OSError("git not found")),
            pytest.raises(OSError, match="git not found"),
        ):
            remove_worktree_gc(entry, tmp_path / "repo")


# ---------------------------------------------------------------------------
# WorktreeGcReport
# ---------------------------------------------------------------------------


def test_verdict_frozensets_are_complete_partition() -> None:
    """GC_REMOVE/KEEP/SKIP_VERDICTS must cover all GcVerdict values exactly once."""
    all_verdicts = frozenset(GcVerdict)
    union = GC_REMOVE_VERDICTS | GC_KEEP_VERDICTS | GC_SKIP_VERDICTS
    assert union == all_verdicts, f"Missing from partition: {all_verdicts - union}"
    assert not (GC_REMOVE_VERDICTS & GC_KEEP_VERDICTS)
    assert not (GC_REMOVE_VERDICTS & GC_SKIP_VERDICTS)
    assert not (GC_KEEP_VERDICTS & GC_SKIP_VERDICTS)


class TestWorktreeGcReport:
    def _make_result(
        self, path: Path, verdict: GcVerdict, branch: str | None = "dev/630"
    ) -> WorktreeGcResult:
        return WorktreeGcResult(
            entry=WorktreeEntry(path=path, branch=branch, locked=False),
            verdict=verdict,
            pr_number=None,
        )

    def test_to_remove(self, tmp_path: Path) -> None:
        results = [
            self._make_result(tmp_path / "a", GcVerdict.REMOVE_MERGED),
            self._make_result(tmp_path / "b", GcVerdict.REMOVE_CLOSED),
            self._make_result(tmp_path / "c", GcVerdict.KEEP_OPEN_PR),
        ]
        report = WorktreeGcReport(results=results)
        assert len(report.to_remove) == 2

    def test_kept(self, tmp_path: Path) -> None:
        results = [
            self._make_result(tmp_path / "a", GcVerdict.KEEP_OPEN_PR),
            self._make_result(tmp_path / "b", GcVerdict.KEEP_NO_PR),
            self._make_result(tmp_path / "c", GcVerdict.KEEP_CLOSED_PR),
        ]
        report = WorktreeGcReport(results=results)
        assert len(report.kept) == 3

    def test_skipped(self, tmp_path: Path) -> None:
        results = [
            self._make_result(tmp_path / "a", GcVerdict.SKIP_LOCKED),
            self._make_result(tmp_path / "b", GcVerdict.SKIP_DETACHED),
            self._make_result(tmp_path / "c", GcVerdict.SKIP_GH_UNAVAILABLE),
            self._make_result(tmp_path / "d", GcVerdict.SKIP_DIRTY),
            self._make_result(tmp_path / "e", GcVerdict.SKIP_BARE),
        ]
        report = WorktreeGcReport(results=results)
        assert len(report.skipped) == 5


# ---------------------------------------------------------------------------
# run_worktree_gc
# ---------------------------------------------------------------------------


def _pr_state_side_effect(
    branch: str, timeout: int = 10, **_kw: object
) -> tuple[str | None, int | None, bool]:
    if branch == "dev/630":
        return "MERGED", 735, True
    if branch == "dev/631":
        return "OPEN", 736, True
    return "", None, True


class TestRunWorktreeGc:
    def _make_entries(self, tmp_path: Path) -> list[WorktreeEntry]:
        return [
            WorktreeEntry(path=tmp_path / "wt-merged", branch="dev/630", locked=False),
            WorktreeEntry(path=tmp_path / "wt-open", branch="dev/631", locked=False),
            WorktreeEntry(path=tmp_path / "wt-locked", branch="dev/600", locked=True),
        ]

    def test_dry_run_does_not_remove(self, tmp_path: Path) -> None:
        entries = self._make_entries(tmp_path)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=False)

        mock_remove.assert_not_called()
        assert len(report.to_remove) == 1
        assert len(report.kept) == 1
        assert len(report.skipped) == 1

    def test_apply_removes_merged(self, tmp_path: Path) -> None:
        entries = self._make_entries(tmp_path)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=True)

        assert mock_remove.call_count == 1
        removed_entry = mock_remove.call_args[0][0]
        assert removed_entry.branch == "dev/630"
        assert len(report.to_remove) == 1

    def test_apply_skips_locked(self, tmp_path: Path) -> None:
        entries = self._make_entries(tmp_path)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            run_worktree_gc(tmp_path / "repo", apply=True)

        # Only the merged one should be removed, not the locked one
        removed_entries = [c[0][0] for c in mock_remove.call_args_list]
        assert all(e.branch == "dev/630" for e in removed_entries)

    def test_include_closed_removes_closed(self, tmp_path: Path) -> None:
        entries = [
            WorktreeEntry(path=tmp_path / "wt-closed", branch="dev/629", locked=False),
        ]

        def _closed_state(
            branch: str, timeout: int = 10, **_kw: object
        ) -> tuple[str | None, int | None, bool]:
            return "CLOSED", 734, True

        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", side_effect=_closed_state),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            run_worktree_gc(tmp_path / "repo", apply=True, include_closed=True)

        mock_remove.assert_called_once()

    def test_closed_default_not_removed(self, tmp_path: Path) -> None:
        entries = [
            WorktreeEntry(path=tmp_path / "wt-closed", branch="dev/629", locked=False),
        ]

        def _closed_state(
            branch: str, timeout: int = 10, **_kw: object
        ) -> tuple[str | None, int | None, bool]:
            return "CLOSED", 734, True

        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", side_effect=_closed_state),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            run_worktree_gc(tmp_path / "repo", apply=True)

        mock_remove.assert_not_called()

    def test_apply_tracks_removal_failures(self, tmp_path: Path) -> None:
        """removal_failures counts worktrees where git worktree remove fails."""
        entries = [
            WorktreeEntry(path=tmp_path / "wt-merged", branch="dev/630", locked=False),
            WorktreeEntry(path=tmp_path / "wt-merged2", branch="dev/631", locked=False),
        ]

        def _all_merged(
            branch: str, timeout: int = 10, **_kw: object
        ) -> tuple[str | None, int | None, bool]:
            return "MERGED", 730, True

        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", side_effect=_all_merged),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc", return_value=False),
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=True)

        assert len(report.to_remove) == 2
        assert report.removal_failures == 2

    def test_apply_no_failures_zero_removal_failures(self, tmp_path: Path) -> None:
        """Successful removals leave removal_failures at 0."""
        entries = [
            WorktreeEntry(path=tmp_path / "wt-merged", branch="dev/630", locked=False),
        ]

        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc", return_value=True),
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=True)

        assert report.removal_failures == 0

    def test_limit_caps_results(self, tmp_path: Path) -> None:
        """--limit N caps results to N after base filtering (D2)."""
        entries = [
            WorktreeEntry(path=tmp_path / f"wt-{i}", branch=f"dev/{i}", locked=False)
            for i in range(5)
        ]

        def _all_merged(
            branch: str, timeout: int = 10, **_kw: object
        ) -> tuple[str | None, int | None, bool]:
            return "MERGED", 700, True

        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", side_effect=_all_merged),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
            patch("cw.worktree_gc.remove_worktree_gc", return_value=True),
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=False, limit=3)

        assert len(report.results) == 3
        assert report.total_discovered == 5
        assert report.capped is True

    def test_limit_not_exceeded_capped_false(self, tmp_path: Path) -> None:
        """When limit >= total, capped is False."""
        entries = [
            WorktreeEntry(path=tmp_path / "wt-a", branch="dev/630", locked=False),
        ]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc._is_dirty", return_value=False),
            patch("cw.worktree_gc._live_worktree_paths", return_value=frozenset()),
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=False, limit=10)

        assert report.capped is False
        assert report.total_discovered == 1


# ---------------------------------------------------------------------------
# SKIP_LIVE verdict
# ---------------------------------------------------------------------------


class TestSkipLive:
    def test_live_worktree_gets_skip_live(self, tmp_path: Path) -> None:
        """A worktree in live_worktree_paths receives SKIP_LIVE, no PR lookup."""
        live_path = tmp_path / "wt-live"
        entry = WorktreeEntry(path=live_path, branch="dev/630", locked=False)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=[entry]),
            patch("cw.worktree_gc.check_pr_state") as mock_gh,
        ):
            results = classify_worktrees(
                tmp_path / "repo",
                live_worktree_paths=frozenset({live_path}),
            )

        assert results[0].verdict == GcVerdict.SKIP_LIVE
        mock_gh.assert_not_called()

    def test_non_live_worktree_not_skipped_as_live(self, tmp_path: Path) -> None:
        """A worktree NOT in live_worktree_paths proceeds to PR lookup."""
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=[entry]),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 735, True)),
            patch("cw.worktree_gc._is_dirty", return_value=False),
        ):
            results = classify_worktrees(
                tmp_path / "repo",
                live_worktree_paths=frozenset({tmp_path / "other"}),
            )

        assert results[0].verdict == GcVerdict.REMOVE_MERGED

    def test_skip_live_in_skip_verdicts(self) -> None:
        assert GcVerdict.SKIP_LIVE in GC_SKIP_VERDICTS

    def test_skip_live_in_partition(self) -> None:
        all_verdicts = frozenset(GcVerdict)
        union = GC_REMOVE_VERDICTS | GC_KEEP_VERDICTS | GC_SKIP_VERDICTS
        assert union == all_verdicts


# ---------------------------------------------------------------------------
# Dirty-check filtering of cw scratch files
# ---------------------------------------------------------------------------


class TestIsDirtyCwScratchFiltering:
    def test_cw_scratch_only_not_dirty(self, tmp_path: Path) -> None:
        """A worktree whose only untracked files are under .claude/ is not dirty."""
        from cw.worktree_gc import _is_dirty

        cw_status = "?? .claude/cw-context.json\n?? .claude/prep-pr-state.json\n"
        with (
            patch("cw.worktree_gc._sp.run") as mock_run,
            patch("cw.worktree_gc._has_unpushed_commits", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=cw_status, stderr="")
            result = _is_dirty(tmp_path, "dev/630")

        assert result is False

    def test_cw_scratch_plus_real_change_is_dirty(self, tmp_path: Path) -> None:
        """When real user changes accompany cw scratch, worktree is still dirty."""
        from cw.worktree_gc import _is_dirty

        mixed_status = "?? .claude/cw-context.json\n M src/foo.py\n"
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=mixed_status, stderr=""
            )
            result = _is_dirty(tmp_path, "dev/630")

        assert result is True

    def test_merged_pr_with_only_cw_scratch_is_removed(self, tmp_path: Path) -> None:
        """Regression: merged-PR worktree dirty only with cw scratch → gc removes it."""
        entry = WorktreeEntry(
            path=tmp_path / "wt-merged", branch="dev/630", locked=False
        )
        cw_status = "?? .claude/cw-context.json\n"
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=[entry]),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 735, True)),
            patch("cw.worktree_gc._sp.run") as mock_run,
            patch("cw.worktree_gc._has_unpushed_commits", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=cw_status, stderr="")
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.REMOVE_MERGED

    def test_worktree_with_real_user_edits_skipped_and_reported(
        self, tmp_path: Path
    ) -> None:
        """Regression: genuine uncommitted edits → SKIP_DIRTY (visible)."""
        entry = WorktreeEntry(path=tmp_path / "wt-wip", branch="dev/631", locked=False)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=[entry]),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", 736, True)),
            patch("cw.worktree_gc._is_dirty", return_value=True),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_DIRTY
        # Skipped worktree must appear in results (visible, not silently dropped).
        assert len(results) == 1


# ---------------------------------------------------------------------------
# _live_worktree_paths
# ---------------------------------------------------------------------------


class TestLiveWorktreePaths:
    def test_returns_non_terminal_session_paths(self) -> None:
        live = Path("/live/wt")
        completed = Path("/done/wt")
        sessions = [
            Session(
                name="c/impl",
                client="c",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                origin=SessionOrigin.DAEMON,
                workspace_path=Path("/repo"),
                worktree_path=live,
            ),
            Session(
                name="c/idea",
                client="c",
                purpose=SessionPurpose.IDEA,
                status=SessionStatus.COMPLETED,
                origin=SessionOrigin.DAEMON,
                workspace_path=Path("/repo"),
                worktree_path=completed,
            ),
        ]
        state = CwState(sessions=sessions)

        with (
            patch("cw.worktree_gc.load_state", return_value=state),
            patch("cw.worktree_gc.load_dev_queue", return_value=MagicMock(tasks=[])),
        ):
            paths = _live_worktree_paths()

        assert live in paths
        assert completed not in paths

    def test_includes_running_dispatch_task_paths(self) -> None:
        running_wt = Path("/running/wt")
        task = TicketTask(
            ticket_id="100",
            client="c",
            status=QueueItemStatus.RUNNING,
            worktree_path=running_wt,
        )
        queue = MagicMock()
        queue.tasks = [task]

        with (
            patch("cw.worktree_gc.load_state", return_value=CwState()),
            patch("cw.worktree_gc.load_dev_queue", return_value=queue),
        ):
            paths = _live_worktree_paths()

        assert running_wt in paths

    def test_state_load_error_returns_empty(self) -> None:
        with (
            patch("cw.worktree_gc.load_state", side_effect=Exception("corrupt")),
            patch("cw.worktree_gc.load_dev_queue", return_value=MagicMock(tasks=[])),
        ):
            paths = _live_worktree_paths()

        assert isinstance(paths, frozenset)
        assert len(paths) == 0

    def test_dev_queue_load_error_returns_empty(self) -> None:
        with (
            patch("cw.worktree_gc.load_state", return_value=CwState()),
            patch(
                "cw.worktree_gc.load_dev_queue",
                side_effect=Exception("queue corrupt"),
            ),
        ):
            paths = _live_worktree_paths()

        assert isinstance(paths, frozenset)
        assert len(paths) == 0


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

_GITHUB_TRACKER = TRACKER_GITHUB_ISSUES


def _cli_patches(
    tmp_path: Path,
    clients: dict[str, object],
    report: WorktreeGcReport,
) -> list[object]:
    """Return common patch context managers for CLI tests."""
    return [
        patch("cw.cli.worktree.load_clients", return_value=clients),
        patch("cw.cli.worktree.run_worktree_gc", return_value=report),
        patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
        patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
        patch(
            "cw.cli.worktree.effective_worktree_bases",
            return_value=frozenset({tmp_path / "wt"}),
        ),
    ]


class TestWorktreeGcCli:
    def _make_client(self, tmp_path: Path, name: str = "test-client") -> object:
        from cw.models import ClientConfig

        return ClientConfig(name=name, workspace_path=tmp_path / "repo")

    def test_dry_run_output(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        entries = [
            WorktreeEntry(path=tmp_path / "wt-merged", branch="dev/630", locked=False),
            WorktreeEntry(path=tmp_path / "wt-open", branch="dev/631", locked=False),
        ]
        results = [
            WorktreeGcResult(
                entry=entries[0], verdict=GcVerdict.REMOVE_MERGED, pr_number=735
            ),
            WorktreeGcResult(
                entry=entries[1], verdict=GcVerdict.KEEP_OPEN_PR, pr_number=736
            ),
        ]
        report = WorktreeGcReport(results=results)

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report),
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(
                cli_main, ["worktree", "gc", "--client", "test-client"]
            )

        assert result.exit_code == 0
        assert "dry run" in result.output
        assert "REMOVE" in result.output
        assert "KEEP" in result.output

    def test_apply_flag_passed(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            runner.invoke(
                cli_main, ["worktree", "gc", "--client", "test-client", "--apply"]
            )

        mock_gc.assert_called_once()
        _, kwargs = mock_gc.call_args
        assert kwargs.get("apply") is True

    def test_apply_output_shows_removed(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        entry = WorktreeEntry(
            path=tmp_path / "wt-merged", branch="dev/630", locked=False
        )
        results = [
            WorktreeGcResult(
                entry=entry, verdict=GcVerdict.REMOVE_MERGED, pr_number=735
            ),
        ]
        report = WorktreeGcReport(results=results)

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report),
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(
                cli_main,
                ["worktree", "gc", "--client", "test-client", "--apply"],
            )

        assert result.exit_code == 0
        assert "applying" in result.output
        assert "removed" in result.output

    def test_timeout_flag_passed(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            runner.invoke(
                cli_main,
                ["worktree", "gc", "--client", "test-client", "--timeout", "30"],
            )

        _, kwargs = mock_gc.call_args
        assert kwargs.get("timeout") == 30

    def test_include_closed_flag_passed(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            runner.invoke(
                cli_main,
                ["worktree", "gc", "--client", "test-client", "--include-closed"],
            )

        _, kwargs = mock_gc.call_args
        assert kwargs.get("include_closed") is True

    def test_multi_client_no_filter_iterates_all(self, tmp_path: Path) -> None:
        """Without --client, all configured GitHub-tracked clients are processed."""
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        clients = {
            "client-a": self._make_client(tmp_path, "client-a"),
            "client-b": self._make_client(tmp_path, "client-b"),
        }
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value=clients),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code == 0
        assert mock_gc.call_count == 2

    def test_non_github_tracker_client_skipped(self, tmp_path: Path) -> None:
        """Clients not tracked by GitHub Issues are skipped."""
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value="linear"),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code == 0
        assert "skipped" in result.output
        mock_gc.assert_not_called()

    def test_no_client_single_client_auto_selects(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report),
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code == 0

    def test_no_clients_configured_prints_message(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        runner = CliRunner()
        with patch("cw.cli.worktree.load_clients", return_value={}):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code == 0
        assert "No clients configured" in result.output

    def test_unknown_client_errors(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path, "real-client")

        runner = CliRunner()
        with patch(
            "cw.cli.worktree.load_clients", return_value={"real-client": client}
        ):
            result = runner.invoke(
                cli_main, ["worktree", "gc", "--client", "nonexistent"]
            )

        assert result.exit_code != 0

    def test_worktree_bases_passed_to_run_gc(self, tmp_path: Path) -> None:
        """effective_worktree_bases result is forwarded to run_worktree_gc."""
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])
        wt_bases = frozenset({tmp_path / "custom-wt", tmp_path / "hash-wt"})

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch("cw.cli.worktree.effective_worktree_bases", return_value=wt_bases),
        ):
            runner.invoke(cli_main, ["worktree", "gc", "--client", "test-client"])

        _, kwargs = mock_gc.call_args
        assert kwargs.get("worktree_bases") == wt_bases

    def test_limit_flag_passed_to_run_gc(self, tmp_path: Path) -> None:
        """--limit N is forwarded to run_worktree_gc."""
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report) as mock_gc,
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            runner.invoke(
                cli_main,
                ["worktree", "gc", "--client", "test-client", "--limit", "50"],
            )

        _, kwargs = mock_gc.call_args
        assert kwargs.get("limit") == 50

    def test_capped_report_shows_message(self, tmp_path: Path) -> None:
        """When report.capped is True, output includes the cap message."""
        from click.testing import CliRunner

        from cw.cli import main as cli_main

        client = self._make_client(tmp_path)
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        report = WorktreeGcReport(
            results=[
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.REMOVE_MERGED, pr_number=735
                )
            ],
            total_discovered=10,
            capped=True,
        )

        runner = CliRunner()
        with (
            patch("cw.cli.worktree.load_clients", return_value={"test-client": client}),
            patch("cw.cli.worktree.run_worktree_gc", return_value=report),
            patch("cw.cli.worktree.resolve_tracker", return_value=_GITHUB_TRACKER),
            patch("cw.cli.worktree._git_dir", return_value=tmp_path / "repo"),
            patch(
                "cw.cli.worktree.effective_worktree_bases",
                return_value=frozenset({tmp_path / "wt"}),
            ),
        ):
            result = runner.invoke(
                cli_main, ["worktree", "gc", "--client", "test-client"]
            )

        assert result.exit_code == 0
        assert "capped" in result.output
        assert "10" in result.output
