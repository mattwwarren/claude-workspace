"""Tests for src/cw/tracker.py — resolve_tracker unit tests."""

from __future__ import annotations

from pathlib import Path

from cw.tracker import PROJECT_CONFIG_RELPATH, resolve_tracker


def _write_config(root: Path, content: str) -> None:
    cfg_dir = root / ".claude"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "project-config.yaml").write_text(content, encoding="utf-8")


class TestResolveTracker:
    def test_github_issues_returns_string(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: github-issues\n")
        assert resolve_tracker(tmp_path) == "github-issues"

    def test_linear_returns_string(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: linear\n")
        assert resolve_tracker(tmp_path) == "linear"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert resolve_tracker(tmp_path) is None

    def test_malformed_yaml_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, ": invalid yaml {{{")
        assert resolve_tracker(tmp_path) is None

    def test_non_dict_root_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "just-a-string\n")
        assert resolve_tracker(tmp_path) is None

    def test_non_dict_tracking_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking: not-a-dict\n")
        assert resolve_tracker(tmp_path) is None

    def test_non_dict_primary_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary: not-a-dict\n")
        assert resolve_tracker(tmp_path) is None

    def test_missing_system_key_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    other: value\n")
        assert resolve_tracker(tmp_path) is None

    def test_non_string_system_returns_none(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: 42\n")
        assert resolve_tracker(tmp_path) is None


class TestProjectConfigRelpath:
    def test_relpath_matches_expected(self) -> None:
        assert Path(".claude") / "project-config.yaml" == PROJECT_CONFIG_RELPATH
