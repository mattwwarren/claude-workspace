"""Library tests for ``cw.dev_queue.fix_handoff`` (#2651)."""

from __future__ import annotations

from contextlib import AbstractContextManager

import pytest

from cw.dev_queue import fix_handoff
from cw.dev_queue.storage import _lock as _real_lock
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError
from cw.models import QueueItemStatus, TicketTask
from tests._ticket_view_helpers import make_task, seed_queue


def _row() -> TicketTask:
    return load_dev_queue().tasks[0]


def test_running_row_gets_handoff_and_returns_own_session() -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    sid = fix_handoff.record_pending_fix_dispatch(
        "ACM-1", "acme", prompt="fix it", cycle=2, requested_by_session_id="sess-1"
    )
    assert sid == "sess-1"
    pending = _row().pending_fix_dispatch
    assert pending is not None
    assert pending.prompt == "fix it"
    assert pending.label == "fix-ACM-1"
    assert pending.cycle == 2
    assert pending.requested_by_session_id == "sess-1"
    assert pending.requested_at.tzinfo is not None


def test_unstamped_row_returns_none() -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])
    sid = fix_handoff.record_pending_fix_dispatch(
        "ACM-1", "acme", prompt="p", cycle=1, requested_by_session_id="sess-1"
    )
    assert sid is None


@pytest.mark.parametrize(
    "status",
    [
        QueueItemStatus.PENDING,
        QueueItemStatus.COMPLETED,
        QueueItemStatus.BLOCKED_ON_USER,
    ],
)
def test_non_running_row_is_refused_and_queue_unchanged(
    status: QueueItemStatus,
) -> None:
    seed_queue([make_task(status=status, session_id="sess-1")])
    before = load_dev_queue().model_dump_json()
    with pytest.raises(CwError, match="not running"):
        fix_handoff.record_pending_fix_dispatch(
            "ACM-1", "acme", prompt="p", cycle=1, requested_by_session_id="sess-1"
        )
    assert load_dev_queue().model_dump_json() == before


def test_no_matching_row_raises() -> None:
    seed_queue([])
    with pytest.raises(CwError):
        fix_handoff.record_pending_fix_dispatch(
            "ACM-1", "acme", prompt="p", cycle=1, requested_by_session_id="sess-1"
        )


def test_existing_handoff_is_overwritten() -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    for prompt in ("first", "second"):
        fix_handoff.record_pending_fix_dispatch(
            "ACM-1", "acme", prompt=prompt, cycle=1, requested_by_session_id="sess-1"
        )
    pending = _row().pending_fix_dispatch
    assert pending is not None
    assert pending.prompt == "second"


def test_lock_bounded_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real_lock = _real_lock

    def spy(*, bounded: bool = False) -> AbstractContextManager[None]:
        seen.append(bounded)
        return real_lock(bounded=bounded)

    monkeypatch.setattr("cw.dev_queue.fix_handoff._lock", spy)
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    fix_handoff.record_pending_fix_dispatch(
        "ACM-1", "acme", prompt="p", cycle=1, requested_by_session_id="sess-1"
    )
    fix_handoff.record_pending_fix_dispatch(
        "ACM-1",
        "acme",
        prompt="p",
        cycle=1,
        requested_by_session_id="sess-1",
        bounded=True,
    )
    assert seen == [False, True]


def test_writer_stamps_the_row_the_fix_dispatch_reader_resolves() -> None:
    """#3313: with duplicate rows, writer and reader agree on the row."""
    from datetime import UTC, datetime

    from cw.reconcile import fix_dispatch

    seed_queue(
        [
            make_task(
                status=QueueItemStatus.COMPLETED,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            make_task(
                status=QueueItemStatus.RUNNING,
                session_id="sess-2",
                created_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
        ]
    )

    fix_handoff.record_pending_fix_dispatch(
        "ACM-1", "acme", prompt="p", cycle=1, requested_by_session_id="sess-2"
    )

    store = load_dev_queue()
    resolved = fix_dispatch._find_task(store, "ACM-1", "acme")
    assert resolved is store.tasks[1]
    assert resolved.pending_fix_dispatch is not None
    assert store.tasks[0].pending_fix_dispatch is None
