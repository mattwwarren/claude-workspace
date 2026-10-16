"""Tests for ``cw.dev_queue.review_records`` (#2701).

The dev-queue row is the record ``cw review settle`` writes onto on every
tracker. These pin the writer's contract: it lands on the row ``_find_ticket``
selects, merges through the provenance-gated newest-wins chokepoint, reports
what it did not apply, and never touches another client's rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import cw.config
from cw.dev_queue import (
    TERMINAL_ROW_STATUSES,
    find_ticket_row,
    load_dev_queue,
    record_finding_dispositions,
    save_dev_queue,
)
from cw.exceptions import CwError, LockTimeoutError
from cw.models import DevQueueStore, QueueItemStatus, TicketTask
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from tests.conftest import _hold_flock, _make_ticket_task

_CLIENT = "acme"
_TICKET = "T-2701"


def _entry(
    file: str = "src/cw/foo.py", summary: str = "Bug here", **overrides: object
) -> dict[str, FindingDisposition]:
    key = _disposition_key(file, summary)
    assert key is not None
    payload: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "intentional tradeoff",
        "recorded_at": "2026-10-01T00:00:00Z",
        "actor": "mattwwarren",
        "reviewed_sha": "abc1234",
        "summary": summary,
    }
    payload.update(overrides)
    return {key: FindingDisposition.model_validate(payload)}


def _seed(*tasks: TicketTask) -> None:
    save_dev_queue(DevQueueStore(tasks=list(tasks)))


def _row(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"ticket_id": _TICKET, "client": _CLIENT}
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def _stored(ticket_id: str = _TICKET, client: str = _CLIENT) -> list[TicketTask]:
    return [
        t
        for t in load_dev_queue().tasks
        if t.ticket_id == ticket_id and t.client == client
    ]


class TestRecordFindingDispositions:
    @pytest.mark.parametrize(
        ("statuses", "selected"),
        [
            (
                [QueueItemStatus.COMPLETED, QueueItemStatus.RUNNING],
                QueueItemStatus.RUNNING,
            ),
            (
                [QueueItemStatus.COMPLETED, QueueItemStatus.BLOCKED_ON_USER],
                QueueItemStatus.BLOCKED_ON_USER,
            ),
            (
                [QueueItemStatus.FAILED, QueueItemStatus.COMPLETED],
                QueueItemStatus.COMPLETED,
            ),
        ],
    )
    def test_lands_on_the_row_find_ticket_selects(
        self, statuses: list[QueueItemStatus], selected: QueueItemStatus
    ) -> None:
        base = datetime(2026, 10, 1, tzinfo=UTC)
        _seed(
            *(
                _row(status=status, created_at=base + timedelta(minutes=index))
                for index, status in enumerate(statuses)
            )
        )

        result = record_finding_dispositions(_TICKET, _CLIENT, _entry())

        assert result.row_status == selected
        by_status = {t.status: t for t in _stored()}
        assert by_status[selected].finding_dispositions == _entry()
        for status in statuses:
            if status != selected:
                assert by_status[status].finding_dispositions == {}

    def test_the_merge_is_additive(self) -> None:
        existing = _entry(file="src/cw/old.py", summary="Old bug")
        _seed(_row(finding_dispositions=existing))

        result = record_finding_dispositions(_TICKET, _CLIENT, _entry())

        assert result.not_applied == ()
        assert _stored()[0].finding_dispositions == {**existing, **_entry()}

    def test_an_older_record_does_not_evict_a_newer_one(self) -> None:
        newer = _entry(recorded_at="2026-10-05T00:00:00Z", rationale="newer")
        _seed(_row(finding_dispositions=newer))
        older = _entry(recorded_at="2026-10-01T00:00:00Z", rationale="older")

        result = record_finding_dispositions(_TICKET, _CLIENT, older)

        assert result.not_applied == tuple(older)
        assert _stored()[0].finding_dispositions == newer

    def test_a_reversal_replaces_a_rejection(self) -> None:
        _seed(_row(finding_dispositions=_entry()))
        reversal = _entry(outcome="REVERSED", recorded_at="2026-10-02T00:00:00Z")

        result = record_finding_dispositions(_TICKET, _CLIENT, reversal)

        assert result.not_applied == ()
        assert _stored()[0].finding_dispositions == reversal

    def test_an_entry_failing_provenance_is_not_written(self) -> None:
        _seed(_row())
        anonymous = _entry(actor="")

        result = record_finding_dispositions(_TICKET, _CLIENT, anonymous)

        assert result.not_applied == tuple(anonymous)
        assert _stored()[0].finding_dispositions == {}

    def test_no_row_raises_naming_the_enqueue_command(self) -> None:
        _seed()

        with pytest.raises(CwError, match="cw dev-queue add"):
            record_finding_dispositions(_TICKET, _CLIENT, _entry())

    def test_the_result_carries_the_row_status(self) -> None:
        _seed(_row(status=QueueItemStatus.CANCELLED))

        result = record_finding_dispositions(_TICKET, _CLIENT, _entry())

        assert result.row_status == QueueItemStatus.CANCELLED
        assert result.row_status in TERMINAL_ROW_STATUSES

    def test_bounded_acquisition_records(self) -> None:
        _seed(_row())

        record_finding_dispositions(_TICKET, _CLIENT, _entry(), bounded=True)

        assert _stored()[0].finding_dispositions == _entry()

    @pytest.mark.usefixtures("fake_lock_clock")
    def test_bounded_acquisition_times_out_and_writes_nothing(self) -> None:
        _seed(_row())

        with (
            _hold_flock(cw.config.dev_queue_lock()),
            pytest.raises(LockTimeoutError),
        ):
            record_finding_dispositions(_TICKET, _CLIENT, _entry(), bounded=True)

        assert _stored()[0].finding_dispositions == {}

    def test_the_saved_ledger_round_trips_through_load(self) -> None:
        _seed(_row())
        ledger = {**_entry(), **_entry(file="src/cw/bar.py", summary="Other bug")}

        record_finding_dispositions(_TICKET, _CLIENT, ledger)

        reloaded = _stored()[0].finding_dispositions
        assert reloaded == ledger
        assert all(isinstance(v, FindingDisposition) for v in reloaded.values())

    def test_another_clients_row_is_untouched(self) -> None:
        _seed(_row(), _row(client="other"))

        record_finding_dispositions(_TICKET, _CLIENT, _entry())

        assert _stored(client="other")[0].finding_dispositions == {}
        assert _stored()[0].finding_dispositions == _entry()


class TestFindTicketRow:
    def test_returns_the_selected_row(self) -> None:
        _seed(_row(status=QueueItemStatus.BLOCKED_ON_USER))

        assert find_ticket_row(_TICKET, _CLIENT).status == (
            QueueItemStatus.BLOCKED_ON_USER
        )

    def test_no_row_raises_the_standard_message(self) -> None:
        _seed(_row(client="other"))

        with pytest.raises(CwError, match="No dev-queue task found"):
            find_ticket_row(_TICKET, _CLIENT)


def test_terminal_row_statuses_exclude_the_parked_ones() -> None:
    """NOT lifecycle's terminal set: a parked row keeps its ledger on requeue."""
    assert frozenset(
        {
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        }
    ) == (TERMINAL_ROW_STATUSES)
