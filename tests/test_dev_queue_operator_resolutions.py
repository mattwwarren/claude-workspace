"""``record_operator_resolution``: the library half of ``cw dev-queue resolve`` (#2702).

Covers the append-only ``operator_resolutions`` ledger on a plan-parked row,
the R4 audit rules (a pure duplicate records neither event nor save), the
refusals that leave the queue byte-identical, event-first ordering, and the
``--approve`` composition of the ``cw dev-queue approve`` gate helpers (R1).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.config import dev_queue_file
from cw.dev_queue import load_dev_queue, record_operator_resolution, requeue_ticket
from cw.dev_queue import operator_resolutions as module
from cw.dev_queue.operator_resolutions import MAX_RESOLUTION_BYTES
from cw.events import read_events
from cw.exceptions import ApproveGateError
from cw.models import DEFAULT_LANE, OrchestratorEventType, QueueItemStatus, Stage
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_blocked,
    seed_queue,
)
from tests.conftest import _write_project_config_yaml

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import OrchestratorEvent, TicketTask
    from cw.native_daemon import FakeNativeDaemonClient

_LOGIN = "octo-operator"
_FINGERPRINT = "a" * 64
_GATE: dict[str, object] = {
    "status": "plan_pending_approval",
    "plan_draft_fingerprint": _FINGERPRINT,
}
_AMBIGUITIES: dict[str, object] = {"status": "ambiguities_pending_resolution"}
_DECISIONS = "1. Use the row.\n2. Keep lane and priority."
_APPROVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_LINEAR_CONFIG = "tracking:\n  primary:\n    system: linear\n"
_SHA_A = hashlib.sha256(b"body A\n").hexdigest()
_BODY_B = "body B\n"


@pytest.fixture(autouse=True)
def acme_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    fake = install_acme_client(tmp_path, monkeypatch)
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: _LOGIN)
    # No live gh in a unit test: the #968 tracker fetch and the #2311 body
    # fetch both degrade to "nothing found".
    monkeypatch.setattr(
        "cw.dev_queue.lifecycle.fetch_approved_plan_comment", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "cw.dev_queue.approval.fetch_issue_body", lambda *_a, **_k: None
    )
    return fake


def _seed(
    *,
    last_result: dict[str, object] | None = None,
    with_session: bool = True,
    **overrides: object,
) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.PLAN, "lane": "fast", "priority": 7}
    kwargs.update(overrides)
    task = make_blocked(**kwargs)
    sessions = (
        [bound_session_for(task, last_result=last_result)]
        if with_session and task.session_id is not None
        else []
    )
    seed_queue([task], sessions)
    return task


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ACME_TICKET)


def _resolution_events() -> list[OrchestratorEvent]:
    return list(
        read_events(event_types=[OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED])
    )


def _record(text: str = _DECISIONS, **kwargs: bool) -> module.OperatorResolutionRecord:
    return record_operator_resolution(ACME_TICKET, ACME, text, **kwargs)


def _approve(text: str = _DECISIONS) -> module.OperatorResolutionRecord:
    return _record(text, approve=True, operator_initiated=True)


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[object, ...]]:
    """Wrap ``operator_resolutions.<name>`` so every call is logged, then run."""
    calls: list[tuple[object, ...]] = []
    original: Callable[..., object] = getattr(module, name)

    def _wrapper(*args: object) -> object:
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(module, name, _wrapper)
    return calls


class TestAppend:
    def test_first_record_is_round_one(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        result = _record(f"  {_DECISIONS}\n\n")
        assert result["round"] == 1
        assert result["duplicate"] is False
        assert result["approved"] is False
        assert result["actor"] == _LOGIN
        assert result["stage"] == "plan"
        assert result["status"] == "blocked_on_user"
        (entry,) = _row().operator_resolutions
        assert entry.text == _DECISIONS
        assert entry.round == 1
        assert entry.actor == _LOGIN
        assert entry.recorded_at.tzinfo is not None

    def test_different_text_appends_round_two_in_order(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        _record("first")
        result = _record("second")
        assert result["round"] == 2
        assert [(e.round, e.text) for e in _row().operator_resolutions] == [
            (1, "first"),
            (2, "second"),
        ]

    def test_append_records_one_event_without_the_text(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        _record()
        (event,) = _resolution_events()
        assert event.correlation_id == ACME_TICKET
        payload = event.payload
        assert (
            payload["text_sha256"]
            == hashlib.sha256(_DECISIONS.encode("utf-8")).hexdigest()
        )
        assert payload["text_bytes"] == len(_DECISIONS.encode("utf-8"))
        assert payload["round"] == 1
        assert payload["actor"] == _LOGIN
        assert payload["approved"] is False
        assert payload["operator_initiated"] is False
        assert payload["duplicate"] is False
        assert _DECISIONS not in str(payload)

    def test_record_touches_only_operator_resolutions(self) -> None:
        _seed(
            last_result=_AMBIGUITIES,
            plan_approved_at=_APPROVED_AT,
            plan_approved_fingerprint=_FINGERPRINT,
            scope_hint="large",
            attempts=3,
        )
        before = _row().model_dump()
        _record()
        after = _row().model_dump()
        assert len(after.pop("operator_resolutions")) == 1
        assert before.pop("operator_resolutions") == []
        assert after == before
        assert after["lane"] == "fast"
        assert after["priority"] == 7


class TestDuplicate:
    def test_pure_duplicate_records_no_event_and_no_save(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        _record()
        queue_bytes = dev_queue_file().read_bytes()
        events_before = len(_resolution_events())
        result = _record(f"{_DECISIONS}\n")
        assert result["duplicate"] is True
        assert result["round"] == 1
        assert len(_row().operator_resolutions) == 1
        assert len(_resolution_events()) == events_before
        assert dev_queue_file().read_bytes() == queue_bytes

    def test_duplicate_of_an_older_round_appends(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        _record("first")
        _record("second")
        result = _record("first")
        assert result["duplicate"] is False
        assert result["round"] == 3

    def test_duplicate_with_approve_stamps_records_and_saves(self) -> None:
        _seed(last_result=_GATE)
        _record()
        queue_bytes = dev_queue_file().read_bytes()
        result = _approve()
        assert result["duplicate"] is True
        assert result["approved"] is True
        assert result["round"] == 1
        row = _row()
        assert len(row.operator_resolutions) == 1
        assert row.plan_approved_fingerprint == _FINGERPRINT
        assert dev_queue_file().read_bytes() != queue_bytes
        events = _resolution_events()
        assert len(events) == 2
        assert events[-1].payload["duplicate"] is True
        assert events[-1].payload["approved"] is True


def _assert_nothing_recorded(queue_bytes: bytes) -> None:
    assert dev_queue_file().read_bytes() == queue_bytes
    assert _resolution_events() == []


class TestRefusals:
    @pytest.mark.parametrize(
        "status", [QueueItemStatus.PENDING, QueueItemStatus.RUNNING]
    )
    def test_unparked_status_refused(self, status: QueueItemStatus) -> None:
        _seed(last_result=_AMBIGUITIES, status=status)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="expected BLOCKED_ON_USER") as exc:
            _record()
        assert "`cw " in str(exc.value)
        _assert_nothing_recorded(queue_bytes)

    def test_non_plan_stage_refused(self) -> None:
        _seed(last_result=_AMBIGUITIES, stage=Stage.IMPL)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="expected the plan stage") as exc:
            _record()
        assert "`cw " in str(exc.value)
        _assert_nothing_recorded(queue_bytes)

    @pytest.mark.parametrize("text", ["", "   \n\t\n"])
    def test_blank_text_refused(self, text: str) -> None:
        _seed(last_result=_AMBIGUITIES)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="blank"):
            _record(text)
        _assert_nothing_recorded(queue_bytes)

    def test_text_over_the_cap_refused(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="65536"):
            _record("x" * (MAX_RESOLUTION_BYTES + 1))
        _assert_nothing_recorded(queue_bytes)

    def test_text_at_exactly_the_cap_accepted(self) -> None:
        assert MAX_RESOLUTION_BYTES == 65536
        _seed(last_result=_AMBIGUITIES)
        result = _record("x" * MAX_RESOLUTION_BYTES)
        assert result["round"] == 1


class TestActor:
    def test_unresolved_login_records_empty_actor_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        _seed(last_result=_AMBIGUITIES)
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            result = _record()
        assert result["actor"] == ""
        assert _row().operator_resolutions[0].actor == ""
        assert any(
            "operator login unresolved" in r.getMessage() for r in caplog.records
        )


class TestEventFirst:
    def test_event_failure_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(last_result=_AMBIGUITIES)
        queue_bytes = dev_queue_file().read_bytes()

        def _boom(*_a: object, **_k: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr(module, "record_event", _boom)
        with pytest.raises(ApproveGateError, match="Nothing was recorded"):
            _record()
        assert dev_queue_file().read_bytes() == queue_bytes

    def test_save_failure_after_the_event_logs_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed(last_result=_AMBIGUITIES)
        queue_bytes = dev_queue_file().read_bytes()

        def _boom(*_a: object, **_k: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(module, "save_dev_queue", _boom)
        with (
            caplog.at_level(logging.ERROR, logger=module.__name__),
            pytest.raises(OSError, match="disk full"),
        ):
            _record()
        assert dev_queue_file().read_bytes() == queue_bytes
        assert len(_resolution_events()) == 1
        assert any(r.levelno == logging.ERROR for r in caplog.records)


class TestApproveAtGate:
    def test_stamps_through_the_reused_gate_helpers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = _seed(last_result=_GATE)
        gate = _spy(monkeypatch, "_raise_if_not_at_approval_gate")
        stamp = _spy(monkeypatch, "_stamp_plan_approval")
        reviewed = _spy(monkeypatch, "_plan_is_reviewed")
        drift = _spy(monkeypatch, "_record_body_drift")

        result = _approve()

        assert result["approved"] is True
        assert result["plan_approved_fingerprint"] == _FINGERPRINT
        assert result["plan_reviewed"] is False
        assert result["body_drift_warning"] is None
        assert len(gate) == len(stamp) == len(reviewed) == len(drift) == 1
        stamped_task, from_stage, session = stamp[0]
        assert from_stage == "plan"
        assert getattr(stamped_task, "ticket_id", None) == ACME_TICKET
        assert getattr(session, "id", None) == task.session_id
        row = _row()
        assert row.plan_approved_at is not None
        assert row.plan_approved_fingerprint == _FINGERPRINT
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.stage == Stage.PLAN
        (event,) = _resolution_events()
        assert event.payload["approved"] is True
        assert event.payload["operator_initiated"] is True
        assert event.payload["plan_approved_fingerprint"] == _FINGERPRINT

    def test_disposition_leg_stamps(self) -> None:
        _seed(last_result={"status": "stage_complete"}, disposition="approval_gate")
        result = _approve()
        assert result["approved"] is True
        assert _row().plan_approved_at is not None

    def test_not_at_a_gate_refuses_with_the_sequence(self) -> None:
        _seed(last_result=_AMBIGUITIES)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError) as exc:
            _approve()
        message = str(exc.value)
        assert "not at an approval gate" in message
        assert "without --approve" in message
        assert "re-park" in message
        assert f"cw dev-queue approve {ACME_TICKET} -c {ACME}" in message
        assert "Nothing was recorded" in message
        _assert_nothing_recorded(queue_bytes)
        assert _row().operator_resolutions == []

    @pytest.mark.parametrize(
        "session_id", [None, "sess-missing"], ids=["no-id", "not-in-state"]
    )
    def test_no_resolvable_session_refuses(self, session_id: str | None) -> None:
        _seed(last_result=_GATE, with_session=False, session_id=session_id)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="session was not found") as exc:
            _approve()
        assert f"session_id={session_id!r}" in str(exc.value)
        _assert_nothing_recorded(queue_bytes)

    @pytest.mark.parametrize(
        "last_result",
        [
            {"status": "plan_pending_approval"},
            {"status": "plan_pending_approval", "plan_draft_fingerprint": "XYZ"},
        ],
        ids=["absent", "malformed"],
    )
    def test_sentinel_without_a_fingerprint_stamps_unbound(
        self, last_result: dict[str, object]
    ) -> None:
        _seed(last_result=last_result)
        result = _approve()
        assert result["approved"] is True
        assert result["plan_approved_fingerprint"] is None
        row = _row()
        assert row.plan_approved_at is not None
        assert row.plan_approved_fingerprint is None
        assert len(row.operator_resolutions) == 1

    def test_approve_without_operator_provenance_refused(self) -> None:
        _seed(last_result=_GATE)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="operator-initiated"):
            _record(approve=True)
        _assert_nothing_recorded(queue_bytes)

    @pytest.mark.parametrize("reviewed", [True, False])
    def test_plan_reviewed_is_reported_never_routed(
        self, monkeypatch: pytest.MonkeyPatch, reviewed: bool
    ) -> None:
        _seed(last_result=_GATE)
        monkeypatch.setattr(module, "_plan_is_reviewed", lambda *_a: reviewed)
        result = _approve()
        assert result["plan_reviewed"] is reviewed
        row = _row()
        assert row.status == QueueItemStatus.BLOCKED_ON_USER
        assert row.stage == Stage.PLAN

    def test_body_drift_warns_records_first_and_still_stamps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(last_result=_GATE)
        monkeypatch.setattr(
            "cw.dev_queue.approval._read_persisted_body_sha", lambda *_a: _SHA_A
        )
        monkeypatch.setattr(
            "cw.dev_queue.approval.fetch_issue_body", lambda *_a, **_k: _BODY_B
        )
        result = _approve()
        warning = result["body_drift_warning"]
        assert warning is not None
        assert "changed after the plan stage last evaluated it" in warning
        assert _row().plan_approved_fingerprint == _FINGERPRINT
        types = [
            e.type
            for e in read_events(
                event_types=[
                    OrchestratorEventType.PLAN_APPROVAL_BODY_DRIFT_WARNED,
                    OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED,
                ]
            )
        ]
        assert types == [
            OrchestratorEventType.PLAN_APPROVAL_BODY_DRIFT_WARNED,
            OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED,
        ]

    def test_body_drift_event_failure_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(last_result=_GATE)
        queue_bytes = dev_queue_file().read_bytes()
        monkeypatch.setattr(
            "cw.dev_queue.approval._read_persisted_body_sha", lambda *_a: _SHA_A
        )
        monkeypatch.setattr(
            "cw.dev_queue.approval.fetch_issue_body", lambda *_a, **_k: _BODY_B
        )

        def _boom(*_a: object, **_k: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr("cw.dev_queue.approval.record_event", _boom)
        with pytest.raises(ApproveGateError, match="Nothing was recorded"):
            _approve()
        _assert_nothing_recorded(queue_bytes)

    def test_linear_client_never_calls_gh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_project_config_yaml(tmp_path / "ws", _LINEAR_CONFIG)
        _seed(last_result=_GATE)

        def _no_gh(*_a: object, **_k: object) -> None:
            msg = "gh must not be called on a Linear-tracked client"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.dev_queue.approval._read_persisted_body_sha", lambda *_a: _SHA_A
        )
        monkeypatch.setattr(
            "cw.dev_queue.lifecycle.fetch_approved_plan_comment", _no_gh
        )
        monkeypatch.setattr("cw.dev_queue.approval.fetch_issue_body", _no_gh)
        result = _approve()
        assert result["approved"] is True
        assert result["plan_reviewed"] is False


class TestApprovePipelineParity:
    """Advisory note 1: ``--approve`` refuses wherever ``approve`` refuses."""

    @pytest.mark.parametrize(
        "stages",
        [["impl", "review", "finalize"], ["plan"]],
        ids=["no-plan-stage", "plan-only"],
    )
    def test_pipeline_refusals(self, tmp_path: Path, stages: list[str]) -> None:
        write_clients_yaml(
            ClientSpec(
                ACME,
                tmp_path / "ws",
                lanes=[
                    {"name": DEFAULT_LANE},
                    {"name": "fast", "pipeline": {"stages": stages}},
                ],
            ),
            ensure_workspaces=True,
        )
        _seed(last_result=_GATE)
        queue_bytes = dev_queue_file().read_bytes()
        with pytest.raises(ApproveGateError, match="Cannot approve ticket"):
            _approve()
        _assert_nothing_recorded(queue_bytes)


class TestRequeueKeepsTheLedger:
    def test_same_stage_requeue_keeps_resolutions_lane_priority_and_approval(
        self, acme_daemon: FakeNativeDaemonClient
    ) -> None:
        _seed(last_result=_GATE)
        _approve()
        requeue_ticket(ACME_TICKET, ACME, native_daemon=acme_daemon)
        row = _row()
        assert row.status == QueueItemStatus.PENDING
        assert row.stage == Stage.PLAN
        assert row.lane == "fast"
        assert row.priority == 7
        assert len(row.operator_resolutions) == 1
        assert row.plan_approved_at is not None
        assert row.plan_approved_fingerprint == _FINGERPRINT
