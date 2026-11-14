"""Tests for :mod:`cw.models.review_engine_settlement` and the v62 row field (#3625)."""

from __future__ import annotations

from datetime import UTC, datetime

from cw.models import DEV_QUEUE_SCHEMA_VERSION, TicketTask
from cw.models.review_engine_settlement import ReviewEngineSettlement
from tests._shared.session_seeding import _make_ticket_task

_HEAD = "a" * 40
_WHEN = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_record_defaults_and_lenient_load() -> None:
    record = ReviewEngineSettlement.model_validate(
        {
            "head_sha": _HEAD,
            "fingerprints": ["fp-1"],
            "recorded_at": _WHEN.isoformat(),
            "unknown_future_key": 1,
        }
    )
    assert record.head_sha == _HEAD
    assert record.fingerprints == ["fp-1"]
    assert record.recorded_at == _WHEN


def test_row_field_round_trips() -> None:
    record = ReviewEngineSettlement(
        head_sha=_HEAD, fingerprints=["fp-1", "fp-2"], recorded_at=_WHEN
    )
    task = _make_ticket_task(ticket_id="3625", review_engine_settlements=[record])
    loaded = TicketTask.model_validate_json(task.model_dump_json())
    assert loaded.review_engine_settlements == [record]


def test_pre_v62_row_loads_with_empty_list() -> None:
    dumped = _make_ticket_task(ticket_id="3625").model_dump(mode="json")
    dumped.pop("review_engine_settlements")
    assert TicketTask.model_validate(dumped).review_engine_settlements == []


def test_field_sits_directly_after_review_debt() -> None:
    names = list(TicketTask.model_fields)
    assert names.index("review_engine_settlements") == names.index("review_debt") + 1


def test_schema_version_is_62() -> None:
    assert DEV_QUEUE_SCHEMA_VERSION == 62
