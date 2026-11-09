"""Tests for the shared ledger-write race protocol (#2834)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.dev_queue.ledger_race import (
    ROW_STARTED_RUNNING_WARNING,
    require_row_not_running,
    row_started_running,
)
from cw.exceptions import CwError
from cw.models import DevQueueStore, QueueItemStatus
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path


def test_warning_constant_is_the_operator_settle_contract() -> None:
    assert ROW_STARTED_RUNNING_WARNING == "row_started_running"


def test_require_row_not_running_refuses_running_with_message() -> None:
    row = _make_ticket_task(status=QueueItemStatus.RUNNING)
    with pytest.raises(CwError, match="owns the ledger"):
        require_row_not_running(row, "T-1 owns the ledger")


@pytest.mark.parametrize(
    "status",
    [QueueItemStatus.BLOCKED_ON_USER, QueueItemStatus.PENDING],
)
def test_require_row_not_running_allows_parked_rows(status: QueueItemStatus) -> None:
    require_row_not_running(_make_ticket_task(status=status), "unused")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(QueueItemStatus.RUNNING, True), (QueueItemStatus.BLOCKED_ON_USER, False)],
)
def test_row_started_running_reads_the_queue_from_disk(
    tmp_config_dir: Path,
    tmp_path: Path,
    status: QueueItemStatus,
    expected: bool,
) -> None:
    from cw.dev_queue import save_dev_queue

    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(ticket_id="T-9", client="genhealth", status=status)
            ]
        )
    )
    assert row_started_running("T-9", "genhealth") is expected
