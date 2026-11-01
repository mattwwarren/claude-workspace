"""Tests for ``cw.review_history.build``: the documented view (#2790)."""

from __future__ import annotations

from pathlib import Path

from cw.models.review_history import ReviewHistory

_RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "dispatch-runbook.md"


def test_runbook_documents_every_top_level_field() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index("### Reading a review's history")
    section = text[start : text.index("\n### ", start + 1)]

    for name in ReviewHistory.model_fields:
        assert f"`{name}`" in section, name
