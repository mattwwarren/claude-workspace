"""Pins the widened .cw/context.json comments schema (#1794): each comment is
now {author, created_at, body}, not a bare body string.
"""

from tests.conftest import _cmd


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
