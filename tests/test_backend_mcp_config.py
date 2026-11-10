"""Tests for src/cw/backend_mcp_config.py (#3055, RFC 0018 S3)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import cw.backend_mcp_config as bmc
from cw.backend_mcp_config import (
    McpConfigReading,
    McpState,
    read_backend_mcp,
    user_layer_path,
)
from cw.models.orchestrator_config import CODEX_BACKEND, OPENCODE_BACKEND
from tests._backend_mcp_helpers import (
    McpStage,
    opencode_user_path,
    stage_mcp_config,
    write_project_layer,
    write_system_layer,
)

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_BACKENDS = (CODEX_BACKEND, OPENCODE_BACKEND)


def _no_git_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retarget the walk's repo marker so no host ``.git`` ancestor stops it.

    A dispatched worker's basetemp sits inside a git worktree.
    """
    monkeypatch.setattr(bmc, "_GIT_MARKER", ".no-such-git-marker-3055")


def _root(tmp_path: Path) -> Path:
    """A client root with its own ``.git`` so the probe walk stops there."""
    root = tmp_path / "client"
    (root / ".git").mkdir(parents=True)
    return root


class TestBackendConstants:
    def test_literals_match_the_backend_constants(self) -> None:
        assert bmc._CODEX == CODEX_BACKEND
        assert bmc._OPENCODE == OPENCODE_BACKEND


class TestUserLayerPath:
    def test_codex_home_env_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
        assert user_layer_path(CODEX_BACKEND) == tmp_path / "ch" / "config.toml"

    def test_codex_default_under_home(self) -> None:
        assert user_layer_path(CODEX_BACKEND) == Path.home() / ".codex" / "config.toml"

    def test_opencode_under_home(self) -> None:
        assert user_layer_path(OPENCODE_BACKEND) == opencode_user_path()

    def test_unsupported_backend(self) -> None:
        assert user_layer_path("claude-native") is None


_STATE_TABLE: list[tuple[str, McpStage, McpState]] = [
    (backend, stage, expected)
    for backend in _BACKENDS
    for stage, expected in (
        ("enabled", McpState.ENABLED),
        ("absent", McpState.ABSENT),
        ("disabled", McpState.DISABLED),
        ("missing", McpState.UNKNOWN),
        ("malformed", McpState.UNKNOWN),
        ("non_dict", McpState.UNKNOWN),
        ("non_bool_enabled", McpState.UNKNOWN),
    )
] + [(OPENCODE_BACKEND, "jsonc", McpState.UNKNOWN)]


class TestReadBackendMcp:
    @pytest.mark.parametrize(("backend", "stage", "expected"), _STATE_TABLE)
    def test_state_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        backend: str,
        stage: McpStage,
        expected: McpState,
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, backend, stage)

        reading = read_backend_mcp(backend, "linear", _root(tmp_path))

        assert reading.state is expected
        assert reading.path == path
        assert reading.reason

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_other_server_name_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "enabled")

        reading = read_backend_mcp(backend, "jira", _root(tmp_path))

        assert reading.state is McpState.ABSENT
        assert "jira" in reading.reason

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_staged_custom_server_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "disabled", server="jira")

        reading = read_backend_mcp(backend, "jira", _root(tmp_path))

        assert reading.state is McpState.DISABLED

    def test_no_servers_section_is_absent(self, tmp_path: Path) -> None:
        path = opencode_user_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"theme": "x"}\n', encoding="utf-8")

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading == McpConfigReading(McpState.ABSENT, path, "no mcp section")

    def test_server_entry_not_an_object_is_unknown(self, tmp_path: Path) -> None:
        path = opencode_user_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"mcp": {"linear": true}}\n', encoding="utf-8")

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert "not a table/object" in reading.reason

    def test_json_top_level_not_an_object_is_unknown(self, tmp_path: Path) -> None:
        path = opencode_user_path()
        path.parent.mkdir(parents=True)
        path.write_text("[]\n", encoding="utf-8")

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert "top level is not an object" in reading.reason

    def test_unreadable_user_file_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")

        def _raise(*_a: object, **_k: object) -> str:
            msg = "denied"
            raise PermissionError(msg)

        monkeypatch.setattr(Path, "read_text", _raise)

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert str(path) in reading.reason
        assert "unreadable" in reading.reason

    def test_unsupported_backend_is_unknown(self, tmp_path: Path) -> None:
        reading = read_backend_mcp("local", "linear", tmp_path)

        assert reading.state is McpState.UNKNOWN
        assert "no MCP config reader" in reading.reason


class TestLayerProbe:
    @pytest.mark.parametrize("backend", _BACKENDS)
    @pytest.mark.parametrize("stage", ["absent", "disabled"])
    def test_project_layer_downgrades_negative_verdict(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        backend: str,
        stage: McpStage,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, stage)
        root = _root(tmp_path)
        layer = write_project_layer(root, backend)

        reading = read_backend_mcp(backend, "linear", root)

        assert reading.state is McpState.UNKNOWN
        assert f"project/system layer present at {layer}; not parsed" in reading.reason

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_project_layer_in_an_ancestor_below_git_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "absent")
        repo = _root(tmp_path)
        layer = write_project_layer(repo, backend)
        nested = repo / "sub" / "dir"
        nested.mkdir(parents=True)

        reading = read_backend_mcp(backend, "linear", nested)

        assert reading.state is McpState.UNKNOWN
        assert str(layer) in reading.reason

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_layer_above_the_git_root_is_not_probed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "absent")
        repo = _root(tmp_path)
        write_project_layer(tmp_path, backend)

        reading = read_backend_mcp(backend, "linear", repo)

        assert reading.state is McpState.ABSENT

    def test_no_git_walk_stops_at_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_git_anywhere(monkeypatch)
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        layer_above_home = write_project_layer(Path.home().parent, OPENCODE_BACKEND)
        root = Path.home() / "work" / "client"
        root.mkdir(parents=True)
        layer = write_project_layer(Path.home(), OPENCODE_BACKEND)

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", root)

        assert reading.state is McpState.UNKNOWN
        assert str(layer) in reading.reason
        assert str(layer_above_home) not in reading.reason
        assert bmc._project_dirs(root)[-1] == Path.home()

    def test_codex_home_dir_user_file_is_not_its_own_project_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Walking up to HOME must not count ~/.codex/config.toml twice."""
        _no_git_anywhere(monkeypatch)
        path = Path.home() / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text("[mcp_servers.other]\ncommand = 'x'\n", encoding="utf-8")
        root = Path.home() / "client"
        root.mkdir()

        reading = read_backend_mcp(CODEX_BACKEND, "linear", root)

        assert reading.state is McpState.ABSENT

    def test_no_git_and_home_not_an_ancestor_walks_to_fs_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_git_anywhere(monkeypatch)
        root = tmp_path / "outside"
        root.mkdir()

        dirs = bmc._project_dirs(root)

        assert dirs[0] == root
        assert dirs[-1] == Path(root.anchor)

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_system_layer_downgrades(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "disabled")
        layer = write_system_layer(monkeypatch, tmp_path, backend)

        reading = read_backend_mcp(backend, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert str(layer) in reading.reason

    def test_jsonc_beside_user_file_downgrades(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        beside = path.with_suffix(".jsonc")
        beside.write_text("{}", encoding="utf-8")

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert str(beside) in reading.reason

    @pytest.mark.parametrize(
        "env", ["OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"]
    )
    def test_opencode_env_var_downgrades(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        monkeypatch.setenv(env, "/somewhere")

        reading = read_backend_mcp(OPENCODE_BACKEND, "linear", _root(tmp_path))

        assert reading.state is McpState.UNKNOWN
        assert f"env {env} is set; not parsed" in reading.reason

    @pytest.mark.parametrize("backend", _BACKENDS)
    def test_enabled_is_never_downgraded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, backend, "enabled")
        root = _root(tmp_path)
        write_project_layer(root, backend)
        write_system_layer(monkeypatch, tmp_path, backend)

        reading = read_backend_mcp(backend, "linear", root)

        assert reading.state is McpState.ENABLED

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_probe_found_file_is_never_opened(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        root = _root(tmp_path)
        layer = root / "opencode.json"
        layer.write_text("not json at all {{{", encoding="utf-8")
        layer.chmod(0)
        try:
            reading = read_backend_mcp(OPENCODE_BACKEND, "linear", root)
        finally:
            layer.chmod(0o600)

        assert reading.state is McpState.UNKNOWN
        assert str(layer) in reading.reason
