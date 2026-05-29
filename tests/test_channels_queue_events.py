"""Tests for cw_queue_events_server: delta detection, notification shape, registry."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from starlette.testclient import TestClient  # noqa: E402

import cw.cw_queue_events_server as _server_mod  # noqa: E402
from cw.cw_queue_events_server import (  # noqa: E402
    _NOTIFICATION_TYPE,
    QueueSnapshot,
    _build_queue_notification,
    _check_wedge,
    _compute_queue_deltas,
    _compute_session_deltas,
    _load_offset_from_file,
    _load_snapshot,
    _save_snapshot,
    broadcast,
    make_app,
    serve,
    subscribe,
    subscribe_with_cursor,
    unsubscribe,
)
from cw.models import (  # noqa: E402
    CwState,
    DevQueueStore,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)


@pytest.fixture(autouse=True)
def _reset_subscribers() -> Generator[None]:
    """Clear global subscriber list between tests to prevent state bleed."""
    with _server_mod._lock:
        _server_mod._subscribers.clear()
    yield
    with _server_mod._lock:
        _server_mod._subscribers.clear()


@pytest.fixture(autouse=True)
def _reset_channel_state() -> Generator[None]:
    """Reset durable-replay in-memory state between tests."""
    with _server_mod._file_lock:
        _server_mod._cursors.clear()
        _server_mod._event_offset[0] = 0
    _server_mod._poller_started[0] = False
    yield
    with _server_mod._file_lock:
        _server_mod._cursors.clear()
        _server_mod._event_offset[0] = 0
    _server_mod._poller_started[0] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    ticket_id: str,
    client: str,
    status: QueueItemStatus,
    session_id: str | None = None,
    attempts: int = 0,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=status,
        session_id=session_id,
        attempts=attempts,
    )


def _make_session(
    session_id: str,
    status: SessionStatus,
    last_result: dict[str, Any] | None = None,
    name: str = "test-client/impl",
    client: str = "test-client",
) -> Session:
    from pathlib import Path

    return Session(
        id=session_id,
        name=name,
        client=client,
        purpose=SessionPurpose.IMPL,
        status=status,
        workspace_path=Path("/tmp/test"),
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        last_result=last_result,
    )


# ---------------------------------------------------------------------------
# TestQueueSnapshotModel
# ---------------------------------------------------------------------------


class TestQueueSnapshotModel:
    def test_empty_defaults(self) -> None:
        snap = QueueSnapshot()
        assert snap.task_statuses == {}
        assert snap.task_session_ids == {}
        assert snap.session_statuses == {}

    def test_round_trip_json(self) -> None:
        snap = QueueSnapshot(
            task_statuses={"T-1": "pending", "T-2": "running"},
            task_session_ids={"T-1": None, "T-2": "sess123"},
            session_statuses={"s1": "active"},
        )
        serialized = snap.model_dump_json()
        restored = QueueSnapshot.model_validate_json(serialized)
        assert restored.task_statuses == snap.task_statuses
        assert restored.task_session_ids == snap.task_session_ids
        assert restored.session_statuses == snap.session_statuses

    def test_partial_fields(self) -> None:
        snap = QueueSnapshot(task_statuses={"T-3": "completed"})
        assert snap.task_session_ids == {}
        assert snap.session_statuses == {}


# ---------------------------------------------------------------------------
# TestQueueEventNotificationShape
# ---------------------------------------------------------------------------


class TestQueueEventNotificationShape:
    def test_notification_type_field(self) -> None:
        event = {
            "event": "queue.ticket_enqueued",
            "ticket_id": "T-1",
            "client": "acme",
            "status": "pending",
        }
        notif = _build_queue_notification(event)
        assert notif["notification_type"] == _NOTIFICATION_TYPE

    def test_message_field_is_valid_json(self) -> None:
        event = {
            "event": "queue.ticket_enqueued",
            "ticket_id": "T-1",
            "client": "acme",
            "status": "pending",
        }
        notif = _build_queue_notification(event)
        data = json.loads(notif["message"])
        assert "event" in data
        assert data["ticket_id"] == "T-1"

    def test_title_field_present(self) -> None:
        event = {
            "event": "queue.ticket_enqueued",
            "ticket_id": "T-1",
            "client": "acme",
            "status": "pending",
        }
        notif = _build_queue_notification(event)
        assert isinstance(notif["title"], str)
        assert notif["title"]

    @pytest.mark.parametrize(
        "event_type",
        [
            "queue.ticket_enqueued",
            "queue.ticket_claimed",
            "queue.ticket_completed",
            "queue.ticket_failed",
            "queue.session_idled",
            "queue.wedge_detected",
        ],
    )
    def test_all_event_types_produce_title(self, event_type: str) -> None:
        event = {"event": event_type, "ticket_id": "T-42", "client": "x"}
        notif = _build_queue_notification(event)
        assert notif["title"]

    def test_title_includes_ticket_id(self) -> None:
        event = {"event": "queue.ticket_claimed", "ticket_id": "T-99", "client": "acme"}
        notif = _build_queue_notification(event)
        assert "T-99" in notif["title"]

    def test_title_without_ticket_id(self) -> None:
        event = {"event": "queue.session_idled", "session_id": "s1"}
        notif = _build_queue_notification(event)
        assert isinstance(notif["title"], str)
        assert notif["title"]


# ---------------------------------------------------------------------------
# TestSubscriberRegistry
# ---------------------------------------------------------------------------


class TestSubscriberRegistry:
    def test_subscribe_adds_to_registry(self) -> None:
        q = subscribe()
        try:
            assert isinstance(q, queue.SimpleQueue)
        finally:
            unsubscribe(q)

    def test_unsubscribe_removes_from_registry(self) -> None:
        q = subscribe()
        unsubscribe(q)
        broadcast({"test": True})
        assert q.empty()

    def test_broadcast_sends_to_all_queues(self) -> None:
        q1 = subscribe()
        q2 = subscribe()
        try:
            broadcast({"x": 1})
            assert q1.get_nowait() == {"x": 1}
            assert q2.get_nowait() == {"x": 1}
        finally:
            unsubscribe(q1)
            unsubscribe(q2)


# ---------------------------------------------------------------------------
# TestAppendEventQueueChannel
# ---------------------------------------------------------------------------


class TestAppendEventQueueChannel:
    def test_appends_to_queue_channel_events_jsonl(self) -> None:
        from cw.config import state_dir
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m", "title": "t"}
        )
        path = state_dir() / "queue-channel-events.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1

    def test_does_not_write_to_channel_events_jsonl(self) -> None:
        """Must use queue-channel-events.jsonl, NOT channel-events.jsonl."""
        from cw.config import state_dir
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m", "title": "t"}
        )
        wrong_path = state_dir() / "channel-events.jsonl"
        assert not wrong_path.exists()

    def test_offset_increments_monotonically(self) -> None:
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m", "title": "t"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m2", "title": "t2"}
        )
        assert _server_mod._event_offset[0] == 2

    def test_record_contains_offset_field(self) -> None:
        from cw.config import state_dir
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m", "title": "t"}
        )
        path = state_dir() / "queue-channel-events.jsonl"
        record = json.loads(path.read_text().splitlines()[0])
        assert record["offset"] == 0

    def test_thread_safe_under_concurrent_appends(self) -> None:
        from cw.config import state_dir
        from cw.cw_queue_events_server import _append_event

        def worker() -> None:
            _append_event(
                {"notification_type": _NOTIFICATION_TYPE, "message": "x", "title": "x"}
            )

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = state_dir() / "queue-channel-events.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) == 10
        records = [json.loads(line) for line in lines]
        offsets = {r["offset"] for r in records}
        assert len(offsets) == 10  # all unique


# ---------------------------------------------------------------------------
# TestReadEventsFromOffsetQueueChannel
# ---------------------------------------------------------------------------


class TestReadEventsFromOffsetQueueChannel:
    def test_returns_empty_for_missing_file(self) -> None:
        from cw.cw_queue_events_server import _read_events_from_offset

        result = _read_events_from_offset(0)
        assert result == []

    def test_returns_all_events_from_zero(self) -> None:
        from cw.cw_queue_events_server import (
            _append_event,
            _read_events_from_offset,
        )

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "b", "title": "b"}
        )
        result = _read_events_from_offset(0)
        assert len(result) == 2

    def test_respects_offset_filter(self) -> None:
        from cw.cw_queue_events_server import (
            _append_event,
            _read_events_from_offset,
        )

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "b", "title": "b"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "c", "title": "c"}
        )
        result = _read_events_from_offset(1)
        assert len(result) == 2
        assert result[0]["offset"] == 1
        assert result[1]["offset"] == 2

    def test_skips_malformed_lines(self) -> None:
        from cw.config import state_dir
        from cw.cw_queue_events_server import (
            _append_event,
            _read_events_from_offset,
        )

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "a", "title": "a"}
        )
        path = state_dir() / "queue-channel-events.jsonl"
        with path.open("a") as f:
            f.write("not-json\n")
        result = _read_events_from_offset(0)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestSubscribeWithCursorQueueChannel
# ---------------------------------------------------------------------------


class TestSubscribeWithCursorQueueChannel:
    def test_returns_queue(self) -> None:
        q = subscribe_with_cursor("sub1")
        assert isinstance(q, queue.SimpleQueue)
        unsubscribe(q)

    def test_replays_missed_events(self) -> None:
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "b", "title": "b"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "c", "title": "c"}
        )

        _server_mod._cursors["sub2"] = 1
        q = subscribe_with_cursor("sub2")
        try:
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            assert len(items) == 2
            assert items[0]["offset"] == 1
            assert items[1]["offset"] == 2
        finally:
            unsubscribe(q)

    def test_no_replay_when_caught_up(self) -> None:
        from cw.cw_queue_events_server import _append_event

        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": _NOTIFICATION_TYPE, "message": "b", "title": "b"}
        )

        _server_mod._cursors["sub3"] = 2
        q = subscribe_with_cursor("sub3")
        try:
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_registers_for_future_events(self) -> None:
        q = subscribe_with_cursor("sub4")
        try:
            broadcast(
                {
                    "notification_type": _NOTIFICATION_TYPE,
                    "message": "future",
                    "title": "f",
                }
            )
            item = q.get_nowait()
            assert item["notification_type"] == _NOTIFICATION_TYPE
        finally:
            unsubscribe(q)


# ---------------------------------------------------------------------------
# TestAckOffsetQueueChannel
# ---------------------------------------------------------------------------


class TestAckOffsetQueueChannel:
    def test_persists_to_queue_channel_cursors_json(self) -> None:
        from cw.config import state_dir
        from cw.cw_queue_events_server import ack_offset

        ack_offset("sub-a", 3)
        path = state_dir() / "queue-channel-cursors.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"sub-a": 3}

    def test_does_not_write_channel_cursors_json(self) -> None:
        """Must use queue-channel-cursors.json, NOT channel-cursors.json."""
        from cw.config import state_dir
        from cw.cw_queue_events_server import ack_offset

        ack_offset("sub-b", 5)
        wrong_path = state_dir() / "channel-cursors.json"
        assert not wrong_path.exists()

    def test_updates_in_memory_cursors(self) -> None:
        from cw.cw_queue_events_server import ack_offset

        ack_offset("sub-c", 7)
        assert _server_mod._cursors["sub-c"] == 7

    def test_overwrites_previous_cursor(self) -> None:
        from cw.cw_queue_events_server import ack_offset

        ack_offset("sub-d", 1)
        ack_offset("sub-d", 5)
        assert _server_mod._cursors["sub-d"] == 5


# ---------------------------------------------------------------------------
# TestHandlePostAckQueueChannel
# ---------------------------------------------------------------------------


class TestHandlePostAckQueueChannel:
    def _make_client(self) -> TestClient:
        return TestClient(make_app())

    def test_valid_request_returns_ok(self) -> None:
        client = self._make_client()
        resp = client.post("/ack", json={"client_id": "c1", "offset": 0})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_invalid_body_returns_400(self) -> None:
        client = self._make_client()
        resp = client.post("/ack", json={"client_id": "c1"})  # missing offset
        assert resp.status_code == 400

    def test_updates_cursor(self) -> None:
        client = self._make_client()
        client.post("/ack", json={"client_id": "c2", "offset": 42})
        assert _server_mod._cursors["c2"] == 42

    def test_route_registered(self) -> None:
        app = make_app()
        route_paths = [r.path for r in app.routes]
        assert "/ack" in route_paths


# ---------------------------------------------------------------------------
# TestMakeAppQueueEvents
# ---------------------------------------------------------------------------


class TestMakeAppQueueEvents:
    def test_app_is_starlette(self) -> None:
        from starlette.applications import Starlette

        app = make_app()
        assert isinstance(app, Starlette)

    def test_redirect_slashes_false(self) -> None:
        app = make_app()
        assert not app.router.redirect_slashes

    def test_sse_slash_middleware_present(self) -> None:
        app = make_app()
        # Check _SSESlashMiddleware is in user_middleware by class name
        mw_classes = [
            m.cls.__name__ if hasattr(m, "cls") else type(m).__name__
            for m in app.user_middleware
        ]
        assert any("_SSESlashMiddleware" in cls for cls in mw_classes)

    def test_no_pr_event_route(self) -> None:
        """Queue events server must NOT have a /pr-event route."""
        app = make_app()
        route_paths = [r.path for r in app.routes]
        assert "/pr-event" not in route_paths


# ---------------------------------------------------------------------------
# TestServeQueueEvents
# ---------------------------------------------------------------------------


class TestServeQueueEvents:
    def test_serve_calls_uvicorn_run(self) -> None:
        mock_run = MagicMock()
        with patch("uvicorn.run", mock_run):
            serve(host="127.0.0.1", port=9999)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("host") == "127.0.0.1"
        assert call_kwargs.get("port") == 9999

    def test_default_port_is_8789(self) -> None:
        from cw.cw_queue_events_server import DEFAULT_PORT

        assert DEFAULT_PORT == 8789


# ---------------------------------------------------------------------------
# TestCLIQueueChannel
# ---------------------------------------------------------------------------


class TestCLIQueueChannel:
    def test_queue_channel_serve_command_invokes_serve(self) -> None:
        from cw.cli import main

        mock_serve = MagicMock()
        runner = CliRunner()
        with patch("cw.cw_queue_events_server.serve", mock_serve):
            result = runner.invoke(main, ["queue-channel", "serve", "--port", "9124"])
        assert result.exit_code == 0
        mock_serve.assert_called_once_with(host="127.0.0.1", port=9124)

    def test_queue_channel_proxy_help(self) -> None:
        from cw.cli import main

        result = CliRunner().invoke(main, ["queue-channel", "proxy", "--help"])
        assert result.exit_code == 0
        assert "--client-id" in result.output


# ---------------------------------------------------------------------------
# TestCheckWedgeStub
# ---------------------------------------------------------------------------


class TestCheckWedgeStub:
    def test_returns_none(self) -> None:
        result = _check_wedge()
        assert result is None

    def test_does_not_raise(self) -> None:
        _check_wedge()  # should not raise


# ---------------------------------------------------------------------------
# TestPollerDeltaDetection
# ---------------------------------------------------------------------------


class TestPollerDeltaDetection:
    def test_new_task_produces_ticket_enqueued(self) -> None:
        old = QueueSnapshot()
        store = DevQueueStore(
            tasks=[_make_task("T-1", "acme", QueueItemStatus.PENDING)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert len(events) == 1
        assert events[0]["event"] == "queue.ticket_enqueued"
        assert events[0]["ticket_id"] == "T-1"
        assert events[0]["client"] == "acme"
        assert events[0]["status"] == "pending"

    def test_pending_to_running_produces_ticket_claimed(self) -> None:
        old = QueueSnapshot(task_statuses={"T-2": "pending"})
        store = DevQueueStore(
            tasks=[
                _make_task(
                    "T-2", "acme", QueueItemStatus.RUNNING, session_id="sess-abc"
                )
            ]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert len(events) == 1
        assert events[0]["event"] == "queue.ticket_claimed"
        assert events[0]["ticket_id"] == "T-2"
        assert events[0]["session_id"] == "sess-abc"

    def test_running_to_completed_produces_ticket_completed(self) -> None:
        old = QueueSnapshot(
            task_statuses={"T-3": "running"},
            task_session_ids={"T-3": "sess-xyz"},
        )
        session = _make_session(
            "sess-xyz",
            SessionStatus.COMPLETED,
            last_result={"status": "success"},
        )
        store = DevQueueStore(
            tasks=[
                _make_task(
                    "T-3", "acme", QueueItemStatus.COMPLETED, session_id="sess-xyz"
                )
            ]
        )
        state = CwState(sessions=[session])
        events = _compute_queue_deltas(old, store, state)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "queue.ticket_completed"
        assert ev["ticket_id"] == "T-3"
        assert ev["queue_status"] == "completed"
        assert ev["sentinel_status"] == "success"

    def test_running_to_completed_sentinel_status_null_when_no_session(self) -> None:
        old = QueueSnapshot(task_statuses={"T-4": "running"})
        store = DevQueueStore(
            tasks=[_make_task("T-4", "acme", QueueItemStatus.COMPLETED)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert events[0]["sentinel_status"] is None

    def test_running_to_failed_produces_ticket_failed(self) -> None:
        old = QueueSnapshot(
            task_statuses={"T-5": "running"},
            task_session_ids={"T-5": "sess-err"},
        )
        session = _make_session(
            "sess-err",
            SessionStatus.COMPLETED,
            last_result={"status": "failed", "blocker": {"reason": "build broke"}},
        )
        store = DevQueueStore(
            tasks=[
                _make_task(
                    "T-5",
                    "acme",
                    QueueItemStatus.FAILED,
                    session_id="sess-err",
                    attempts=3,
                )
            ]
        )
        state = CwState(sessions=[session])
        events = _compute_queue_deltas(old, store, state)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "queue.ticket_failed"
        assert ev["ticket_id"] == "T-5"
        assert ev["error"] == "build broke"
        assert ev["attempts"] == 3

    def test_running_to_failed_error_null_when_no_blocker(self) -> None:
        old = QueueSnapshot(task_statuses={"T-6": "running"})
        store = DevQueueStore(
            tasks=[_make_task("T-6", "acme", QueueItemStatus.FAILED, attempts=1)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert events[0]["error"] is None

    def test_running_to_cancelled_produces_no_event(self) -> None:
        old = QueueSnapshot(task_statuses={"T-7": "running"})
        store = DevQueueStore(
            tasks=[_make_task("T-7", "acme", QueueItemStatus.CANCELLED)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert events == []

    def test_running_to_blocked_on_user_produces_no_event(self) -> None:
        old = QueueSnapshot(task_statuses={"T-8": "running"})
        store = DevQueueStore(
            tasks=[_make_task("T-8", "acme", QueueItemStatus.BLOCKED_ON_USER)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert events == []

    def test_known_task_with_no_state_change_produces_no_event(self) -> None:
        old = QueueSnapshot(task_statuses={"T-9": "pending"})
        store = DevQueueStore(
            tasks=[_make_task("T-9", "acme", QueueItemStatus.PENDING)]
        )
        state = CwState()
        events = _compute_queue_deltas(old, store, state)
        assert events == []


# ---------------------------------------------------------------------------
# TestPollerSessionDeltaDetection
# ---------------------------------------------------------------------------


class TestPollerSessionDeltaDetection:
    def test_active_to_idle_produces_session_idled(self) -> None:
        sess = _make_session("s1", SessionStatus.IDLE, name="acme/impl")
        old = QueueSnapshot(session_statuses={"s1": "active"})
        state = CwState(sessions=[sess])
        events = _compute_session_deltas(old, state)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "queue.session_idled"
        assert ev["session_id"] == "s1"
        assert ev["session_name"] == "acme/impl"

    def test_idle_to_completed_produces_no_event(self) -> None:
        sess = _make_session("s2", SessionStatus.COMPLETED)
        old = QueueSnapshot(session_statuses={"s2": "idle"})
        state = CwState(sessions=[sess])
        events = _compute_session_deltas(old, state)
        assert events == []

    def test_new_active_session_produces_no_event(self) -> None:
        sess = _make_session("s3", SessionStatus.ACTIVE)
        old = QueueSnapshot()
        state = CwState(sessions=[sess])
        events = _compute_session_deltas(old, state)
        assert events == []

    def test_active_to_active_no_event(self) -> None:
        sess = _make_session("s4", SessionStatus.ACTIVE)
        old = QueueSnapshot(session_statuses={"s4": "active"})
        state = CwState(sessions=[sess])
        events = _compute_session_deltas(old, state)
        assert events == []


# ---------------------------------------------------------------------------
# TestQueueSnapshotPersistence
# ---------------------------------------------------------------------------


class TestQueueSnapshotPersistence:
    def test_load_snapshot_returns_empty_when_missing(self) -> None:
        snap = _load_snapshot()
        assert snap.task_statuses == {}
        assert snap.session_statuses == {}

    def test_save_and_load_round_trip(self) -> None:
        snap = QueueSnapshot(
            task_statuses={"T-1": "running"},
            task_session_ids={"T-1": "sess1"},
            session_statuses={"s1": "active"},
        )
        _save_snapshot(snap)
        loaded = _load_snapshot()
        assert loaded.task_statuses == snap.task_statuses
        assert loaded.task_session_ids == snap.task_session_ids
        assert loaded.session_statuses == snap.session_statuses

    def test_snapshot_path_is_under_channels_subdir(self) -> None:
        from cw.config import state_dir

        snap = QueueSnapshot(task_statuses={"T-1": "pending"})
        _save_snapshot(snap)
        expected_path = state_dir() / "channels" / "queue-events.snapshot.json"
        assert expected_path.exists()

    def test_load_snapshot_returns_empty_on_corrupt_file(self) -> None:
        from cw.config import state_dir

        path = state_dir() / "channels" / "queue-events.snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json")
        snap = _load_snapshot()
        assert snap.task_statuses == {}


# ---------------------------------------------------------------------------
# TestLazyStarlette
# ---------------------------------------------------------------------------


class TestLazyStarlette:
    def test_module_import_does_not_require_starlette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib
        import sys

        for key in (
            "starlette",
            "starlette.applications",
            "starlette.responses",
            "starlette.routing",
            "starlette.requests",
        ):
            monkeypatch.setitem(sys.modules, key, None)

        mod_name = "cw.cw_queue_events_server"
        original_mod = sys.modules.pop(mod_name, None)
        try:
            mod = importlib.import_module(mod_name)
            assert mod is not None
        finally:
            sys.modules.pop(mod_name, None)
            if original_mod is not None:
                sys.modules[mod_name] = original_mod

    def test_make_app_raises_clear_importerror_without_starlette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "starlette.applications", None)

        with pytest.raises(ImportError, match=r"channel server requires \[mcp\] extra"):
            _server_mod.make_app()


# ---------------------------------------------------------------------------
# TestDurableReplayQueueChannel
# ---------------------------------------------------------------------------


class TestDurableReplayQueueChannel:
    def test_five_events_survive_subscriber_restart(self) -> None:
        """Primary acceptance: 5 events survive subscribe/unsubscribe cycle."""
        for i in range(5):
            broadcast(
                {
                    "notification_type": _NOTIFICATION_TYPE,
                    "message": f"msg{i}",
                    "title": f"t{i}",
                }
            )

        q = subscribe_with_cursor("dispatcher")
        try:
            items = []
            while not q.empty():
                items.append(q.get_nowait())

            assert len(items) == 5
            assert {item["offset"] for item in items} == {0, 1, 2, 3, 4}
        finally:
            unsubscribe(q)


# ---------------------------------------------------------------------------
# TestLoadOffsetFromFile
# ---------------------------------------------------------------------------


class TestLoadOffsetFromFile:
    """Deterministic coverage for _load_offset_from_file parse loop.

    Without an explicit populated-file test, lines 134-146 are only covered
    incidentally when a prior test leaves a file behind — which fails on a
    clean ubuntu run where the early return (no file) fires first.
    """

    def test_returns_zero_when_file_missing(self) -> None:
        from cw.config import state_dir

        assert not (state_dir() / "queue-channel-events.jsonl").exists()
        assert _load_offset_from_file() == 0

    def test_returns_max_offset_plus_one_skipping_blank_and_malformed(self) -> None:
        from cw.config import state_dir

        path = state_dir() / "queue-channel-events.jsonl"
        lines = [
            json.dumps({"offset": 2, "message": "a"}),
            "",  # blank line -> continue (137-138)
            json.dumps({"offset": 5, "message": "b"}),
            "not-json",  # malformed -> JSONDecodeError continue (144-145)
            json.dumps({"offset": 3, "message": "c"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        assert _load_offset_from_file() == 6

    def test_returns_zero_when_only_blank_and_malformed_lines(self) -> None:
        from cw.config import state_dir

        path = state_dir() / "queue-channel-events.jsonl"
        path.write_text("\n   \nnot-json\n")
        assert _load_offset_from_file() == 0

    def test_ignores_non_int_offset(self) -> None:
        from cw.config import state_dir

        path = state_dir() / "queue-channel-events.jsonl"
        lines = [
            json.dumps({"offset": "bad", "message": "a"}),
            json.dumps({"offset": 4, "message": "b"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        assert _load_offset_from_file() == 5
