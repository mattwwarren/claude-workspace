"""Tests for cw.config - configuration loading and state persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import (
    detect_client_from_cwd,
    ensure_config,
    get_client,
    load_clients,
    load_state,
    save_state,
    show_config,
)
from cw.exceptions import CwError
from cw.models import DEFAULT_AUTO_PURPOSES, CwState, Session, SessionPurpose

if TYPE_CHECKING:
    from pathlib import Path


class TestLoadClients:
    def test_missing_file_returns_empty(self, tmp_config_dir: Path) -> None:
        result = load_clients()
        assert result == {}

    def test_valid_yaml_returns_clients(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        acme_dir = tmp_path / "acme"
        beta_dir = tmp_path / "beta"
        acme_dir.mkdir()
        beta_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {acme_dir}\n"
            "    default_branch: main\n"
            "  beta:\n"
            f"    workspace_path: {beta_dir}\n"
        )
        result = load_clients()
        assert len(result) == 2
        assert "acme" in result
        assert "beta" in result
        assert result["acme"].name == "acme"
        assert result["acme"].workspace_path == acme_dir

    def test_empty_yaml_returns_empty(self, tmp_config_dir: Path) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("")
        result = load_clients()
        assert result == {}

    def test_malformed_yaml_no_clients_key(self, tmp_config_dir: Path) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("something_else: true\n")
        result = load_clients()
        assert result == {}

    def test_auto_purposes_from_yaml(
        self, tmp_config_dir: Path, tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  sigma:\n"
            f"    workspace_path: {ws_dir}\n"
            "    auto_purposes: [impl, review]\n"
        )
        result = load_clients()
        assert len(result["sigma"].auto_purposes) == 2
        assert SessionPurpose.DEBT not in result["sigma"].auto_purposes

    def test_default_auto_purposes_when_not_specified(
        self, tmp_config_dir: Path, tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
        )
        result = load_clients()
        assert result["acme"].auto_purposes == DEFAULT_AUTO_PURPOSES


class TestGetClient:
    def test_valid_name_returns_config(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        acme_dir = tmp_path / "acme"
        acme_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {acme_dir}\n"
        )
        result = get_client("acme")
        assert result.name == "acme"

    def test_invalid_name_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        acme_dir = tmp_path / "acme"
        acme_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {acme_dir}\n"
        )
        with pytest.raises(CwError, match="Unknown client 'nope'"):
            get_client("nope")

    def test_error_shows_available_clients(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        alpha_dir = tmp_path / "alpha"
        beta_dir = tmp_path / "beta"
        alpha_dir.mkdir()
        beta_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  alpha:\n"
            f"    workspace_path: {alpha_dir}\n"
            "  beta:\n"
            f"    workspace_path: {beta_dir}\n"
        )
        with pytest.raises(CwError, match="Available: alpha, beta"):
            get_client("nope")

    def test_no_clients_shows_none(self, tmp_config_dir: Path) -> None:
        with pytest.raises(CwError, match=r"\(none configured\)"):
            get_client("nope")


class TestDetectClientFromCwd:
    def test_match_when_cwd_under_workspace(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_config_dir / "workspace"
        workspace.mkdir(parents=True)
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  proj:\n"
            f"    workspace_path: {workspace}\n"
        )
        monkeypatch.chdir(workspace)
        result = detect_client_from_cwd()
        assert result is not None
        assert result.name == "proj"

    def test_no_match_returns_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  proj:\n"
            "    workspace_path: /nowhere/special\n"
        )
        monkeypatch.chdir(tmp_config_dir)
        result = detect_client_from_cwd()
        assert result is None


class TestLoadSaveState:
    def test_missing_file_returns_empty_state(self, tmp_config_dir: Path) -> None:
        state = load_state()
        assert state.sessions == []

    def test_round_trip(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        state = CwState(
            sessions=[
                Session(
                    id="test1234",
                    name="c/impl",
                    client="c",
                    purpose=SessionPurpose.IMPL,
                    workspace_path=ws_dir,
                )
            ]
        )
        save_state(state)
        loaded = load_state()
        assert len(loaded.sessions) == 1
        assert loaded.sessions[0].id == "test1234"
        assert loaded.sessions[0].name == "c/impl"

    def test_save_creates_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state_dir = tmp_path / "new" / "state" / "dir"
        state_file = state_dir / "sessions.json"
        monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
        monkeypatch.setattr("cw.config.STATE_FILE", state_file)

        save_state(CwState())
        assert state_file.exists()


class TestEnsureConfig:
    def test_creates_dir_and_file(self, tmp_config_dir: Path) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        # Remove the file that fixture may have created
        if clients_file.exists():
            clients_file.unlink()

        ensure_config()
        assert clients_file.exists()

    def test_idempotent(self, tmp_config_dir: Path) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("clients:\n  existing: true\n")
        original_content = clients_file.read_text()

        ensure_config()
        assert clients_file.read_text() == original_content


class TestShowConfig:
    def test_no_clients(
        self, tmp_config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        show_config()
        output = capsys.readouterr().out
        assert "No clients configured" in output

    def test_with_clients(
        self, tmp_config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        acme_dir = tmp_path / "acme"
        acme_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {acme_dir}\n"
            "    default_branch: develop\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "acme:" in output
        assert str(acme_dir) in output
        assert "develop" in output

    def test_with_custom_purposes(
        self, tmp_config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  sigma:\n"
            f"    workspace_path: {ws_dir}\n"
            "    auto_purposes: [impl, review]\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "purposes: impl, review" in output

    def test_default_purposes_not_shown(
        self, tmp_config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "purposes:" not in output

    def test_with_worktree(
        self, tmp_config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        acme_dir = tmp_path / "acme"
        worktree_dir = tmp_path / "acme-worktrees"
        acme_dir.mkdir()
        worktree_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {acme_dir}\n"
            f"    worktree_base: {worktree_dir}\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "worktrees:" in output
        assert str(worktree_dir) in output
