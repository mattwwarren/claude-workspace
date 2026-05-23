"""Tests for cw.cmux - cmux terminal multiplexer adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.cmux import (
    CmuxAdapter,
    FakeCmuxAdapter,
    MultiplexerAdapter,
    RealCmuxAdapter,
    _resolve_backend_name,
    get_backend_adapter,
    get_cmux_adapter,
)
from cw.exceptions import CwError
from cw.models import BackendName, OrchestratorConfig
from cw.tmux import TmuxAdapter

if TYPE_CHECKING:
    pass


class TestFakeCmuxAdapterSpawn:
    def test_returns_deterministic_ref(self) -> None:
        adapter = FakeCmuxAdapter()
        ref1 = adapter.spawn("my-ws", "claude")
        ref2 = adapter.spawn("my-ws", "claude")
        assert ref1 == "fake-pane-1"
        assert ref2 == "fake-pane-2"

    def test_increments_counter(self) -> None:
        adapter = FakeCmuxAdapter()
        refs = [adapter.spawn("ws", "cmd") for _ in range(5)]
        assert refs == [f"fake-pane-{i}" for i in range(1, 6)]

    def test_records_workspace_command_surface(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.spawn("workspace-a", "echo hello", "right")
        assert adapter.calls["spawn"] == [("workspace-a", "echo hello", "right")]

    def test_default_surface_is_right(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.spawn("ws", "cmd")
        _ws, _cmd, surface = adapter.calls["spawn"][0]
        assert surface == "right"

    def test_records_multiple_spawns(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.spawn("ws1", "cmd1")
        adapter.spawn("ws2", "cmd2", "down")
        assert len(adapter.calls["spawn"]) == 2
        assert adapter.calls["spawn"][1] == ("ws2", "cmd2", "down")


class TestFakeCmuxAdapterClose:
    def test_records_surface_ref(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.close("fake-pane-1")
        assert adapter.calls["close"] == [("fake-pane-1",)]

    def test_records_multiple_closes(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.close("ref-a")
        adapter.close("ref-b")
        assert len(adapter.calls["close"]) == 2
        assert adapter.calls["close"][0] == ("ref-a",)
        assert adapter.calls["close"][1] == ("ref-b",)

    def test_close_returns_none(self) -> None:
        adapter = FakeCmuxAdapter()
        result = adapter.close("any-ref")
        assert result is None


class TestFakeCmuxAdapterIdentify:
    def test_returns_stub_focus(self) -> None:
        adapter = FakeCmuxAdapter()
        result = adapter.identify()
        assert "focused" in result
        assert result["focused"]["workspace_id"] == "fake-ws-1"
        assert result["focused"]["surface_id"] == "fake-pane-1"

    def test_records_call(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.identify()
        adapter.identify()
        assert len(adapter.calls["identify"]) == 2

    def test_identify_records_empty_tuple(self) -> None:
        adapter = FakeCmuxAdapter()
        adapter.identify()
        assert adapter.calls["identify"][0] == ()


class TestCmuxAdapterProtocol:
    def test_fake_satisfies_protocol(self) -> None:
        """FakeCmuxAdapter must satisfy the CmuxAdapter Protocol."""
        adapter = FakeCmuxAdapter()
        assert isinstance(adapter, CmuxAdapter)

    def test_spawn_returns_str(self) -> None:
        adapter: CmuxAdapter = FakeCmuxAdapter()
        ref = adapter.spawn("ws", "cmd")
        assert isinstance(ref, str)

    def test_close_accepts_str(self) -> None:
        adapter: CmuxAdapter = FakeCmuxAdapter()
        # Must not raise
        adapter.close("some-ref")

    def test_identify_returns_dict(self) -> None:
        adapter: CmuxAdapter = FakeCmuxAdapter()
        result = adapter.identify()
        assert isinstance(result, dict)


class TestRealCmuxAdapterPlatformGuard:
    def test_raises_on_non_macos(self) -> None:
        """RealCmuxAdapter raises CwError when not on macOS."""
        if sys.platform == "darwin":
            pytest.skip("skipping on macOS — guard not triggered")

        with pytest.raises(CwError, match="requires macOS"):
            RealCmuxAdapter()

    def test_get_cmux_adapter_raises_on_non_macos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forcing cmux on a non-macOS platform still raises CwError."""
        if sys.platform == "darwin":
            pytest.skip("skipping on macOS — guard not triggered")

        # The default selector on Linux now returns TmuxAdapter, so force
        # cmux via the env-var tier to exercise the platform guard.
        monkeypatch.setenv("CW_BACKEND", "cmux")
        with pytest.raises(CwError, match="requires macOS"):
            get_cmux_adapter()


class TestRealCmuxAdapterSpawn:
    """Tests for RealCmuxAdapter.spawn() via _call mocking (platform-independent)."""

    def _make_adapter(self, monkeypatch: pytest.MonkeyPatch) -> RealCmuxAdapter:
        """Create a RealCmuxAdapter with the platform guard bypassed."""
        monkeypatch.setattr("cw.cmux.sys.platform", "darwin")
        return RealCmuxAdapter(socket_path=Path("/tmp/fake-cmux.sock"))

    def test_spawn_calls_surface_list_for_surface_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn() calls surface.list to get a surface_id before surface.split."""
        adapter = self._make_adapter(monkeypatch)
        call_log: list[tuple[str, dict[str, Any]]] = []

        def fake_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            call_log.append((method, params))
            if method == "workspace.list":
                return {"workspaces": [{"id": "ws-uuid-1", "title": "my-workspace"}]}
            if method == "surface.list":
                return {"surfaces": [{"id": "surf-uuid-1", "ref": "surface:0"}]}
            if method == "surface.split":
                return {"surface_id": "new-surf-uuid"}
            if method == "surface.send_text":
                return {}
            return {}

        monkeypatch.setattr(adapter, "_call", fake_call)
        result = adapter.spawn("my-workspace", "claude")
        assert result == "new-surf-uuid"
        methods = [m for m, _ in call_log]
        assert methods == [
            "workspace.list",
            "surface.list",
            "surface.split",
            "surface.send_text",
        ]

    def test_spawn_passes_surface_id_to_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn() passes surface_id from surface.list to surface.split params."""
        adapter = self._make_adapter(monkeypatch)
        split_params: dict[str, Any] = {}

        def fake_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "workspace.list":
                return {"workspaces": [{"id": "ws-uuid-1", "title": "my-workspace"}]}
            if method == "surface.list":
                return {"surfaces": [{"id": "surf-uuid-1", "ref": "surface:0"}]}
            if method == "surface.split":
                split_params.update(params)
                return {"surface_id": "new-surf-uuid"}
            if method == "surface.send_text":
                return {}
            return {}

        monkeypatch.setattr(adapter, "_call", fake_call)
        adapter.spawn("my-workspace", "claude", "right")
        assert split_params["workspace_id"] == "ws-uuid-1"
        assert split_params["surface_id"] == "surf-uuid-1"
        assert split_params["direction"] == "right"

    def test_spawn_omits_surface_id_when_surface_list_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn() omits surface_id when surface.list returns empty."""
        adapter = self._make_adapter(monkeypatch)
        split_params: dict[str, Any] = {}

        def fake_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "workspace.list":
                return {"workspaces": [{"id": "ws-uuid-1", "title": "my-workspace"}]}
            if method == "surface.list":
                return {"surfaces": []}
            if method == "surface.split":
                split_params.update(params)
                return {"surface_id": "new-surf-uuid"}
            if method == "surface.send_text":
                return {}
            return {}

        monkeypatch.setattr(adapter, "_call", fake_call)
        adapter.spawn("my-workspace", "claude")
        assert "surface_id" not in split_params
        assert split_params["workspace_id"] == "ws-uuid-1"

    def test_spawn_raises_when_workspace_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn() raises CwError when workspace label is not in workspace.list."""
        adapter = self._make_adapter(monkeypatch)

        def fake_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "workspace.list":
                return {"workspaces": [{"id": "ws-uuid-1", "title": "other-ws"}]}
            return {}

        monkeypatch.setattr(adapter, "_call", fake_call)
        with pytest.raises(CwError, match="workspace not found"):
            adapter.spawn("my-workspace", "claude")


class TestFindSocket:
    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CMUX_SOCKET_PATH env var is honoured."""
        from cw.cmux import _find_socket

        monkeypatch.setenv("CMUX_SOCKET_PATH", "/custom/path.sock")
        monkeypatch.delenv("CMUX_SOCKET", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)
        assert _find_socket() == Path("/custom/path.sock")

    def test_cmux_socket_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CMUX_SOCKET alias is checked when CMUX_SOCKET_PATH is absent."""
        from cw.cmux import _find_socket

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)
        monkeypatch.setenv("CMUX_SOCKET", "/alias/path.sock")
        assert _find_socket() == Path("/alias/path.sock")

    def test_cmux_socket_path_takes_priority_over_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CMUX_SOCKET_PATH takes priority over CMUX_SOCKET."""
        from cw.cmux import _find_socket

        monkeypatch.setenv("CMUX_SOCKET_PATH", "/primary/path.sock")
        monkeypatch.setenv("CMUX_SOCKET", "/alias/path.sock")
        monkeypatch.delenv("CMUX_TAG", raising=False)
        assert _find_socket() == Path("/primary/path.sock")

    def test_tag_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CMUX_TAG env var builds /tmp/cmux-debug-<tag>.sock."""
        from cw.cmux import _find_socket

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_SOCKET", raising=False)
        monkeypatch.setenv("CMUX_TAG", "my-tag")
        assert _find_socket() == Path("/tmp/cmux-debug-my-tag.sock")

    def test_hint_file_used_when_stable_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """last-socket-path hint file is read when stable path does not exist."""
        import cw.cmux as cmux_module

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_SOCKET", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)

        # Create a fake socket target that "exists"
        fake_sock = tmp_path / "cmux-session.sock"
        fake_sock.touch()

        # Create the hint file inside a simulated Application Support dir
        app_support = tmp_path / "Library" / "Application Support" / "cmux"
        app_support.mkdir(parents=True)
        hint_file = app_support / "last-socket-path"
        hint_file.write_text(str(fake_sock))

        monkeypatch.setattr("cw.cmux.Path.home", lambda: tmp_path)
        # Redirect legacy hint so it doesn't interfere
        monkeypatch.setattr(cmux_module, "_LEGACY_HINT_PATH", tmp_path / "no-such-hint")
        assert cmux_module._find_socket() == fake_sock

    def test_legacy_hint_file_used_when_primary_hint_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """/tmp/cmux-last-socket-path legacy hint is read as fallback."""
        import cw.cmux as cmux_module

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_SOCKET", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)

        # Create a fake socket target that "exists"
        fake_sock = tmp_path / "cmux-legacy.sock"
        fake_sock.touch()

        # Create the legacy hint file in tmp_path (simulates /tmp/cmux-last-socket-path)
        legacy_hint = tmp_path / "cmux-last-socket-path"
        legacy_hint.write_text(str(fake_sock))

        # Redirect home so the stable and primary hint paths don't exist
        app_support = tmp_path / "Library" / "Application Support" / "cmux"
        app_support.mkdir(parents=True)
        monkeypatch.setattr("cw.cmux.Path.home", lambda: tmp_path)

        # Inject the tmp-based legacy hint path into the module for this test
        monkeypatch.setattr(cmux_module, "_LEGACY_HINT_PATH", legacy_hint)
        assert cmux_module._find_socket() == fake_sock

    def test_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Falls back to /tmp/cmux.sock when stable path does not exist."""
        import cw.cmux as cmux_module

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_SOCKET", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)
        # Stable path won't exist in tmp_path environment
        monkeypatch.setattr("cw.cmux.Path.home", lambda: tmp_path)
        # Redirect legacy hint to a non-existent path so it doesn't interfere
        monkeypatch.setattr(cmux_module, "_LEGACY_HINT_PATH", tmp_path / "no-such-hint")
        assert cmux_module._find_socket() == Path("/tmp/cmux.sock")


class TestMigration:
    """Test that load_state() migrates old zellij_pane/zellij_tab field names."""

    def test_migrates_zellij_pane_to_surface_ref(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """zellij_pane field is renamed to surface_ref on load."""
        from cw.config import STATE_DIR, load_state

        state_file = STATE_DIR / "sessions.json"
        old_state = {
            "sessions": [
                {
                    "id": "migrate01",
                    "name": "client/impl",
                    "client": "client",
                    "purpose": "impl",
                    "status": "active",
                    "workspace_path": str(tmp_config_dir),
                    "zellij_pane": "impl",
                    "zellij_tab": "client",
                    "started_at": "2025-01-15T10:00:00+00:00",
                }
            ]
        }
        state_file.write_text(json.dumps(old_state))

        state = load_state()
        assert len(state.sessions) == 1
        session = state.sessions[0]
        assert session.surface_ref == "impl"
        # zellij_pane/zellij_tab fields no longer exist on Session

    def test_drops_zellij_tab(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """zellij_tab field is silently dropped on load."""
        from cw.config import STATE_DIR, load_state

        state_file = STATE_DIR / "sessions.json"
        old_state = {
            "sessions": [
                {
                    "id": "migrate02",
                    "name": "client/idea",
                    "client": "client",
                    "purpose": "idea",
                    "status": "active",
                    "workspace_path": str(tmp_config_dir),
                    "zellij_tab": "client",
                    "started_at": "2025-01-15T10:00:00+00:00",
                }
            ]
        }
        state_file.write_text(json.dumps(old_state))

        state = load_state()
        assert len(state.sessions) == 1
        # No validation error from extra zellij_tab field

    def test_no_migration_needed_for_new_format(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """New format with surface_ref loads without modification."""
        from cw.config import STATE_DIR, load_state

        state_file = STATE_DIR / "sessions.json"
        new_state = {
            "sessions": [
                {
                    "id": "newform1",
                    "name": "client/debt",
                    "client": "client",
                    "purpose": "debt",
                    "status": "active",
                    "workspace_path": str(tmp_config_dir),
                    "surface_ref": "surf-abc123",
                    "started_at": "2025-01-15T10:00:00+00:00",
                }
            ]
        }
        state_file.write_text(json.dumps(new_state))

        state = load_state()
        assert state.sessions[0].surface_ref == "surf-abc123"

    def test_both_zellij_pane_and_surface_ref_present(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """If both zellij_pane and surface_ref are present, surface_ref wins."""
        from cw.config import STATE_DIR, load_state

        state_file = STATE_DIR / "sessions.json"
        state_data = {
            "sessions": [
                {
                    "id": "both0001",
                    "name": "client/impl",
                    "client": "client",
                    "purpose": "impl",
                    "status": "active",
                    "workspace_path": str(tmp_config_dir),
                    "zellij_pane": "old-ref",
                    "surface_ref": "new-ref",
                    "started_at": "2025-01-15T10:00:00+00:00",
                }
            ]
        }
        state_file.write_text(json.dumps(state_data))

        state = load_state()
        # surface_ref already present — zellij_pane should be discarded
        assert state.sessions[0].surface_ref == "new-ref"


class TestMultiplexerAdapterAlias:
    def test_cmux_adapter_is_multiplexer_adapter(self) -> None:
        # The legacy name must still resolve to the new protocol so any
        # downstream `from cw.cmux import CmuxAdapter` survives this
        # release.
        assert CmuxAdapter is MultiplexerAdapter

    def test_get_cmux_adapter_is_backend_adapter(self) -> None:
        assert get_cmux_adapter is get_backend_adapter


class TestResolveBackendName:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        assert _resolve_backend_name() is BackendName.FAKE

    def test_env_var_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_BACKEND", "TMUX")
        assert _resolve_backend_name() is BackendName.TMUX

    def test_invalid_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_BACKEND", "wat")
        with pytest.raises(CwError, match="Invalid CW_BACKEND"):
            _resolve_backend_name()

    def test_config_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_BACKEND", raising=False)
        monkeypatch.setattr(
            "cw.cmux.load_orchestrator_config",
            lambda: OrchestratorConfig(backend=BackendName.TMUX),
        )
        assert _resolve_backend_name() is BackendName.TMUX

    def test_platform_default_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_BACKEND", raising=False)
        monkeypatch.setattr(
            "cw.cmux.load_orchestrator_config",
            lambda: OrchestratorConfig(backend=None),
        )
        monkeypatch.setattr("cw.cmux.sys.platform", "linux")
        assert _resolve_backend_name() is BackendName.TMUX

    def test_platform_default_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_BACKEND", raising=False)
        monkeypatch.setattr(
            "cw.cmux.load_orchestrator_config",
            lambda: OrchestratorConfig(backend=None),
        )
        monkeypatch.setattr("cw.cmux.sys.platform", "darwin")
        assert _resolve_backend_name() is BackendName.CMUX


class TestGetBackendAdapter:
    def test_fake_backend_returns_fake_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        adapter = get_backend_adapter()
        assert isinstance(adapter, FakeCmuxAdapter)

    def test_tmux_backend_raises_without_tmux_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: None)
        with pytest.raises(CwError, match="tmux not found"):
            get_backend_adapter()

    def test_tmux_backend_returns_tmux_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.tmux.shutil.which", lambda _name: "/usr/bin/tmux")
        adapter = get_backend_adapter()
        assert isinstance(adapter, TmuxAdapter)


def test_fake_adapter_list_live_surface_commands_tracks_spawn() -> None:
    """FakeCmuxAdapter.list_live_surface_commands returns spawned refs as 'claude'."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    assert adapter.list_live_surface_commands() == {ref: "claude"}


def test_fake_adapter_list_live_surface_commands_empty_after_close() -> None:
    """Closing a surface removes it from the command map."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    adapter.close(ref)
    assert ref not in adapter.list_live_surface_commands()


def test_fake_adapter_set_pane_command_override() -> None:
    """set_pane_command changes the command shown in the live command map."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    adapter.set_pane_command(ref, "bash")
    assert adapter.list_live_surface_commands() == {ref: "bash"}


def test_fake_adapter_records_list_live_surface_commands_call() -> None:
    """list_live_surface_commands call is recorded in adapter.calls."""
    adapter = FakeCmuxAdapter()
    adapter.spawn("ws", "claude", "right")
    adapter.list_live_surface_commands()
    assert len(adapter.calls["list_live_surface_commands"]) == 1


def test_real_cmux_list_live_surface_commands_uses_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RealCmuxAdapter.list_live_surface_commands returns non-shell sentinel values.

    The cmux surface.list response does not expose a current-command field,
    so the adapter maps every live surface to a sentinel ('cmux-surface')
    so the zombie filter is a transparent no-op for the cmux backend.
    """
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter

    def fake_call(
        self: object, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "workspace.list":
            return {
                "workspaces": [
                    {"id": "ws-a", "title": "client-a"},
                ]
            }
        if method == "surface.list":
            return {"surfaces": [{"id": "surf-1"}, {"id": "surf-2"}]}
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))

    result = adapter.list_live_surface_commands()
    assert set(result.keys()) == {"surf-1", "surf-2"}
    # All values must be the non-shell sentinel — never a shell name.
    shell_names = {"bash", "zsh", "sh", "fish", "dash", "tcsh", "ksh"}
    for cmd in result.values():
        assert cmd not in shell_names, (
            f"Sentinel should not be a shell name, got {cmd!r}"
        )


def test_fake_adapter_list_surfaces_tracks_spawn_and_close() -> None:
    """FakeCmuxAdapter tracks live surfaces via spawn/close."""
    adapter = FakeCmuxAdapter()

    assert adapter.list_surfaces() == set()

    ref1 = adapter.spawn("ws-1", "echo hi")
    ref2 = adapter.spawn("ws-1", "echo bye")
    assert adapter.list_surfaces() == {ref1, ref2}

    adapter.close(ref1)
    assert adapter.list_surfaces() == {ref2}


def test_fake_adapter_close_unknown_ref_is_noop() -> None:
    """Closing a surface we never spawned must not raise."""
    adapter = FakeCmuxAdapter()
    adapter.close("never-spawned")  # must not raise
    assert adapter.list_surfaces() == set()


def test_real_cmux_list_surfaces_aggregates_across_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_surfaces calls workspace.list then surface.list for each workspace."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(
        self: object, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        calls.append((method, dict(params)))
        if method == "workspace.list":
            return {
                "workspaces": [
                    {"id": "ws-a", "title": "client-a"},
                    {"id": "ws-b", "title": "client-b"},
                ]
            }
        if method == "surface.list":
            ws_id = params["workspace_id"]
            if ws_id == "ws-a":
                return {"surfaces": [{"id": "surf-a1"}, {"id": "surf-a2"}]}
            return {"surfaces": [{"id": "surf-b1"}]}
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))

    assert adapter.list_surfaces() == {"surf-a1", "surf-a2", "surf-b1"}
    assert calls[0][0] == "workspace.list"
    assert {c[1]["workspace_id"] for c in calls if c[0] == "surface.list"} == {
        "ws-a",
        "ws-b",
    }


def test_real_cmux_list_surfaces_returns_empty_on_socket_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cmux socket is down, return empty set instead of raising."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter
    from cw.exceptions import CwError

    def fake_call(
        self: object, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        raise CwError("connection refused")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))
    assert adapter.list_surfaces() == set()


def test_real_cmux_list_surfaces_aborts_on_surface_list_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-workspace surface.list failure collapses the whole result to empty.

    Partial enumeration would let the reconciler falsely treat surfaces in
    the failing workspace as phantom while preserving the rest, so the
    adapter must return all-or-nothing.
    """
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter
    from cw.exceptions import CwError

    def fake_call(
        self: object, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "workspace.list":
            return {
                "workspaces": [
                    {"id": "ws-ok", "title": "a"},
                    {"id": "ws-broken", "title": "b"},
                ]
            }
        if method == "surface.list":
            if params["workspace_id"] == "ws-broken":
                raise CwError("permission denied")
            return {"surfaces": [{"id": "surf-ok"}]}
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(RealCmuxAdapter, "_call", fake_call)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))

    assert adapter.list_surfaces() == set()


def test_real_cmux_call_translates_os_error_to_cwerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_call converts socket OSError into CwError; callers get one exception type."""
    import socket as socket_module
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter
    from cw.exceptions import CwError

    def fake_connect(self: socket_module.socket, address: object) -> None:
        raise ConnectionRefusedError("no such file")

    monkeypatch.setattr(socket_module.socket, "connect", fake_connect)
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/nonexistent.sock"))

    with pytest.raises(CwError, match="cmux socket error"):
        adapter._call("whatever", {})


def test_real_cmux_call_translates_json_decode_error_to_cwerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_call converts malformed JSON response into CwError."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    from cw.cmux import RealCmuxAdapter
    from cw.exceptions import CwError

    class FakeSock:
        def connect(self, address: object) -> None:
            return None

        def sendall(self, data: bytes) -> None:
            return None

        def recv(self, bufsize: int) -> bytes:
            return b"not json\n"

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "socket.socket",
        # FakeSock mimics the socket.socket protocol structurally; mypy
        # cannot see that and flags the return type as incompatible.
        lambda *_args: FakeSock(),  # type: ignore[misc]
    )
    adapter = RealCmuxAdapter(socket_path=Path("/tmp/fake.sock"))

    with pytest.raises(CwError, match="malformed JSON"):
        adapter._call("whatever", {})
