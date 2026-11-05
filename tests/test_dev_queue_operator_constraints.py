"""``record_operator_constraint``: the library half of ``cw review constrain`` (#2964).

Covers the append-only ``operator_constraints`` list on a row, the duplicate
rule (an identical text of ANY earlier entry records neither event nor save),
the per-entry and total byte caps, event-first ordering, the unresolved-actor
literal, and that no requeue or regress clears the list.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from typing import TYPE_CHECKING

import pytest

from cw.config import dev_queue_file
from cw.dev_queue import load_dev_queue, requeue_ticket
from cw.dev_queue import operator_constraints as module
from cw.dev_queue.operator_constraints import (
    MAX_CONSTRAINT_BYTES,
    MAX_TOTAL_CONSTRAINT_BYTES,
    UNRESOLVED_ACTOR,
    record_operator_constraint,
)
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    events_of,
    install_acme_client,
    make_blocked,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import OrchestratorEvent, TicketTask
    from cw.native_daemon import FakeNativeDaemonClient

_LOGIN = "octo-operator"
_TEXT = "Do not add `foo_bar`; the row is the source of truth."
_EVENT = OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED


@pytest.fixture(autouse=True)
def acme_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    return install_acme_client(tmp_path, monkeypatch)


def _seed(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.REVIEW, "lane": "fast"}
    kwargs.update(overrides)
    task = make_task(**kwargs)
    seed_queue([task])
    return task


def _seed_blocked(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.REVIEW, "lane": "fast"}
    kwargs.update(overrides)
    task = make_blocked(**kwargs)
    seed_queue([task], [bound_session_for(task)])
    return task


def _record(
    text: str = _TEXT, *, actor: str | None = _LOGIN
) -> module.OperatorConstraintRecord:
    return record_operator_constraint(ACME_TICKET, ACME, text, actor=actor)


def _events() -> list[OrchestratorEvent]:
    return events_of(_EVENT)


def _assert_nothing_recorded(queue_bytes: bytes) -> None:
    assert dev_queue_file().read_bytes() == queue_bytes
    assert _events() == []


class TestAppend:
    def test_first_record_is_round_one_and_stripped(self) -> None:
        _seed()
        result = _record(f"  {_TEXT}\n\n")
        assert result["round"] == 1
        assert result["duplicate"] is False
        assert result["actor"] == _LOGIN
        assert result["actor_resolved"] is True
        assert result["row_status"] is QueueItemStatus.PENDING
        assert result["total_bytes"] == len(_TEXT.encode("utf-8"))
        (entry,) = queue_row().operator_constraints
        assert entry == result["entry"]
        assert entry.text == _TEXT
        assert entry.round == 1
        assert entry.actor == _LOGIN
        assert entry.recorded_at.tzinfo is not None

    def test_entries_append_in_round_order(self) -> None:
        _seed()
        _record("No new lock files.")
        second = _record(_TEXT)
        assert second["round"] == 2
        assert second["total_bytes"] == len(b"No new lock files.") + len(
            _TEXT.encode("utf-8")
        )
        assert [(e.round, e.text) for e in queue_row().operator_constraints] == [
            (1, "No new lock files."),
            (2, _TEXT),
        ]
        assert [e.payload["round"] for e in _events()] == [1, 2]

    def test_duplicate_of_any_earlier_entry_records_nothing(self) -> None:
        _seed()
        _record(_TEXT)
        _record("No new lock files.")
        queue_bytes = dev_queue_file().read_bytes()
        events_before = len(_events())
        result = _record(f"\n{_TEXT}  ")
        assert result["duplicate"] is True
        assert result["round"] == 1
        assert result["entry"].text == _TEXT
        assert result["entry"].round == 1
        assert dev_queue_file().read_bytes() == queue_bytes
        assert len(_events()) == events_before
        assert len(queue_row().operator_constraints) == 2


class TestRefusals:
    def test_blank_text_is_refused(self) -> None:
        _seed()
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(CwError) as exc:
            _record(" \n\t ")
        assert str(exc.value) == (
            f"Cannot record a constraint for ticket '{ACME_TICKET}': the"
            " constraint text is blank. Nothing was recorded."
        )
        _assert_nothing_recorded(queue_bytes)

    def test_exactly_the_per_entry_cap_is_accepted(self) -> None:
        _seed()
        result = _record("a" * MAX_CONSTRAINT_BYTES)
        assert result["total_bytes"] == MAX_CONSTRAINT_BYTES

    def test_one_byte_over_the_per_entry_cap_is_refused(self) -> None:
        _seed()
        queue_bytes = dev_queue_file().read_bytes()
        size = MAX_CONSTRAINT_BYTES + 1
        with pytest.raises(CwError) as exc:
            _record("a" * size)
        assert str(exc.value) == (
            f"Cannot record a constraint for ticket '{ACME_TICKET}': the text is"
            f" {size} UTF-8 bytes, over the {MAX_CONSTRAINT_BYTES}-byte per-entry"
            " cap. Nothing was recorded. Constraints are append-only and cannot"
            " be edited or removed (retraction is tracked in #2968), so state"
            " only what is binding: the forbidden mechanisms and why, not the"
            " surrounding discussion."
        )
        _assert_nothing_recorded(queue_bytes)

    def test_multibyte_text_is_measured_in_utf8_bytes(self) -> None:
        _seed()
        # "é" is two UTF-8 bytes: half the cap in characters fills it exactly.
        _record("é" * (MAX_CONSTRAINT_BYTES // 2))
        with pytest.raises(CwError, match="per-entry cap"):
            _record("é" * (MAX_CONSTRAINT_BYTES // 2) + "x")

    def test_total_cap_is_refused(self) -> None:
        _seed()
        entries = MAX_TOTAL_CONSTRAINT_BYTES // MAX_CONSTRAINT_BYTES
        for index in range(entries):
            _record(str(index) * MAX_CONSTRAINT_BYTES)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(CwError) as exc:
            _record("z")
        assert str(exc.value) == (
            f"Cannot record a constraint for ticket '{ACME_TICKET}': the row"
            f" already holds {MAX_TOTAL_CONSTRAINT_BYTES} UTF-8 bytes of"
            " constraints and this entry adds 1, over the"
            f" {MAX_TOTAL_CONSTRAINT_BYTES}-byte total cap. Nothing was recorded."
            " Constraints are append-only and cannot be consolidated or removed"
            " (retraction is tracked in #2968); shorten this entry to what is"
            " binding."
        )
        assert dev_queue_file().read_bytes() == queue_bytes
        assert len(_events()) == entries

    def test_no_row_is_refused(self) -> None:
        with pytest.raises(CwError, match="No dev-queue task found"):
            _record()
        assert _events() == []


class TestEventFirst:
    def test_event_failure_leaves_the_row_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        queue_bytes = dev_queue_file().read_bytes()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "inbox full"
            raise OSError(msg)

        monkeypatch.setattr(module, "record_event", _boom)
        with pytest.raises(CwError) as exc:
            _record()
        assert str(exc.value) == (
            f"Cannot record a constraint for ticket '{ACME_TICKET}': recording the"
            " ticket.constraint_recorded audit event failed (OSError: inbox"
            " full). Nothing was recorded; fix the events inbox, then re-run"
            " constrain."
        )
        assert dev_queue_file().read_bytes() == queue_bytes

    def test_save_failure_logs_and_reraises_after_the_event(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr(module, "save_dev_queue", _boom)
        with (
            caplog.at_level(logging.ERROR, logger=module.__name__),
            pytest.raises(OSError, match="No space left"),
        ):
            _record()
        assert len(_events()) == 1
        assert queue_row().operator_constraints == []
        assert "operator constraint save failed" in caplog.text


class TestRowStatus:
    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.PENDING,
            QueueItemStatus.RUNNING,
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        ],
    )
    def test_result_carries_the_row_status(self, status: QueueItemStatus) -> None:
        _seed(status=status)
        result = _record()
        assert result["row_status"] is status
        assert _events()[0].payload["row_status"] == status.value


class TestActor:
    def test_unresolved_actor_is_stored_as_the_literal(self) -> None:
        _seed()
        result = _record(actor=None)
        assert result["actor"] == UNRESOLVED_ACTOR == "unresolved"
        assert result["actor_resolved"] is False
        assert queue_row().operator_constraints[0].actor == "unresolved"
        assert _events()[0].payload["actor_resolved"] is False

    def test_real_login_named_unresolved_is_still_resolved(self) -> None:
        _seed()
        result = _record(actor="unresolved")
        assert result["actor_resolved"] is True
        assert _events()[0].payload["actor_resolved"] is True

    def test_blank_actor_is_stored_as_unresolved(self) -> None:
        _seed()
        result = _record(actor=" \t ")
        assert result["actor"] == UNRESOLVED_ACTOR
        assert result["actor_resolved"] is False
        assert queue_row().operator_constraints[0].actor == UNRESOLVED_ACTOR


class TestAuditPayload:
    def test_payload_carries_digest_and_sizes_never_the_text(self) -> None:
        _seed()
        _record()
        (event,) = _events()
        encoded = _TEXT.encode("utf-8")
        assert event.correlation_id == ACME_TICKET
        assert event.payload == {
            "ticket_id": ACME_TICKET,
            "client": ACME,
            "round": 1,
            "actor": _LOGIN,
            "actor_resolved": True,
            "text_sha256": hashlib.sha256(encoded).hexdigest(),
            "text_bytes": len(encoded),
            "total_bytes": len(encoded),
            "row_status": "pending",
        }
        assert _TEXT not in repr(event.payload)


class TestSurvivesRequeue:
    def test_same_stage_requeue_keeps_the_list(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed_blocked()
        _record()
        before = queue_row().operator_constraints
        requeue_ticket(ACME_TICKET, ACME, native_daemon=acme_daemon)
        row = queue_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.operator_constraints == before

    def test_regress_to_impl_keeps_the_list(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed_blocked()
        _record()
        before = queue_row().operator_constraints
        requeue_ticket(
            ACME_TICKET, ACME, "impl", allow_regress=True, native_daemon=acme_daemon
        )
        row = queue_row()
        assert row.stage == Stage.IMPL
        assert row.operator_constraints == before

    def test_requeue_from_failed_keeps_the_list(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed_blocked(status=QueueItemStatus.FAILED)
        _record()
        before = queue_row().operator_constraints
        requeue_ticket(ACME_TICKET, ACME, from_failed=True, native_daemon=acme_daemon)
        row = queue_row()
        assert row.status == QueueItemStatus.PENDING
        assert row.operator_constraints == before
        assert len(load_dev_queue().tasks) == 1


def test_signature_takes_bounded() -> None:
    assert "bounded" in inspect.signature(record_operator_constraint).parameters
