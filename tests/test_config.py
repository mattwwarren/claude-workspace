"""Tests for cw.config - configuration loading and state persistence."""

from __future__ import annotations

import ast
import fcntl
import logging
import os
import select
import subprocess
import sys
import textwrap
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import MagicMock

import pytest

import cw.config
from cw import _flock
from cw._config_migrate import migrate_cw_state
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
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
    mutate_state,
    refuse_real_state_write,
    save_state,
    sessions_lock,
    sessions_lock_file,
    show_config,
)
from cw.exceptions import (
    CwError,
    SessionsLockReentryError,
    SessionsLockTimeoutError,
)
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    DEFAULT_AUTO_PURPOSES,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._invalid_utf8 import INVALID_UTF8
from tests.conftest import (
    _assert_lock_held,
    _fake_fcntl,
    _FakeClock,
    _hold_flock,
    _hold_sessions_lock,
    _make_daemon_session,
    _raise_eio,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from typing import IO

    from cw.dispatch_state import ExecutorBlockedMarker


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

    def test_load_clients_accepts_legacy_sentinel_mismatch_veto_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Clients written during the #2405 rollout remain loadable."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    sentinel_mismatch_veto_enabled: true\n"
        )

        result = load_clients()

        assert result["acme"].sentinel_mismatch_veto_enabled is True

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

    def test_non_mapping_top_level_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A top-level YAML list (or any non-mapping document) must raise
        ConfigValidationError instead of silently being read as 'no clients',
        or crashing with AttributeError/TypeError (operator round-3
        resolution, #2158)."""
        from cw.exceptions import ConfigValidationError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("- acme\n- beta\n")
        with pytest.raises(ConfigValidationError, match=r"clients\.yaml"):
            load_clients()

    def test_non_mapping_clients_key_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A 'clients:' key whose value isn't a mapping must raise
        ConfigValidationError instead of crashing with AttributeError on
        .items() (operator round-3 resolution, #2158)."""
        from cw.exceptions import ConfigValidationError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("clients:\n  - acme\n  - beta\n")
        with pytest.raises(ConfigValidationError, match=r"clients\.yaml"):
            load_clients()

    def test_non_mapping_client_entry_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A client entry whose value isn't a mapping must raise
        ConfigValidationError instead of crashing with TypeError from
        ClientConfig(**data) (operator round-3 resolution, #2158)."""
        from cw.exceptions import ConfigValidationError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text('clients:\n  acme: "not a mapping"\n')
        with pytest.raises(ConfigValidationError, match=r"clients\.yaml"):
            load_clients()

    def test_non_string_client_key_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A client key that isn't a string (e.g. a bare int) must raise
        ConfigValidationError instead of crashing with TypeError from
        _SAFE_CLIENT_NAME.match(name) (operator round-3 resolution, #2158)."""
        from cw.exceptions import ConfigValidationError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text("clients:\n  123:\n    workspace_path: /tmp/acme\n")
        with pytest.raises(ConfigValidationError, match=r"clients\.yaml"):
            load_clients()

    def test_non_string_key_in_client_mapping_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A client mapping with a non-string key (e.g. {1: x, repo_path: y})
        passes the isinstance(data, dict) shape check but raises a bare
        TypeError ('keywords must be strings') from ClientConfig(**data),
        which the existing `except ValidationError` doesn't catch. Must raise
        ConfigValidationError naming clients.yaml, the client name, and the
        underlying error instead (operator round-4 resolution, #2158)."""
        from cw.exceptions import ConfigValidationError

        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n  acme:\n    1: x\n    repo_path: /tmp/acme\n"
        )
        with pytest.raises(ConfigValidationError, match=r"clients\.yaml.*acme"):
            load_clients()

    def test_invalid_utf8_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        """A clients.yaml that is not valid UTF-8 raises ConfigValidationError
        with a fixed message that never carries file bytes or the decoder's
        own text (#2554)."""
        from cw.exceptions import ConfigValidationError

        path = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        path.write_bytes(INVALID_UTF8)
        with pytest.raises(ConfigValidationError) as ei:
            load_clients()
        assert str(ei.value) == f"{path}: file is not valid UTF-8"
        for leaked in ("s3cr3t-marker", "0xff", "\\xff", "codec", "position"):
            assert leaked not in str(ei.value)
        assert isinstance(ei.value.__cause__, UnicodeDecodeError)


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

    def test_ensure_config_empty_branch_writes_utf8(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no example file next to the package, ensure_config writes the
        empty clients mapping as explicit UTF-8 bytes (#2554)."""
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        if clients_file.exists():
            clients_file.unlink()
        # example = Path(__file__).parent.parent.parent / "config" / ...; point
        # __file__ at a tmp tree that has no config/ directory so it is absent.
        fake_module = tmp_path / "pkg" / "cw" / "config.py"
        monkeypatch.setattr(cw.config, "__file__", str(fake_module))

        ensure_config()

        assert clients_file.read_bytes() == b"clients: {}\n"


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
        with caplog.at_level("WARNING", logger="cw._config_migrate"):
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

    def test_v14_to_v15_fills_consecutive_park_vetoes_default(self) -> None:
        """migrate_cw_state fills consecutive_park_vetoes=0 on v14 sessions
        that lack the key (#1445)."""
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
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_park_vetoes"] == 0
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v15_consecutive_park_vetoes_preserved_idempotently(self) -> None:
        """Existing nonzero consecutive_park_vetoes survives a migration pass."""
        raw = {
            "schema_version": 15,
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
                    "consecutive_park_vetoes": 2,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_park_vetoes"] == 2

    def test_v15_to_v16_fills_last_result_source_default(self) -> None:
        """migrate_cw_state fills last_result_source=None on v15 sessions
        that lack the key (RFC 0012 S2, #1456)."""
        raw = {
            "schema_version": 15,
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
                    "consecutive_park_vetoes": 0,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["last_result_source"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v16_last_result_source_preserved_idempotently(self) -> None:
        """Existing last_result_source survives a migration pass."""
        raw = {
            "schema_version": 16,
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
                    "consecutive_park_vetoes": 0,
                    "last_result_source": "emit_cli",
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["last_result_source"] == "emit_cli"

    def test_v16_to_v17_fills_consecutive_sentinel_mismatch_vetoes_default(
        self,
    ) -> None:
        """migrate_cw_state fills consecutive_sentinel_mismatch_vetoes=0 on v16
        sessions that lack the key (#1449)."""
        raw = {
            "schema_version": 16,
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
                    "consecutive_park_vetoes": 0,
                    "last_result_source": None,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_sentinel_mismatch_vetoes"] == 0
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_v17_to_v18_fills_liveness_attention_next_eligible_at_default(
        self,
    ) -> None:
        """migrate_cw_state fills liveness_attention_next_eligible_at=None on
        v17 sessions that lack the key (#1858)."""
        raw = {
            "schema_version": 17,
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
                    "consecutive_park_vetoes": 0,
                    "last_result_source": None,
                    "consecutive_sentinel_mismatch_vetoes": 0,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["liveness_attention_next_eligible_at"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    @staticmethod
    def _v18_session(
        sid: str, local_liveness: dict[str, object] | None
    ) -> dict[str, object]:
        return {
            "id": sid,
            "parent_session_id": None,
            "worker_session_ids": [],
            "last_result": None,
            "cost_usd": None,
            "cost_breakdown": None,
            "lane": None,
            "stage": None,
            "consecutive_salvage_skips": 0,
            "liveness_bucket": "live",
            "consecutive_park_vetoes": 0,
            "last_result_source": None,
            "consecutive_sentinel_mismatch_vetoes": 0,
            "liveness_attention_next_eligible_at": None,
            "local_liveness": local_liveness,
        }

    def test_v18_to_v19_fills_local_liveness_backend_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """migrate_cw_state fills local_liveness.backend='aider' on v18 handles
        that lack the key, and leaves a None handle untouched (#2369)."""
        raw = {
            "schema_version": 18,
            "sessions": [
                self._v18_session("s1", {"pid": 1, "start_time_ns": 1}),
                self._v18_session("s2", None),
            ],
        }
        with caplog.at_level("WARNING", logger="cw._config_migrate"):
            migrated = migrate_cw_state(raw)
        with_handle, without_handle = migrated["sessions"]
        assert with_handle["local_liveness"] == {
            "pid": 1,
            "start_time_ns": 1,
            "backend": "aider",
        }
        assert without_handle["local_liveness"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION
        assert any(
            "local_liveness.backend missing" in rec.message for rec in caplog.records
        )

    def test_v18_local_liveness_backend_preserved_idempotently(self) -> None:
        """An existing local_liveness.backend value survives a migration pass
        unchanged (#2369)."""
        raw = {
            "schema_version": 18,
            "sessions": [
                self._v18_session(
                    "s1", {"pid": 1, "start_time_ns": 1, "backend": "opencode"}
                ),
            ],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["local_liveness"]["backend"] == "opencode"
        assert migrate_cw_state(migrated) == migrated

    def test_v17_consecutive_sentinel_mismatch_vetoes_preserved_idempotently(
        self,
    ) -> None:
        """Existing nonzero consecutive_sentinel_mismatch_vetoes survives a
        migration pass (#1449)."""
        raw = {
            "schema_version": 17,
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
                    "consecutive_park_vetoes": 0,
                    "last_result_source": None,
                    "consecutive_sentinel_mismatch_vetoes": 3,
                }
            ],
        }
        migrated = migrate_cw_state(raw)
        session = migrated["sessions"][0]
        assert session["consecutive_sentinel_mismatch_vetoes"] == 3

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
        # pid/start_time_ns kept as-is; the v19 pass adds only the backend key.
        assert session["local_liveness"] == {
            "pid": 4242,
            "start_time_ns": 1782938077013950000,
            "backend": "aider",
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

    # -- #1983: lazy migration (version-gated per-session walk) ------------

    def test_current_schema_version_skips_migration_walk(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file already at the current version is not walked per-session."""
        raw = {
            "schema_version": CW_STATE_SCHEMA_VERSION,
            "sessions": [{"id": "s1", "origin": "delegate"}],
        }
        with caplog.at_level("WARNING", logger="cw._config_migrate"):
            migrated = migrate_cw_state(raw)
        # Untouched: the origin coercion never ran.
        assert migrated["sessions"][0]["origin"] == "delegate"
        assert "parent_session_id" not in migrated["sessions"][0]
        assert not any("unknown origin" in rec.message for rec in caplog.records)
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_below_current_schema_version_still_runs_full_walk(self) -> None:
        """Regression guard: an older file still gets the full normalisation."""
        raw = {
            "schema_version": CW_STATE_SCHEMA_VERSION - 1,
            "sessions": [{"id": "s1", "origin": "delegate"}],
        }
        migrated = migrate_cw_state(raw)
        assert migrated["sessions"][0]["origin"] == SessionOrigin.USER.value
        assert migrated["sessions"][0]["parent_session_id"] is None
        assert migrated["schema_version"] == CW_STATE_SCHEMA_VERSION

    def test_migration_walk_call_count_independent_of_session_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At the current version, per-session helpers run zero times for N=50."""
        calls: list[dict[str, object]] = []

        def spy(session_raw: dict[str, object]) -> None:
            calls.append(session_raw)

        monkeypatch.setattr(cw._config_migrate, "_coerce_session_origin", spy)
        raw = {
            "schema_version": CW_STATE_SCHEMA_VERSION,
            "sessions": [{"id": f"s{i}"} for i in range(50)],
        }
        migrate_cw_state(raw)
        assert calls == []

    def test_load_state_skips_walk_for_current_version_file(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_state on a current-version file does no per-session normalisation."""
        import json

        calls: list[dict[str, object]] = []

        def spy(session_raw: dict[str, object]) -> None:
            calls.append(session_raw)

        session = _make_daemon_session(id="lazy0001")
        save_state(CwState(sessions=[session]))

        state_path = tmp_config_dir / ".local" / "share" / "cw" / "sessions.json"
        on_disk = json.loads(state_path.read_text())
        assert on_disk["schema_version"] == CW_STATE_SCHEMA_VERSION

        monkeypatch.setattr(cw._config_migrate, "_coerce_session_origin", spy)
        loaded = load_state()
        assert calls == []
        assert [s.id for s in loaded.sessions] == ["lazy0001"]


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


class TestLoadOrchestratorConfigUndecodable:
    """load_orchestrator_config() and invalid-UTF-8 bytes (#2554)."""

    def test_invalid_utf8_raises_config_validation_error(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.config import load_orchestrator_config, orchestrator_config_file
        from cw.exceptions import ConfigValidationError

        path = orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(INVALID_UTF8)
        with pytest.raises(ConfigValidationError) as ei:
            load_orchestrator_config()
        assert str(ei.value) == f"{path}: file is not valid UTF-8"
        for leaked in ("s3cr3t-marker", "0xff", "\\xff", "codec", "position"):
            assert leaked not in str(ei.value)
        assert isinstance(ei.value.__cause__, UnicodeDecodeError)
        # The file existed, so the default-creation branch never ran and the
        # operator's bytes were not overwritten.
        assert path.read_bytes() == INVALID_UTF8

    def test_missing_file_still_creates_default(self, tmp_config_dir: Path) -> None:
        from cw.config import (
            _DEFAULT_ORCHESTRATOR_YAML,
            load_orchestrator_config,
            orchestrator_config_file,
        )
        from cw.models import OrchestratorConfig

        path = orchestrator_config_file()
        assert not path.exists()
        assert isinstance(load_orchestrator_config(), OrchestratorConfig)
        assert path.read_bytes() == _DEFAULT_ORCHESTRATOR_YAML.encode("utf-8")

    def test_valid_non_ascii_utf8_still_loads(self, tmp_config_dir: Path) -> None:
        from cw.config import load_orchestrator_config, orchestrator_config_file
        from cw.models import ReapPolicy

        path = orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("# café\nreap_policy: auto\n".encode())
        assert load_orchestrator_config().reap_policy == ReapPolicy.AUTO


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
# TestSessionsLockAcquire / TestSessionsLockBounded / allowlist guard (#2491)
# ---------------------------------------------------------------------------

_TINY_TIMEOUT_S = "0.05"
_CHILD_READY_TIMEOUT_S = 10.0

# Call sites allowed to pass a literal ``bounded=True``, keyed by (path relative
# to ``src/``, enclosing function qualname) with the exact number of calls.
# Bounded is safe ONLY for observe/operator callers with no irreversible side
# effect before the lock: a timeout after a side effect orphans a live worker
# or loses a result, and the caller's broad ``except`` then reverts the task to
# PENDING so the next tick spawns a duplicate. Per-FUNCTION granularity matters:
# a file-level allowlist would let a second bounded call (or a flipped
# post-``spawn_bg`` one) slip into an already-allowlisted file. Adding an entry
# is a conscious call: verify nothing irreversible happens before the lock, that
# no unattended loop reaches the site, and that, if it is reachable inside
# ``dispatch_tick``, SessionsLockTimeoutError is handled.
_BOUNDED_TRUE_ALLOWLIST: dict[tuple[str, str], int] = {
    ("cw/cli/maintenance.py", "doctor"): 1,  # `cw doctor --reap <SESSION>`
    ("cw/cli/spawn.py", "_spawn_close_impl"): 1,  # `cw spawn close`
    ("cw/cli/spawn.py", "_spawn_complete_impl"): 1,  # `cw spawn complete`
    ("cw/dev_queue/requeue.py", "unblock_ticket"): 1,  # `cw dev-queue unblock`
    ("cw/doctor/wedge.py", "_reap_wedge_findings"): 1,  # `cw doctor --reap`
    # `cw doctor --reap`, stranded routed-result close (#2524)
    ("cw/doctor/routed_result_wedge.py", "reap_routed_result_findings"): 1,
    ("cw/orchestrate.py", "retire_merged_prs"): 1,  # `cw orchestrate retire`
    ("cw/reconcile/core.py", "reconcile"): 1,  # list/status/start + tick pre-pass
    ("cw/session.py", "background_session"): 1,  # `cw bg`
    ("cw/session.py", "resume_session"): 1,  # live-surface `_update_live` only
    ("cw/session.py", "done_session"): 1,  # `cw done`
    ("cw/session_retention.py", "prune_sessions"): 1,  # session prune
}
# Call sites that pass a non-literal ``bounded=<name>``: they only forward their
# caller's choice. Anything else computing ``bounded`` at runtime defeats the
# audit above, so the set is exact.
_BOUNDED_FORWARDERS_ALLOWLIST: dict[tuple[str, str], int] = {
    # mutate_state() forwards its own ``bounded`` parameter to sessions_lock().
    ("cw/config.py", "mutate_state"): 1,
    # The reap helper forwards its caller's choice: the operator callers above
    # pass True, the unattended `cw orchestrate run --lane` poll loop keeps the
    # unbounded default (a timeout would end that consumer for good).
    ("cw/doctor/loop_health.py", "_reap_session_by_selector"): 1,
}
# Callables whose ``bounded=`` argument the scan audits.
_LOCK_ENTRY_POINTS = frozenset(
    {"sessions_lock", "mutate_state", "_reap_session_by_selector"}
)


class _BoundedScan(NamedTuple):
    """Result of :func:`_scan_bounded_calls`; every key is (rel path, qualname)."""

    literal_true: Counter[tuple[str, str]]
    non_literal: Counter[tuple[str, str]]
    true_targets: dict[tuple[str, str], list[str]]
    violations: list[str]


class _BoundedCallVisitor(ast.NodeVisitor):
    """Collect ``bounded=`` usage per enclosing function qualname.

    Nested defs and classes extend the qualname (``Outer.inner``). Forms the
    scan cannot attribute reliably are reported as ``violations`` rather than
    silently ignored: aliased imports of the entry points, a bare reference
    that is not a call (``x = sessions_lock``), and ``**kwargs`` calls.
    """

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.literal_true: Counter[tuple[str, str]] = Counter()
        self.non_literal: Counter[tuple[str, str]] = Counter()
        self.true_targets: dict[tuple[str, str], list[str]] = {}
        self.violations: list[str] = []
        self._call_funcs: set[int] = set()

    def _where(self) -> tuple[str, str]:
        return (self.rel, ".".join(self.scope) or "<module>")

    def _visit_scope(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _LOCK_ENTRY_POINTS and alias.asname not in (
                None,
                alias.name,
            ):
                self.violations.append(
                    f"{self.rel}:{node.lineno}: {alias.name} imported as"
                    f" {alias.asname}; the scan cannot follow aliases"
                )

    def visit_Name(self, node: ast.Name) -> None:
        self._check_bare_reference(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_bare_reference(node, node.attr)
        self.generic_visit(node)

    def _check_bare_reference(self, node: ast.Name | ast.Attribute, name: str) -> None:
        if name in _LOCK_ENTRY_POINTS and id(node) not in self._call_funcs:
            self.violations.append(
                f"{self.rel}:{node.lineno}: {name} referenced without being"
                " called; the scan cannot follow it"
            )

    def visit_Call(self, node: ast.Call) -> None:
        self._call_funcs.add(id(node.func))
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _LOCK_ENTRY_POINTS:
            self._record(node, name)
        self.generic_visit(node)

    def _record(self, node: ast.Call, name: str) -> None:
        where = self._where()
        for keyword in node.keywords:
            if keyword.arg is None:
                self.violations.append(
                    f"{self.rel}:{node.lineno}: **kwargs call to {name};"
                    " bounded= cannot be audited"
                )
            elif keyword.arg == "bounded":
                value = keyword.value
                if isinstance(value, ast.Constant) and value.value is True:
                    self.literal_true[where] += 1
                    target = ast.unparse(node.args[0]) if node.args else ""
                    self.true_targets.setdefault(where, []).append(target)
                elif not (isinstance(value, ast.Constant) and value.value is False):
                    self.non_literal[where] += 1


def _scan_bounded_calls(src_root: Path) -> _BoundedScan:
    """Scan every ``*.py`` under *src_root* for audited ``bounded=`` calls."""
    scan = _BoundedScan(Counter(), Counter(), {}, [])
    for path in sorted(src_root.rglob("*.py")):
        visitor = _BoundedCallVisitor(path.relative_to(src_root).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        scan.literal_true.update(visitor.literal_true)
        scan.non_literal.update(visitor.non_literal)
        scan.true_targets.update(visitor.true_targets)
        scan.violations.extend(visitor.violations)
    return scan


class _RecordingLockPath:
    """Duck-typed lock path that remembers every handle ``sessions_lock`` opens."""

    def __init__(self, real: Path) -> None:
        self._real = real
        self.handles: list[IO[str]] = []

    def open(self, mode: str) -> IO[str]:
        handle = self._real.open(mode)
        self.handles.append(handle)
        return handle

    def __str__(self) -> str:
        return str(self._real)


@pytest.fixture
def recording_lock_path(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> _RecordingLockPath:
    recorder = _RecordingLockPath(sessions_lock_file())
    monkeypatch.setattr("cw.config.sessions_lock_file", lambda: recorder)
    return recorder


class TestSessionsLockAcquire:
    """Acquisition basics, identical for the default and the bounded mode."""

    @pytest.mark.parametrize("bounded", [False, True], ids=["default", "bounded"])
    def test_holds_the_lock_inside_the_body_and_releases_after(
        self, tmp_config_dir: Path, bounded: bool
    ) -> None:
        lock_path = sessions_lock_file()

        with sessions_lock(bounded=bounded):
            _assert_lock_held(lock_path)

        with lock_path.open("w") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @pytest.mark.parametrize("bounded", [False, True], ids=["default", "bounded"])
    def test_reentry_still_raises_reentry_not_timeout_or_hang(
        self, tmp_config_dir: Path, bounded: bool
    ) -> None:
        with (
            sessions_lock(),
            pytest.raises(SessionsLockReentryError),
            sessions_lock(bounded=bounded),
        ):
            pytest.fail("must not reach body")


class TestSessionsLockBounded:
    """``sessions_lock(bounded=True)`` and its opt-in contract (GitHub #2491)."""

    def test_times_out_with_actionable_message_when_another_fd_holds_lock(
        self,
        held_sessions_lock: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, _TINY_TIMEOUT_S)
        caplog.set_level(logging.DEBUG)

        with (
            pytest.raises(SessionsLockTimeoutError) as exc_info,
            sessions_lock(bounded=True),
        ):
            pytest.fail("must not reach body")

        err = exc_info.value
        assert isinstance(err, CwError)
        assert err.lock_path == held_sessions_lock
        assert err.waited_s >= float(_TINY_TIMEOUT_S)
        message = str(err)
        assert str(held_sessions_lock) in message
        assert f"lsof {held_sessions_lock}" in message
        assert "cw dev-queue serve" in message
        assert "dispatch.tick" in message
        assert "restart" in message
        assert f"{SESSIONS_LOCK_TIMEOUT_ENV} (seconds; currently 0.05)" in message
        # The raiser does not also log the message (log-and-raise duplication).
        assert not [r for r in caplog.records if "Timed out" in r.getMessage()]

    def test_zero_timeout_fails_with_zero_sleeps_when_held(
        self,
        held_sessions_lock: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")
        clock = _FakeClock()
        monkeypatch.setattr(_flock, "time", clock)

        with pytest.raises(SessionsLockTimeoutError), sessions_lock(bounded=True):
            pytest.fail("must not reach body")

        assert clock.sleeps == []

    def test_zero_timeout_acquires_when_free(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0")

        with sessions_lock(bounded=True):
            _assert_lock_held(sessions_lock_file())

    def test_timeout_closes_fd_and_leaves_lock_and_guard_usable(
        self,
        recording_lock_path: _RecordingLockPath,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, _TINY_TIMEOUT_S)

        with (
            _hold_sessions_lock(),
            pytest.raises(SessionsLockTimeoutError),
            sessions_lock(bounded=True),
        ):
            pytest.fail("must not reach body")

        assert [h.closed for h in recording_lock_path.handles] == [True]
        # Held flag was never set: a real acquisition is not mistaken for re-entry.
        with sessions_lock(bounded=True):
            pass

    @pytest.mark.parametrize("bounded", [False, True], ids=["default", "bounded"])
    def test_non_contention_oserror_propagates_and_closes_fd(
        self,
        recording_lock_path: _RecordingLockPath,
        monkeypatch: pytest.MonkeyPatch,
        bounded: bool,
    ) -> None:
        with monkeypatch.context() as patch_ctx:
            patch_ctx.setattr(_flock, "fcntl", _fake_fcntl(_raise_eio))
            with (
                pytest.raises(OSError, match="disk on fire"),
                sessions_lock(bounded=bounded),
            ):
                pytest.fail("must not reach body")

        assert [h.closed for h in recording_lock_path.handles] == [True]
        with sessions_lock(bounded=bounded):
            pass

    def test_keyboard_interrupt_while_waiting_closes_fd_and_clears_guard(
        self,
        recording_lock_path: _RecordingLockPath,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "30")

        def _interrupt(_sleep_count: int) -> None:
            raise KeyboardInterrupt

        with _hold_sessions_lock(), monkeypatch.context() as patch_ctx:
            patch_ctx.setattr(_flock, "time", _FakeClock(on_sleep=_interrupt))
            with pytest.raises(KeyboardInterrupt), sessions_lock(bounded=True):
                pytest.fail("must not reach body")

        assert [h.closed for h in recording_lock_path.handles] == [True]
        with sessions_lock(bounded=True):
            pass

    def test_acquires_when_holder_releases_before_deadline(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "30")
        entered = False

        with _hold_flock(sessions_lock_file()) as release:
            # First poll finds the lock held; the holder frees it during the
            # sleep, so the second poll acquires. No wall-clock waiting.
            clock = _FakeClock(on_sleep=lambda _n: release())
            monkeypatch.setattr(_flock, "time", clock)
            with sessions_lock(bounded=True):
                entered = True

        assert entered
        assert len(clock.sleeps) == 1

    def test_default_is_unbounded_while_bounded_times_out(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same held lock, same tiny env bound: only ``bounded=True`` times out.

        Plain ``sessions_lock()`` must wait the holder out however short the
        env timeout is, because commit-after-side-effect callers rely on it.
        """
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, _TINY_TIMEOUT_S)
        held = threading.Event()
        release = threading.Event()

        def _holder() -> None:
            with _hold_sessions_lock():
                held.set()
                release.wait(timeout=10)

        releaser = threading.Timer(0.3, release.set)
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(_holder)
            try:
                assert held.wait(timeout=5)
                with (
                    pytest.raises(SessionsLockTimeoutError),
                    sessions_lock(bounded=True),
                ):
                    pytest.fail("must not reach body")

                releaser.start()
                with sessions_lock():
                    holder_released_before_entry = release.is_set()
            finally:
                releaser.cancel()
                release.set()
            holder.result(timeout=10)  # re-raises anything the holder thread hit

        assert holder_released_before_entry

    def test_mutate_state_forwards_bounded(
        self, held_sessions_lock: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, _TINY_TIMEOUT_S)

        with pytest.raises(SessionsLockTimeoutError):
            mutate_state(lambda _state: None, bounded=True)

    def test_times_out_when_another_process_holds_lock(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.2")
        path = sessions_lock_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        script = textwrap.dedent(
            """
            import fcntl, sys
            fd = open(sys.argv[1], "w")
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("locked", flush=True)
            sys.stdin.read()
            """
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], _CHILD_READY_TIMEOUT_S)
            assert ready, "child never reported that it holds the lock"
            assert proc.stdout.readline().strip() == "locked"

            with pytest.raises(SessionsLockTimeoutError), sessions_lock(bounded=True):
                pytest.fail("must not reach body")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    pipe.close()

        # Holder is dead -> its fd is gone -> the lock is free again.
        with sessions_lock(bounded=True):
            pass


class TestBoundedSessionsLockAllowlist:
    """Guard: ``bounded=True`` may only appear in the audited files (#2491)."""

    @pytest.fixture(scope="class")
    def src_scan(self) -> _BoundedScan:
        return _scan_bounded_calls(Path(_flock.__file__).resolve().parent.parent)

    def test_bounded_true_call_sites_match_the_allowlist(
        self, src_scan: _BoundedScan
    ) -> None:
        assert dict(src_scan.literal_true) == _BOUNDED_TRUE_ALLOWLIST, (
            "bounded=True call sites changed. A bounded acquisition that runs"
            " AFTER an irreversible side effect (spawn, launch, result emit)"
            " orphans work when it times out. If the new site has no prior side"
            " effect and no unattended loop reaches it, add (file, function) to"
            " _BOUNDED_TRUE_ALLOWLIST; if it is reachable from dispatch_tick,"
            " also handle SessionsLockTimeoutError there."
        )

    def test_non_literal_bounded_is_only_forwarded_by_the_audited_helpers(
        self, src_scan: _BoundedScan
    ) -> None:
        assert dict(src_scan.non_literal) == _BOUNDED_FORWARDERS_ALLOWLIST

    def test_scan_finds_no_alias_bare_reference_or_kwargs_forms(
        self, src_scan: _BoundedScan
    ) -> None:
        assert src_scan.violations == []

    def test_session_py_bounds_only_the_pre_side_effect_paths(
        self, src_scan: _BoundedScan
    ) -> None:
        session_py = {
            qualname: count
            for (rel, qualname), count in src_scan.literal_true.items()
            if rel == "cw/session.py"
        }

        assert session_py == {
            "background_session": 1,
            "resume_session": 1,
            "done_session": 1,
        }
        # start_session spawns the daemon worker and records it afterwards:
        # a timeout there would orphan the worker, so it must stay unbounded.
        assert ("cw/session.py", "start_session") not in src_scan.literal_true
        assert ("cw/session.py", "start_session") not in src_scan.non_literal
        # In resume_session only the live-surface path may be bounded. The
        # dead-surface `_update_dead` runs AFTER spawn_bg re-spawned the worker.
        targets = src_scan.true_targets[("cw/session.py", "resume_session")]
        assert targets == ["_update_live"]

    def test_the_unattended_reap_consumer_stays_unbounded(
        self, src_scan: _BoundedScan
    ) -> None:
        """`cw orchestrate run --lane` polls forever; a timeout would end it."""
        key = ("cw/cli/orchestrate.py", "_drain_reap_proposals")

        assert key not in src_scan.literal_true
        assert key not in src_scan.non_literal

    def test_scanner_tracks_enclosing_function_qualnames(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(
            "def top():\n"
            "    with sessions_lock(bounded=True):\n"
            "        pass\n"
            "    def inner():\n"
            "        config.mutate_state(fn, bounded=True)\n"
            "        config.mutate_state(fn, bounded=True)\n"
            "class K:\n"
            "    def method(self):\n"
            "        sessions_lock(bounded=True)\n"
            "sessions_lock(bounded=True)\n"
        )
        (tmp_path / "b.py").write_text(
            "def f():\n    with sessions_lock(bounded=flag):\n        pass\n"
        )
        (tmp_path / "c.py").write_text(
            "def f():\n    with sessions_lock(bounded=False):\n        pass\n"
            "    with sessions_lock():\n        pass\n"
        )

        scan = _scan_bounded_calls(tmp_path)

        assert dict(scan.literal_true) == {
            ("a.py", "top"): 1,
            ("a.py", "top.inner"): 2,
            ("a.py", "K.method"): 1,
            ("a.py", "<module>"): 1,
        }
        assert scan.true_targets[("a.py", "top.inner")] == ["fn", "fn"]
        assert dict(scan.non_literal) == {("b.py", "f"): 1}
        assert scan.violations == []

    def test_scanner_distinguishes_functions_within_one_file(
        self, tmp_path: Path
    ) -> None:
        """The regression the per-file allowlist missed (flipped `_update_dead`)."""
        (tmp_path / "s.py").write_text(
            "def resume():\n"
            "    mutate_state(_update_live, bounded=True)\n"
            "    mutate_state(_update_dead, bounded=True)\n"
        )

        scan = _scan_bounded_calls(tmp_path)

        assert scan.literal_true == {("s.py", "resume"): 2}
        assert scan.true_targets[("s.py", "resume")] == ["_update_live", "_update_dead"]

    @pytest.mark.parametrize(
        ("source", "needle"),
        [
            ("from cw.config import sessions_lock as lock\n", "imported as lock"),
            ("from cw.config import mutate_state as ms\n", "imported as ms"),
            ("lock = sessions_lock\n", "referenced without being called"),
            ("lock = config.mutate_state\n", "referenced without being called"),
            ("sessions_lock(**opts)\n", "**kwargs call to sessions_lock"),
            ("cfg.mutate_state(fn, **opts)\n", "**kwargs call to mutate_state"),
        ],
    )
    def test_scanner_reports_forms_it_cannot_follow(
        self, tmp_path: Path, source: str, needle: str
    ) -> None:
        (tmp_path / "x.py").write_text(source)

        scan = _scan_bounded_calls(tmp_path)

        assert len(scan.violations) == 1
        assert needle in scan.violations[0]

    def test_scanner_accepts_unaliased_import_and_plain_calls(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "ok.py").write_text(
            "from cw.config import sessions_lock, mutate_state\n"
            "from cw.config import sessions_lock as sessions_lock\n"
            "with sessions_lock():\n    pass\n"
            "mutate_state(fn)\n"
        )

        scan = _scan_bounded_calls(tmp_path)

        assert scan.violations == []
        assert not scan.literal_true
        assert not scan.non_literal


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

# Lanes declared by the 'acme' client in the effective-clients tests below.
_ACME_LANES = [
    {"name": "default", "max_parallel": 1},
    {"name": "fast", "max_parallel": 2},
]


class TestLoadEffectiveClients:
    """Tests for load_effective_clients() — lane pause override propagation."""

    def test_no_overrides_returns_declared(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No override file → effective clients equal declared clients."""
        from cw.config import load_effective_clients

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
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

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
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

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
        assert load_effective_clients() == load_clients()


# ---------------------------------------------------------------------------
# TestGetEffectiveClient
# ---------------------------------------------------------------------------


class TestGetEffectiveClient:
    """Tests for get_effective_client() — single-client effective lookup (#875)."""

    def test_returns_declared_when_no_override(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No override → the effective client's lanes match the declared state."""
        from cw.config import get_effective_client

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
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

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
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

        write_clients_yaml(
            ClientSpec("acme", tmp_path / "ws", lanes=_ACME_LANES),
            ensure_workspaces=True,
        )
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


class TestOrchestratorConfigOperatorGithubLoginByRepo:
    """OrchestratorConfig.operator_github_login_by_repo field (RFC 0011, #1171)."""

    def test_defaults_to_empty_dict(self) -> None:
        from cw.models import OrchestratorConfig

        assert OrchestratorConfig().operator_github_login_by_repo == {}

    def test_round_trips_via_model_validate(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig.model_validate(
            {"operator_github_login_by_repo": {"acme/widgets": "alice"}}
        )
        assert config.operator_github_login_by_repo == {"acme/widgets": "alice"}

    def test_wrong_value_type_raises_validation_error(self) -> None:
        from pydantic import ValidationError

        from cw.models import OrchestratorConfig

        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate(
                {"operator_github_login_by_repo": {"acme/widgets": 123}}
            )


class TestBusyWaitGuardConfigFields:
    """The #1946 busy-wait guard's per-lane-with-global-default knobs."""

    def test_global_defaults_are_on(self) -> None:
        """Default-on: the guard fires unless an operator opts out."""
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig()
        assert config.busy_wait_guard_enabled is True
        assert config.busy_wait_guard_repeat_threshold == 3
        assert config.busy_wait_guard_window_seconds == 300

    def test_lane_overrides_default_to_none(self) -> None:
        """None on a lane means "inherit", mirroring LaneConfig.reap_policy."""
        from cw.models import LaneConfig

        lane = LaneConfig(name="fast")
        assert lane.busy_wait_guard_enabled is None
        assert lane.busy_wait_guard_repeat_threshold is None
        assert lane.busy_wait_guard_window_seconds is None

    def test_global_overrides_round_trip(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig.model_validate(
            {
                "busy_wait_guard_enabled": False,
                "busy_wait_guard_repeat_threshold": 7,
                "busy_wait_guard_window_seconds": 60,
            }
        )
        assert config.busy_wait_guard_enabled is False
        assert config.busy_wait_guard_repeat_threshold == 7
        assert config.busy_wait_guard_window_seconds == 60

    def test_lane_overrides_round_trip_via_clients_yaml(
        self, tmp_config_dir: Path
    ) -> None:
        """A lane block in clients.yaml loads the three overrides."""
        ws_dir = tmp_config_dir / "ws"
        ws_dir.mkdir()
        clients_path = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_path.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    lanes:\n"
            "      - name: fast\n"
            "        busy_wait_guard_enabled: false\n"
            "        busy_wait_guard_repeat_threshold: 2\n"
            "        busy_wait_guard_window_seconds: 120\n"
        )

        lane = load_clients()["acme"].lanes[0]
        assert lane.busy_wait_guard_enabled is False
        assert lane.busy_wait_guard_repeat_threshold == 2
        assert lane.busy_wait_guard_window_seconds == 120

    def test_wrong_type_raises_validation_error(self) -> None:
        from pydantic import ValidationError

        from cw.models import OrchestratorConfig

        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate(
                {"busy_wait_guard_repeat_threshold": "many"}
            )


# ---------------------------------------------------------------------------
# TestDispositionDriftCheckConfigFields
# ---------------------------------------------------------------------------


class TestDispositionDriftCheckConfigFields:
    """#2232's drift-check gate: per-lane override over a default-ON global.

    Shaped on ``busy_wait_guard_enabled``, not on
    ``codex_claim_suppression_enabled``: this is a CHECK presumed wanted, not
    a feature presumed unwanted, so there is no master kill switch and no
    hardcoded-off floor — a lane override wins, otherwise the global.
    """

    def test_global_default_is_on(self) -> None:
        from cw.models import OrchestratorConfig

        assert OrchestratorConfig().disposition_drift_check_enabled is True

    def test_lane_override_defaults_to_none(self) -> None:
        """None on a lane means "inherit", mirroring busy_wait_guard_enabled."""
        from cw.models import LaneConfig

        assert LaneConfig(name="fast").disposition_drift_check_enabled is None

    def test_global_override_round_trips(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig.model_validate(
            {"disposition_drift_check_enabled": False}
        )
        assert config.disposition_drift_check_enabled is False

    def test_lane_override_round_trips_via_clients_yaml(
        self, tmp_config_dir: Path
    ) -> None:
        ws_dir = tmp_config_dir / "ws"
        ws_dir.mkdir()
        clients_path = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_path.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    lanes:\n"
            "      - name: fast\n"
            "        disposition_drift_check_enabled: false\n"
        )

        lane = load_clients()["acme"].lanes[0]
        assert lane.disposition_drift_check_enabled is False

    def test_wrong_type_raises_validation_error(self) -> None:
        from pydantic import ValidationError

        from cw.models import OrchestratorConfig

        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate(
                {"disposition_drift_check_enabled": "sometimes"}
            )


# ---------------------------------------------------------------------------
# TestSubagentSpawnGuardConfig
# ---------------------------------------------------------------------------


class TestSubagentSpawnGuardConfig:
    """The #2211 spawn-shape guard's per-lane-with-global-default kill switch."""

    def test_global_default_is_on(self) -> None:
        """Default-on: an explicit fork is refused unless an operator opts out."""
        from cw.models import OrchestratorConfig

        assert OrchestratorConfig().subagent_spawn_guard_enabled is True

    def test_lane_override_defaults_to_none(self) -> None:
        """None on a lane means "inherit", mirroring busy_wait_guard_enabled."""
        from cw.models import LaneConfig

        assert LaneConfig(name="fast").subagent_spawn_guard_enabled is None

    def test_global_override_round_trips(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig.model_validate(
            {"subagent_spawn_guard_enabled": False}
        )

        assert config.subagent_spawn_guard_enabled is False

    def test_lane_override_round_trips_via_clients_yaml(
        self, tmp_config_dir: Path
    ) -> None:
        """A lane block in clients.yaml loads the override independently."""
        ws_dir = tmp_config_dir / "ws"
        ws_dir.mkdir()
        clients_path = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_path.write_text(
            "clients:\n"
            "  acme:\n"
            f"    workspace_path: {ws_dir}\n"
            "    lanes:\n"
            "      - name: fast\n"
            "        subagent_spawn_guard_enabled: false\n"
        )

        lane = load_clients()["acme"].lanes[0]

        assert lane.subagent_spawn_guard_enabled is False

    def test_default_template_documents_the_key(self) -> None:
        """A fresh orchestrator.yaml names the kill switch inline."""
        from cw.config import _DEFAULT_ORCHESTRATOR_YAML

        assert "subagent_spawn_guard_enabled: true" in _DEFAULT_ORCHESTRATOR_YAML


class TestBackgroundToolGuardConfig:
    """The #2303 background-tool guard's per-lane-with-global-default switch."""

    def test_global_default_is_on(self) -> None:
        """Default-on: a headless backgrounded Bash/Monitor call is refused."""
        from cw.models import OrchestratorConfig

        assert OrchestratorConfig().background_tool_guard_enabled is True

    def test_lane_override_defaults_to_none(self) -> None:
        """None on a lane means "inherit", mirroring subagent_spawn_guard_enabled."""
        from cw.models import LaneConfig

        assert LaneConfig(name="fast").background_tool_guard_enabled is None

    def test_global_override_round_trips(self) -> None:
        from cw.models import OrchestratorConfig

        config = OrchestratorConfig.model_validate(
            {"background_tool_guard_enabled": False}
        )

        assert config.background_tool_guard_enabled is False

    def test_lane_override_round_trips_via_clients_yaml(
        self, tmp_config_dir: Path
    ) -> None:
        """A lane block in clients.yaml loads the override independently."""
        write_clients_yaml(
            ClientSpec(
                "acme",
                tmp_config_dir / "ws",
                lanes=[{"name": "fast", "background_tool_guard_enabled": False}],
            ),
            ensure_workspaces=True,
        )

        lane = load_clients()["acme"].lanes[0]

        assert lane.background_tool_guard_enabled is False

    def test_wrong_type_raises_validation_error(self) -> None:
        from pydantic import ValidationError

        from cw.models import OrchestratorConfig

        with pytest.raises(ValidationError):
            OrchestratorConfig.model_validate(
                {"background_tool_guard_enabled": "not-a-bool"}
            )

    def test_default_template_documents_the_key(self) -> None:
        """A fresh orchestrator.yaml names the kill switch inline."""
        from cw.config import _DEFAULT_ORCHESTRATOR_YAML

        assert "background_tool_guard_enabled: true" in _DEFAULT_ORCHESTRATOR_YAML


# ---------------------------------------------------------------------------
# TestDispatchStateLock
# ---------------------------------------------------------------------------


class TestDispatchStateLock:
    """Smoke test for dispatch_state_lock() (#1256)."""

    def test_dispatch_state_lock_creates_and_releases(
        self, tmp_config_dir: Path
    ) -> None:
        """dispatch_state_lock() creates lock file and releases on exit."""
        from cw.dispatch_state import dispatch_state_lock, dispatch_state_lock_file

        lock_path = dispatch_state_lock_file()
        with dispatch_state_lock():
            assert lock_path.exists()
        # Lock released — file still exists but lock is no longer held


# ---------------------------------------------------------------------------
# TestUsageLimitedUntilPersistence
# ---------------------------------------------------------------------------


class TestUsageLimitedUntilPersistence:
    """Unit tests for load/merge_and_save of usage_limited_until (#804).

    #1409: the key holds a per-client ``{client: expiry}`` mapping (it was a
    single fleet-wide scalar), so every load returns a ``dict`` — ``{}`` when
    there is nothing usable — and every save takes a mapping. Round 5
    collapsed the parallel exact-overwrite ``save_usage_limited_until``
    primitive into ``merge_and_save_usage_limited_until``, which is now the
    sole writer.
    """

    def test_save_and_load_roundtrip(self, tmp_config_dir: Path) -> None:
        """save then load returns the same per-client mapping (#1409)."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        assert load_usage_limited_until() == {"test-client": future}

    def test_load_returns_empty_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_usage_limited_until

        cw.dispatch_state.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_usage_limited_until() == {}

    def test_load_returns_empty_for_expired_timestamp(
        self, tmp_config_dir: Path
    ) -> None:
        """A persisted timestamp in the past is treated as expired → {} (#1409)."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
        )

        past = datetime.now(UTC) - timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": past})
        assert load_usage_limited_until() == {}

    def test_merge_and_save_empty_mapping_is_a_no_op(
        self, tmp_config_dir: Path
    ) -> None:
        """An empty-mapping merge is a no-op, not a clear (#1409 round 5).

        The pre-round-5 exact-overwrite ``save_usage_limited_until({})``
        cleared every window. That primitive is gone; the sole surviving
        writer merges, so an empty mapping merges nothing new in and an
        existing window is left standing.
        """
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        merge_and_save_usage_limited_until({})
        assert load_usage_limited_until() == {"test-client": future}

    def test_load_returns_empty_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        """Corrupt JSON in DISPATCH_STATE_FILE → {} (silent, no exception)."""
        import cw.dispatch_state
        from cw.dispatch_state import load_usage_limited_until

        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_usage_limited_until() == {}

    def test_load_drops_naive_timestamp_entry(self, tmp_config_dir: Path) -> None:
        """A naive (timezone-unaware) entry is dropped, no crash (#804, #1409).

        The drop is per entry: a well-formed sibling window in the same
        mapping still loads.
        """
        import json
        from datetime import UTC, datetime, timedelta

        import cw.dispatch_state
        from cw.dispatch_state import load_usage_limited_until

        future = datetime.now(UTC) + timedelta(hours=1)
        # Write a naive ISO string (no +00:00 suffix) beside a valid entry.
        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(
                {
                    "usage_limited_until": {
                        "naive-client": "2099-01-01T00:00:00",
                        "test-client": future.isoformat(),
                    }
                }
            )
        )
        assert load_usage_limited_until() == {"test-client": future}

    def test_save_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merge_and_save_usage_limited_until swallows OSError and warns (#804)."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        from cw.dispatch_state import merge_and_save_usage_limited_until

        future = datetime.now(UTC) + timedelta(hours=1)
        with patch(
            "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
        ):
            merge_and_save_usage_limited_until({"test-client": future})

    def test_merge_and_save_usage_limited_until_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above.

        merge_and_save_usage_limited_until wraps its body in `except
        OSError`; CwError is a distinct exception type and must NOT be
        swallowed by that guard.
        """
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import merge_and_save_usage_limited_until

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE", real_dispatch_state_file
        )
        mock_write = MagicMock()
        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", mock_write)

        future = datetime.now(UTC) + timedelta(hours=1)
        with pytest.raises(CwError, match="refusing real-state write"):
            merge_and_save_usage_limited_until({"test-client": future})

        mock_write.assert_not_called()


class TestUsageLimitArmedAt:
    """Unit tests for load_usage_limit_armed_at / save_usage_limit_armed_at (#1343).

    Sibling of TestUsageLimitedUntilPersistence: persisted in the same
    DISPATCH_STATE_FILE sidecar under the ``"usage_limit_armed_at"`` key.
    Unlike ``usage_limited_until``, this is a historical marker (not a
    forward-looking deadline) — it is NOT expiry-checked against "now".
    """

    def test_save_and_load_roundtrip(self, tmp_config_dir: Path) -> None:
        from datetime import UTC, datetime

        from cw.dispatch_state import (
            load_usage_limit_armed_at,
            save_usage_limit_armed_at,
        )

        armed_at = datetime.now(UTC)
        save_usage_limit_armed_at(armed_at)
        loaded = load_usage_limit_armed_at()
        assert loaded is not None
        assert abs((loaded - armed_at).total_seconds()) < 1

    def test_load_returns_none_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_usage_limit_armed_at

        cw.dispatch_state.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_usage_limit_armed_at() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_usage_limit_armed_at

        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_usage_limit_armed_at() is None

    def test_load_does_not_expire_old_timestamp(self, tmp_config_dir: Path) -> None:
        """Unlike usage_limited_until, an armed_at far in the past still loads.

        It's a historical marker read at the moment the loop notices a
        cleared window, not a forward-looking deadline — see the class
        docstring and dispatch_state.py's load_usage_limited_until for the
        contrast.
        """
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limit_armed_at,
            save_usage_limit_armed_at,
        )

        past = datetime.now(UTC) - timedelta(days=1)
        save_usage_limit_armed_at(past)
        loaded = load_usage_limit_armed_at()
        assert loaded is not None
        assert abs((loaded - past).total_seconds()) < 1

    def test_save_usage_limit_armed_at_preserves_usage_limited_until(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the armed_at key must not clobber usage_limited_until."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
            save_usage_limit_armed_at,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        save_usage_limit_armed_at(datetime.now(UTC))
        assert load_usage_limited_until() == {"test-client": future}

    def test_merge_and_save_usage_limited_until_preserves_usage_limit_armed_at(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing usage_limited_until must not clobber the armed_at key."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limit_armed_at,
            merge_and_save_usage_limited_until,
            save_usage_limit_armed_at,
        )

        armed_at = datetime.now(UTC)
        save_usage_limit_armed_at(armed_at)
        merge_and_save_usage_limited_until(
            {"test-client": datetime.now(UTC) + timedelta(hours=1)}
        )
        loaded = load_usage_limit_armed_at()
        assert loaded is not None
        assert abs((loaded - armed_at).total_seconds()) < 1

    def test_save_usage_limit_armed_at_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """save_usage_limit_armed_at also tolerates a corrupt existing sidecar."""
        from datetime import UTC, datetime

        import cw.dispatch_state
        from cw.dispatch_state import (
            load_usage_limit_armed_at,
            save_usage_limit_armed_at,
        )

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        armed_at = datetime.now(UTC)
        save_usage_limit_armed_at(armed_at)
        loaded = load_usage_limit_armed_at()
        assert loaded is not None
        assert abs((loaded - armed_at).total_seconds()) < 1

    def test_save_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """save_usage_limit_armed_at swallows OSError and emits a warning."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        from cw.dispatch_state import save_usage_limit_armed_at

        armed_at = datetime.now(UTC)
        with patch(
            "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
        ):
            save_usage_limit_armed_at(armed_at)

    def test_save_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above."""
        from datetime import UTC, datetime

        from cw.dispatch_state import save_usage_limit_armed_at

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE", real_dispatch_state_file
        )
        mock_write = MagicMock()
        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", mock_write)

        armed_at = datetime.now(UTC)
        with pytest.raises(CwError, match="refusing real-state write"):
            save_usage_limit_armed_at(armed_at)

        mock_write.assert_not_called()


class TestAvailabilityProbeCachePersistence:
    """Unit tests for the fleet-wide gh-availability probe cache (RFC 0011 A5).

    Sibling of TestUsageLimitedUntilPersistence: the cache is persisted in the
    same DISPATCH_STATE_FILE sidecar under the ``"availability_probe"`` key.
    The two clobber-regression tests pin the read-merge-write contract that
    keeps merge_and_save_usage_limited_until and save_availability_probe_cache from
    overwriting each other's key (#1157).
    """

    def test_save_then_load_round_trip_available_true(
        self, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.dispatch_state import (
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

        from cw.dispatch_state import (
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
        import cw.dispatch_state
        from cw.dispatch_state import load_availability_probe_cache

        cw.dispatch_state.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_availability_probe_cache() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_availability_probe_cache

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_availability_probe_cache() is None

    def test_load_returns_none_when_key_absent(self, tmp_config_dir: Path) -> None:
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_availability_probe_cache

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": {}})
        )
        assert load_availability_probe_cache() is None

    def test_load_returns_none_on_malformed_shape(self, tmp_config_dir: Path) -> None:
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_availability_probe_cache

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # available is an int, not a bool; latched/probed_at missing → None.
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
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

        from cw.dispatch_state import (
            AvailabilityProbeCache,
            save_availability_probe_cache,
        )

        cache = AvailabilityProbeCache(
            probed_at=datetime.now(UTC), available=True, latched=False
        )
        with (
            patch(
                "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.WARNING, logger="cw.dispatch_state"),
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

        from cw.dispatch_state import (
            AvailabilityProbeCache,
            save_availability_probe_cache,
        )

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE", real_dispatch_state_file
        )
        mock_write = MagicMock()
        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", mock_write)

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

        from cw.dispatch_state import (
            AvailabilityProbeCache,
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
            save_availability_probe_cache,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=False, latched=True
            )
        )
        assert load_usage_limited_until() == {"test-client": future}

    def test_merge_and_save_usage_limited_until_preserves_availability_probe_cache(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the usage-limit key must not clobber the probe cache."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            merge_and_save_usage_limited_until,
            save_availability_probe_cache,
        )

        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=False, latched=True
            )
        )
        merge_and_save_usage_limited_until(
            {"test-client": datetime.now(UTC) + timedelta(hours=1)}
        )
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is False
        assert loaded.latched is True

    def test_save_availability_probe_cache_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """A corrupt existing sidecar is replaced, not raised on (#1157)."""
        from datetime import UTC, datetime

        import cw.dispatch_state
        from cw.dispatch_state import (
            AvailabilityProbeCache,
            load_availability_probe_cache,
            save_availability_probe_cache,
        )

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        save_availability_probe_cache(
            AvailabilityProbeCache(
                probed_at=datetime.now(UTC), available=True, latched=False
            )
        )
        loaded = load_availability_probe_cache()
        assert loaded is not None
        assert loaded.available is True

    def test_merge_and_save_usage_limited_until_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """merge_and_save_usage_limited_until tolerates a corrupt existing sidecar."""
        from datetime import UTC, datetime, timedelta

        import cw.dispatch_state
        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
        )

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        assert load_usage_limited_until() == {"test-client": future}


class TestMainDriftLatchesPersistence:
    """Unit tests for the per-client main-checkout-drift latch (#1258).

    Sibling of TestAvailabilityProbeCachePersistence: persisted in the same
    DISPATCH_STATE_FILE sidecar under the ``"main_drift_latches"`` key.
    """

    def test_save_then_load_round_trip(self, tmp_config_dir: Path) -> None:
        from cw.dispatch_state import load_main_drift_latches, save_main_drift_latches

        save_main_drift_latches({"client-a": True, "client-b": False})
        assert load_main_drift_latches() == {"client-a": True, "client-b": False}

    def test_load_returns_empty_when_file_absent(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_main_drift_latches

        cw.dispatch_state.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_when_key_absent(self, tmp_config_dir: Path) -> None:
        """File exists (sibling sidecar key present) but no main_drift_latches key."""
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_main_drift_latches

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": {}})
        )
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_on_corrupt_json(self, tmp_config_dir: Path) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_main_drift_latches

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_main_drift_latches() == {}

    def test_load_returns_empty_on_malformed_value_type(
        self, tmp_config_dir: Path
    ) -> None:
        """A non-bool latch value is treated as malformed, not coerced."""
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_main_drift_latches

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
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

        from cw.dispatch_state import save_main_drift_latches

        with (
            patch(
                "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.WARNING, logger="cw.dispatch_state"),
        ):
            save_main_drift_latches({"client-a": True})

        assert "main_drift_latches" in caplog.text

    def test_save_refuses_real_path_and_does_not_swallow(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above."""
        from cw.dispatch_state import save_main_drift_latches

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE", real_dispatch_state_file
        )
        mock_write = MagicMock()
        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            save_main_drift_latches({"client-a": True})

        mock_write.assert_not_called()

    def test_save_preserves_availability_probe_cache(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing the latch map must not clobber the availability probe key."""
        from datetime import UTC, datetime

        from cw.dispatch_state import (
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


class TestExecutorBlockedMarkerPersistence:
    """Unit tests for the per-(client, ticket) executor-blocked marker (#1742).

    Sibling of TestAvailabilityProbeCachePersistence/TestMainDriftLatchesPersistence:
    persisted in the same DISPATCH_STATE_FILE sidecar under the
    ``"executor_blocked"`` key. Transient, non-durable state — wiped wholesale
    at dispatch-loop process boot rather than schema-versioned.
    """

    def _marker(
        self,
        *,
        client: str = "client-a",
        ticket_id: str = "1723",
        started_at: datetime | None = None,
    ) -> ExecutorBlockedMarker:
        from datetime import UTC, datetime

        from cw.dispatch_state import ExecutorBlockedMarker

        return ExecutorBlockedMarker(
            client=client,
            ticket_id=ticket_id,
            executor="codex",
            reviewer_role=None,
            started_at=started_at or datetime.now(UTC),
            session_id=f"sid-{ticket_id}",
        )

    def test_save_executor_blocked_marker_round_trips(
        self, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.dispatch_state import (
            load_executor_blocked_markers,
            save_executor_blocked_marker,
        )

        started_at = datetime.now(UTC)
        save_executor_blocked_marker(self._marker(started_at=started_at))

        markers = load_executor_blocked_markers()
        assert list(markers) == ["client-a/1723"]
        marker = markers["client-a/1723"]
        assert marker.client == "client-a"
        assert marker.ticket_id == "1723"
        assert marker.executor == "codex"
        assert marker.reviewer_role is None
        assert marker.session_id == "sid-1723"
        assert abs((marker.started_at - started_at).total_seconds()) < 1

    def test_save_executor_blocked_marker_preserves_usage_limited_until(
        self, tmp_config_dir: Path
    ) -> None:
        """Writing a marker must not clobber the usage-limit key."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
            save_executor_blocked_marker,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        save_executor_blocked_marker(self._marker())

        assert load_usage_limited_until() == {"test-client": future}

    def test_save_executor_blocked_marker_swallows_corrupt_existing_sidecar(
        self, tmp_config_dir: Path
    ) -> None:
        """A corrupt existing sidecar is replaced, not raised on (#1157)."""
        import cw.dispatch_state
        from cw.dispatch_state import (
            load_executor_blocked_markers,
            save_executor_blocked_marker,
        )

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        save_executor_blocked_marker(self._marker())

        markers = load_executor_blocked_markers()
        assert "client-a/1723" in markers

    def test_save_executor_blocked_marker_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        from unittest.mock import patch

        from cw.dispatch_state import save_executor_blocked_marker

        with (
            patch(
                "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.WARNING, logger="cw.dispatch_state"),
        ):
            save_executor_blocked_marker(self._marker())

        assert "executor_blocked" in caplog.text

    def test_save_executor_blocked_marker_refuses_real_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1017 CwError guard must propagate, unlike the OSError above."""
        from cw.dispatch_state import save_executor_blocked_marker

        real_dispatch_state_file = _REAL_STATE_DIR / "dispatch_state.json"
        monkeypatch.setattr(
            "cw.dispatch_state.DISPATCH_STATE_FILE", real_dispatch_state_file
        )
        mock_write = MagicMock()
        monkeypatch.setattr("cw.dispatch_state.atomic_write_text", mock_write)

        with pytest.raises(CwError, match="refusing real-state write"):
            save_executor_blocked_marker(self._marker())

        mock_write.assert_not_called()

    def test_clear_executor_blocked_marker_removes_only_that_entry(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.dispatch_state import (
            clear_executor_blocked_marker,
            load_executor_blocked_markers,
            save_executor_blocked_marker,
        )

        save_executor_blocked_marker(self._marker(client="client-a", ticket_id="1"))
        save_executor_blocked_marker(self._marker(client="client-b", ticket_id="2"))
        clear_executor_blocked_marker("client-a", "1")

        markers = load_executor_blocked_markers()
        assert list(markers) == ["client-b/2"]

    def test_clear_executor_blocked_marker_missing_entry_is_noop(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.dispatch_state import (
            clear_executor_blocked_marker,
            load_executor_blocked_markers,
        )

        clear_executor_blocked_marker("nobody", "nothing")
        assert load_executor_blocked_markers() == {}

    def test_clear_all_executor_blocked_markers_wipes_every_entry(
        self, tmp_config_dir: Path
    ) -> None:
        """Boot wipe clears all markers but preserves sibling sidecar keys."""
        from datetime import UTC, datetime, timedelta

        from cw.dispatch_state import (
            clear_all_executor_blocked_markers,
            load_executor_blocked_markers,
            load_usage_limited_until,
            merge_and_save_usage_limited_until,
            save_executor_blocked_marker,
        )

        future = datetime.now(UTC) + timedelta(hours=1)
        merge_and_save_usage_limited_until({"test-client": future})
        save_executor_blocked_marker(self._marker(client="client-a", ticket_id="1"))
        save_executor_blocked_marker(self._marker(client="client-b", ticket_id="2"))

        clear_all_executor_blocked_markers()

        assert load_executor_blocked_markers() == {}
        assert load_usage_limited_until() == {"test-client": future}

    def test_clear_all_executor_blocked_markers_warns_on_oserror(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        from unittest.mock import patch

        from cw.dispatch_state import clear_all_executor_blocked_markers

        with (
            patch(
                "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.WARNING, logger="cw.dispatch_state"),
        ):
            clear_all_executor_blocked_markers()

        assert "executor_blocked" in caplog.text

    def test_load_executor_blocked_markers_returns_empty_when_file_absent(
        self, tmp_config_dir: Path
    ) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.unlink(missing_ok=True)
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_returns_empty_on_corrupt_json(
        self, tmp_config_dir: Path
    ) -> None:
        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_returns_empty_when_key_absent(
        self, tmp_config_dir: Path
    ) -> None:
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"usage_limited_until": {}})
        )
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_tolerates_malformed_entry(
        self, tmp_config_dir: Path
    ) -> None:
        """One malformed entry is dropped; well-formed siblings survive."""
        import json
        from datetime import UTC, datetime

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(
                {
                    "executor_blocked": {
                        "client-a/1": {
                            "client": "client-a",
                            "ticket_id": "1",
                            "executor": "codex",
                            "reviewer_role": None,
                            "started_at": datetime.now(UTC).isoformat(),
                            "session_id": "sid-1",
                        },
                        # started_at missing → dropped, not raised on.
                        "client-b/2": {
                            "client": "client-b",
                            "ticket_id": "2",
                            "executor": "codex",
                            "reviewer_role": None,
                            "session_id": "sid-2",
                        },
                    }
                }
            )
        )
        markers = load_executor_blocked_markers()
        assert list(markers) == ["client-a/1"]

    def test_load_executor_blocked_markers_returns_empty_on_non_dict_key(
        self, tmp_config_dir: Path
    ) -> None:
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"executor_blocked": ["not", "a", "dict"]})
        )
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_returns_empty_on_non_dict_document(
        self, tmp_config_dir: Path
    ) -> None:
        """Valid JSON that is not an object at the top level → {}, not a raise."""
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(json.dumps(["a", "list"]))
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_drops_non_dict_entry(
        self, tmp_config_dir: Path
    ) -> None:
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps({"executor_blocked": {"client-a/1": "not-a-dict"}})
        )
        assert load_executor_blocked_markers() == {}

    def test_load_executor_blocked_markers_drops_unparseable_started_at(
        self, tmp_config_dir: Path
    ) -> None:
        """A str started_at that isn't an ISO timestamp is dropped, not raised on."""
        import json

        import cw.dispatch_state
        from cw.dispatch_state import load_executor_blocked_markers

        cw.dispatch_state.DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text(
            json.dumps(
                {
                    "executor_blocked": {
                        "client-a/1": {
                            "client": "client-a",
                            "ticket_id": "1",
                            "executor": "codex",
                            "reviewer_role": None,
                            "started_at": "yesterday-ish",
                            "session_id": "sid-1",
                        }
                    }
                }
            )
        )
        assert load_executor_blocked_markers() == {}

    def test_clear_executor_blocked_marker_warns_and_does_not_raise_on_oserror(
        self,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The finally: caller must never see a raise from the clear (#1742)."""
        import logging
        from unittest.mock import patch

        from cw.dispatch_state import (
            clear_executor_blocked_marker,
            save_executor_blocked_marker,
        )

        save_executor_blocked_marker(self._marker())
        with (
            patch(
                "cw.dispatch_state.atomic_write_text", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.WARNING, logger="cw.dispatch_state"),
        ):
            clear_executor_blocked_marker("client-a", "1723")

        assert "executor_blocked" in caplog.text
