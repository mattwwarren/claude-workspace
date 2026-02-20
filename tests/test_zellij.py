"""Tests for cw.zellij - Zellij terminal multiplexer integration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from cw.models import ClientConfig
from cw.zellij import (
    check_pane_health,
    current_session_name,
    focus_pane,
    generate_layout,
    go_to_tab,
    in_zellij_session,
    is_installed,
    list_sessions,
    new_tab,
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
        assert 'name="review"' in content
        assert 'name="debt"' in content
        assert 'name="files"' in content

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
        assert 'name="review"' not in content
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
        result = generate_layout(client, purposes=["impl", "review"])
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="review"' in content
        assert 'name="debt"' not in content
        assert 'size="70%"' in content
        assert 'size="30%"' in content

    def test_four_purpose_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layouts_dir = tmp_path / "layouts"
        monkeypatch.setattr("cw.zellij.GENERATED_LAYOUTS_DIR", layouts_dir)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        client = ClientConfig(name="quad", workspace_path=ws_dir)
        result = generate_layout(
            client, purposes=["impl", "review", "debt", "explore"],
        )
        content = result.read_text()
        assert 'name="impl"' in content
        assert 'name="review"' in content
        assert 'name="debt"' in content
        assert 'name="explore"' in content
        assert 'size="60%"' in content
        assert 'size="40%"' in content

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
                    '"claude --resume abc'
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
            "review": {"claude_cmd": '"claude"'},
        }
        result = generate_layout(client, panes=panes, purposes=["impl", "review"])
        content = result.read_text()
        assert 'cwd "/custom/worktree"' in content
        assert f'cwd "{ws_dir}"' in content


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
    def test_cycles_to_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, ...]] = []

        # dump-layout with command= so pane-to-terminal mapping works
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="yazi" name="files" {\n'
            '  pane command="claude" name="impl" {\n'
            '  pane command="claude" name="review" {\n'
            '  pane command="claude" name="debt" {\n'
        )
        # Cycle: start on review (terminal_2), target impl (terminal_1)
        focused_terminal = ["terminal_2"]

        def mock_run(*args: str, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0)
            if "dump-layout" in args:
                result.stdout = layout
            elif "list-clients" in args:
                result.stdout = (
                    "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n"
                    f"1         {focused_terminal[0]}     claude\n"
                )
            elif "focus-next-pane" in args:
                # Simulate cycle: 2 -> 3 -> 0 -> 1
                cycle = {
                    "terminal_2": "terminal_3",
                    "terminal_3": "terminal_0",
                    "terminal_0": "terminal_1",
                }
                focused_terminal[0] = cycle.get(
                    focused_terminal[0], "terminal_0"
                )
            return result

        monkeypatch.setattr("cw.zellij._run_zellij", mock_run)
        focus_pane("impl")  # impl = terminal_1
        focus_calls = [c for c in calls if "focus-next-pane" in c]
        # Should cycle 3 times: review->debt->files->impl
        assert len(focus_calls) == 3


class TestCheckPaneHealth:
    def test_all_panes_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="yazi" name="files" {\n'
            '  pane command="claude" name="impl" {\n'
            '  pane command="claude" name="review" {\n'
            '  pane command="claude" name="debt" {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert health == {
            "files": True,
            "impl": True,
            "review": True,
            "debt": True,
        }

    def test_exited_pane_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layout = (
            'tab name="Tab #1" {\n'
            '  pane command="yazi" name="files" {\n'
            '  pane command="claude" name="impl" exited {\n'
            '  pane command="claude" name="review" {\n'
            '  pane command="claude" name="debt" exited {\n'
        )
        mock_result = MagicMock(returncode=0, stdout=layout)
        monkeypatch.setattr(
            "cw.zellij._run_zellij", lambda *_a, **_kw: mock_result
        )

        health = check_pane_health()
        assert health["impl"] is False
        assert health["review"] is True
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
