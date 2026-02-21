"""Tests for cw.hooks - hook installation, status, and event dispatch."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cw.cli import main as cli_main
from cw.exceptions import CwError
from cw.hooks import (
    add_event_hook,
    dispatch_event_hooks,
    hook_status,
    install_context_hook,
    list_event_hooks,
    load_event_hooks,
    remove_event_hook,
    reset_turn_count,
    save_event_hooks,
    uninstall_context_hook,
)
from cw.models import EventHookRegistry, HookRule

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def tmp_hooks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOOKS_DIR to tmp_path/hooks for isolation."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.HOOKS_DIR", hooks_dir)
    monkeypatch.setattr("cw.hooks.HOOKS_DIR", hooks_dir)
    return hooks_dir


class TestInstallContextHook:
    def test_creates_script_file(
        self, tmp_hooks_dir: Path
    ) -> None:
        script_path = install_context_hook("acme", threshold=20)
        assert script_path.exists()

    def test_script_contains_threshold(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=42)
        script_path = tmp_hooks_dir / "context-check-acme.sh"
        content = script_path.read_text()
        assert "THRESHOLD=42" in content

    def test_script_contains_client_name(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("myproject", threshold=10)
        script_path = tmp_hooks_dir / "context-check-myproject.sh"
        content = script_path.read_text()
        assert "myproject" in content

    def test_script_is_executable(
        self, tmp_hooks_dir: Path
    ) -> None:
        script_path = install_context_hook("acme", threshold=20)
        mode = script_path.stat().st_mode
        assert mode & stat.S_IXUSR, "Script should be user-executable"
        assert mode & stat.S_IXGRP, "Script should be group-executable"
        assert mode & stat.S_IXOTH, "Script should be world-executable"

    def test_returns_path_to_script(
        self, tmp_hooks_dir: Path
    ) -> None:
        script_path = install_context_hook("acme", threshold=20)
        assert script_path == tmp_hooks_dir / "context-check-acme.sh"

    def test_creates_hooks_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hooks_dir = tmp_path / "nonexistent" / "hooks"
        monkeypatch.setattr("cw.config.HOOKS_DIR", hooks_dir)
        monkeypatch.setattr("cw.hooks.HOOKS_DIR", hooks_dir)
        install_context_hook("acme", threshold=20)
        assert hooks_dir.exists()


class TestUninstallContextHook:
    def test_removes_script_file(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        script_path = tmp_hooks_dir / "context-check-acme.sh"
        assert script_path.exists()
        uninstall_context_hook("acme")
        assert not script_path.exists()

    def test_raises_if_not_installed(
        self, tmp_hooks_dir: Path
    ) -> None:
        with pytest.raises(CwError, match="No hook installed for client 'acme'"):
            uninstall_context_hook("acme")

    def test_cleans_up_turn_counter(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        turn_path.write_text("5")
        assert turn_path.exists()
        uninstall_context_hook("acme")
        assert not turn_path.exists()

    def test_succeeds_without_turn_counter(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        assert not turn_path.exists()
        # Should not raise even though turn file is absent
        uninstall_context_hook("acme")


class TestHookStatus:
    def test_returns_installed_true_when_hook_exists(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        status = hook_status("acme")
        assert status["installed"] is True

    def test_returns_installed_false_when_no_hook(
        self, tmp_hooks_dir: Path
    ) -> None:
        status = hook_status("acme")
        assert status["installed"] is False

    def test_reads_turn_count_from_file(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        turn_path.write_text("7")
        status = hook_status("acme")
        assert status["turn_count"] == 7

    def test_turn_count_defaults_to_zero_when_file_absent(
        self, tmp_hooks_dir: Path
    ) -> None:
        status = hook_status("acme")
        assert status["turn_count"] == 0

    def test_turn_count_defaults_to_zero_on_invalid_content(
        self, tmp_hooks_dir: Path
    ) -> None:
        install_context_hook("acme", threshold=20)
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        turn_path.write_text("not-a-number")
        status = hook_status("acme")
        assert status["turn_count"] == 0

    def test_includes_script_path_in_result(
        self, tmp_hooks_dir: Path
    ) -> None:
        status = hook_status("acme")
        expected = str(tmp_hooks_dir / "context-check-acme.sh")
        assert status["script_path"] == expected


class TestResetTurnCount:
    def test_writes_zero_to_turn_file(
        self, tmp_hooks_dir: Path
    ) -> None:
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        turn_path.write_text("15")
        reset_turn_count("acme")
        assert turn_path.read_text() == "0"

    def test_no_op_when_file_absent(
        self, tmp_hooks_dir: Path
    ) -> None:
        turn_path = tmp_hooks_dir / ".turn-count-acme"
        assert not turn_path.exists()
        # Should not raise
        reset_turn_count("acme")
        assert not turn_path.exists()


# ---------------------------------------------------------------------------
# Event hook registry tests
# ---------------------------------------------------------------------------


class TestLoadSaveEventHooks:
    def test_load_missing_file_returns_empty_registry(
        self, tmp_hooks_dir: Path
    ) -> None:
        registry = load_event_hooks("acme")
        assert registry.rules == []

    def test_save_creates_json_file(self, tmp_hooks_dir: Path) -> None:
        registry = EventHookRegistry()
        save_event_hooks("acme", registry)
        path = tmp_hooks_dir / "event-hooks-acme.json"
        assert path.exists()

    def test_roundtrip_preserves_rules(self, tmp_hooks_dir: Path) -> None:
        rule = HookRule(
            event_type="session_started",
            command="echo started",
            description="test hook",
        )
        registry = EventHookRegistry(rules=[rule])
        save_event_hooks("acme", registry)
        loaded = load_event_hooks("acme")
        assert len(loaded.rules) == 1
        assert loaded.rules[0].event_type == "session_started"
        assert loaded.rules[0].command == "echo started"
        assert loaded.rules[0].description == "test hook"

    def test_file_is_valid_json(self, tmp_hooks_dir: Path) -> None:
        save_event_hooks("acme", EventHookRegistry())
        raw = (tmp_hooks_dir / "event-hooks-acme.json").read_text()
        parsed = json.loads(raw)
        assert "rules" in parsed

    def test_creates_hooks_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hooks_dir = tmp_path / "nonexistent" / "hooks"
        monkeypatch.setattr("cw.config.HOOKS_DIR", hooks_dir)
        monkeypatch.setattr("cw.hooks.HOOKS_DIR", hooks_dir)
        save_event_hooks("acme", EventHookRegistry())
        assert hooks_dir.exists()


class TestAddEventHook:
    def test_returns_hook_rule(self, tmp_hooks_dir: Path) -> None:
        rule = add_event_hook("acme", "session_started", "echo hi")
        assert isinstance(rule, HookRule)
        assert rule.event_type == "session_started"
        assert rule.command == "echo hi"

    def test_persists_to_disk(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        loaded = load_event_hooks("acme")
        assert len(loaded.rules) == 1

    def test_appends_multiple_rules(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo a")
        add_event_hook("acme", "session_backgrounded", "echo b")
        loaded = load_event_hooks("acme")
        assert len(loaded.rules) == 2

    def test_allows_duplicate_event_types(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo first")
        add_event_hook("acme", "session_started", "echo second")
        loaded = load_event_hooks("acme")
        assert len(loaded.rules) == 2

    def test_description_is_optional(self, tmp_hooks_dir: Path) -> None:
        rule = add_event_hook("acme", "session_started", "echo hi")
        assert rule.description == ""

    def test_description_is_preserved(self, tmp_hooks_dir: Path) -> None:
        rule = add_event_hook(
            "acme", "session_started", "echo hi", description="my hook",
        )
        assert rule.description == "my hook"


class TestRemoveEventHook:
    def test_removes_matching_rules(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo a")
        removed = remove_event_hook("acme", "session_started")
        assert removed == 1
        assert load_event_hooks("acme").rules == []

    def test_removes_multiple_matching_rules(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo a")
        add_event_hook("acme", "session_started", "echo b")
        removed = remove_event_hook("acme", "session_started")
        assert removed == 2

    def test_leaves_non_matching_rules(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo a")
        add_event_hook("acme", "session_backgrounded", "echo b")
        remove_event_hook("acme", "session_started")
        loaded = load_event_hooks("acme")
        assert len(loaded.rules) == 1
        assert loaded.rules[0].event_type == "session_backgrounded"

    def test_returns_zero_when_no_match(self, tmp_hooks_dir: Path) -> None:
        removed = remove_event_hook("acme", "session_started")
        assert removed == 0

    def test_does_not_write_when_no_match(self, tmp_hooks_dir: Path) -> None:
        path = tmp_hooks_dir / "event-hooks-acme.json"
        assert not path.exists()
        remove_event_hook("acme", "session_started")
        assert not path.exists()


class TestListEventHooks:
    def test_empty_when_no_hooks(self, tmp_hooks_dir: Path) -> None:
        assert list_event_hooks("acme") == []

    def test_returns_all_rules(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo a")
        add_event_hook("acme", "session_backgrounded", "echo b")
        rules = list_event_hooks("acme")
        assert len(rules) == 2


class TestDispatchEventHooks:
    def test_spawns_subprocess_for_matching_hook(
        self, tmp_hooks_dir: Path
    ) -> None:
        add_event_hook("acme", "session_started", "echo dispatched")
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks("acme", "session_started")
            mock_popen.assert_called_once()
            args = mock_popen.call_args
            assert args[0][0] == ["/bin/sh", "-c", "echo dispatched"]

    def test_sets_env_vars(self, tmp_hooks_dir: Path) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks(
                "acme", "session_started", {"session_id": "abc123"},
            )
            env = mock_popen.call_args[1]["env"]
            assert env["CW_CLIENT"] == "acme"
            assert env["CW_EVENT_TYPE"] == "session_started"
            assert env["CW_META_SESSION_ID"] == "abc123"

    def test_skips_non_matching_event_type(
        self, tmp_hooks_dir: Path
    ) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks("acme", "session_backgrounded")
            mock_popen.assert_not_called()

    def test_dispatches_multiple_matching_hooks(
        self, tmp_hooks_dir: Path
    ) -> None:
        add_event_hook("acme", "session_started", "echo first")
        add_event_hook("acme", "session_started", "echo second")
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks("acme", "session_started")
            assert mock_popen.call_count == 2

    def test_no_op_when_no_hooks_registered(
        self, tmp_hooks_dir: Path
    ) -> None:
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks("acme", "session_started")
            mock_popen.assert_not_called()

    def test_hook_failure_does_not_propagate(
        self, tmp_hooks_dir: Path
    ) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        with patch(
            "cw.hooks.subprocess.Popen", side_effect=OSError("spawn failed"),
        ):
            # Must not raise
            dispatch_event_hooks("acme", "session_started")

    def test_corrupt_registry_does_not_propagate(
        self, tmp_hooks_dir: Path
    ) -> None:
        # Write invalid JSON to the registry file
        path = tmp_hooks_dir / "event-hooks-acme.json"
        path.write_text("not valid json")
        # Must not raise
        dispatch_event_hooks("acme", "session_started")

    def test_metadata_keys_are_uppercased(
        self, tmp_hooks_dir: Path
    ) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        with patch("cw.hooks.subprocess.Popen") as mock_popen:
            dispatch_event_hooks(
                "acme", "session_started", {"my_key": "val"},
            )
            env = mock_popen.call_args[1]["env"]
            assert env["CW_META_MY_KEY"] == "val"


# ---------------------------------------------------------------------------
# CLI integration tests for event hook commands
# ---------------------------------------------------------------------------


class TestHookCLI:
    @pytest.fixture(autouse=True)
    def _setup_hooks_dir(self, tmp_hooks_dir: Path) -> None:
        """All CLI tests use the tmp_hooks_dir fixture."""

    def test_hook_add(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["hook", "add", "acme", "session_started", "echo hello"],
        )
        assert result.exit_code == 0
        assert "Added hook" in result.output
        rules = list_event_hooks("acme")
        assert len(rules) == 1

    def test_hook_add_with_description(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "hook", "add", "acme", "session_started", "echo hi",
                "-d", "test desc",
            ],
        )
        assert result.exit_code == 0
        rules = list_event_hooks("acme")
        assert rules[0].description == "test desc"

    def test_hook_add_rejects_invalid_event_type(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["hook", "add", "acme", "invalid_event", "echo hi"],
        )
        assert result.exit_code != 0

    def test_hook_remove(self) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["hook", "remove", "acme", "session_started"],
        )
        assert result.exit_code == 0
        assert "Removed 1" in result.output

    def test_hook_remove_no_match(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["hook", "remove", "acme", "session_started"],
        )
        assert result.exit_code == 0
        assert "No hooks found" in result.output

    def test_hook_list_empty(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_main, ["hook", "list", "acme"])
        assert result.exit_code == 0
        assert "No event hooks" in result.output

    def test_hook_list_with_rules(self) -> None:
        add_event_hook("acme", "session_started", "echo hi")
        add_event_hook("acme", "session_backgrounded", "echo bg")
        runner = CliRunner()
        result = runner.invoke(cli_main, ["hook", "list", "acme"])
        assert result.exit_code == 0
        assert "session_started" in result.output
        assert "session_backgrounded" in result.output
