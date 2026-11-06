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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.config import dev_queue_file
from cw.dev_queue import load_dev_queue, requeue_ticket
from cw.dev_queue import operator_constraints as module
from cw.dev_queue.operator_constraints import (
    MAX_CONSTRAINT_BYTES,
    MAX_PLAN_CONSTRAINT_BYTES,
    MAX_TOTAL_CONSTRAINT_BYTES,
    UNRESOLVED_ACTOR,
    record_operator_constraint,
    record_plan_constraints,
)
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.models.operator_constraint import PLAN_ACTOR, OperatorConstraint
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
    from cw.models.operator_constraint import PlanConstraintKind
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
            "kind": "operator",
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


# --- #2984: plan-derived entries -------------------------------------------


def _seed_plan(**overrides: object) -> TicketTask:
    return _seed(stage=Stage.PLAN, **overrides)


def _plan(
    *items: tuple[PlanConstraintKind, str],
) -> module.PlanRecordResult:
    return record_plan_constraints(ACME_TICKET, ACME, list(items))


class TestPlanEntries:
    def test_kind_and_plan_actor_are_stored_and_audited(self) -> None:
        _seed_plan()
        result = _plan(("non_goal", "Do not add a store."))
        assert result["unrecorded"] == []
        assert result["duplicates"] == 0
        (entry,) = queue_row().operator_constraints
        assert entry.kind == "non_goal"
        assert entry.actor == PLAN_ACTOR
        assert result["recorded"] == [entry]
        (event,) = _events()
        assert event.payload["kind"] == "non_goal"
        assert event.payload["actor"] == PLAN_ACTOR
        assert event.payload["actor_resolved"] is True

    def test_records_in_order_under_one_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_plan()
        calls: list[bool] = []
        real_lock = module._lock

        def _counting_lock(*, bounded: bool) -> object:
            calls.append(bounded)
            return real_lock(bounded=bounded)

        monkeypatch.setattr(module, "_lock", _counting_lock)
        _plan(
            ("adopted_assumption", "assume A"),
            ("non_goal", "not X"),
            ("plan_decision", "decided D"),
        )
        assert calls == [False]
        assert [
            (e.round, e.kind, e.text) for e in queue_row().operator_constraints
        ] == [
            (1, "adopted_assumption", "assume A"),
            (2, "non_goal", "not X"),
            (3, "plan_decision", "decided D"),
        ]
        assert [e.payload["round"] for e in _events()] == [1, 2, 3]

    def test_second_run_records_nothing(self) -> None:
        _seed_plan()
        _plan(("non_goal", "not X"), ("plan_decision", "decided D"))
        queue_bytes = dev_queue_file().read_bytes()
        result = _plan(("non_goal", "not X"), ("plan_decision", "decided D"))
        assert result["recorded"] == []
        assert result["duplicates"] == 2
        _assert_unchanged_since(queue_bytes, events=2)

    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.IMPL])
    def test_refused_outside_the_plan_stage(self, stage: Stage) -> None:
        _seed(stage=stage)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(CwError) as exc:
            _plan(("non_goal", "not X"))
        message = str(exc.value)
        assert "only while the row is in the plan stage" in message
        assert f"the row is at {stage.value}" in message
        assert "Nothing was recorded" in message
        assert "cw review constrain" in message
        _assert_nothing_recorded(queue_bytes)

    def test_record_locked_refuses_a_plan_kind_outside_plan_stage(self) -> None:
        _seed(stage=Stage.REVIEW)
        queue_bytes = dev_queue_file().read_bytes()
        with module._lock(bounded=False), pytest.raises(CwError, match="plan stage"):
            module._record_locked(
                ACME_TICKET, ACME, "not X", actor=PLAN_ACTOR, kind="non_goal"
            )
        _assert_nothing_recorded(queue_bytes)

    def test_missing_row_is_refused(self) -> None:
        with pytest.raises(CwError, match="No dev-queue task found"):
            _plan(("non_goal", "not X"))
        assert _events() == []

    def test_plan_entry_equal_to_an_operator_entry_is_a_no_op(self) -> None:
        _seed_plan()
        _record("Keep the store single.")
        queue_bytes = dev_queue_file().read_bytes()
        result = _plan(("non_goal", "Keep the store single."))
        assert result["duplicates"] == 1
        assert result["recorded"] == []
        _assert_unchanged_since(queue_bytes, events=1)
        assert [e.kind for e in queue_row().operator_constraints] == ["operator"]

    def test_operator_record_of_plan_text_appends_its_own_entry(self) -> None:
        _seed_plan()
        _plan(("non_goal", "Keep the store single."))
        result = _record("Keep the store single.")
        assert result["duplicate"] is False
        assert result["round"] == 2
        assert [(e.kind, e.text) for e in queue_row().operator_constraints] == [
            ("non_goal", "Keep the store single."),
            ("operator", "Keep the store single."),
        ]

    def test_operator_record_still_dedupes_against_operator_entries(self) -> None:
        _seed_plan()
        _record(_TEXT)
        _plan(("non_goal", "Other text."))
        result = _record(_TEXT)
        assert result["duplicate"] is True
        assert result["round"] == 1


class TestPlanCaps:
    def test_plan_sub_budget_refuses_while_an_operator_entry_records(self) -> None:
        _seed_plan()
        per_entry = MAX_CONSTRAINT_BYTES
        fills = MAX_PLAN_CONSTRAINT_BYTES // per_entry
        result = _plan(
            *(("non_goal", str(i) * per_entry) for i in range(fills)),
            ("plan_decision", "z"),
        )
        assert len(result["recorded"]) == fills
        assert result["unrecorded"] == [("plan_decision", "z")]
        assert "plan" in (result["unrecorded_reason"] or "")
        assert str(MAX_PLAN_CONSTRAINT_BYTES) in (result["unrecorded_reason"] or "")
        operator = _record("z")
        assert operator["duplicate"] is False
        assert operator["total_bytes"] == MAX_PLAN_CONSTRAINT_BYTES + 1

    def test_over_cap_item_is_refused_whole_and_stops_the_batch(self) -> None:
        _seed_plan()
        big = "a" * (17 * 1024)
        result = _plan(
            ("adopted_assumption", "first"),
            ("non_goal", big),
            ("plan_decision", "after"),
        )
        assert [e.text for e in result["recorded"]] == ["first"]
        assert result["unrecorded"] == [("non_goal", big), ("plan_decision", "after")]
        assert "per-entry cap" in (result["unrecorded_reason"] or "")
        assert [e.text for e in queue_row().operator_constraints] == ["first"]
        assert len(_events()) == 1

    def test_total_cap_stops_a_plan_batch(self) -> None:
        _seed_plan()
        for index in range(3):
            _record(str(index) * MAX_CONSTRAINT_BYTES)
        result = _plan(("non_goal", "a" * MAX_CONSTRAINT_BYTES), ("non_goal", "c"))
        assert len(result["recorded"]) == 1
        assert result["unrecorded"] == [("non_goal", "c")]
        assert str(MAX_TOTAL_CONSTRAINT_BYTES) in (result["unrecorded_reason"] or "")

    def test_empty_batch_records_nothing(self) -> None:
        _seed_plan()
        queue_bytes = dev_queue_file().read_bytes()
        result = _plan()
        assert result == {
            "recorded": [],
            "duplicates": 0,
            "unrecorded": [],
            "unrecorded_reason": None,
        }
        _assert_nothing_recorded(queue_bytes)


class TestPlanEventFirst:
    def test_event_failure_records_nothing_for_that_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_plan()
        queue_bytes = dev_queue_file().read_bytes()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "inbox full"
            raise OSError(msg)

        monkeypatch.setattr(module, "record_event", _boom)
        with pytest.raises(CwError, match="audit event failed"):
            _plan(("non_goal", "not X"))
        assert dev_queue_file().read_bytes() == queue_bytes


class TestPlanEntriesSurviveRequeue:
    _ENTRY = OperatorConstraint(
        text="Do not add a store.",
        recorded_at=datetime(2026, 11, 5, 9, 30, tzinfo=UTC),
        actor=PLAN_ACTOR,
        round=1,
        kind="non_goal",
    )

    def test_same_stage_requeue_keeps_plan_entries(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed_blocked(operator_constraints=[self._ENTRY])
        requeue_ticket(ACME_TICKET, ACME, native_daemon=acme_daemon)
        assert queue_row().operator_constraints == [self._ENTRY]

    def test_regress_to_impl_keeps_plan_entries(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed_blocked(operator_constraints=[self._ENTRY])
        requeue_ticket(
            ACME_TICKET, ACME, "impl", allow_regress=True, native_daemon=acme_daemon
        )
        row = queue_row()
        assert row.stage == Stage.IMPL
        assert row.operator_constraints == [self._ENTRY]


def _assert_unchanged_since(queue_bytes: bytes, *, events: int) -> None:
    assert dev_queue_file().read_bytes() == queue_bytes
    assert len(_events()) == events


def test_plan_signature_takes_bounded() -> None:
    assert "bounded" in inspect.signature(record_plan_constraints).parameters
