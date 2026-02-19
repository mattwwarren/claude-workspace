"""Tests for cw.zellij - Zellij terminal multiplexer integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cw.models import ClientConfig
from cw.zellij import (
    current_session_name,
    focus_pane,
    generate_layout,
    go_to_tab,
    in_zellij_session,
    is_installed,
    list_sessions,
    session_exists,
    write_to_pane,
)


class TestIsInstalled:
    def test_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.zellij.shutil.which", lambda cmd: "/usr/bin/zellij")
        assert is_installed() is True

    def test_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.zellij.shutil.which", lambda cmd: None)
        assert is_installed() is False


class TestListSessions:
    def test_parses_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "session1 [created ...]\nsession2 [created ...]\n"
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert list_sessions() == ["session1", "session2"]

    def test_empty_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert list_sessions() == []

    def test_empty_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert list_sessions() == []

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  cw [Created 5h ago]  \n"
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert list_sessions() == ["cw"]


class TestSessionExists:
    def test_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "cw [created ...]\n"
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert session_exists("cw") is True

    def test_not_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "other-session [created ...]\n"
        monkeypatch.setattr("cw.zellij._run_zellij", lambda *a, **kw: mock_result)
        assert session_exists("cw") is False


class TestGenerateLayout:
    def test_renders_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        client = ClientConfig(
            name="test-proj", workspace_path="/home/user/projects/test"
        )
        result = generate_layout(client)

        assert result.exists()
        assert result.name == "cw-test-proj.kdl"
        content = result.read_text()
        assert "/home/user/projects/test" in content

    def test_creates_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        layouts_dir = tmp_path / "new" / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        client = ClientConfig(name="test", workspace_path="/tmp/ws")
        generate_layout(client)
        assert layouts_dir.exists()

    def test_layout_includes_pane_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        client = ClientConfig(name="proj", workspace_path="/tmp/ws")
        result = generate_layout(client)
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="review"' in content
        assert 'name="debt"' in content
        assert 'name="files"' in content


class TestWriteToPane:
    def test_calls_zellij(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        write_to_pane("hello\n")
        assert len(calls) == 1
        assert calls[0] == ("action", "write-chars", "hello\n")


class TestGoToTab:
    def test_calls_zellij(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            calls.append(args)
            return result

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        go_to_tab("my-tab")
        assert len(calls) == 1
        assert calls[0] == ("action", "go-to-tab-name", "my-tab")


class TestFocusPane:
    def test_calls_zellij(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        focus_pane("impl")
        assert len(calls) == 1
        assert calls[0] == ("action", "focus-pane", "--name", "impl")


class TestInZellijSession:
    def test_inside(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "cw")
        assert in_zellij_session() is True

    def test_outside(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        assert in_zellij_session() is False


class TestCurrentSessionName:
    def test_has_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "my-session")
        assert current_session_name() == "my-session"

    def test_no_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
        assert current_session_name() is None
