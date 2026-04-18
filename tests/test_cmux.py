"""Tests for cw.cmux - cmux terminal multiplexer adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cmux import CmuxAdapter, FakeCmuxAdapter, RealCmuxAdapter, get_cmux_adapter
from cw.exceptions import CwError

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

    def test_get_cmux_adapter_raises_on_non_macos(self) -> None:
        """get_cmux_adapter() raises CwError on non-macOS."""
        if sys.platform == "darwin":
            pytest.skip("skipping on macOS — guard not triggered")

        with pytest.raises(CwError, match="requires macOS"):
            get_cmux_adapter()


class TestFindSocket:
    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CMUX_SOCKET_PATH env var is honoured."""
        from cw.cmux import _find_socket

        monkeypatch.setenv("CMUX_SOCKET_PATH", "/custom/path.sock")
        monkeypatch.delenv("CMUX_TAG", raising=False)
        assert _find_socket() == Path("/custom/path.sock")

    def test_tag_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CMUX_TAG env var builds /tmp/cmux-<tag>.sock."""
        from cw.cmux import _find_socket

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.setenv("CMUX_TAG", "my-tag")
        assert _find_socket() == Path("/tmp/cmux-my-tag.sock")

    def test_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Falls back to /tmp/cmux.sock when stable path does not exist."""
        from cw.cmux import _find_socket

        monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
        monkeypatch.delenv("CMUX_TAG", raising=False)
        # Stable path won't exist in tmp_path environment
        monkeypatch.setattr("cw.cmux.Path.home", lambda: tmp_path)
        assert _find_socket() == Path("/tmp/cmux.sock")


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
