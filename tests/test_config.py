"""Tests for cw.config - configuration loading and state persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import (
    detect_client_from_cwd,
    ensure_config,
    get_client,
    init_client,
    load_clients,
    load_state,
    save_state,
    show_config,
)
from cw.exceptions import CwError
from cw.models import DEFAULT_AUTO_PURPOSES, CwState, Session, SessionPurpose

if TYPE_CHECKING:
    from collections.abc import Callable
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

    def test_invalid_client_name_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        ws = tmp_path / "bad"
        ws.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  'bad;name':\n    workspace_path: {ws}\n")
        with pytest.raises(CwError, match="Invalid client name"):
            load_clients()

    def test_valid_client_name_patterns(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ok"
        ws.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  my-project.v2:\n    workspace_path: {ws}\n"
        )
        result = load_clients()
        assert "my-project.v2" in result

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
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  sigma:\n"
            f"    workspace_path: {ws_dir}\n"
            "    auto_purposes: [impl, idea]\n"
        )
        result = load_clients()
        assert len(result["sigma"].auto_purposes) == 2
        assert SessionPurpose.DEBT not in result["sigma"].auto_purposes

    def test_default_auto_purposes_when_not_specified(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {ws_dir}\n")
        result = load_clients()
        assert result["acme"].auto_purposes == DEFAULT_AUTO_PURPOSES


class TestLoadWorktreeClients:
    def test_worktree_client_from_yaml(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        repo = tmp_path / "meta-work"
        repo.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  client-a:\n    repo_path: {repo}\n    branch: client-a\n"
        )
        result = load_clients()
        assert len(result) == 1
        c = result["client-a"]
        assert c.is_worktree_client is True
        assert c.repo_path == repo
        assert c.branch == "client-a"
        # workspace_path sentinel = repo_path
        assert c.workspace_path == repo

    def test_mixed_legacy_and_worktree(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        repo = tmp_path / "meta-work"
        ws = tmp_path / "personal"
        repo.mkdir()
        ws.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  client-a:\n"
            f"    repo_path: {repo}\n"
            "    branch: client-a\n"
            "  personal:\n"
            f"    workspace_path: {ws}\n"
        )
        result = load_clients()
        assert result["client-a"].is_worktree_client is True
        assert result["personal"].is_worktree_client is False


class TestGetClient:
    def test_valid_name_returns_config(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        acme_dir = tmp_path / "acme"
        acme_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {acme_dir}\n")
        result = get_client("acme")
        assert result.name == "acme"

    def test_invalid_name_raises(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        acme_dir = tmp_path / "acme"
        acme_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {acme_dir}\n")
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
        clients_file.write_text(f"clients:\n  proj:\n    workspace_path: {workspace}\n")
        monkeypatch.chdir(workspace)
        result = detect_client_from_cwd()
        assert result is not None
        assert result.name == "proj"

    def test_no_match_returns_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n  proj:\n    workspace_path: /nowhere/special\n"
        )
        monkeypatch.chdir(tmp_config_dir)
        result = detect_client_from_cwd()
        assert result is None

    def test_skips_worktree_clients(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Worktree clients have sentinel workspace_path and should be skipped."""
        repo = tmp_config_dir / "repo"
        repo.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  wt-client:\n    repo_path: {repo}\n    branch: wt-branch\n"
        )
        monkeypatch.chdir(repo)
        result = detect_client_from_cwd()
        assert result is None


class TestLoadSaveState:
    def test_missing_file_returns_empty_state(self, tmp_config_dir: Path) -> None:
        state = load_state()
        assert state.sessions == []

    def test_round_trip(self, tmp_config_dir: Path, tmp_path: Path) -> None:
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
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  sigma:\n"
            f"    workspace_path: {ws_dir}\n"
            "    auto_purposes: [impl, idea]\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "purposes: impl, idea" in output

    def test_default_purposes_not_shown(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {ws_dir}\n")
        show_config()
        output = capsys.readouterr().out
        assert "purposes:" not in output

    def test_worktree_client_display(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = tmp_path / "meta-work"
        repo.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  client-a:\n    repo_path: {repo}\n    branch: client-a\n"
        )
        show_config()
        output = capsys.readouterr().out
        assert "repo:" in output
        assert str(repo) in output
        assert "branch: client-a" in output
        # Should NOT show "path:" for worktree clients
        assert "path:" not in output

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


class TestInitClient:
    def test_init_creates_config(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("new-project")
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.unlink(missing_ok=True)

        init_client("new-project", repo)

        assert clients_file.exists()
        clients = load_clients()
        assert "new-project" in clients
        assert clients["new-project"].workspace_path == repo

    def test_init_appends_to_existing(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo_a = make_git_repo("project-a")
        repo_b = make_git_repo("project-b")

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"# My config\nclients:\n  project-a:\n    workspace_path: {repo_a}\n"
        )

        init_client("project-b", repo_b)

        # Both should be loadable
        clients = load_clients()
        assert "project-a" in clients
        assert "project-b" in clients

        # Comment should be preserved in raw text
        raw = clients_file.read_text()
        assert "# My config" in raw

    def test_init_rejects_duplicate(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("dup-project")

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n  dup-project:\n    workspace_path: {repo}\n"
        )

        with pytest.raises(CwError, match="already exists"):
            init_client("dup-project", repo)

    def test_init_rejects_name_with_special_chars(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(CwError, match="Invalid client name"):
            init_client("bad;name", tmp_path)

    def test_init_rejects_name_starting_with_dash(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(CwError, match="Invalid client name"):
            init_client("-starts-with-dash", tmp_path)

    def test_init_validates_path_exists(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        nonexistent = tmp_path / "does-not-exist"

        with pytest.raises(CwError, match="does not exist"):
            init_client("test", nonexistent)

    def test_init_validates_git_repo(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        not_git = tmp_path / "not-a-repo"
        not_git.mkdir()

        with pytest.raises(CwError, match="not a git repository"):
            init_client("test", not_git)

    def test_init_with_custom_branch(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("repo")

        init_client("test", repo, default_branch="develop")

        clients = load_clients()
        assert clients["test"].default_branch == "develop"

    def test_init_with_purposes(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("repo")

        init_client("test", repo, auto_purposes=["impl", "idea"])

        clients = load_clients()
        purposes = [p.value for p in clients["test"].auto_purposes]
        assert purposes == ["impl", "idea"]

    def test_xdg_config_home_respected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """XDG_CONFIG_HOME should control config directory location."""
        xdg_config = tmp_path / "xdg-config"
        xdg_data = tmp_path / "xdg-data"

        # Patch derived paths to use custom directories
        config_dir = xdg_config / "cw"
        state_dir = xdg_data / "cw"
        clients_file = config_dir / "clients.yaml"
        state_file = state_dir / "sessions.json"

        config_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
        monkeypatch.setattr("cw.config.CLIENTS_FILE", clients_file)
        monkeypatch.setattr("cw.config.STATE_FILE", state_file)

        repo = make_git_repo("repo")

        init_client("test", repo)

        assert clients_file.exists()
        clients = load_clients()
        assert "test" in clients

    def test_init_handles_empty_config_file(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("repo")
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("")

        init_client("test", repo)

        clients = load_clients()
        assert "test" in clients

    def test_init_rejects_invalid_purposes(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("repo")
        with pytest.raises(CwError, match="Invalid purpose"):
            init_client("test", repo, auto_purposes=["impl", "bogus"])

    def test_init_rejects_malformed_config(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("repo")
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("something_else: true\n")

        with pytest.raises(CwError, match="no 'clients:' key"):
            init_client("test", repo)
