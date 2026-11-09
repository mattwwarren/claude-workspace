"""The fixture-path screen that keeps leaked test deferrals unfiled (#3113)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.review_debt_filing import (
    FIXTURE_ENTRY_PATHS,
    DebtEntry,
    is_fixture_entry,
    split_fixture_entries,
)

_ROOT = Path(__file__).resolve().parents[1]


def _entry(file: str, *, ticket: str = "", summary: str = "Bug here") -> DebtEntry:
    return DebtEntry(
        severity="MUST_FIX",
        file=file,
        summary=summary,
        rationale="r",
        ticket=ticket,
    )


@pytest.mark.parametrize(
    "file", ["src/cw/foo.py", "./src/cw/foo.py", "  src/cw/foo.py\n", "src//cw/foo.py"]
)
def test_a_fixture_path_is_matched(file: str) -> None:
    assert is_fixture_entry(_entry(file))


@pytest.mark.parametrize("file", ["src/thing.py", "src/cw/foo.pyx", "", "foo.py"])
def test_a_real_path_is_not_matched(file: str) -> None:
    assert not is_fixture_entry(_entry(file))


def test_a_ticketed_fixture_entry_is_not_matched() -> None:
    assert not is_fixture_entry(_entry("src/cw/foo.py", ticket="#9"))


def test_split_preserves_order() -> None:
    entries = [
        _entry("src/a.py"),
        _entry("src/cw/foo.py", summary="one"),
        _entry("src/b.py"),
        _entry("./src/cw/foo.py", summary="two"),
    ]
    fileable, refused = split_fixture_entries(entries)
    assert [e.file for e in fileable] == ["src/a.py", "src/b.py"]
    assert [e.summary for e in refused] == ["one", "two"]


def test_no_denylisted_path_exists_in_this_repo() -> None:
    """The denylist can never refuse a real file of this checkout."""
    assert FIXTURE_ENTRY_PATHS
    for path in FIXTURE_ENTRY_PATHS:
        assert not (_ROOT / path).exists()
