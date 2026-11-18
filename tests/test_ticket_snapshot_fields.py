"""TicketTask snapshot fields, dev-queue v63, new event members (RFC 0019 T1)."""

from __future__ import annotations

from datetime import UTC, datetime

from cw.dev_queue.storage import load_dev_queue, save_dev_queue
from cw.models import (
    DEV_QUEUE_SCHEMA_VERSION,
    DevQueueStore,
    OrchestratorEventType,
    TicketTask,
)
from cw.models.orchestrator_config.operator_forward import _DEFAULT_OPERATOR_EVENT_TYPES
from tests._ticket_view_helpers import make_task


def test_snapshot_fields_default_unset() -> None:
    task = make_task()
    assert task.snapshot_digest is None
    assert task.snapshot_fetched_at is None


def test_snapshot_fields_round_trip_utc() -> None:
    fetched = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    save_dev_queue(
        DevQueueStore(
            tasks=[make_task(snapshot_digest="e" * 64, snapshot_fetched_at=fetched)]
        )
    )
    loaded = load_dev_queue().tasks[0]
    assert loaded.snapshot_digest == "e" * 64
    assert loaded.snapshot_fetched_at == fetched


def test_pre_v63_blob_loads_with_snapshot_fields_unset() -> None:
    blob = make_task().model_dump(mode="json")
    blob.pop("snapshot_digest", None)
    blob.pop("snapshot_fetched_at", None)
    task = TicketTask.model_validate(blob)
    assert task.snapshot_digest is None


def test_schema_version_is_63() -> None:
    assert DEV_QUEUE_SCHEMA_VERSION == 63


def test_snapshot_fields_sit_after_review_engine_settlements() -> None:
    names = list(TicketTask.model_fields)
    assert (
        names.index("snapshot_digest") == names.index("review_engine_settlements") + 1
    )
    assert names.index("snapshot_fetched_at") == names.index("snapshot_digest") + 1


def test_ticket_snapshot_events_round_trip_and_are_not_operator_defaults() -> None:
    for member, value in (
        ("TICKET_SNAPSHOT_RECORDED", "ticket.snapshot_recorded"),
        ("TICKET_SNAPSHOT_RENDER_FAILED", "ticket.snapshot_render_failed"),
    ):
        etype = OrchestratorEventType[member]
        assert etype.value == value
        assert etype not in _DEFAULT_OPERATOR_EVENT_TYPES
