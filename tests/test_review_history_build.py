"""Tests for ``cw.review_history.build``: the documented view (#2790, #3792)."""

from __future__ import annotations

from pathlib import Path

from cw.models import review_history as review_history_models
from cw.models.review_history import REVIEW_HISTORY_SCHEMA_VERSION, ReviewHistory
from tests._review_history_helpers import (
    ROUND_STAGE_SECONDS,
    STAGE_SECONDS_DOC,
    STAGE_SECONDS_GATE,
    STAGE_SECONDS_QUEUED,
    STAGE_SECONDS_REPAIRING,
    STAGE_SECONDS_REVIEWING,
    SUMMARY_AS_OF,
    SUMMARY_STAGE_SECONDS,
    WALL_SECONDS,
    history_for,
    seed_fix_cycle,
)

_RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "dispatch-runbook.md"


def _runbook_section() -> str:
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index("### Reading a review's history")
    return text[start : text.index("\n### ", start + 1)]


def test_runbook_documents_every_top_level_field() -> None:
    section = _runbook_section()

    for name in ReviewHistory.model_fields:
        assert f"`{name}`" in section, name


def test_runbook_documents_the_stage_split() -> None:
    section = _runbook_section()

    for name in (
        "stage_seconds",
        "queued",
        "reviewing",
        "repairing",
        "gate",
        "stage_seconds_as_of",
    ):
        assert f"`{name}`" in section, name


def test_the_schema_version_stays_one(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path)

    history = history_for(task, tmp_path, sessions=sessions)

    assert REVIEW_HISTORY_SCHEMA_VERSION == 1
    assert history.schema_version == 1


def test_events_is_a_source_when_only_transitions_were_used(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path, with_start=False)

    assert "events" in history_for(task, tmp_path, sessions=sessions).sources


def test_events_is_not_a_source_when_no_event_was_used(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path, steps=(), with_start=False)

    assert "events" not in history_for(task, tmp_path, sessions=sessions).sources


def test_every_public_description_is_the_documented_string() -> None:
    models = review_history_models
    block = models.StageSeconds

    assert block.__doc__ == STAGE_SECONDS_DOC
    assert block.model_fields["queued"].description == STAGE_SECONDS_QUEUED
    assert block.model_fields["reviewing"].description == STAGE_SECONDS_REVIEWING
    assert block.model_fields["repairing"].description == STAGE_SECONDS_REPAIRING
    assert block.model_fields["gate"].description == STAGE_SECONDS_GATE
    round_fields = models.RoundView.model_fields
    assert round_fields["wall_seconds"].description == WALL_SECONDS
    assert round_fields["stage_seconds"].description == ROUND_STAGE_SECONDS
    summary_fields = models.HistorySummary.model_fields
    assert summary_fields["stage_seconds"].description == SUMMARY_STAGE_SECONDS
    assert summary_fields["stage_seconds_as_of"].description == SUMMARY_AS_OF
