"""Tests for cw.worktree_gc — GC worktrees for squash-merged/closed branches."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from cw.worktree_gc import (
    GcVerdict,
    WorktreeEntry,
    WorktreeGcReport,
    WorktreeGcResult,
    check_pr_state,
    classify_worktrees,
    list_repo_worktrees,
    remove_worktree_gc,
    run_worktree_gc,
)

if TYPE_CHECKING:
    pass


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


# ---------------------------------------------------------------------------
# check_pr_state
# ---------------------------------------------------------------------------


class TestCheckPrState:
    def test_returns_merged(self) -> None:
        payload = json.dumps([{"state": "MERGED", "number": 735}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, gh_available = check_pr_state("dev/630")

        assert state == "MERGED"
        assert gh_available is True

    def test_returns_open(self) -> None:
        payload = json.dumps([{"state": "OPEN", "number": 736}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, gh_available = check_pr_state("dev/631")

        assert state == "OPEN"
        assert gh_available is True

    def test_returns_closed(self) -> None:
        payload = json.dumps([{"state": "CLOSED", "number": 734}])
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            state, gh_available = check_pr_state("dev/629")

        assert state == "CLOSED"
        assert gh_available is True

    def test_no_prs_returns_empty_string(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            state, gh_available = check_pr_state("rfc/0006")

        assert state == ""
        assert gh_available is True

    def test_gh_not_found_returns_none_false(self) -> None:
        with patch("cw.worktree_gc._sp.run", side_effect=FileNotFoundError):
            state, gh_available = check_pr_state("dev/630")

        assert state is None
        assert gh_available is False

    def test_timeout_returns_none_true(self) -> None:
        with patch(
            "cw.worktree_gc._sp.run",
            side_effect=subprocess.TimeoutExpired("gh", 10),
        ):
            state, gh_available = check_pr_state("dev/630")

        assert state is None
        assert gh_available is True

    def test_non_zero_exit_returns_none_true(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            state, gh_available = check_pr_state("dev/630")

        assert state is None
        assert gh_available is True

    def test_bad_json_returns_none_true(self) -> None:
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not json", stderr=""
            )
            state, gh_available = check_pr_state("dev/630")

        assert state is None
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

    def test_merged_pr_gets_remove_merged(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("MERGED", True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.REMOVE_MERGED

    def test_closed_pr_gets_remove_closed(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/629")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("CLOSED", True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.REMOVE_CLOSED

    def test_open_pr_gets_keep_open_pr(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/631")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("OPEN", True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_OPEN_PR

    def test_no_pr_gets_keep_no_pr(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="rfc/0006")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=("", True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_NO_PR

    def test_gh_unavailable_gets_skip(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=(None, False)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.SKIP_GH_UNAVAILABLE

    def test_transient_error_keeps(self, tmp_path: Path) -> None:
        """None state with gh_available=True is a transient error — keep."""
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc.check_pr_state", return_value=(None, True)),
        ):
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].verdict == GcVerdict.KEEP_NO_PR

    def test_pr_number_stored(self, tmp_path: Path) -> None:
        entries = [self._make_entry(tmp_path, "wt1", branch="dev/630")]
        payload = json.dumps([{"state": "MERGED", "number": 735}])
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch("cw.worktree_gc._sp.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            results = classify_worktrees(tmp_path / "repo")

        assert results[0].pr_number == 735


# ---------------------------------------------------------------------------
# remove_worktree_gc
# ---------------------------------------------------------------------------


class TestRemoveWorktreeGc:
    def test_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            remove_worktree_gc(entry, tmp_path / "repo")

        calls = mock_run.call_args_list
        cmds = [c[0][0] for c in calls]
        assert any("worktree" in cmd and "remove" in cmd for cmd in cmds)
        assert any("branch" in cmd and "-d" in cmd for cmd in cmds)

    def test_branch_delete_failure_does_not_raise(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/630", locked=False)

        def _side_effect(cmd: list[str], **_kw: object) -> MagicMock:
            if "branch" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="not merged")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("cw.worktree_gc._sp.run", side_effect=_side_effect):
            # Should not raise
            remove_worktree_gc(entry, tmp_path / "repo")

    def test_skip_branch_delete_when_no_branch(self, tmp_path: Path) -> None:
        entry = WorktreeEntry(path=tmp_path / "wt1", branch=None, locked=False)
        with patch("cw.worktree_gc._sp.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            remove_worktree_gc(entry, tmp_path / "repo")

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any("branch" in cmd for cmd in cmds)


# ---------------------------------------------------------------------------
# WorktreeGcReport
# ---------------------------------------------------------------------------


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
        ]
        report = WorktreeGcReport(results=results)
        assert len(report.kept) == 2

    def test_skipped(self, tmp_path: Path) -> None:
        results = [
            self._make_result(tmp_path / "a", GcVerdict.SKIP_LOCKED),
            self._make_result(tmp_path / "b", GcVerdict.SKIP_DETACHED),
            self._make_result(tmp_path / "c", GcVerdict.SKIP_GH_UNAVAILABLE),
        ]
        report = WorktreeGcReport(results=results)
        assert len(report.skipped) == 3


# ---------------------------------------------------------------------------
# run_worktree_gc
# ---------------------------------------------------------------------------


def _pr_state_side_effect(branch: str, timeout: int = 10) -> tuple[str | None, bool]:
    if branch == "dev/630":
        return "MERGED", True
    if branch == "dev/631":
        return "OPEN", True
    return "", True


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
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=False)

        mock_remove.assert_not_called()
        assert len(report.to_remove) == 1
        assert len(report.kept) == 1
        assert len(report.skipped) == 1

    def test_apply_removes(self, tmp_path: Path) -> None:
        entries = self._make_entries(tmp_path)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            report = run_worktree_gc(tmp_path / "repo", apply=True)

        assert mock_remove.call_count == 1
        assert len(report.to_remove) == 1

    def test_apply_skips_locked(self, tmp_path: Path) -> None:
        entries = self._make_entries(tmp_path)
        with (
            patch("cw.worktree_gc.list_repo_worktrees", return_value=entries),
            patch(
                "cw.worktree_gc.check_pr_state",
                side_effect=_pr_state_side_effect,
            ),
            patch("cw.worktree_gc.remove_worktree_gc") as mock_remove,
        ):
            run_worktree_gc(tmp_path / "repo", apply=True)

        # Only the merged one should be removed, not the locked one
        removed_entries = [c[0][0] for c in mock_remove.call_args_list]
        assert all(e.branch == "dev/630" for e in removed_entries)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestWorktreeGcCli:
    def test_dry_run_output(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main
        from cw.models import ClientConfig

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "repo",
        )
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
            patch(
                "cw.cli.maintenance.load_clients",
                return_value={"test-client": client},
            ),
            patch("cw.cli.maintenance.run_worktree_gc", return_value=report),
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
        from cw.models import ClientConfig

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "repo",
        )
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch(
                "cw.cli.maintenance.load_clients",
                return_value={"test-client": client},
            ),
            patch("cw.cli.maintenance.run_worktree_gc", return_value=report) as mock_gc,
        ):
            runner.invoke(
                cli_main, ["worktree", "gc", "--client", "test-client", "--apply"]
            )

        mock_gc.assert_called_once()
        _, kwargs = mock_gc.call_args
        assert kwargs.get("apply") is True

    def test_no_client_multi_clients_errors(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main
        from cw.models import ClientConfig

        clients = {
            "client-a": ClientConfig(name="client-a", workspace_path=tmp_path / "a"),
            "client-b": ClientConfig(name="client-b", workspace_path=tmp_path / "b"),
        }

        runner = CliRunner()
        with patch("cw.cli.maintenance.load_clients", return_value=clients):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code != 0

    def test_no_client_single_client_auto_selects(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from cw.cli import main as cli_main
        from cw.models import ClientConfig

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path / "repo",
        )
        report = WorktreeGcReport(results=[])

        runner = CliRunner()
        with (
            patch(
                "cw.cli.maintenance.load_clients",
                return_value={"test-client": client},
            ),
            patch("cw.cli.maintenance.run_worktree_gc", return_value=report),
        ):
            result = runner.invoke(cli_main, ["worktree", "gc"])

        assert result.exit_code == 0
