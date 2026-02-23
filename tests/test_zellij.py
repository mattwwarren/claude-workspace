"""Tests for cw.zellij - Zellij terminal multiplexer integration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from cw.models import ClientConfig
from cw.zellij import (
    check_pane_health,
    current_session_name,
    delete_exited_session,
    focus_pane,
    generate_layout,
    go_to_tab,
    in_zellij_session,
    is_installed,
    list_sessions,
    new_tab,
    rename_tab,
    session_exists,
    write_to_pane,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestIsInstalled:
    def test_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.zellij.shutil.which", lambda _cmd: "/usr/bin/zellij"
        )
        assert is_installed() is True

    def test_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.zellij.shutil.which", lambda _cmd: None)
        assert is_installed() is False


class TestListSessions:
    def test_parses_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "session1 [created ...]\nsession2 [created ...]\n"
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert list_sessions() == ["session1", "session2"]

    def test_empty_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert list_sessions() == []

    def test_empty_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert list_sessions() == []

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  cw [Created 5h ago]  \n"
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert list_sessions() == ["cw"]

    def test_excludes_exited_sessions(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "active-session [Created 2h ago]\n"
            "dead-session [Created 1d ago] (EXITED - attach to resurrect)\n"
        )
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert list_sessions() == ["active-session"]


class TestSessionExists:
    def test_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "cw [created ...]\n"
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert session_exists("cw") is True

    def test_not_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "other-session [created ...]\n"
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert session_exists("cw") is False


class TestDeleteExitedSession:
    def test_deletes_exited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0)
            result.stdout = "cw [Created 1d ago] (EXITED - attach to resurrect)\n"
            return result

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        assert delete_exited_session("cw") is True
        assert any("delete-session" in c for c in calls)

    def test_ignores_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0)
            result.stdout = "cw [Created 2h ago]\n"
            return result

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        assert delete_exited_session("cw") is False
        assert not any("delete-session" in c for c in calls)

    def test_no_matching_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock(returncode=0)
        mock_result.stdout = "other [Created 1d ago] (EXITED)\n"
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )
        assert delete_exited_session("cw") is False


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

    def test_creates_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layouts_dir = tmp_path / "new" / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="test", workspace_path=ws_dir)
        generate_layout(client)
        assert layouts_dir.exists()

    def test_layout_includes_pane_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="proj", workspace_path=ws_dir)
        result = generate_layout(client)
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="idea"' in content
        assert 'name="debt"' in content

    def test_single_purpose_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="solo", workspace_path=ws_dir)
        result = generate_layout(client, purposes=["impl"])
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="idea"' not in content
        assert 'name="debt"' not in content
        # Single pane should not have split_direction="vertical" secondary area
        assert "split_direction" not in content.split('name="impl"')[1].split("}")[0]

    def test_two_purpose_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="duo", workspace_path=ws_dir)
        result = generate_layout(client, purposes=["impl", "idea"])
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="idea"' in content
        assert 'name="debt"' not in content
        assert 'size="50%"' in content

    def test_four_purpose_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="quad", workspace_path=ws_dir)
        result = generate_layout(
            client, purposes=["impl", "idea", "debt", "explore"],
        )
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="idea"' in content
        assert 'name="debt"' in content
        assert 'name="explore"' in content
        assert 'size="50%"' in content

    def test_layout_with_prompt_special_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="special", workspace_path=ws_dir)
        panes = {
            "impl": {
                "claude_cmd": (
                    '"claude'
                    " --append-system-prompt 'say \\\"hello\\\"'\""
                ),
            },
        }
        result = generate_layout(client, panes=panes, purposes=["impl"])
        content = result.read_text()
        # The escaped quotes should be in the output
        assert "say \\" in content

    def test_per_pane_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="cwdtest", workspace_path=ws_dir)
        panes = {
            "impl": {"claude_cmd": '"claude"', "cwd": "/custom/worktree"},
            "idea": {"claude_cmd": '"claude"'},
        }
        result = generate_layout(client, panes=panes, purposes=["impl", "idea"])
        content = result.read_text()
        assert 'cwd "/custom/worktree"' in content
        assert f'cwd "{ws_dir}"' in content

    def test_default_layout_structure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default layout: idea primary left, impl+debt right."""
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="regress", workspace_path=ws_dir)
        result = generate_layout(client)
        content = result.read_text()

        # idea is the primary (first) pane with focus
        assert 'name="idea" focus=true' in content

        # impl and debt are secondary panes (no focus)
        assert 'name="impl"' in content
        assert 'name="debt"' in content
        assert content.index('name="idea"') < content.index('name="impl"')
        assert content.index('name="impl"') < content.index('name="debt"')

        # Terminal pane below idea (runs daemon)
        assert 'name="terminal"' in content
        assert "cw daemon start" in content
        assert content.index('name="idea"') < content.index('name="terminal"')

        # No yazi/files pane
        assert "yazi" not in content
        assert 'name="files"' not in content

    def test_primary_pane_gets_focus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: first purpose always gets focus=true."""
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="focus", workspace_path=ws_dir)
        result = generate_layout(client, purposes=["debt", "impl"])
        content = result.read_text()
        assert 'name="debt" focus=true' in content

    def test_all_panes_get_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: every pane gets a cwd set to workspace path."""
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="cwd", workspace_path=ws_dir)
        result = generate_layout(client)
        content = result.read_text()
        # 3 purpose panes + 1 terminal pane = 4 cwd references
        assert content.count(f'cwd "{ws_dir}"') == 4


class TestWriteToPane:
    def test_calls_zellij(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        write_to_pane("hello\n")
        assert len(calls) == 2
        assert calls[0] == ("action", "write-chars", "hello")
        assert calls[1] == ("action", "write", "13")

    def test_no_trailing_newline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        write_to_pane("hello")
        assert len(calls) == 1
        assert calls[0] == ("action", "write-chars", "hello")


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
    def test_cycles_to_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        # Pane names in cycle order
        pane_order = ["idea", "terminal", "impl", "debt"]
        focused_idx = [0]  # Start on idea

        def _make_layout() -> str:
            lines = ['tab name="Tab #1" {\n']
            for i, name in enumerate(pane_order):
                focus = " focus=true" if i == focused_idx[0] else ""
                lines.append(
                    f'  pane command="claude" name="{name}"{focus} {{\n'
                )
            return "".join(lines)

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0)
            if "dump-layout" in args:
                result.stdout = _make_layout()
            elif "focus-next-pane" in args:
                focused_idx[0] = (focused_idx[0] + 1) % len(pane_order)
            return result

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        focus_pane("impl")
        focus_calls = [c for c in calls if "focus-next-pane" in c]
        # Should cycle 2 times: idea->terminal->impl
        assert len(focus_calls) == 2


class TestCheckPaneHealth:
    def test_all_panes_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="claude" name="idea" {\n'
            '  pane command="bash" name="terminal" {\n'
            '  pane command="claude" name="impl" {\n'
            '  pane command="claude" name="debt" {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert health == {
            "terminal": True,
            "idea": True,
            "impl": True,
            "debt": True,
        }

    def test_exited_pane_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="claude" name="idea" {\n'
            '  pane command="bash" name="terminal" {\n'
            '  pane command="claude" name="impl" exited {\n'
            '  pane command="claude" name="debt" exited {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert health["impl"] is False
        assert health["idea"] is True
        assert health["debt"] is False

    def test_pane_without_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane name="shell" {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert health["shell"] is False

    def test_returns_empty_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_result = MagicMock(returncode=1)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        assert check_pane_health() == {}

    def test_only_first_tab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="claude" name="impl" {\n'
            'tab name="Tab #2" {\n'
            '  pane command="claude" name="extra" {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert "impl" in health
        assert "extra" not in health

    def test_passes_session_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_args: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            captured_args.append(args)
            return MagicMock(returncode=0, stdout='tab name="T" {\n')

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        check_pane_health(session="cw")
        assert "-s" in captured_args[0]
        assert "cw" in captured_args[0]


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


class TestLayoutSessionMode:
    def test_session_mode_true_has_bars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="proj", workspace_path=ws_dir)
        result = generate_layout(client, session_mode=True)
        content = result.read_text()
        assert "tab-bar" in content
        assert "status-bar" in content
        assert 'tab name="proj"' in content

    def test_session_mode_false_no_bars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="proj", workspace_path=ws_dir)
        result = generate_layout(client, session_mode=False)
        content = result.read_text()
        assert "tab-bar" not in content
        assert "status-bar" not in content
        assert 'tab name="proj"' in content

    def test_tab_name_in_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="my-client", workspace_path=ws_dir)
        result = generate_layout(client)
        content = result.read_text()
        assert 'tab name="my-client"' in content


class TestNewTab:
    def test_calls_zellij_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="proj", workspace_path=ws_dir)
        new_tab(client, session="cw")

        assert len(calls) == 1
        assert calls[0][:2] == ("-s", "cw")
        assert "new-tab" in calls[0]
        assert "--layout" in calls[0]


class TestRenameTab:
    def test_calls_zellij_action(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)

        rename_tab("proj [bg]")

        assert len(calls) == 1
        assert "rename-tab" in calls[0]
        assert "proj [bg]" in calls[0]

    def test_with_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)

        rename_tab("proj", session="cw")

        assert len(calls) == 1
        assert calls[0][:2] == ("-s", "cw")
        assert "rename-tab" in calls[0]
        assert "proj" in calls[0]


class TestCheckPaneHealthTabScoped:
    def test_specific_tab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="client-a" {\n'
            '  pane command="claude" name="impl" {\n'
            'tab name="client-b" {\n'
            '  pane command="claude" name="impl" exited {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health_a = check_pane_health(tab_name="client-a")
        assert health_a == {"impl": True}

    def test_second_tab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="client-a" {\n'
            '  pane command="claude" name="impl" {\n'
            'tab name="client-b" {\n'
            '  pane command="claude" name="impl" exited {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health_b = check_pane_health(tab_name="client-b")
        assert health_b == {"impl": False}

    def test_no_tab_name_uses_first(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layout = (
            'tab name="first" {\n'
            '  pane command="claude" name="impl" {\n'
            'tab name="second" {\n'
            '  pane command="claude" name="extra" {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert "impl" in health
        assert "extra" not in health
