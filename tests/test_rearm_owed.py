"""Focused tests for #2993's durable auto-merge re-arm marker."""

from __future__ import annotations

from datetime import UTC, datetime

from cw.dev_queue import migrate_dev_queue
from cw.dev_queue.rearm_owed import clear_after_verified_finalize_rearm
from cw.models import TicketTask


def test_finalize_clears_only_after_verified_rearm() -> None:
    owed_at = datetime(2026, 11, 6, tzinfo=UTC)
    reported_at = datetime(2026, 11, 6, 0, 1, tzinfo=UTC)
    task = TicketTask(
        ticket_id="GEN-1",
        client="acme",
        auto_merge_rearm_owed_at=owed_at,
        auto_merge_rearm_attention_at=reported_at,
    )

    assert not clear_after_verified_finalize_rearm(
        task, was_finalizing=True, auto_merge_verified=False
    )
    assert task.auto_merge_rearm_owed_at == owed_at
    assert task.auto_merge_rearm_attention_at == reported_at

    assert clear_after_verified_finalize_rearm(
        task, was_finalizing=True, auto_merge_verified=True
    )
    assert task.auto_merge_rearm_owed_at is None
    assert task.auto_merge_rearm_attention_at is None


def test_non_finalize_arm_does_not_clear_marker() -> None:
    owed_at = datetime(2026, 11, 6, tzinfo=UTC)
    task = TicketTask(
        ticket_id="GEN-1", client="acme", auto_merge_rearm_owed_at=owed_at
    )

    assert not clear_after_verified_finalize_rearm(
        task, was_finalizing=False, auto_merge_verified=True
    )
    assert task.auto_merge_rearm_owed_at == owed_at


def test_v54_migration_fills_new_row_markers() -> None:
    migrated = migrate_dev_queue(
        {"schema_version": 53, "tasks": [{"ticket_id": "GEN-1", "client": "acme"}]}
    )

    assert migrated["schema_version"] == 61
    assert migrated["tasks"][0]["auto_merge_rearm_pr_url"] is None
    assert migrated["tasks"][0]["auto_merge_rearm_pr_state"] is None
    assert migrated["tasks"][0]["auto_merge_rearm_owed_at"] is None
    assert migrated["tasks"][0]["auto_merge_rearm_attention_at"] is None
