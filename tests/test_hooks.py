"""Tests for cw.hooks - hook installation and status management."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.hooks import (
    hook_status,
    install_context_hook,
    reset_turn_count,
    uninstall_context_hook,
)

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
