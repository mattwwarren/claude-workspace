"""Tests for ``cw worktree`` (``gc``, #1389; ``merge-probe``, #2979;
``changelog-sync``, #3190)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.tracker import TRACKER_GITHUB_ISSUES
from cw.worktree_gc import GcVerdict, WorktreeEntry, WorktreeGcReport, WorktreeGcResult
from tests._changelog_cut_helpers import (
    GAMMA,
    commit_files,
    cut_straddle_repo,
    pyproject,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable


class TestWorktreeGcMultiClient:
    """cw worktree gc default (all-clients) run — per-client isolation (#1389)."""

    def test_default_run_processes_all_configured_clients(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: every configured GitHub-tracked client is actually
        called, not silently truncated (the ticket's literal complaint)."""
        ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        write_clients_yaml(
            ClientSpec("client-a", str(ws_a)), ClientSpec("client-b", str(ws_b))
        )
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        called: list[str] = []

        def _mock_gc(git_cwd: Path, **_kwargs: object) -> WorktreeGcReport:
            called.append(str(git_cwd))
            return WorktreeGcReport(results=[])

        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", _mock_gc)

        result = CliRunner().invoke(main, ["worktree", "gc"])
        assert result.exit_code == 0, result.output
        assert str(ws_a) in called
        assert str(ws_b) in called

    def test_client_failure_does_not_abort_remaining_clients(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One client raising mid-loop must not prevent later clients from
        being reached — the ticket's core bug (claude-workspace never examined)."""
        ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        write_clients_yaml(
            ClientSpec("client-a", str(ws_a)), ClientSpec("client-b", str(ws_b))
        )
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        called: list[str] = []

        def _mock_gc(git_cwd: Path, **_kwargs: object) -> WorktreeGcReport:
            called.append(str(git_cwd))
            if str(git_cwd) == str(ws_a):
                msg = "boom"
                raise RuntimeError(msg)
            return WorktreeGcReport(results=[])

        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", _mock_gc)

        result = CliRunner().invoke(main, ["worktree", "gc"])
        assert result.exit_code == 0, result.output
        assert str(ws_a) in called
        assert str(ws_b) in called  # <-- would fail today: client-b never reached

    def test_client_failure_prints_error_line_with_client_name(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        write_clients_yaml(ClientSpec("client-a", str(ws_a)))
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        def _mock_gc(_git_cwd: Path, **_kwargs: object) -> WorktreeGcReport:
            msg = "gh timed out"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", _mock_gc)

        result = CliRunner().invoke(main, ["worktree", "gc"])
        assert "[client-a] ERROR" in result.output
        assert "gh timed out" in result.output

    def test_client_failure_keeps_exit_code_zero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R1 binding: exit code stays 0 on partial failure this ticket."""
        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        write_clients_yaml(ClientSpec("client-a", str(ws_a)))
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        def _mock_gc(_git_cwd: Path, **_kwargs: object) -> WorktreeGcReport:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", _mock_gc)

        result = CliRunner().invoke(main, ["worktree", "gc"])
        assert result.exit_code == 0

    def test_multi_client_summary_reports_examined_and_skipped_counts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        write_clients_yaml(
            ClientSpec("client-a", str(ws_a)), ClientSpec("client-b", str(ws_b))
        )
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        entry = WorktreeEntry(path=tmp_path / "wt1", branch="dev/1", locked=False)
        report = WorktreeGcReport(
            results=[
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.KEEP_NO_PR, pr_number=None
                ),
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_DIRTY, pr_number=None
                ),
            ]
        )
        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", lambda _c, **_k: report)

        result = CliRunner().invoke(main, ["worktree", "gc"])
        assert "[client-a] 2 examined / 1 skipped" in result.output
        assert "[client-b] 2 examined / 1 skipped" in result.output

    def test_explicit_single_client_failure_also_isolated_and_exits_zero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--client X applies the same isolation as the default sweep (uniform
        implementation — see Adopted Assumptions)."""
        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        write_clients_yaml(ClientSpec("client-a", str(ws_a)))
        monkeypatch.setattr(
            "cw.cli.worktree.resolve_tracker", lambda _root: TRACKER_GITHUB_ISSUES
        )

        def _mock_gc(_git_cwd: Path, **_kwargs: object) -> WorktreeGcReport:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.cli.worktree.run_worktree_gc", _mock_gc)

        result = CliRunner().invoke(main, ["worktree", "gc", "--client", "client-a"])
        assert result.exit_code == 0
        assert "[client-a] ERROR" in result.output


class TestWorktreeMergeProbe:
    """``cw worktree merge-probe`` (#2979): JSON plus the 0/1/3 exit contract."""

    @staticmethod
    def _repo(make_git_repo: Callable[..., Path], *, conflict: bool) -> Path:
        """``feature`` behind ``origin/main``; same-line edits when *conflict*."""
        repo = make_git_repo("repo")
        commit_tracked_file(repo, "s.txt", "base\n")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "s.txt", "branch\n")
        git_in(repo, "checkout", "main")
        commit_tracked_file(repo, "s.txt" if conflict else "other.txt", "main\n")
        git_in(repo, "update-ref", "refs/remotes/origin/main", "main")
        git_in(repo, "checkout", "feature")
        return repo

    def _invoke(self, *args: str) -> tuple[int, dict[str, object]]:
        result = CliRunner().invoke(main, ["worktree", "merge-probe", *args])
        return result.exit_code, json.loads(result.output)

    def test_clean_exits_zero_with_base_sha(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = self._repo(make_git_repo, conflict=False)

        code, payload = self._invoke("--worktree", str(repo), "--base", "main")

        assert code == 0
        assert payload["state"] == "clean"
        assert payload["base"] == "main"
        assert payload["base_sha"] == git_in(repo, "rev-parse", "origin/main")
        assert payload["conflicted_files"] == []

    def test_up_to_date_exits_zero(self, make_git_repo: Callable[..., Path]) -> None:
        repo = self._repo(make_git_repo, conflict=False)
        git_in(repo, "merge", "--no-edit", "origin/main")

        code, payload = self._invoke("--worktree", str(repo), "--base", "main")

        assert (code, payload["state"]) == (0, "up_to_date")

    def test_conflict_exits_one(self, make_git_repo: Callable[..., Path]) -> None:
        repo = self._repo(make_git_repo, conflict=True)

        code, payload = self._invoke("--worktree", str(repo), "--base", "main")

        assert code == 1
        assert payload["state"] == "conflict"
        assert payload["conflicted_files"] == ["s.txt"]

    def test_unavailable_exits_three(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("repo")

        code, payload = self._invoke("--worktree", str(repo), "--base", "nope")

        assert code == 3
        assert payload["state"] == "unavailable"
        assert payload["base_sha"] is None
        assert payload["detail"] == "origin/nope does not resolve"

    def test_worktree_defaults_to_the_cwd(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo(make_git_repo, conflict=True)
        monkeypatch.chdir(repo)

        code, payload = self._invoke("--base", "main")

        assert (code, payload["state"]) == (1, "conflict")

    def test_base_is_required(self) -> None:
        result = CliRunner().invoke(main, ["worktree", "merge-probe"])

        assert result.exit_code == 2
        assert "--base" in result.output

    def test_nonexistent_worktree_is_a_usage_error(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "worktree",
                "merge-probe",
                "--worktree",
                str(tmp_path / "x"),
                "--base",
                "main",
            ],
        )

        assert result.exit_code == 2


class TestWorktreeChangelogSync:
    """``cw worktree changelog-sync`` (#3190): JSON plus the 0/1/3 exit contract."""

    _KEYS = frozenset(
        {
            "base",
            "base_sha",
            "head_sha",
            "state",
            "disposition",
            "conflicted_files",
            "inserted",
            "detail",
        }
    )

    def _invoke(self, repo: Path, base: str = "main") -> tuple[int, dict[str, object]]:
        result = CliRunner().invoke(
            main,
            ["worktree", "changelog-sync", "--worktree", str(repo), "--base", base],
        )
        return result.exit_code, json.loads(result.output)

    def test_moved_exits_zero_with_every_key(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        scenario = cut_straddle_repo(tmp_path, make_git_repo)

        code, payload = self._invoke(scenario.clone)

        assert code == 0
        assert set(payload) == self._KEYS
        assert payload["state"] == "moved"
        assert payload["base"] == "main"
        assert payload["base_sha"] == scenario.main_sha
        assert payload["head_sha"] == git_in(scenario.clone, "rev-parse", "HEAD")
        assert payload["inserted"] == [GAMMA]
        assert payload["conflicted_files"] == []
        assert payload["disposition"] is None

    def test_clean_exits_zero(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        scenario = cut_straddle_repo(tmp_path, make_git_repo)
        assert self._invoke(scenario.clone)[0] == 0

        code, payload = self._invoke(scenario.clone)

        assert (code, payload["state"], payload["inserted"]) == (0, "clean", [])
        assert set(payload) == self._KEYS

    def test_conflict_and_refused_both_exit_one(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        scenario = cut_straddle_repo(tmp_path, make_git_repo)
        other = scenario.clone / "src" / "other.py"
        other.write_text("X = 1\n", encoding="utf-8")
        refused_code, refusal = self._invoke(scenario.clone)
        git_in(scenario.clone, "checkout", "--", "src/other.py")
        commit_files(scenario.clone, {"pyproject.toml": pyproject("1.0.1")}, "bump")

        conflict_code, conflict = self._invoke(scenario.clone)

        assert (conflict_code, refused_code) == (1, 1)
        assert conflict["state"] == "conflict"
        assert conflict["conflicted_files"] == ["pyproject.toml"]
        assert refusal["state"] == "refused"
        assert refusal["disposition"] == "dirty_worktree"

    def test_unavailable_exits_three(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        scenario = cut_straddle_repo(tmp_path, make_git_repo)

        code, payload = self._invoke(scenario.clone, base="nope")

        assert code == 3
        assert payload["state"] == "unavailable"
        assert payload["base_sha"] is None
        assert payload["detail"] == "origin/nope does not resolve"

    def test_help_states_the_contract(self) -> None:
        result = CliRunner().invoke(main, ["worktree", "changelog-sync", "--help"])
        text = " ".join(result.output.split())

        assert result.exit_code == 0
        assert "Never fetches: run `git fetch origin <base>` first" in text
        assert "only `state` tells them apart" in text
        assert "Exit 3: unavailable" in text
        assert "a merge already in progress in the worktree" in text
        assert "already an ancestor of HEAD" in text

    def test_base_is_required(self) -> None:
        result = CliRunner().invoke(main, ["worktree", "changelog-sync"])

        assert result.exit_code == 2
        assert "--base" in result.output
