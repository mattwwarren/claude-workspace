"""Tests for the review-debt tracker mode and ``auto_create_issues`` (#3041, R9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.review_debt_filing import TrackerMode, auto_create_issues, tracker_mode
from cw.sprint import GhClient
from tests._shared.cli_preflight import _write_project_config_yaml
from tests._tool_fakes import FakeGh

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_debt_filing import DebtTracker


@pytest.mark.parametrize(
    ("system", "mode"),
    [
        ("github-issues", TrackerMode.GITHUB),
        ("linear", TrackerMode.MCP),
        (None, TrackerMode.SKIP_NO_TRACKER),
        ("notion", TrackerMode.SKIP_NO_TRACKER),
        ("GitHub-Issues", TrackerMode.SKIP_NO_TRACKER),
    ],
)
def test_tracker_mode_is_strict(system: str | None, mode: TrackerMode) -> None:
    assert tracker_mode(system) is mode


def _config(root: Path, value: str | None) -> None:
    line = "" if value is None else f"\n    auto_create_issues: {value}"
    _write_project_config_yaml(
        root, f"tracking:\n  primary:\n    system: github-issues{line}\n"
    )


def test_a_literal_true_files(tmp_path: Path) -> None:
    _config(tmp_path, "true")
    assert auto_create_issues(tmp_path) is True


@pytest.mark.parametrize("value", ["false", None, '"true"', "1", "yes-please"])
def test_anything_else_is_emit_only(tmp_path: Path, value: str | None) -> None:
    _config(tmp_path, value)
    assert auto_create_issues(tmp_path) is False


def test_a_missing_config_is_emit_only(tmp_path: Path) -> None:
    assert auto_create_issues(tmp_path) is False


def test_the_key_is_read_from_tracking_primary(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "tracking:\n  auto_create_issues: true\n")
    assert auto_create_issues(tmp_path) is False


def test_unreadable_yaml_is_emit_only(tmp_path: Path) -> None:
    _write_project_config_yaml(tmp_path, "tracking: [unclosed\n")
    assert auto_create_issues(tmp_path) is False


def test_both_gh_surfaces_satisfy_the_debt_tracker() -> None:
    trackers: list[DebtTracker] = [GhClient(), FakeGh()]
    assert len(trackers) == len({type(t) for t in trackers})
