"""Tests for cw.plan_files — plan ``## Files Modified`` parsing (#1905).

Mirrors test_check_plan_scope_conformance.py's parsing-test shape: both
exercise the same ``## Files Modified`` contract, one via the standalone
.claude/scripts mirror, the other via the in-package src/cw.plan_files copy
that local_runner/executor consume. The shared fixture builder ``_plan_text``
lives in tests/conftest.py so the two suites cannot drift apart.
"""

from __future__ import annotations

from cw.plan_files import parse_plan_files_modified
from tests.conftest import _plan_text


def test_parses_bullet_paths_from_files_modified_section() -> None:
    """Bullets under the heading yield their path token, in document order."""
    plan = "## Files Modified\n- a/b.py (~10 lines)\n- c.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py", "c.py"]


def test_parses_table_rows_under_files_modified_heading() -> None:
    """Markdown-table rows yield their first cell's path token (#1779)."""
    plan = (
        "## Files Modified\n\n"
        "| File | Change |\n"
        "|---|---|\n"
        "| src/cw/thing.py | edit |\n"
        "| tests/test_thing.py | new |\n"
    )
    assert parse_plan_files_modified(plan) == [
        "src/cw/thing.py",
        "tests/test_thing.py",
    ]


def test_missing_heading_returns_empty_list() -> None:
    """No ``## Files`` heading → [] (not an error) — the fallback contract."""
    assert parse_plan_files_modified("# Plan\n\njust do the thing\n") == []


def test_ignores_prose_bullets_without_path_markers() -> None:
    """A prose bullet with no path marker is not counted as a file."""
    plan = "## Files Modified\n- Note that nothing else is touched\n- a/b.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_strips_backticks_and_bold_from_bullet_paths() -> None:
    """Backtick and bold decoration is stripped off the path token."""
    assert parse_plan_files_modified("## Files Modified\n- `a/b.py`\n") == ["a/b.py"]
    assert parse_plan_files_modified("## Files Modified\n- **a/b.py**\n") == ["a/b.py"]


def test_heading_prefix_match_ignores_exact_wording() -> None:
    """Heading is matched by prefix, so the real #1784 variant still parses."""
    plan = "## Files touched, with estimated line deltas\n- src/cw/x.py (~5)\n"
    assert parse_plan_files_modified(plan) == ["src/cw/x.py"]


def test_stops_at_next_heading() -> None:
    """Bullets under a later section are outside the files-modified body."""
    plan = "## Files Modified\n- a/b.py\n\n## Testing\n- tests/other.py\n"
    assert parse_plan_files_modified(plan) == ["a/b.py"]


def test_deduplicates_paths_preserving_first_occurrence_order() -> None:
    """Repeated paths collapse to the first occurrence, order preserved."""
    plan = "## Files Modified\n- b.py\n- a/b.py\n- b.py\n"
    assert parse_plan_files_modified(plan) == ["b.py", "a/b.py"]


def test_parses_shared_conftest_plan_fixture() -> None:
    """The shared ``_plan_text`` builder round-trips through the parser.

    Pins that src/cw.plan_files agrees with the .claude/scripts mirror on the
    exact fixture shape test_check_plan_scope_conformance.py asserts against.
    """
    paths = ["src/cw/one.py", "tests/test_one.py"]
    assert parse_plan_files_modified(_plan_text(paths)) == paths
