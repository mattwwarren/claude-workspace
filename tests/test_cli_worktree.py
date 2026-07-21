"""Tests for the ``cw worktree gc`` CLI command (#1389 per-client isolation)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.tracker import TRACKER_GITHUB_ISSUES
from cw.worktree_gc import GcVerdict, WorktreeEntry, WorktreeGcReport, WorktreeGcResult
from tests.test_cli import _write_clients_yaml_for_test


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
        _write_clients_yaml_for_test(
            tmp_config_dir, [("client-a", str(ws_a)), ("client-b", str(ws_b))]
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
        _write_clients_yaml_for_test(
            tmp_config_dir, [("client-a", str(ws_a)), ("client-b", str(ws_b))]
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
        _write_clients_yaml_for_test(tmp_config_dir, [("client-a", str(ws_a))])
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
        _write_clients_yaml_for_test(tmp_config_dir, [("client-a", str(ws_a))])
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
        _write_clients_yaml_for_test(
            tmp_config_dir, [("client-a", str(ws_a)), ("client-b", str(ws_b))]
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
        _write_clients_yaml_for_test(tmp_config_dir, [("client-a", str(ws_a))])
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
