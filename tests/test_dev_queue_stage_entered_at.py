"""TicketTask.stage_entered_at: stamped on every stage change (GitHub #2155).

The stamp is written only through ``cw.dev_queue.stage_entry.enter_stage``.
It is set at initial enqueue and on every real stage move (advance, regress,
salvage to FINALIZE). A same-stage requeue leaves it unchanged. Pre-v60 rows
load with it ``None`` and no migration filler stamps them on load.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.dev_queue.crud import build_queued_task, load_dev_queue
from cw.dev_queue.lifecycle import (
    _advance_stage,
    _advance_task_pointer,
    _stage_regress,
    _stamp_salvage_stage,
)
from cw.dev_queue.stage_entry import enter_stage
from cw.models import DevQueueStore, Stage, TicketTask
from cw.models.tasks.ticket_task import QueueItemStatus
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_STAGES = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
_T0 = "2026-11-12T09:30:00Z"
_T1 = "2026-11-12T10:45:00Z"
_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class TestEnterStage:
    def test_sets_stage_and_stamp_together(self) -> None:
        task = _make_ticket_task()
        with freeze_time(_T0):
            enter_stage(task, Stage.REVIEW)
        assert task.stage == Stage.REVIEW
        assert task.stage_entered_at == _T0

    def test_explicit_entered_at_wins_over_clock(self) -> None:
        task = _make_ticket_task()
        with freeze_time(_T1):
            enter_stage(task, Stage.IMPL, entered_at=_T0)
        assert task.stage_entered_at == _T0

    def test_stamp_is_whole_second_z_format(self) -> None:
        task = _make_ticket_task()
        with freeze_time("2026-11-12T09:30:00.654321Z"):
            enter_stage(task, Stage.PLAN)
        assert task.stage_entered_at is not None
        assert _STAMP_RE.match(task.stage_entered_at)
        assert task.stage_entered_at == _T0


class TestInitialEnqueue:
    def test_build_queued_task_default_stage_stamped(self) -> None:
        with freeze_time(_T0):
            task = build_queued_task("GEN-1", "genhealth", lane="default")
        assert task.stage == Stage.PLAN
        assert task.stage_entered_at == _T0

    def test_build_queued_task_explicit_stage_stamped(self) -> None:
        with freeze_time(_T0):
            task = build_queued_task(
                "GEN-2", "genhealth", lane="default", stage=Stage.IMPL
            )
        assert task.stage == Stage.IMPL
        assert task.stage_entered_at == _T0

    def test_add_ticket_roundtrip_keeps_stamp(self) -> None:
        from cw.dev_queue import add_ticket

        with freeze_time(_T0):
            task = build_queued_task("GEN-3", "genhealth", lane="default")
            add_ticket(task)
        loaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-3")
        assert loaded.stage_entered_at == _T0

    def test_stamp_never_changes_on_load(self, tmp_config_dir: Path) -> None:
        from cw.dev_queue.storage import save_dev_queue

        row = _make_ticket_task(ticket_id="LEG-1", client="genhealth")
        assert row.stage_entered_at is None
        save_dev_queue(DevQueueStore(tasks=[row]))
        with freeze_time(_T1):
            loaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "LEG-1")
        assert loaded.stage_entered_at is None


class TestAdvance:
    def test_advance_task_pointer_restamps(self) -> None:
        task = _make_ticket_task(stage=Stage.PLAN, status=QueueItemStatus.RUNNING)
        with freeze_time(_T0):
            enter_stage(task, Stage.PLAN)
        with freeze_time(_T1):
            _advance_task_pointer(task, _STAGES)
        assert task.stage == Stage.IMPL
        assert task.stage_entered_at == _T1

    def test_advance_stage_forward_restamps(self) -> None:
        task = _make_ticket_task(stage=Stage.PLAN)
        with freeze_time(_T0):
            enter_stage(task, Stage.PLAN)
        with freeze_time(_T1):
            _advance_stage(task, _STAGES, Stage.REVIEW)
        assert task.stage == Stage.REVIEW
        assert task.stage_entered_at == _T1

    def test_advance_stage_same_stage_leaves_stamp(self) -> None:
        task = _make_ticket_task(stage=Stage.FINALIZE)
        with freeze_time(_T0):
            enter_stage(task, Stage.FINALIZE)
        with freeze_time(_T1):
            _advance_stage(task, _STAGES, Stage.FINALIZE)
        assert task.stage == Stage.FINALIZE
        assert task.stage_entered_at == _T0


class TestRegress:
    def test_stage_regress_restamps(self) -> None:
        task = _make_ticket_task(stage=Stage.REVIEW)
        with freeze_time(_T0):
            enter_stage(task, Stage.REVIEW)
        with freeze_time(_T1):
            _stage_regress(task, Stage.IMPL)
        assert task.stage == Stage.IMPL
        assert task.stage_entered_at == _T1

    def test_red_pr_recovery_same_stage_regress_leaves_stamp_unchanged(self) -> None:
        task = _make_ticket_task(stage=Stage.IMPL, status=QueueItemStatus.COMPLETED)
        with freeze_time(_T0):
            enter_stage(task, Stage.IMPL)
        with freeze_time(_T1):
            _stage_regress(task, Stage.IMPL)
        assert task.stage == Stage.IMPL
        assert task.stage_entered_at == _T0
        assert task.regress_attempts == 1


class TestSalvage:
    def test_salvage_from_impl_stamps_finalize(self) -> None:
        task = _make_ticket_task(stage=Stage.IMPL)
        with freeze_time(_T0):
            enter_stage(task, Stage.IMPL)
        with freeze_time(_T1):
            _stamp_salvage_stage(task)
        assert task.stage == Stage.FINALIZE
        assert task.stage_entered_at == _T1

    def test_salvage_at_finalize_does_not_restamp(self) -> None:
        task = _make_ticket_task(stage=Stage.FINALIZE)
        with freeze_time(_T0):
            enter_stage(task, Stage.FINALIZE)
        with freeze_time(_T1):
            _stamp_salvage_stage(task)
        assert task.stage == Stage.FINALIZE
        assert task.stage_entered_at == _T0


class TestLegacyRow:
    def test_row_without_field_loads_none(self) -> None:
        payload = _make_ticket_task().model_dump(mode="json")
        payload.pop("stage_entered_at", None)
        assert TicketTask.model_validate(payload).stage_entered_at is None


class TestTasksJson:
    def _save_one(self, stamp: str | None) -> None:
        from cw.dev_queue.storage import save_dev_queue

        task = _make_ticket_task(ticket_id="JS-1", client="genhealth")
        task.stage_entered_at = stamp
        save_dev_queue(DevQueueStore(tasks=[task]))

    def test_tasks_json_carries_stage_entered_at(self, tmp_config_dir: Path) -> None:
        import json

        self._save_one(_T0)
        result = CliRunner().invoke(main, ["dev-queue", "tasks", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert rows[0]["stage_entered_at"] == _T0

    def test_tasks_json_null_for_legacy_row(self, tmp_config_dir: Path) -> None:
        import json

        self._save_one(None)
        result = CliRunner().invoke(main, ["dev-queue", "tasks", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert "stage_entered_at" in rows[0]
        assert rows[0]["stage_entered_at"] is None

    def test_human_tasks_table_unchanged(self, tmp_config_dir: Path) -> None:
        self._save_one(_T0)
        result = CliRunner().invoke(main, ["dev-queue", "tasks"])
        assert result.exit_code == 0, result.output
        assert "stage_entered_at" not in result.output


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.REVIEW])
def test_enter_stage_accepts_any_stage(stage: Stage) -> None:
    task = _make_ticket_task()
    with freeze_time(_T0):
        enter_stage(task, stage)
    assert task.stage == stage
    assert task.stage_entered_at == _T0
