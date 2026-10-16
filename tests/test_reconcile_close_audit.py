"""Tests for ``cw.reconcile.close_audit`` (#2694).

The one durable audit of an operator session close, shared by the routed-orphan
close, inline ``cw spawn close``, queued closes and ``cw dev-queue cancel``: a
durable intent in the audit outbox before the close, a ``SESSION_COMPLETED``
history event after it, and the intent finalized into a
``session.reap_authorized`` event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.events import read_events
from cw.history import EventType, HistoryEvent, load_history
from cw.models import OrchestratorEventType, SessionStatus
from cw.reconcile.close_audit import (
    OPERATOR_CLOSE_REASON,
    CloseAudit,
    CloseAuditError,
    audit_outbox_path,
    emit_audit_record,
    finalize_audit_intent,
    finalize_close_audit_intent,
    queue_close_audit_intent,
    read_audit_outbox,
    record_close_audit,
)
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from cw.models import Session

_MODULE = "cw.reconcile.close_audit"
_CLIENT = "client-a"


def _session(**overrides: object) -> Session:
    return _make_daemon_session(client=_CLIENT, **overrides)


def _audit(
    *,
    ticket_id: str | None = "T-1",
    client: str | None = None,
    stop_succeeded: bool | None = None,
    details: dict[str, str] | None = None,
) -> CloseAudit:
    return CloseAudit(
        command="cw spawn close",
        reason=OPERATOR_CLOSE_REASON,
        ticket_id=ticket_id,
        client=client,
        stop_succeeded=stop_succeeded,
        details=details or {},
    )


def _authorized() -> list[dict[str, object]]:
    return [
        dict(e.payload)
        for e in read_events(
            event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED]
        )
    ]


def _history() -> list[HistoryEvent]:
    return load_history(_CLIENT, event_types=[EventType.SESSION_COMPLETED])


def _fail(*_args: object, **_kwargs: object) -> None:
    raise OSError(28, "No space left on device")


class TestQueueIntent:
    def test_writes_a_pending_intent_before_any_close(self) -> None:
        sess = _session()

        queue_close_audit_intent(sess, _audit())

        (record,) = read_audit_outbox()
        assert record["session_id"] == sess.id
        assert record["status"] == "pending_stop"
        assert record["surface_ref"] == sess.surface_ref
        payload = record["payload"]
        assert payload["proposed_action"] == OPERATOR_CLOSE_REASON
        assert payload["ticket_id"] == "T-1"
        assert payload["authority"] == "operator"
        assert payload["daemon_stop_succeeded"] is None
        assert isinstance(payload["audit_id"], str)
        assert sess.status is SessionStatus.ACTIVE

    def test_is_idempotent_per_session(self) -> None:
        sess = _session()

        queue_close_audit_intent(sess, _audit())
        queue_close_audit_intent(sess, _audit())

        assert len(read_audit_outbox()) == 1

    def test_no_ticket_and_no_surface_still_record_an_intent(self) -> None:
        sess = _session(surface_ref=None)

        queue_close_audit_intent(sess, _audit(ticket_id=None))

        (record,) = read_audit_outbox()
        assert record["correlation_id"] == sess.id
        assert record["payload"]["ticket_id"] is None

    def test_an_outbox_write_failure_raises_and_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(f"{_MODULE}._write_audit_outbox", _fail)
        caplog.set_level(logging.ERROR, logger=_MODULE)

        with pytest.raises(CloseAuditError, match="Could not prepare the durable"):
            queue_close_audit_intent(_session(), _audit())

        assert not audit_outbox_path().exists()
        assert any(
            r.getMessage().startswith("close_audit_intent_failed:")
            for r in caplog.records
        )


class TestRecord:
    def test_records_a_session_completed_event_with_base_and_details(self) -> None:
        sess = _session()
        audit = _audit(client="other-client", details={"origin": "spawn_close"})

        record_close_audit(sess, audit, prior_status=SessionStatus.IDLE)

        (event,) = _history()
        assert event.session_id == sess.id
        assert event.session_name == sess.name
        assert event.detail == OPERATOR_CLOSE_REASON
        assert event.metadata == {
            "actor": "operator",
            "command": "cw spawn close",
            "ticket_id": "T-1",
            "client": "other-client",
            "session_id": sess.id,
            "surface_ref": "live-ref",
            "prior_status": "idle",
            "resulting_status": "completed",
            "reason": OPERATOR_CLOSE_REASON,
            "origin": "spawn_close",
        }

    def test_defaults_the_client_and_blank_optional_fields(self) -> None:
        sess = _session(surface_ref=None)

        record_close_audit(sess, _audit(ticket_id=None), prior_status=sess.status)

        (event,) = _history()
        assert event.metadata["client"] == _CLIENT
        assert event.metadata["ticket_id"] == ""
        assert event.metadata["surface_ref"] == ""

    def test_a_history_write_failure_raises_close_audit_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(f"{_MODULE}.record_history_event", _fail)
        caplog.set_level(logging.ERROR, logger=_MODULE)

        with pytest.raises(
            CloseAuditError, match="durable audit record could not be written"
        ):
            record_close_audit(_session(), _audit(), prior_status=SessionStatus.ACTIVE)

        (failed,) = [
            r
            for r in caplog.records
            if r.getMessage().startswith("close_audit_failed:")
        ]
        assert failed.exc_info is not None


class TestFinalize:
    def test_delivers_the_intent_as_reap_authorized_and_empties_the_outbox(
        self,
    ) -> None:
        sess = _session()
        audit = _audit()
        queue_close_audit_intent(sess, audit)

        assert finalize_close_audit_intent(sess, audit) is True

        (payload,) = _authorized()
        assert payload["session_id"] == sess.id
        assert payload["proposed_action"] == OPERATOR_CLOSE_REASON
        assert payload["mutations"] == ["session_status_completed"]
        assert payload["daemon_stop_succeeded"] is None
        assert read_audit_outbox() == []

    def test_a_confirmed_stop_is_recorded_as_a_mutation(self) -> None:
        sess = _session()
        audit = _audit(stop_succeeded=True)
        queue_close_audit_intent(sess, audit)

        finalize_close_audit_intent(sess, audit)

        (payload,) = _authorized()
        assert payload["mutations"] == ["session_status_completed", "daemon_stopped"]
        assert payload["daemon_stop_succeeded"] is True

    def test_replay_without_an_intent_is_quietly_done(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING)

        assert finalize_close_audit_intent(_session(), _audit(), replay=True) is True

        assert _authorized() == []
        assert caplog.records == []

    def test_a_missing_intent_outside_replay_is_not_done(self) -> None:
        assert finalize_close_audit_intent(_session(), _audit()) is False

    def test_an_unreadable_outbox_never_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = audit_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        caplog.set_level(logging.ERROR, logger=_MODULE)

        assert finalize_close_audit_intent(_session(), _audit()) is False

        assert any(
            r.getMessage().startswith("close_audit_finalize_failed:")
            for r in caplog.records
        )

    def test_an_inbox_outage_keeps_the_finalized_intent_durable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess = _session()
        audit = _audit()
        queue_close_audit_intent(sess, audit)
        monkeypatch.setattr(f"{_MODULE}.record_event", _fail)

        assert finalize_close_audit_intent(sess, audit) is True

        (record,) = read_audit_outbox()
        assert record["status"] == "pending_event"

    def test_a_re_close_after_a_resume_is_delivered_again(self) -> None:
        """Delivery dedup is per intent, so a second incarnation is audited too."""
        sess = _session()
        audit = _audit()
        queue_close_audit_intent(sess, audit)
        finalize_close_audit_intent(sess, audit)
        resumed = sess.model_copy(update={"resumed_at": datetime.now(UTC)})

        queue_close_audit_intent(resumed, audit)
        finalize_close_audit_intent(resumed, audit)

        assert len(_authorized()) == 2
        assert read_audit_outbox() == []


class TestEmit:
    def test_the_same_intent_is_never_delivered_twice(self) -> None:
        sess = _session()
        queue_close_audit_intent(sess, _audit())
        record = finalize_audit_intent(
            sess.id,
            mutations=["session_status_completed"],
            stop_succeeded=None,
            stop_error=None,
        )
        assert record is not None

        assert emit_audit_record(record) is True
        assert emit_audit_record(record) is True

        assert len(_authorized()) == 1

    def test_an_unreadable_inbox_keeps_the_record_for_a_retry(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        sess = _session()
        queue_close_audit_intent(sess, _audit())
        monkeypatch.setattr(f"{_MODULE}.read_events", _fail)
        caplog.set_level(logging.ERROR, logger=_MODULE)

        assert finalize_close_audit_intent(sess, _audit()) is True

        (record,) = read_audit_outbox()
        assert record["status"] == "pending_event"
        assert any(
            r.getMessage().startswith("cannot inspect audit delivery")
            for r in caplog.records
        )

    def test_a_legacy_record_without_an_audit_id_dedups_by_session_and_action(
        self,
    ) -> None:
        record: dict[str, object] = {
            "session_id": "legacy",
            "correlation_id": "legacy",
            "payload": {
                "session_id": "legacy",
                "proposed_action": "close_routed_result_session",
            },
        }

        assert emit_audit_record(record) is True
        assert emit_audit_record(record) is True

        assert len(_authorized()) == 1
