"""Tests for .claude/scripts/utils/runtime_paths.py.

runtime_paths is a standalone utility module (not part of the cw package), so
it is loaded by path — same pattern as test_preflight.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass

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


class TestRepoRoot:
    def test_repo_root_exists(self) -> None:
        mod = _load()
        assert hasattr(mod, "repo_root"), "repo_root() must exist"

    def test_claude_dir_removed(self) -> None:
        mod = _load()
        assert not hasattr(mod, "claude_dir"), (
            "claude_dir() must not exist; repo_root() is the correct name"
        )

    def test_repo_root_contains_pyproject_toml(self) -> None:
        mod = _load()
        result: Path = mod.repo_root()
        assert isinstance(result, Path)
        assert (result / "pyproject.toml").exists(), (
            f"repo_root() must return a dir with pyproject.toml, got {result}"
        )

    def test_repo_root_is_directory(self) -> None:
        mod = _load()
        assert mod.repo_root().is_dir()

    def test_repo_root_raises_when_no_pyproject(self, tmp_path: Path) -> None:
        mod = _load()
        # Patch module __file__ to a path with no pyproject.toml ancestor
        mod.__file__ = str(tmp_path / "fake_scripts" / "utils" / "runtime_paths.py")
        with pytest.raises(RuntimeError, match=r"pyproject\.toml"):
            mod.repo_root()


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
        expected = mod.repo_root() / ".claude" / "scripts" / "review_monitor.py"
        assert expected.exists(), (
            "Test precondition: review_monitor.py must exist in .claude/scripts/"
        )
        assert mod.review_monitor_script_path() == expected


class TestUtilsInit:
    def test_repo_root_exported(self) -> None:
        content = (_RUNTIME_PATHS.parent / "__init__.py").read_text(encoding="utf-8")
        assert "repo_root" in content, "__init__.py must export repo_root"

    def test_claude_dir_not_exported(self) -> None:
        content = (_RUNTIME_PATHS.parent / "__init__.py").read_text(encoding="utf-8")
        assert "claude_dir" not in content, "__init__.py must not export claude_dir"
