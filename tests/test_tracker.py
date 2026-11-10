"""Tests for src/cw/tracker.py — resolve_tracker and tracker_kind unit tests."""

from __future__ import annotations

from pathlib import Path

from cw.tracker import (
    PROJECT_CONFIG_RELPATH,
    TRACKER_GITHUB_ISSUES,
    TRACKER_LINEAR,
    TrackerClass,
    TrackerKind,
    resolve_tracker,
    tracker_kind,
)


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


class TestTrackerKind:
    def test_github_issues_is_github(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: github-issues\n")
        assert tracker_kind(tmp_path) == TrackerKind(
            TrackerClass.GITHUB, TRACKER_GITHUB_ISSUES
        )

    def test_linear_is_other(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: linear\n")
        assert tracker_kind(tmp_path) == TrackerKind(TrackerClass.OTHER, TRACKER_LINEAR)

    def test_missing_file_is_unset(self, tmp_path: Path) -> None:
        assert tracker_kind(tmp_path) == TrackerKind(TrackerClass.UNSET, None)

    def test_missing_tracking_key_is_unset(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "other: value\n")
        assert tracker_kind(tmp_path).tracker_class is TrackerClass.UNSET

    def test_non_string_system_is_unset(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "tracking:\n  primary:\n    system: 42\n")
        assert tracker_kind(tmp_path).tracker_class is TrackerClass.UNSET

    def test_empty_string_system_is_unset(self, tmp_path: Path) -> None:
        _write_config(tmp_path, 'tracking:\n  primary:\n    system: ""\n')
        assert tracker_kind(tmp_path) == TrackerKind(TrackerClass.UNSET, None)

    def test_linear_constant_reexported_from_debt_filing(self) -> None:
        from cw.review_debt_filing import TRACKER_LINEAR as DEBT_LINEAR

        assert DEBT_LINEAR is TRACKER_LINEAR
        assert TRACKER_LINEAR == "linear"


class TestProjectConfigRelpath:
    def test_relpath_matches_expected(self) -> None:
        assert Path(".claude") / "project-config.yaml" == PROJECT_CONFIG_RELPATH
