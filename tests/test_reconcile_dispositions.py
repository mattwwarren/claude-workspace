"""Unit tests for cw.reconcile.dispositions.

Covers the R2 split from GitHub #1306: ``build_salvage_completion_payload``
(pure — builds the 8-field SESSION_COMPLETED payload, zero I/O) and
``emit_routed_sentinel_completion`` (side-effecting — builds the same payload,
records the event, then conditionally queues the daemon surface stop on the
post-lock sink, #1232).
"""

from __future__ import annotations

import pytest

from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.dispositions import (
    build_salvage_completion_payload,
    emit_routed_sentinel_completion,
)
from tests._reconcile_helpers import call_and_drain
from tests.conftest import _make_daemon_session


def test_build_salvage_completion_payload_field_shape() -> None:
    session = _make_daemon_session(claude_session_id="csid-1", surface_ref="ref-1")

    payload = build_salvage_completion_payload(
        session, ticket_id="TKT-1", status="shipped"
    )

    assert payload == {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": "TKT-1",
        "claude_session_id": "csid-1",
        "crashed": False,
        "salvaged": True,
        "status": "shipped",
    }


def test_build_salvage_completion_payload_ticket_id_none() -> None:
    session = _make_daemon_session()

    payload = build_salvage_completion_payload(session, ticket_id=None, status="no_op")

    assert payload["ticket_id"] is None


def test_build_salvage_completion_payload_is_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero I/O: neither record_event nor the daemon client is touched."""

    def _boom_record_event(*_args: object, **_kwargs: object) -> None:
        msg = "record_event must not be called by build_salvage_completion_payload"
        raise AssertionError(msg)

    def _boom_get_native_daemon_client() -> None:
        msg = (
            "get_native_daemon_client must not be called by "
            "build_salvage_completion_payload"
        )
        raise AssertionError(msg)

    monkeypatch.setattr("cw.reconcile.dispositions.record_event", _boom_record_event)
    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client", _boom_get_native_daemon_client
    )

    session = _make_daemon_session()
    payload = build_salvage_completion_payload(
        session, ticket_id="TKT-1", status="shipped"
    )

    assert payload["status"] == "shipped"


def test_emit_routed_sentinel_completion_emits_session_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    session = _make_daemon_session(claude_session_id="csid-2", surface_ref="ref-2")

    call_and_drain(
        emit_routed_sentinel_completion,
        session,
        ticket_id="TKT-2",
        status="stage_complete",
    )

    events = read_events(
        consumer="test-emit-routed-sentinel-completion-emits",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].correlation_id is None
    assert events[0].payload == {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": "TKT-2",
        "claude_session_id": "csid-2",
        "crashed": False,
        "salvaged": True,
        "status": "stage_complete",
    }


def test_emit_routed_sentinel_completion_stops_daemon_when_surface_ref_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    session = _make_daemon_session(surface_ref="ref-3")

    call_and_drain(
        emit_routed_sentinel_completion, session, ticket_id="TKT-3", status="shipped"
    )

    assert daemon.stop_calls == ["ref-3"]


def test_emit_routed_sentinel_completion_skips_stop_when_surface_ref_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    session = _make_daemon_session().model_copy(update={"surface_ref": None})

    call_and_drain(
        emit_routed_sentinel_completion, session, ticket_id="TKT-4", status="shipped"
    )

    assert daemon.stop_calls == []


def test_emit_routed_sentinel_completion_emits_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []

    def _recording_record_event(*_args: object, **_kwargs: object) -> None:
        call_order.append("record_event")

    class _RecordingDaemonClient:
        def stop(self, _surface_ref: str) -> None:
            call_order.append("stop")

    monkeypatch.setattr(
        "cw.reconcile.dispositions.record_event", _recording_record_event
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        _RecordingDaemonClient,
    )
    session = _make_daemon_session(surface_ref="ref-5")

    call_and_drain(
        emit_routed_sentinel_completion, session, ticket_id="TKT-5", status="shipped"
    )

    assert call_order == ["record_event", "stop"]


def test_emit_routed_sentinel_completion_queues_stop_until_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1232: the event is emitted in the act, the stop only at the drain."""
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    session = _make_daemon_session(surface_ref="ref-6")
    sink = DeferredReconcileJobs()

    emit_routed_sentinel_completion(
        session, ticket_id="TKT-6", status="shipped", deferred=sink
    )

    events = read_events(
        consumer="test-emit-routed-sentinel-completion-queues",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert daemon.stop_calls == []
    assert [job.label for job in sink.post_lock] == ["surface_stop:ref-6"]

    run_post_lock_jobs(sink)

    assert daemon.stop_calls == ["ref-6"]


def test_emit_routed_sentinel_completion_queues_nothing_without_surface_ref() -> None:
    session = _make_daemon_session().model_copy(update={"surface_ref": None})
    sink = DeferredReconcileJobs()

    emit_routed_sentinel_completion(
        session, ticket_id="TKT-7", status="shipped", deferred=sink
    )

    assert sink.post_lock == []


_OWN_PR_URL = "https://github.com/foo/bar/pull/99"


def test_build_salvage_completion_payload_adds_exactly_pr_url() -> None:
    """#2715: a supplied ``pr_url`` adds one key to the 8-field payload."""
    session = _make_daemon_session(claude_session_id="csid-8")
    base = build_salvage_completion_payload(session, ticket_id="T-8", status="shipped")

    payload = build_salvage_completion_payload(
        session, ticket_id="T-8", status="shipped", pr_url=_OWN_PR_URL
    )

    assert payload == {**base, "pr_url": _OWN_PR_URL}
    assert len(payload) == len(base) + 1


def test_build_salvage_completion_payload_none_pr_url_omits_the_key() -> None:
    session = _make_daemon_session()

    payload = build_salvage_completion_payload(
        session, ticket_id="T-9", status="shipped", pr_url=None
    )

    assert "pr_url" not in payload
    assert payload == build_salvage_completion_payload(
        session, ticket_id="T-9", status="shipped"
    )


def test_build_salvage_completion_payload_with_pr_url_stays_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "build_salvage_completion_payload must stay pure"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.reconcile.dispositions.record_event", _boom)
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", _boom)

    payload = build_salvage_completion_payload(
        _make_daemon_session(), ticket_id="T-10", status="shipped", pr_url=_OWN_PR_URL
    )

    assert payload["pr_url"] == _OWN_PR_URL


def test_emit_routed_sentinel_completion_carries_pr_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    session = _make_daemon_session(claude_session_id="csid-11", surface_ref="ref-11")

    call_and_drain(
        emit_routed_sentinel_completion,
        session,
        ticket_id="T-11",
        status="shipped",
        pr_url=_OWN_PR_URL,
    )

    events = read_events(
        consumer="test-emit-routed-sentinel-completion-pr-url",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].payload == {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": "T-11",
        "claude_session_id": "csid-11",
        "crashed": False,
        "salvaged": True,
        "status": "shipped",
        "pr_url": _OWN_PR_URL,
    }
    assert daemon.stop_calls == ["ref-11"]
