"""Tests for :mod:`cw.dev_queue.review_engine_settlements` (#3625)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw import _lock_guard
from cw.dev_queue import add_ticket
from cw.dev_queue.review_engine_settlements import (
    MAX_SETTLEMENT_HEADS,
    record_engine_settlement,
)
from cw.dev_queue.storage import load_dev_queue, save_dev_queue
from cw.exceptions import DEV_QUEUE_LOCK_NAME, CwError
from cw.models import QueueItemStatus, Stage
from tests._cli_review_helpers import _seed_review_row
from tests._shared.session_seeding import (
    _make_daemon_session,
    _make_ticket_task,
    _seed_sessions,
)

if TYPE_CHECKING:
    from cw.models import DevQueueStore

_TICKET = "3625"
_CLIENT = "acme"
_HEAD = "b" * 40
_WHEN = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _row_settlements() -> list[tuple[str, list[str]]]:
    rows = [t for t in load_dev_queue().tasks if t.ticket_id == _TICKET]
    return [(s.head_sha, s.fingerprints) for s in rows[0].review_engine_settlements]


def _head(index: int) -> str:
    return f"{index:040x}"


def test_record_lands_sorted_and_deduped_on_a_running_review_row() -> None:
    _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
    record_engine_settlement(
        _TICKET, _CLIENT, _HEAD, ["fp-2", "fp-1", "fp-2"], recorded_at=_WHEN
    )
    assert _row_settlements() == [(_HEAD, ["fp-1", "fp-2"])]


def test_same_head_write_replaces_the_earlier_record() -> None:
    _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
    record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_WHEN)
    record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-9"], recorded_at=_WHEN)
    assert _row_settlements() == [(_HEAD, ["fp-9"])]


def test_list_is_bounded_to_the_newest_heads() -> None:
    _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
    for index in range(MAX_SETTLEMENT_HEADS + 1):
        record_engine_settlement(
            _TICKET, _CLIENT, _head(index + 1), ["fp-1"], recorded_at=_WHEN
        )
    heads = [head for head, _ in _row_settlements()]
    assert len(heads) == MAX_SETTLEMENT_HEADS
    assert heads[0] == _head(2)
    assert heads[-1] == _head(MAX_SETTLEMENT_HEADS + 1)


def test_missing_row_raises_cwerror() -> None:
    with pytest.raises(CwError):
        record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_WHEN)


@pytest.mark.parametrize(
    ("stage", "status", "head"),
    [
        (Stage.IMPL, QueueItemStatus.RUNNING, _HEAD),
        (Stage.REVIEW, QueueItemStatus.PENDING, _HEAD),
        (Stage.REVIEW, QueueItemStatus.BLOCKED_ON_USER, _HEAD),
        (Stage.REVIEW, QueueItemStatus.RUNNING, "sha-0001"),
    ],
    ids=["other-stage", "pending", "blocked", "non-40-hex"],
)
def test_refusals_raise_and_leave_the_store_unchanged(
    stage: Stage, status: QueueItemStatus, head: str
) -> None:
    add_ticket(
        _make_ticket_task(ticket_id=_TICKET, client=_CLIENT, stage=stage, status=status)
    )
    before = load_dev_queue().model_dump_json()
    with pytest.raises(CwError):
        record_engine_settlement(_TICKET, _CLIENT, head, ["fp-1"], recorded_at=_WHEN)
    assert load_dev_queue().model_dump_json() == before


def test_write_takes_only_the_dev_queue_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
    seen: list[tuple[str, ...]] = []

    def spy_save(store: DevQueueStore) -> None:
        seen.append(tuple(lock.name for lock in _lock_guard.held_locks()))
        save_dev_queue(store)

    monkeypatch.setattr(
        "cw.dev_queue.review_engine_settlements.save_dev_queue", spy_save
    )
    record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_WHEN)
    assert seen == [(DEV_QUEUE_LOCK_NAME,)]


def test_a_failed_save_propagates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)

    def refuse(_store: DevQueueStore) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.review_engine_settlements.save_dev_queue", refuse)
    with pytest.raises(OSError, match="disk full"):
        record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_WHEN)


def test_record_succeeds_from_inside_a_dispatch_worker_on_a_running_review_row() -> (
    None
):
    # The writer's intended caller is `record-round` inside the review worker, so a
    # live daemon session bound to the row must not refuse the write.
    _seed_review_row(
        _TICKET, _CLIENT, status=QueueItemStatus.RUNNING, session_id="cw-worker"
    )
    _seed_sessions(
        _make_daemon_session(
            id="cw-worker",
            claude_session_id="claude-worker",
            status="active",
        )
    )
    record_engine_settlement(_TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_WHEN)
    assert _row_settlements() == [(_HEAD, ["fp-1"])]
