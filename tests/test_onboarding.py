"""Tests for cw.onboarding — agent-onboarding helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from cw.onboarding import (
    _CLAUDE_MD_MARKER,
    _SESSIONSTART_COMMAND,
    CW_ALLOWLIST_ENTRY,
    install_claude_md_snippet,
    install_cw_allowlist,
    install_sessionstart_hook,
    register_mcp_servers,
)

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# register_mcp_servers
# ---------------------------------------------------------------------------


class TestRegisterMcpServers:
    def test_happy_path_creates_mcp_json(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        register_mcp_servers(workspace, "my-client")

        mcp_path = workspace / ".mcp.json"
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text())
        servers = data["mcpServers"]
        assert "cw-queue-events" in servers
        assert "cw-pr-events" in servers

        q = servers["cw-queue-events"]
        assert q["command"] == "cw"
        assert "--client-id" in q["args"]
        assert "my-client" in q["args"]
        assert "CW_QUEUE_EVENTS_BASE_URL" in q["env"]

        p = servers["cw-pr-events"]
        assert p["command"] == "cw"
        assert "--client-id" in p["args"]
        assert "my-client" in p["args"]
        assert "CW_PR_EVENTS_BASE_URL" in p["env"]

    def test_idempotent_skip_if_present(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        # First call writes the entries.
        register_mcp_servers(workspace, "my-client")

        # Manually check initial content.
        mcp_path = workspace / ".mcp.json"
        first_content = mcp_path.read_text()

        # Second call must not change anything.
        register_mcp_servers(workspace, "other-client")

        second_content = mcp_path.read_text()
        # Keys were not overwritten; client-id still points to first call's value.
        assert first_content == second_content

    def test_merges_into_existing_mcp_json(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        mcp_path = workspace / ".mcp.json"
        existing = {
            "mcpServers": {
                "other-server": {"command": "other", "args": [], "env": {}},
            }
        }
        mcp_path.write_text(json.dumps(existing))

        register_mcp_servers(workspace, "my-client")

        data = json.loads(mcp_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "cw-queue-events" in data["mcpServers"]
        assert "cw-pr-events" in data["mcpServers"]

    def test_absent_dir_still_creates_file(self, tmp_path: Path) -> None:
        # workspace itself doesn't need to exist for .mcp.json; but the parent
        # dir is created by register_mcp_servers when needed.  Here the
        # workspace dir exists (mkdir), so .mcp.json is created inside it.
        workspace = tmp_path / "deep" / "repo"
        workspace.mkdir(parents=True)

        register_mcp_servers(workspace, "x")

        assert (workspace / ".mcp.json").exists()


# ---------------------------------------------------------------------------
# install_cw_allowlist
# ---------------------------------------------------------------------------


class TestInstallCwAllowlist:
    def test_happy_path_adds_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = tmp_path / "settings.json"
        monkeypatch.setattr("cw.onboarding._CLAUDE_SETTINGS_PATH", settings)

        install_cw_allowlist()

        data = json.loads(settings.read_text())
        assert CW_ALLOWLIST_ENTRY in data["permissions"]["allow"]

    def test_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = tmp_path / "settings.json"
        monkeypatch.setattr("cw.onboarding._CLAUDE_SETTINGS_PATH", settings)

        install_cw_allowlist()
        install_cw_allowlist()

        data = json.loads(settings.read_text())
        assert data["permissions"]["allow"].count(CW_ALLOWLIST_ENTRY) == 1

    def test_absent_file_creates_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = tmp_path / "subdir" / "settings.json"
        monkeypatch.setattr("cw.onboarding._CLAUDE_SETTINGS_PATH", settings)

        install_cw_allowlist()

        assert settings.exists()
        data = json.loads(settings.read_text())
        assert CW_ALLOWLIST_ENTRY in data["permissions"]["allow"]

    def test_unparseable_json_echoes_manual_instruction_no_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not-valid-json{{{")
        monkeypatch.setattr("cw.onboarding._CLAUDE_SETTINGS_PATH", settings)

        install_cw_allowlist()

        # File must be unchanged (no write on parse failure).
        assert settings.read_text() == "not-valid-json{{{"
        captured = capsys.readouterr()
        assert "manually" in captured.out

    def test_merges_into_existing_allow_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = tmp_path / "settings.json"
        existing = {"permissions": {"allow": ["Bash(git:*)", "Bash(gh:*)"]}}
        settings.write_text(json.dumps(existing))
        monkeypatch.setattr("cw.onboarding._CLAUDE_SETTINGS_PATH", settings)

        install_cw_allowlist()

        data = json.loads(settings.read_text())
        allow = data["permissions"]["allow"]
        assert CW_ALLOWLIST_ENTRY in allow
        assert "Bash(git:*)" in allow
        assert "Bash(gh:*)" in allow


# ---------------------------------------------------------------------------
# install_sessionstart_hook
# ---------------------------------------------------------------------------


class TestInstallSessionstartHook:
    def test_happy_path_writes_hook(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        install_sessionstart_hook(workspace)

        settings_path = workspace / ".claude" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        session_start = data["hooks"]["SessionStart"]
        commands = [
            h["command"] for item in session_start for h in item.get("hooks", [])
        ]
        assert _SESSIONSTART_COMMAND in commands

    def test_idempotent(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        install_sessionstart_hook(workspace)
        install_sessionstart_hook(workspace)

        settings_path = workspace / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())
        commands = [
            h["command"]
            for item in data["hooks"]["SessionStart"]
            for h in item.get("hooks", [])
        ]
        assert commands.count(_SESSIONSTART_COMMAND) == 1

    def test_existing_different_hooks_preserved(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "echo hello"}],
                    }
                ]
            }
        }
        settings_path.write_text(json.dumps(existing))

        install_sessionstart_hook(workspace)

        data = json.loads(settings_path.read_text())
        commands = [
            h["command"]
            for item in data["hooks"]["SessionStart"]
            for h in item.get("hooks", [])
        ]
        assert "echo hello" in commands
        assert _SESSIONSTART_COMMAND in commands

    def test_absent_dir_created(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        # .claude/ does NOT exist yet
        assert not (workspace / ".claude").exists()

        install_sessionstart_hook(workspace)

        assert (workspace / ".claude" / "settings.json").exists()


# ---------------------------------------------------------------------------
# install_claude_md_snippet
# ---------------------------------------------------------------------------


class TestInstallClaudeMdSnippet:
    def test_cw_schema_fails_skips_with_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["cw", "schema", "list"],
                returncode=1,
                stdout=b"",
                stderr=b"",
            )
            install_claude_md_snippet(workspace)

        captured = capsys.readouterr()
        assert "unavailable" in captured.out
        assert not (workspace / "CLAUDE.md").exists()

    def test_idempotent_marker_already_present(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text(f"# Existing\n\n{_CLAUDE_MD_MARKER}\nAlready here.\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["cw", "schema", "list"],
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
            install_claude_md_snippet(workspace)

        # Content must be unchanged.
        assert claude_md.read_text().count(_CLAUDE_MD_MARKER) == 1

    def test_appends_marker_when_absent(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["cw", "schema", "list"],
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
            install_claude_md_snippet(workspace)

        text = claude_md.read_text()
        assert _CLAUDE_MD_MARKER in text
        assert "cw Agent Integration" in text

    def test_creates_claude_md_when_absent(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["cw", "schema", "list"],
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
            install_claude_md_snippet(workspace)

        claude_md = workspace / "CLAUDE.md"
        assert claude_md.exists()
        assert _CLAUDE_MD_MARKER in claude_md.read_text()

    def test_cw_schema_fails_content_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When cw schema list fails, pre-existing CLAUDE.md must not be modified."""
        workspace = tmp_path / "repo"
        workspace.mkdir()
        claude_md = workspace / "CLAUDE.md"
        original_content = "# Existing project\n\nSome content.\n"
        claude_md.write_text(original_content)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["cw", "schema", "list"],
                returncode=1,
                stdout=b"",
                stderr=b"",
            )
            install_claude_md_snippet(workspace)

        assert claude_md.read_text() == original_content
        captured = capsys.readouterr()
        assert "unavailable" in captured.out


class TestInstallSessionstartHookExtra:
    def test_corrupt_settings_file_warns_and_writes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Unparseable settings.json emits a warning, resets to empty, writes hook."""
        workspace = tmp_path / "repo"
        workspace.mkdir()
        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        settings_path.write_text("{invalid json{{")

        # Must not raise
        install_sessionstart_hook(workspace)

        # Warning was emitted
        captured = capsys.readouterr()
        assert "could not parse" in captured.out

        # Valid settings.json was written with the hook
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        commands = [
            h["command"]
            for item in data["hooks"]["SessionStart"]
            for h in item.get("hooks", [])
        ]
        assert _SESSIONSTART_COMMAND in commands
