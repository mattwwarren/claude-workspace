"""Tests for .claude/scripts/utils/runtime_paths.py.

runtime_paths is a standalone utility module (not part of the cw package), so
it is loaded by path — same pattern as test_preflight.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_RUNTIME_PATHS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "scripts"
    / "utils"
    / "runtime_paths.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "runtime_paths_under_test", _RUNTIME_PATHS
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestClaudeDir:
    def test_claude_dir_exists(self) -> None:
        mod = _load()
        assert hasattr(mod, "claude_dir"), (
            "claude_dir() must exist (renamed from repo_root)"
        )

    def test_repo_root_removed(self) -> None:
        mod = _load()
        assert not hasattr(mod, "repo_root"), "repo_root() was renamed to claude_dir()"

    def test_claude_dir_ends_in_dotclaude(self) -> None:
        mod = _load()
        result: Path = mod.claude_dir()
        assert isinstance(result, Path)
        assert result.name == ".claude", (
            f"claude_dir() should end in '.claude', got {result.name!r}"
        )

    def test_claude_dir_is_directory(self) -> None:
        mod = _load()
        assert mod.claude_dir().is_dir()


class TestReviewMonitorScriptPath:
    def test_env_override_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_script = tmp_path / "my_monitor.py"
        fake_script.touch()
        monkeypatch.setenv("GLOBAL_CLAUDE_REVIEW_MONITOR_SCRIPT", str(fake_script))
        mod = _load()
        assert mod.review_monitor_script_path() == fake_script

    def test_script_name_is_review_monitor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GLOBAL_CLAUDE_REVIEW_MONITOR_SCRIPT", raising=False)
        mod = _load()
        result: Path = mod.review_monitor_script_path()
        assert result.name == "review_monitor.py"

    def test_prefers_repo_script_when_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GLOBAL_CLAUDE_REVIEW_MONITOR_SCRIPT", raising=False)
        mod = _load()
        expected = mod.claude_dir() / "scripts" / "review_monitor.py"
        assert expected.exists(), (
            "Test precondition: review_monitor.py must exist in .claude/scripts/"
        )
        assert mod.review_monitor_script_path() == expected


class TestUtilsInit:
    def test_claude_dir_exported(self) -> None:
        content = (_RUNTIME_PATHS.parent / "__init__.py").read_text(encoding="utf-8")
        assert "claude_dir" in content, "__init__.py must export claude_dir"

    def test_repo_root_not_exported(self) -> None:
        content = (_RUNTIME_PATHS.parent / "__init__.py").read_text(encoding="utf-8")
        assert "repo_root" not in content, "__init__.py must not export repo_root"
