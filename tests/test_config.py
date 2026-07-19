"""Tests for cw.config - configuration loading and state persistence."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import cw.config
from cw.config import (
    _REAL_CONFIG_DIR,
    _REAL_STATE_DIR,
    _backup_state_file,
    _under_pytest,
    ensure_config,
    get_client,
    init_client,
    load_clients,
    load_state,
    migrate_cw_state,
    mutate_state,
    refuse_real_state_write,
    save_state,
    sessions_lock,
    show_config,
)
from cw.exceptions import CwError, SessionsLockReentryError
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    DEFAULT_AUTO_PURPOSES,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
)
from tests.conftest import _make_daemon_session

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

    def test_load_clients_with_worker_model(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    worker_model: claude-sonnet-4-6-20251015\n"
        )
        result = load_clients()
        assert result["acme"].worker_model == "claude-sonnet-4-6-20251015"

    def test_default_worker_model_is_none_when_unset(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {ws_dir}\n")
        result = load_clients()
        assert result["acme"].worker_model is None

    def test_load_clients_with_operator_github_login(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    operator_github_login: alice\n"
        )
        result = load_clients()
        assert result["acme"].operator_github_login == "alice"

    def test_default_operator_github_login_is_none_when_unset(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(f"clients:\n  acme:\n    workspace_path: {ws_dir}\n")
        result = load_clients()
        assert result["acme"].operator_github_login is None

    def test_typo_lane_key_raises_config_validation_error(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A typo'd lane key (review_recipies) is wrapped as ConfigValidationError,
        naming the offending file and key, instead of a raw pydantic
        ValidationError leaking out of load_clients() (#1200)."""
        from cw.exceptions import ConfigValidationError

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    lanes:\n"
            "      - name: default\n"
            "        review_recipies:\n"
            "          address_review: true\n"
        )
        with pytest.raises(
            ConfigValidationError, match=r"(?s)clients\.yaml.*review_recipies"
        ):
            load_clients()


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


class TestDiagnosticsDir:
    def test_diagnostics_dir_accessor_matches_state_dir_convention(
        self, tmp_config_dir: Path
    ) -> None:
        """diagnostics_dir(sid) resolves under state_dir(), monkeypatchable the
        same way state_dir() is (via the autouse tmp_config_dir fixture)."""
        from cw.config import diagnostics_dir, state_dir

        sid = "abc123"
        expected = state_dir() / "sessions" / sid / "diagnostics"
        assert diagnostics_dir(sid) == expected
        # Reflects the fixture-monkeypatched STATE_DIR, not the real one.
        assert diagnostics_dir(sid).is_relative_to(state_dir())
        assert not diagnostics_dir(sid).is_relative_to(_REAL_STATE_DIR)


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

    def test_save_state_refuses_real_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """save_state must refuse to write under the real state dir (#1017)."""
        real_state_file = _REAL_STATE_DIR / "sessions.json"
        monkeypatch.setattr("cw.config.STATE_DIR", _REAL_STATE_DIR)
        monkeypatch.setattr("cw.config.STATE_FILE", real_state_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            save_state(CwState())

        mock_write.assert_not_called()


class TestRefuseRealStateWrite:
    """Tests for refuse_real_state_write — the #1017 belt-and-suspenders guard."""

    def test_raises_for_path_under_real_state_dir(self) -> None:
        with pytest.raises(CwError, match=r"#1017"):
            refuse_real_state_write(_REAL_STATE_DIR / "dev_queue.json")

    def test_raises_for_path_under_real_config_dir(self) -> None:
        with pytest.raises(CwError, match=r"pytest"):
            refuse_real_state_write(_REAL_CONFIG_DIR / "clients.yaml")

    def test_noop_for_tmp_path(self, tmp_path: Path) -> None:
        refuse_real_state_write(tmp_path / "dev_queue.json")

    def test_noop_when_not_under_pytest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cw.config, "_under_pytest", lambda: False)
        # Would raise if the guard were active; must be a silent no-op.
        refuse_real_state_write(_REAL_STATE_DIR / "dev_queue.json")

    def test_resolves_dotdot_relative_paths(self, tmp_path: Path) -> None:
        assert _under_pytest() is True
        escaping = _REAL_STATE_DIR.parent / "cw" / ".." / "cw" / "dev_queue.json"
        with pytest.raises(CwError, match="refusing real-state write"):
            refuse_real_state_write(escaping)

    def test_resolves_symlinked_paths(self, tmp_path: Path) -> None:
        """A symlink from a tmp-rooted path into the real state dir must
        still be caught — the guard resolves via ``.resolve()``, not string
        prefix matching, so a symlink-based evasion is not a bypass."""
        link = tmp_path / "escape_link"
        link.symlink_to(_REAL_STATE_DIR, target_is_directory=True)
        with pytest.raises(CwError, match="refusing real-state write"):
            refuse_real_state_write(link / "dev_queue.json")


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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Strip GIT_* vars that leak from Claude Code worktree environments
        # and would make any path appear to be inside a git repo.
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            monkeypatch.delenv(key, raising=False)

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

    def test_init_client_refuses_real_config_dir(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """init_client must refuse to write clients.yaml under the real config
        dir (#1017)."""
        repo = make_git_repo("repo")
        real_clients_file = _REAL_CONFIG_DIR / "clients.yaml"
        monkeypatch.setattr("cw.config.CONFIG_DIR", _REAL_CONFIG_DIR)
        monkeypatch.setattr("cw.config.CLIENTS_FILE", real_clients_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            init_client("test", repo)

        mock_write.assert_not_called()


class TestMigrateCwState:
    def test_new_state_carries_schema_version(self) -> None:
        state = CwState()
        assert state.schema_version == CW_STATE_SCHEMA_VERSION

    def test_rename_zellij_pane_to_surface_ref(self) -> None:
        # Zellij pane IDs ("0:1.0") are non-hex → cleared to None by the v5
        # migration that runs on files below schema_version 5. The rename step
        # still fires (zellij_pane is removed) but the subsequent non-hex
        # cleaner nulls out the legacy value.
        raw = {"sessions": [{"id": "s1", "zellij_pane": "0:1.0"}]}
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["surface_ref"] is None
        assert "zellij_pane" not in session

    def test_drop_zellij_pane_when_surface_ref_already_set(self) -> None:
        # "fresh" is non-hex so it's also cleared by the v5 migration pass.
        raw = {
            "sessions": [
                {"id": "s1", "zellij_pane": "stale", "surface_ref": "fresh"},
            ]
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["surface_ref"] is None
        assert "zellij_pane" not in session

    def test_drop_zellij_tab_unconditionally(self) -> None:
        raw = {"sessions": [{"id": "s1", "zellij_tab": "tab0"}]}
        migrated = migrate_cw_state(raw)
        assert "zellij_tab" not in migrated["sessions"][0]

    def test_unknown_origin_coerced_to_user(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = {"sessions": [{"id": "s1", "origin": "delegate"}]}
        with caplog.at_level("WARNING", logger="cw.config"):
            migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["origin"] == SessionOrigin.USER.value
        assert any("unknown origin" in rec.message for rec in caplog.records)

    def test_known_origin_preserved(self) -> None:
        raw = {"sessions": [{"id": "s1", "origin": "daemon"}]}
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["origin"] == "daemon"

    def test_load_state_survives_unknown_origin(self, tmp_config_dir: Path) -> None:
        # Simulate a sessions.json from a diverged branch that contains
        # an origin value this version of cw doesn't know about.
        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        state_file = state_dir / "sessions.json"
        state_file.write_text(
            '{"sessions": [{'
            '"id": "stale01",'
            '"name": "cw/impl",'
            '"client": "cw",'
            '"purpose": "impl",'
            '"origin": "delegate",'
            f'"workspace_path": "{state_dir}"'
            "}]}"
        )
        state = load_state()
        assert state.sessions[0].origin == SessionOrigin.USER

    def test_v1_to_v2_fills_linkage_fields(self) -> None:
        raw = {
            "schema_version": 1,
            "sessions": [{"id": "s1"}],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["parent_session_id"] is None
        assert session["worker_session_ids"] == []
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v2_file_is_idempotent(self) -> None:
        raw = {
            "schema_version": 2,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": "root0001",
                    "worker_session_ids": ["abc123"],
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["parent_session_id"] == "root0001"
        assert session["worker_session_ids"] == ["abc123"]
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_multiple_sessions_all_get_linkage_fields(self) -> None:
        raw = {
            "schema_version": 1,
            "sessions": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
        }
        migrated = migrate_cw_state(raw)
        for session in migrated["sessions"]:
            assert session["parent_session_id"] is None
            assert session["worker_session_ids"] == []

    def test_v1_zellij_and_linkage_both_migrate(self) -> None:
        raw = {
            "schema_version": 1,
            "sessions": [{"id": "s1", "zellij_pane": "0:1.0"}],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        # Zellij armor ran (rename happened, but "0:1.0" is non-hex so v5
        # cleaner nulls it out)
        assert session["surface_ref"] is None
        assert "zellij_pane" not in session
        # Linkage fields filled
        assert session["parent_session_id"] is None
        assert session["worker_session_ids"] == []
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_no_sessions_key_still_bumps_schema_version(self) -> None:
        # A state with no sessions key at all is legitimately empty; schema
        # version should still be stamped so re-saves are at current version.
        raw: dict[str, int] = {"schema_version": 1}
        migrated = migrate_cw_state(raw)
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_missing_schema_version_gets_stamped(self) -> None:
        # A file without schema_version (very old or hand-crafted) should be
        # stamped with the current version after migration runs.
        raw = {"sessions": [{"id": "s1"}]}
        migrated = migrate_cw_state(raw)
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v2_to_v3_fills_last_result_default(self) -> None:
        raw = {
            "schema_version": 2,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["last_result"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v3_last_result_preserved_idempotently(self) -> None:
        existing = {"schema_version": 1, "status": "shipped"}
        raw = {
            "schema_version": 3,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": existing,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["last_result"] == existing

    def test_non_list_sessions_does_not_bump_version(self) -> None:
        # Malformed payload: sessions is not a list. The corruption must NOT
        # be certified as fully migrated — schema_version stays unchanged so
        # the problem surfaces downstream.
        raw = {"schema_version": 1, "sessions": "oops"}
        migrated = migrate_cw_state(raw)
        assert migrated["schema_version"] == 1

    def test_v3_to_v4_fills_cost_fields(self) -> None:
        """migrate_cw_state fills cost_usd and cost_breakdown on sessions."""
        raw = {
            "schema_version": 3,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["cost_usd"] is None
        assert session["cost_breakdown"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v4_cost_fields_preserved_idempotently(self) -> None:
        """Existing cost values survive a second migration pass."""
        raw = {
            "schema_version": 4,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": 1.5,
                    "cost_breakdown": {"claude-sonnet-4-6": 1.5},
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["cost_usd"] == 1.5
        assert session["cost_breakdown"] == {"claude-sonnet-4-6": 1.5}

    def test_v8_to_v9_fills_session_lane_default(self) -> None:
        """migrate_cw_state fills lane=None on v8 sessions that lack the key."""
        raw = {
            "schema_version": 8,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["lane"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v9_session_lane_preserved_idempotently(self) -> None:
        """Existing non-None lane survives a migration pass."""
        raw = {
            "schema_version": 9,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": "my-lane",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["lane"] == "my-lane"

    def test_v9_to_v10_fills_session_stage_default(self) -> None:
        """migrate_cw_state fills stage=None on v9 sessions that lack the key."""
        raw = {
            "schema_version": 9,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["stage"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v10_session_stage_preserved_idempotently(self) -> None:
        """Existing non-None stage survives a migration pass."""
        raw = {
            "schema_version": 10,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": "impl",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["stage"] == "impl"

    def test_v11_to_v12_fills_consecutive_salvage_skips_default(self) -> None:
        """migrate_cw_state fills consecutive_salvage_skips=0 on v11 sessions
        that lack the key (#974)."""
        raw = {
            "schema_version": 11,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_salvage_skips"] == 0
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v12_consecutive_salvage_skips_preserved_idempotently(self) -> None:
        """Existing nonzero consecutive_salvage_skips survives a migration pass."""
        raw = {
            "schema_version": 12,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                    "consecutive_salvage_skips": 3,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_salvage_skips"] == 3

    def test_v12_to_v13_fills_liveness_bucket_default(self) -> None:
        """migrate_cw_state fills liveness_bucket='live' on v12 sessions
        that lack the key (GitHub #1001)."""
        raw = {
            "schema_version": 12,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                    "consecutive_salvage_skips": 0,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["liveness_bucket"] == "live"
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v13_liveness_bucket_preserved_idempotently(self) -> None:
        """Existing non-default liveness_bucket survives a migration pass."""
        raw = {
            "schema_version": 13,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                    "consecutive_salvage_skips": 0,
                    "liveness_bucket": "stale_30m",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["liveness_bucket"] == "stale_30m"

    def test_v13_to_v14_clears_stale_local_liveness(self) -> None:
        """A pre-v14 local_liveness handle (boot-relative start_time_ns) is
        cleared on migration, since it can never compare equal to a
        freshly-read epoch-relative value for the same live process
        (GitHub #921)."""
        raw = {
            "schema_version": 13,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                    "consecutive_salvage_skips": 0,
                    "liveness_bucket": "live",
                    "local_liveness": {"pid": 4242, "start_time_ns": 123456},
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["local_liveness"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v14_local_liveness_preserved_idempotently(self) -> None:
        """A local_liveness handle already on schema v14+ survives a
        migration pass unchanged (it's in the current epoch-relative
        format)."""
        raw = {
            "schema_version": 14,
            "sessions": [
                {
                    "id": "s1",
                    "parent_session_id": None,
                    "worker_session_ids": [],
                    "last_result": None,
                    "cost_usd": None,
                    "cost_breakdown": None,
                    "lane": None,
                    "stage": None,
                    "consecutive_salvage_skips": 0,
                    "liveness_bucket": "live",
                    "local_liveness": {
                        "pid": 4242,
                        "start_time_ns": 1782938077013950000,
                    },
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["local_liveness"] == {
            "pid": 4242,
            "start_time_ns": 1782938077013950000,
        }

    # -----------------------------------------------------------------------
    # Phase F: cmux surface_ref migration tests (schema v5)
    # -----------------------------------------------------------------------

    def test_migrate_clears_non_hex_surface_ref(self) -> None:
        """surface_ref like 'ws:0.1' (legacy cmux pane ID) should be cleared."""
        raw = {
            "schema_version": 4,
            "sessions": [
                {
                    "id": "s1",
                    "surface_ref": "ws:0.1",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["surface_ref"] is None

    def test_migrate_preserves_valid_hex_surface_ref(self) -> None:
        """surface_ref like 'a1b2c3d4' (8-char hex) should be left unchanged."""
        raw = {
            "schema_version": 4,
            "sessions": [
                {
                    "id": "s1",
                    "surface_ref": "a1b2c3d4",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["surface_ref"] == "a1b2c3d4"

    def test_migrate_preserves_none_surface_ref(self) -> None:
        """surface_ref of None should be left as None."""
        raw = {
            "schema_version": 4,
            "sessions": [
                {
                    "id": "s1",
                    "surface_ref": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["surface_ref"] is None

    def test_migrate_bumps_schema_version_to_current(self) -> None:
        """After migration, schema_version must equal CW_STATE_SCHEMA_VERSION."""
        from cw.models import CW_STATE_SCHEMA_VERSION

        raw: dict[str, object] = {
            "schema_version": 4,
            "sessions": [],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_migrate_round_trip_clears_legacy_surface_ref(
        self, tmp_config_dir: Path
    ) -> None:
        """Round-trip: write v4 state with legacy surface_ref, load_state(),
        assert the loaded session's surface_ref is None."""
        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        sf = state_dir / "sessions.json"
        import json

        sf.write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "sessions": [
                        {
                            "id": "roundtrip",
                            "name": "c/impl",
                            "client": "c",
                            "purpose": "impl",
                            "workspace_path": str(state_dir),
                            "surface_ref": "fake-pane-1",
                        }
                    ],
                }
            )
        )
        loaded = load_state()
        assert len(loaded.sessions) == 1
        assert loaded.sessions[0].surface_ref is None

    def test_backup_created_with_original_content(self, tmp_config_dir: Path) -> None:
        """_backup_state_file() creates .sessions.json.0.x-backup with
        the original pre-migration content."""
        import json

        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        sf = state_dir / "sessions.json"
        original = {
            "schema_version": 4,
            "sessions": [{"id": "orig", "surface_ref": "ws:0.2"}],
        }
        sf.write_text(json.dumps(original))

        _backup_state_file(original)

        backup = state_dir / ".sessions.json.0.x-backup"
        assert backup.exists()
        content = json.loads(backup.read_text())
        assert content["schema_version"] == 4
        assert content["sessions"][0]["surface_ref"] == "ws:0.2"

    def test_loaded_state_has_none_surface_ref_and_current_version(
        self, tmp_config_dir: Path
    ) -> None:
        """Loaded state after migration has surface_ref=None and current version."""
        import json

        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        sf = state_dir / "sessions.json"
        sf.write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "sessions": [
                        {
                            "id": "chk01",
                            "name": "c/impl",
                            "client": "c",
                            "purpose": "impl",
                            "workspace_path": str(state_dir),
                            "surface_ref": "cmux-legacy",
                        }
                    ],
                }
            )
        )
        from cw.models import CW_STATE_SCHEMA_VERSION

        loaded = load_state()
        assert loaded.schema_version == CW_STATE_SCHEMA_VERSION
        assert loaded.sessions[0].surface_ref is None

    def test_backup_is_idempotent(self, tmp_config_dir: Path) -> None:
        """Second call to _backup_state_file() does NOT overwrite backup."""
        import json

        state_dir = tmp_config_dir / ".local" / "share" / "cw"
        sf = state_dir / "sessions.json"
        original = {
            "schema_version": 4,
            "sessions": [{"id": "idem", "surface_ref": "tmux-pane"}],
        }
        sf.write_text(json.dumps(original))

        # First call creates backup
        _backup_state_file(original)
        backup = state_dir / ".sessions.json.0.x-backup"
        first_mtime = backup.stat().st_mtime

        # Overwrite the state file to simulate a post-migration state
        migrated = {"schema_version": 5, "sessions": []}
        sf.write_text(json.dumps(migrated))

        # Second call must NOT overwrite the backup (it already exists)
        _backup_state_file(migrated)
        assert backup.stat().st_mtime == first_mtime


class TestOrchestratorConfigUsageLimitBackoff:
    """OrchestratorConfig.usage_limit_backoff_seconds field."""

    def test_default_is_3600(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig()
        assert config.usage_limit_backoff_seconds == 3600

    def test_can_be_overridden(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig(usage_limit_backoff_seconds=7200)
        assert config.usage_limit_backoff_seconds == 7200


class TestOrchestratorConfigReapPolicy:
    """OrchestratorConfig.reap_policy field and fail-safe validator."""

    def test_default_is_signal_only(self) -> None:
        from cw.models import OrchestratorConfig, ReapPolicy

        config = OrchestratorConfig()
        assert config.reap_policy == ReapPolicy.SIGNAL_ONLY

    def test_explicit_auto(self) -> None:
        from cw.models import OrchestratorConfig, ReapPolicy

        config = OrchestratorConfig.model_validate({"reap_policy": "auto"})
        assert config.reap_policy == ReapPolicy.AUTO

    def test_unknown_string_coerces_to_signal_only(self) -> None:
        from cw.models import OrchestratorConfig, ReapPolicy

        config = OrchestratorConfig.model_validate({"reap_policy": "bogus"})
        assert config.reap_policy == ReapPolicy.SIGNAL_ONLY

    def test_non_string_coerces_to_signal_only(self) -> None:
        from cw.models import OrchestratorConfig, ReapPolicy

        config = OrchestratorConfig.model_validate({"reap_policy": True})
        assert config.reap_policy == ReapPolicy.SIGNAL_ONLY

    def test_numeric_coerces_to_signal_only(self) -> None:
        from cw.models import OrchestratorConfig, ReapPolicy

        config = OrchestratorConfig.model_validate({"reap_policy": 42})
        assert config.reap_policy == ReapPolicy.SIGNAL_ONLY

    def test_unknown_key_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """load_orchestrator_config() wraps a pydantic ValidationError from an
        unrecognized top-level key as ConfigValidationError (#1200)."""
        from cw.config import load_orchestrator_config, orchestrator_config_file
        from cw.exceptions import ConfigValidationError

        orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
        orchestrator_config_file().write_text("bogus_field: 1\n")
        with pytest.raises(ConfigValidationError, match=r"orchestrator\.yaml"):
            load_orchestrator_config()


class TestMutateState:
    """Tests for mutate_state() — load-mutate-save under sessions_lock."""

    def _make_session(self, sid: str) -> Session:
        from datetime import UTC, datetime
        from pathlib import Path

        return _make_daemon_session(
            id=sid,
            name=f"client-a/{sid}",
            client="client-a",
            origin=SessionOrigin.USER,
            workspace_path=Path("/tmp/ws"),
            surface_ref=None,
            worktree_path=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_mutate_state_applies_callback_and_persists(
        self, tmp_config_dir: Path
    ) -> None:
        """Callback mutation is reflected in reloaded state from disk."""
        s1 = self._make_session("ms-sess-1")
        save_state(CwState(sessions=[s1]))

        s2 = self._make_session("ms-sess-2")

        def _append(state: CwState) -> None:
            state.sessions.append(s2)

        mutate_state(_append)

        reloaded = load_state()
        ids = {s.id for s in reloaded.sessions}
        assert "ms-sess-1" in ids
        assert "ms-sess-2" in ids

    def test_mutate_state_releases_lock_on_exception(
        self, tmp_config_dir: Path
    ) -> None:
        """Lock is released even when the callback raises; subsequent call succeeds."""
        save_state(CwState())

        def _raises(state: CwState) -> None:
            msg = "intentional error"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="intentional error"):
            mutate_state(_raises)

        # A second call must succeed — no deadlock from an unreleased lock.
        s = self._make_session("ms-after-exc")

        def _append(state: CwState) -> None:
            state.sessions.append(s)

        mutate_state(_append)

        reloaded = load_state()
        assert any(sess.id == "ms-after-exc" for sess in reloaded.sessions)

    def test_mutate_state_returns_mutated_state(self, tmp_config_dir: Path) -> None:
        """Return value is the post-mutation CwState, not the pre-mutation one."""
        save_state(CwState())
        s = self._make_session("ms-return-1")

        def _append(state: CwState) -> None:
            state.sessions.append(s)

        result = mutate_state(_append)

        assert isinstance(result, CwState)
        assert any(sess.id == "ms-return-1" for sess in result.sessions)


# ---------------------------------------------------------------------------
# TestSessionsLockReentrancy
# ---------------------------------------------------------------------------


class TestSessionsLockReentrancy:
    """Tests for sessions_lock()'s same-thread reentrancy guard (GitHub #1228)."""

    def test_nested_sessions_lock_raises_reentry_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A nested acquisition raises instead of blocking in flock().

        NOTE: a wrong implementation that actually calls flock() a second
        time here would HANG the whole test run (not just fail an
        assertion) — the guard must raise before any second flock() syscall.
        """
        with sessions_lock(), pytest.raises(SessionsLockReentryError), sessions_lock():
            pytest.fail("must not reach body")

    def test_sessions_lock_sequential_reacquire_still_succeeds(
        self, tmp_config_dir: Path
    ) -> None:
        """Two sequential, non-nested acquisitions both succeed."""
        with sessions_lock():
            pass
        with sessions_lock():
            pass

    def test_sessions_lock_releases_guard_on_exception(
        self, tmp_config_dir: Path
    ) -> None:
        """The held flag resets via finally even on exceptional exit."""
        msg = "boom"
        with pytest.raises(ValueError, match="boom"), sessions_lock():
            raise ValueError(msg)

        # A second call must succeed — no stuck "held" flag from the raise.
        with sessions_lock():
            pass


# ---------------------------------------------------------------------------
# TestLoadEffectiveConfig
# ---------------------------------------------------------------------------


class TestLoadEffectiveConfig:
    """Tests for load_effective_config() and ConcurrencyOverrides."""

    def test_declared_only_no_override_file(self, tmp_config_dir: Path) -> None:
        """No override file → effective config equals declared config."""
        from cw.config import load_effective_config, load_orchestrator_config

        declared = load_orchestrator_config()
        effective = load_effective_config()
        assert effective.default_ceiling == declared.default_ceiling
        assert effective.max_parallel_clients == declared.max_parallel_clients

    def test_override_wins_max_parallel_clients(self, tmp_config_dir: Path) -> None:
        """Override file with max_parallel_clients=5 wins over declared None."""
        from cw.config import (
            concurrency_override_file,
            concurrency_override_lock,
            load_effective_config,
        )
        from cw.models import ConcurrencyOverrides

        overrides = ConcurrencyOverrides(max_parallel_clients=5)
        with concurrency_override_lock():
            concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
            concurrency_override_file().write_text(overrides.model_dump_json())

        effective = load_effective_config()
        assert effective.max_parallel_clients == 5

    def test_override_wins_per_client_ceiling(self, tmp_config_dir: Path) -> None:
        """Override file with client ceiling overrides declared value."""
        from cw.config import (
            concurrency_override_file,
            concurrency_override_lock,
            load_effective_config,
        )
        from cw.models import ClientConcurrencyOverride, ConcurrencyOverrides

        overrides = ConcurrencyOverrides(
            clients={"acme": ClientConcurrencyOverride(ceiling=7)}
        )
        with concurrency_override_lock():
            concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
            concurrency_override_file().write_text(overrides.model_dump_json())

        effective = load_effective_config()
        assert effective.per_client_ceiling.get("acme") == 7

    def test_no_override_file_returns_pure_declared(self, tmp_config_dir: Path) -> None:
        """Absent override file: returns declared config unchanged."""
        from cw.config import concurrency_override_file, load_effective_config

        assert not concurrency_override_file().exists()
        effective = load_effective_config()
        assert effective.max_parallel_clients is None  # default declared value

    def test_concurrency_override_lock_creates_and_releases(
        self, tmp_config_dir: Path
    ) -> None:
        """concurrency_override_lock() creates lock file and releases on exit."""
        from cw.config import concurrency_override_lock, concurrency_override_lock_file

        lock_path = concurrency_override_lock_file()
        with concurrency_override_lock():
            assert lock_path.exists()
        # Lock released — file still exists but lock is no longer held

    def test_concurrency_overrides_null_keys_accepted(self) -> None:
        """ConcurrencyOverrides accepts None values on all keys."""
        from cw.models import ConcurrencyOverrides

        o = ConcurrencyOverrides(max_parallel_clients=None)
        assert o.max_parallel_clients is None

    def test_concurrency_overrides_int_coercion(self) -> None:
        """ConcurrencyOverrides accepts integer values."""
        from cw.models import ConcurrencyOverrides

        o = ConcurrencyOverrides(max_parallel_clients=3)
        assert o.max_parallel_clients == 3

    def test_corrupt_override_file_returns_empty(self, tmp_config_dir: Path) -> None:
        """Corrupt JSON in override file returns empty ConcurrencyOverrides."""
        from cw.config import concurrency_override_file, load_effective_config

        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        concurrency_override_file().write_text("not-valid-json{{{")
        effective = load_effective_config()
        # Should not raise; falls back to declared config unchanged
        assert effective is not None

    def test_lanes_override_populated_does_not_crash(
        self, tmp_config_dir: Path
    ) -> None:
        """Non-empty overrides.lanes does not crash load_effective_config."""
        from cw.config import (
            _save_concurrency_overrides,
            concurrency_override_file,
            load_effective_config,
        )
        from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride

        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        overrides = ConcurrencyOverrides(
            lanes={"acme/default": LaneConcurrencyOverride(paused=True)}
        )
        _save_concurrency_overrides(overrides)
        effective = load_effective_config()
        assert effective is not None

    def test_save_concurrency_overrides_refuses_real_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_save_concurrency_overrides must refuse a real-state write (#1017)."""
        from cw.config import _save_concurrency_overrides
        from cw.models import ConcurrencyOverrides

        real_override_file = _REAL_STATE_DIR / "concurrency_overrides.json"
        monkeypatch.setattr("cw.config.CONCURRENCY_OVERRIDE_FILE", real_override_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            _save_concurrency_overrides(ConcurrencyOverrides())

        mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# TestLoadEffectiveClients
# ---------------------------------------------------------------------------


class TestLoadEffectiveClients:
    """Tests for load_effective_clients() — lane pause override propagation."""

    def _write_clients_yaml(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  acme:\n    workspace_path: {ws}\n"
            f"    lanes:\n      - name: default\n        max_parallel: 1\n"
            f"      - name: fast\n        max_parallel: 2\n"
        )

    def test_no_overrides_returns_declared(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No override file → effective clients equal declared clients."""
        from cw.config import load_effective_clients

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        clients = load_effective_clients()
        assert "acme" in clients
        lane_names = [ln.name for ln in clients["acme"].effective_lanes]
        assert "default" in lane_names
        assert "fast" in lane_names
        assert not any(ln.paused for ln in clients["acme"].effective_lanes)

    def test_lane_pause_override_applied(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Override paused=True for a lane propagates to effective client lanes."""
        from cw.config import (
            _save_concurrency_overrides,
            concurrency_override_file,
            load_effective_clients,
        )
        from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        overrides = ConcurrencyOverrides(
            lanes={"acme/fast": LaneConcurrencyOverride(paused=True)}
        )
        _save_concurrency_overrides(overrides)

        clients = load_effective_clients()
        lane_map = {ln.name: ln for ln in clients["acme"].effective_lanes}
        assert lane_map["fast"].paused is True
        assert lane_map["default"].paused is False

    def test_lane_resume_override_clears_yaml_pause(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Override paused=False re-enables a lane that was paused in yaml."""
        from cw.config import (
            _save_concurrency_overrides,
            concurrency_override_file,
            load_effective_clients,
        )
        from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  acme:\n    workspace_path: {ws}\n"
            "    lanes:\n"
            "      - name: default\n"
            "        max_parallel: 1\n"
            "        paused: true\n"
        )
        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        overrides = ConcurrencyOverrides(
            lanes={"acme/default": LaneConcurrencyOverride(paused=False)}
        )
        _save_concurrency_overrides(overrides)

        clients = load_effective_clients()
        lane_map = {ln.name: ln for ln in clients["acme"].effective_lanes}
        assert lane_map["default"].paused is False

    def test_no_lane_overrides_returns_same_objects(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No lane overrides → load_effective_clients returns load_clients result."""
        from cw.config import load_clients, load_effective_clients

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        assert load_effective_clients() == load_clients()


# ---------------------------------------------------------------------------
# TestGetEffectiveClient
# ---------------------------------------------------------------------------


class TestGetEffectiveClient:
    """Tests for get_effective_client() — single-client effective lookup (#875)."""

    def _write_clients_yaml(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  acme:\n    workspace_path: {ws}\n"
            f"    lanes:\n      - name: default\n        max_parallel: 1\n"
            f"      - name: fast\n        max_parallel: 2\n"
        )

    def test_returns_declared_when_no_override(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No override → the effective client's lanes match the declared state."""
        from cw.config import get_effective_client

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        client = get_effective_client("acme")
        lane_map = {ln.name: ln for ln in client.effective_lanes}
        assert lane_map["fast"].paused is False

    def test_reflects_lane_pause_override(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A paused override propagates to the effective client's lane."""
        from cw.config import (
            _save_concurrency_overrides,
            concurrency_override_file,
            get_effective_client,
        )
        from cw.models import ConcurrencyOverrides, LaneConcurrencyOverride

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        concurrency_override_file().parent.mkdir(parents=True, exist_ok=True)
        _save_concurrency_overrides(
            ConcurrencyOverrides(
                lanes={"acme/fast": LaneConcurrencyOverride(paused=True)}
            )
        )

        client = get_effective_client("acme")
        lane_map = {ln.name: ln for ln in client.effective_lanes}
        assert lane_map["fast"].paused is True

    def test_unknown_client_raises(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """An unknown client name raises CwError with the available-clients hint."""
        from cw.config import get_effective_client

        self._write_clients_yaml(tmp_config_dir, tmp_path)
        with pytest.raises(CwError, match="Unknown client 'nope'"):
            get_effective_client("nope")


class TestOrchestratorConfigLaneCircuitBreaker:
    """OrchestratorConfig.lane_circuit_breaker_threshold field (#875)."""

    def test_lane_circuit_breaker_threshold_default(self) -> None:
        from cw.models import OrchestratorConfig

        assert OrchestratorConfig().lane_circuit_breaker_threshold == 3


class TestOrchestratorConfigLivenessFirstBucketByStage:
    """OrchestratorConfig.liveness_first_bucket_by_stage field (#1001)."""

    def test_default_is_impl_35(self) -> None:
        from cw.models import OrchestratorConfig, Stage

        config = OrchestratorConfig()
        assert config.liveness_first_bucket_by_stage == {Stage.IMPL: 35}


# ---------------------------------------------------------------------------
# TestDispatchStateLock
# ---------------------------------------------------------------------------


class TestDispatchStateLock:
    """Smoke test for dispatch_state_lock() (#1256)."""

    def test_dispatch_state_lock_creates_and_releases(
        self, tmp_config_dir: Path
    ) -> None:
        """dispatch_state_lock() creates lock file and releases on exit."""
        from cw.config import dispatch_state_lock, dispatch_state_lock_file

        lock_path = dispatch_state_lock_file()
        with dispatch_state_lock():
            assert lock_path.exists()
        # Lock released — file still exists but lock is no longer held


# ---------------------------------------------------------------------------
# TestUsageLimitedUntilPersistence
# ---------------------------------------------------------------------------


class TestUsageLimitedUntilPersistence:
    """Unit tests for load_usage_limited_until / save_usage_limited_until (#804)."""

    def test_save_and_load_roundtrip(self, tmp_config_dir: Path) -> None:
        """save then load returns the same datetime (within 1s due to isoformat)."""
        from datetime import UTC, datetime, timedelta

        from cw.config import load_usage_limited_until, save_usage_limited_until

        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)
        loaded = load_usage_limited_until()
        assert loaded is not None
        assert abs((loaded - future).total_seconds()) < 1

    def test_load_returns_none_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.config
        from cw.config import load_usage_limited_until

        cw.config.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_usage_limited_until() is None

    def test_load_returns_none_for_expired_timestamp(
        self, tmp_config_dir: Path
    ) -> None:
        """A persisted timestamp in the past is treated as expired → None."""
        from datetime import UTC, datetime, timedelta

        from cw.config import load_usage_limited_until, save_usage_limited_until

        past = datetime.now(UTC) - timedelta(hours=1)
        save_usage_limited_until(past)
        assert load_usage_limited_until() is None

    def test_save_none_clears_backoff(self, tmp_config_dir: Path) -> None:
        """save_usage_limited_until(None) writes null → load returns None."""
        from datetime import UTC, datetime, timedelta

        from cw.config import load_usage_limited_until, save_usage_limited_until

        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)
        save_usage_limited_until(None)
        assert load_usage_limited_until() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        """Corrupt JSON in DISPATCH_STATE_FILE → None (silent, no exception)."""
        import cw.config
        from cw.config import load_usage_limited_until

        cw.config.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_usage_limited_until() is None

    def test_load_returns_none_for_naive_timestamp(self, tmp_config_dir: Path) -> None:
        """Naive (timezone-unaware) ISO timestamp in sidecar → None, no crash (#804)."""
        import json

        import cw.config
        from cw.config import load_usage_limited_until

        # Write a naive ISO string (no +00:00 suffix) to the sidecar.
        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": "2099-01-01T00:00:00"})
        )
        assert load_usage_limited_until() is None

    def test_save_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """save_usage_limited_until swallows OSError and emits a warning (#804)."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from cw.config import save_usage_limited_until

        future = datetime.now(UTC) + timedelta(hours=1)
        with patch("cw.config.atomic_write_text", side_effect=OSError("disk full")):
            save_usage_limited_until(future)

    def test_save_usage_limited_until_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above.

        save_usage_limited_until wraps its body in `except OSError`; CwError
        is a distinct exception type and must NOT be swallowed by that guard.
        """
        from datetime import UTC, datetime, timedelta

        from cw.config import save_usage_limited_until

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr("cw.config.DISPATCH_STATE_FILE", real_dispatch_state_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        future = datetime.now(UTC) + timedelta(hours=1)
        with pytest.raises(CwError, match="refusing real-state write"):
            save_usage_limited_until(future)

        mock_write.assert_not_called()


class TestAvailabilityProbeCachePersistence:
    """Unit tests for the fleet-wide gh-availability probe cache (RFC 0011 A5).

    Sibling of TestUsageLimitedUntilPersistence: the cache is persisted in the
    same DISPATCH_STATE_FILE sidecar under the ``"availability_probe"`` key.
    The two clobber-regression tests pin the read-merge-write contract that
    keeps save_usage_limited_until and save_availability_probe_cache from
    overwriting each other's key (#1157).
    """

    def test_save_then_load_round_trip_available_true(
        self, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.config import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
        )

        probed_at = datetime.now(UTC)
        save_availability_probe_cache(
            AvailabilityProbeCache(probed_at=probed_at, available=True, latched=False)
        )
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is True
        assert loaded.latched is False
        assert abs((loaded.probed_at - probed_at).total_seconds()) < 1

    def test_save_then_load_round_trip_available_false(
        self, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.config import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
        )

        probed_at = datetime.now(UTC)
        save_availability_probe_cache(
            AvailabilityProbeCache(probed_at=probed_at, available=False, latched=True)
        )
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is False
        assert loaded.latched is True

    def test_load_returns_none_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.config
        from cw.config import load_availability_probe_cache

        cw.config.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_availability_probe_cache() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        import cw.config
        from cw.config import load_availability_probe_cache

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_availability_probe_cache() is None

    def test_load_returns_none_when_key_absent(self, tmp_config_dir: Path) -> None:
        import json

        import cw.config
        from cw.config import load_availability_probe_cache

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": None})
        )
        assert load_availability_probe_cache() is None

    def test_load_returns_none_on_malformed_shape(self, tmp_config_dir: Path) -> None:
        import json

        import cw.config
        from cw.config import load_availability_probe_cache

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # available is an int, not a bool; latched/probed_at missing → None.
        cw.config.DISPATCH_STATE_FILE.write_text(
            json.dumps({"availability_probe": {"available": 1}})
        )
        assert load_availability_probe_cache() is None

    def test_save_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        from datetime import UTC, datetime
        from unittest.mock import patch

        from cw.config import (
            AvailabilityProbeCache,
            save_availability_probe_cache,
        )

        cache = AvailabilityProbeCache(
            probed_at=datetime.now(UTC), available=True, latched=False
        )
        with (
            patch("cw.config.atomic_write_text", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING, logger="cw.config"),
        ):
            save_availability_probe_cache(cache)

        assert "availability_probe" in caplog.text

    def test_save_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above."""
        from datetime import UTC, datetime

        from cw.config import (
            AvailabilityProbeCache,
            save_availability_probe_cache,
        )

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr("cw.config.DISPATCH_STATE_FILE", real_dispatch_state_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        cache = AvailabilityProbeCache(
            probed_at=datetime.now(UTC), available=True, latched=False
        )
        with pytest.raises(CwError, match="refusing real-state write"):
            save_availability_probe_cache(cache)

        mock_write.assert_not_called()

    def test_save_availability_probe_cache_preserves_usage_limited_until(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the probe cache must not clobber the usage-limit key."""
        from datetime import UTC, datetime, timedelta

        from cw.config import (
            AvailabilityProbeCache,
            load_usage_limited_until,
            save_availability_probe_cache,
            save_usage_limited_until,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=False, latched=True
            )
        )
        loaded = load_usage_limited_until()
        assert loaded is not None
        assert abs((loaded - future).total_seconds()) < 1

    def test_save_usage_limited_until_preserves_availability_probe_cache(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the usage-limit key must not clobber the probe cache."""
        from datetime import UTC, datetime, timedelta

        from cw.config import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
            save_usage_limited_until,
        )

        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=False, latched=True
            )
        )
        save_usage_limited_until(datetime.now(UTC) + timedelta(hours=1))
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is False
        assert loaded.latched is True

    def test_save_availability_probe_cache_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """A corrupt existing sidecar is replaced, not raised on (#1157)."""
        from datetime import UTC, datetime

        import cw.config
        from cw.config import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
        )

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text("not-json")
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=True, latched=False
            )
        )
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is True

    def test_save_usage_limited_until_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """save_usage_limited_until also tolerates a corrupt existing sidecar."""
        from datetime import UTC, datetime, timedelta

        import cw.config
        from cw.config import load_usage_limited_until, save_usage_limited_until

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text("not-json")
        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)
        loaded = load_usage_limited_until()
        assert loaded is not None
        assert abs((loaded - future).total_seconds()) < 1


class TestMainDriftLatchesPersistence:
    """Unit tests for the per-client main-checkout-drift latch (#1258).

    Sibling of TestAvailabilityProbeCachePersistence: persisted in the same
    DISPATCH_STATE_FILE sidecar under the ``"main_drift_latches"`` key.
    """

    def test_save_then_load_round_trip(self, tmp_config_dir: Path) -> None:
        from cw.config import load_main_drift_latches, save_main_drift_latches

        save_main_drift_latches({"client-a": True, "client-b": False})
        assert load_main_drift_latches() == {"client-a": True, "client-b": False}

    def test_load_returns_empty_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.config
        from cw.config import load_main_drift_latches

        cw.config.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_when_key_absent(self, tmp_config_dir: Path) -> None:
        """File exists (sibling sidecar key present) but no main_drift_latches key."""
        import json

        import cw.config
        from cw.config import load_main_drift_latches

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": None})
        )
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        import cw.config
        from cw.config import load_main_drift_latches

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_on_malformed_value_type(
        self, tmp_config_dir: Path
    ) -> None:
        """A non-bool latch value is treated as malformed, not coerced."""
        import json

        import cw.config
        from cw.config import load_main_drift_latches

        cw.config.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.config.DISPATCH_STATE_FILE.write_text(
            json.dumps({"main_drift_latches": {"client-a": "yes"}})
        )
        assert load_main_drift_latches() == {}

    def test_save_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        from unittest.mock import patch

        from cw.config import save_main_drift_latches

        with (
            patch("cw.config.atomic_write_text", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING, logger="cw.config"),
        ):
            save_main_drift_latches({"client-a": True})

        assert "main_drift_latches" in caplog.text

    def test_save_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above."""
        from cw.config import save_main_drift_latches

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr("cw.config.DISPATCH_STATE_FILE", real_dispatch_state_file)
        mock_write = MagicMock()
        monkeypatch.setattr("cw.config.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            save_main_drift_latches({"client-a": True})

        mock_write.assert_not_called()

    def test_save_preserves_availability_probe_cache(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the latch map must not clobber the availability probe key."""
        from datetime import UTC, datetime

        from cw.config import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
            save_main_drift_latches,
        )

        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=False, latched=True
            )
        )
        save_main_drift_latches({"client-a": True})
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is False
        assert loaded.latched is True
