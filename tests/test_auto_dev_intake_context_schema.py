"""Pins the widened .cw/context.json comments schema (#1794): each comment is
now {author, created_at, body}, not a bare body string.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / ".claude" / "commands"


# NOTE: another local copy of the `_cmd(name)` helper carried by ~10 other test
# files. Consolidation into conftest.py is deferred — tracked as #1787.
def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text(encoding="utf-8")


def test_comments_schema_carries_created_at() -> None:
    content = _cmd("auto-dev-intake.md")
    assert '"created_at"' in content
    assert '"author"' in content


def test_comments_schema_maps_tracker_created_at_field() -> None:
    content = _cmd("auto-dev-intake.md")
    assert "createdAt" in content  # gh's field name, mapped into created_at


def test_comments_schema_no_longer_bare_string_array() -> None:
    """The old `["<comment 1>", ...]` literal must be gone, or a producer
    copying the heredoc verbatim would keep writing the timestamp-less shape."""
    content = _cmd("auto-dev-intake.md")
    assert '"comments": ["<comment 1>"' not in content
