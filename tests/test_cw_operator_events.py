"""Tests for cw_operator_events: operator-attention filter + inbox->SSE bridge.

RFC 0008 W3 (#1002). ``cw_operator_events`` is a sibling of
``cw_queue_events_server``/``cw_pr_events_server`` — its append/subscribe/
broadcast/cursor machinery is shared via ``EventBus`` (``cw.event_bus``,
#1303), mirroring that pair's own migration onto the same core; it still owns
its own subscriber registry and MCP route builder.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

import cw.cw_operator_events as _operator_mod
from cw.cw_operator_events import (
    _NOTIFICATION_TYPE,
    _OPERATOR_BRIDGE_CONSUMER,
    _admits,
    _build_operator_notification,
    ack_offset,
    broadcast,
    poll_and_forward_operator_channel,
    subscribe,
    subscribe_with_cursor,
    unsubscribe,
)
from cw.events import load_cursor, record_event
from cw.models import (
    LivenessBucket,
    OperatorChannelForward,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_operator_subscribers() -> Generator[None]:
    """Clear the operator-channel subscriber list between tests."""
    with _operator_mod._lock:
        _operator_mod._subscribers.clear()
    yield
    with _operator_mod._lock:
        _operator_mod._subscribers.clear()


@pytest.fixture(autouse=True)
def _reset_operator_channel_state() -> Generator[None]:
    """Reset durable-replay in-memory state between tests."""
    with _operator_mod._file_lock:
        _operator_mod._cursors.clear()
        _operator_mod._event_offset[0] = 0
    yield
    with _operator_mod._file_lock:
        _operator_mod._cursors.clear()
        _operator_mod._event_offset[0] = 0


# ---------------------------------------------------------------------------
# TestAdmitsFilterEngine
# ---------------------------------------------------------------------------


class TestAdmitsFilterEngine:
    def _default_forward(self) -> OperatorChannelForward:
        return OperatorChannelForward()

    def test_event_type_not_in_forward_set_dropped(self) -> None:
        forward = OperatorChannelForward(
            event_types=frozenset({OrchestratorEventType.TASK_DELETED})
        )
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION, payload={}
        )
        assert _admits(event, forward) is False

    @pytest.mark.parametrize(
        "status",
        [
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
        ],
    )
    def test_task_transition_admitted_for_default_statuses(
        self, status: QueueItemStatus
    ) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_TRANSITION,
            payload={"old_status": "running", "new_status": str(status)},
        )
        assert _admits(event, self._default_forward()) is True

    def test_task_transition_dropped_for_pending(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_TRANSITION,
            payload={"old_status": "pending", "new_status": "pending"},
        )
        assert _admits(event, self._default_forward()) is False

    def test_task_transition_dropped_for_running(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_TRANSITION,
            payload={"old_status": "pending", "new_status": "running"},
        )
        assert _admits(event, self._default_forward()) is False

    def test_task_transition_override_widens_admitted_set(self) -> None:
        forward = OperatorChannelForward(
            task_transition_statuses=frozenset({QueueItemStatus.RUNNING})
        )
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_TRANSITION,
            payload={"old_status": "pending", "new_status": "running"},
        )
        assert _admits(event, forward) is True

    def test_task_deleted_always_admitted(self) -> None:
        event = OrchestratorEvent(type=OrchestratorEventType.TASK_DELETED, payload={})
        assert _admits(event, self._default_forward()) is True

    def test_session_needs_attention_always_admitted(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION, payload={}
        )
        assert _admits(event, self._default_forward()) is True

    @pytest.mark.parametrize(
        "event_type",
        [
            OrchestratorEventType.PR_REGISTERED,
            OrchestratorEventType.PR_CI_FAILED,
            OrchestratorEventType.PR_REVIEW_RECEIVED,
            OrchestratorEventType.PR_MERGEABLE,
            OrchestratorEventType.PR_MERGED,
        ],
    )
    def test_pr_star_always_admitted(self, event_type: OrchestratorEventType) -> None:
        event = OrchestratorEvent(type=event_type, payload={})
        assert _admits(event, self._default_forward()) is True

    def test_operator_escalation_admitted_by_default(self) -> None:
        """OPERATOR_ESCALATION forwards unconditionally (#1015, Q3)."""
        event = OrchestratorEvent(
            type=OrchestratorEventType.OPERATOR_ESCALATION, payload={}
        )
        assert _admits(event, self._default_forward()) is True

    def test_requeue_review_delivery_degraded_admitted_by_default(self) -> None:
        """REQUEUE_REVIEW_DELIVERY_DEGRADED forwards unconditionally (#1730)."""
        event = OrchestratorEvent(
            type=OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED, payload={}
        )
        assert _admits(event, self._default_forward()) is True

    def test_concierge_recovered_not_admitted_by_default(self) -> None:
        """CONCIERGE_RECOVERED is audit-trail only — NOT in the default
        forward set (#1015, Q3)."""
        event = OrchestratorEvent(
            type=OrchestratorEventType.CONCIERGE_RECOVERED, payload={}
        )
        assert _admits(event, self._default_forward()) is False

    def test_liveness_changed_admitted_at_stale_30m(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "stale_30m"},
        )
        assert _admits(event, self._default_forward()) is True

    def test_liveness_changed_admitted_at_stale_45m(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "stale_45m"},
        )
        assert _admits(event, self._default_forward()) is True

    def test_liveness_changed_dropped_below_stale_30m(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "stale_15m"},
        )
        assert _admits(event, self._default_forward()) is False

    def test_liveness_changed_dropped_at_live(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "live"},
        )
        assert _admits(event, self._default_forward()) is False

    def test_liveness_changed_override_lowers_threshold(self) -> None:
        forward = OperatorChannelForward(liveness_min_bucket=LivenessBucket.STALE_15M)
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "stale_15m"},
        )
        assert _admits(event, forward) is True

    def test_liveness_changed_malformed_bucket_dropped(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={"new_bucket": "not-a-real-bucket"},
        )
        assert _admits(event, self._default_forward()) is False

    def test_liveness_changed_missing_bucket_dropped(self) -> None:
        """payload.get("new_bucket") returning None (non-str) is dropped."""
        event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            payload={},
        )
        assert _admits(event, self._default_forward()) is False


# ---------------------------------------------------------------------------
# TestBuildOperatorNotification
# ---------------------------------------------------------------------------


class TestBuildOperatorNotification:
    def test_notification_type_field(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_DELETED,
            payload={"ticket_id": "T-1"},
            correlation_id="T-1",
        )
        notif = _build_operator_notification(event)
        assert notif["notification_type"] == _NOTIFICATION_TYPE

    def test_message_field_is_valid_json_with_event_and_payload(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_DELETED,
            payload={"ticket_id": "T-1", "client": "acme"},
            correlation_id="T-1",
        )
        notif = _build_operator_notification(event)
        data = json.loads(notif["message"])
        assert data["event"] == "task.deleted"
        assert data["ticket_id"] == "T-1"
        assert data["client"] == "acme"
        assert data["correlation_id"] == "T-1"
        assert data["id"] == event.id

    def test_title_present(self) -> None:
        event = OrchestratorEvent(type=OrchestratorEventType.TASK_DELETED, payload={})
        notif = _build_operator_notification(event)
        assert isinstance(notif["title"], str)
        assert notif["title"]

    def test_title_includes_correlation_id(self) -> None:
        event = OrchestratorEvent(
            type=OrchestratorEventType.TASK_DELETED,
            payload={},
            correlation_id="T-99",
        )
        notif = _build_operator_notification(event)
        assert "T-99" in notif["title"]


# ---------------------------------------------------------------------------
# TestSubscriberRegistry (operator channel)
# ---------------------------------------------------------------------------


class TestSubscriberRegistry:
    def test_subscribe_adds_to_registry(self) -> None:
        q = subscribe()
        try:
            assert isinstance(q, queue.SimpleQueue)
        finally:
            unsubscribe(q)

    def test_unsubscribe_removes_from_registry(self, tmp_events_dir: Path) -> None:
        q = subscribe()
        unsubscribe(q)
        broadcast(
            {"notification_type": _NOTIFICATION_TYPE, "message": "m", "title": "t"}
        )
        assert q.empty()

    def test_broadcast_sends_to_all_queues(self, tmp_events_dir: Path) -> None:
        q1 = subscribe()
        q2 = subscribe()
        try:
            broadcast({"notification_type": _NOTIFICATION_TYPE, "x": 1})
            assert q1.get_nowait()["x"] == 1
            assert q2.get_nowait()["x"] == 1
        finally:
            unsubscribe(q1)
            unsubscribe(q2)


# ---------------------------------------------------------------------------
# TestAckOffsetOperatorChannel
# ---------------------------------------------------------------------------


class TestAckOffsetOperatorChannel:
    def test_persists_to_operator_channel_cursors_json(
        self, tmp_events_dir: Path
    ) -> None:
        from cw.config import state_dir

        ack_offset("sub-a", 3)
        path = state_dir() / "operator-channel-cursors.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"sub-a": 3}

    def test_updates_in_memory_cursors(self, tmp_events_dir: Path) -> None:
        ack_offset("sub-c", 7)
        assert _operator_mod._cursors["sub-c"] == 7


# ---------------------------------------------------------------------------
# TestSubscribeWithCursorOperatorChannel
# ---------------------------------------------------------------------------


class TestSubscribeWithCursorOperatorChannel:
    def test_replays_missed_events(self, tmp_events_dir: Path) -> None:
        broadcast({"notification_type": _NOTIFICATION_TYPE, "message": "a"})
        broadcast({"notification_type": _NOTIFICATION_TYPE, "message": "b"})

        q = subscribe_with_cursor("sub-op-1")
        try:
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            assert len(items) == 2
        finally:
            unsubscribe(q)


# ---------------------------------------------------------------------------
# TestPollAndForwardOperatorChannel
# ---------------------------------------------------------------------------


class TestPollAndForwardOperatorChannel:
    def test_empty_inbox_is_noop(self, tmp_events_dir: Path) -> None:
        config = OrchestratorConfig()
        poll_and_forward_operator_channel(config)
        assert load_cursor(_OPERATOR_BRIDGE_CONSUMER) is None

    def test_admitted_event_is_broadcast(self, tmp_events_dir: Path) -> None:
        record_event(
            OrchestratorEventType.TASK_DELETED,
            payload={"ticket_id": "T-1", "client": "acme"},
            correlation_id="T-1",
        )
        q = subscribe()
        try:
            config = OrchestratorConfig()
            poll_and_forward_operator_channel(config)
            item = q.get_nowait()
            data = json.loads(item["message"])
            assert data["event"] == "task.deleted"
            assert data["ticket_id"] == "T-1"
        finally:
            unsubscribe(q)

    def test_dropped_event_is_not_broadcast(self, tmp_events_dir: Path) -> None:
        record_event(
            OrchestratorEventType.TASK_TRANSITION,
            payload={
                "ticket_id": "T-2",
                "old_status": "pending",
                "new_status": "running",
            },
            correlation_id="T-2",
        )
        q = subscribe()
        try:
            config = OrchestratorConfig()
            poll_and_forward_operator_channel(config)
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_cursor_advances_past_dropped_events(self, tmp_events_dir: Path) -> None:
        """Cursor must advance past ALL read events, not just admitted ones."""
        record_event(
            OrchestratorEventType.TASK_TRANSITION,
            payload={
                "ticket_id": "T-3",
                "old_status": "pending",
                "new_status": "running",
            },
        )
        config = OrchestratorConfig()
        poll_and_forward_operator_channel(config)
        assert load_cursor(_OPERATOR_BRIDGE_CONSUMER) is not None

        # A second call with no new events must not re-emit the dropped one.
        q = subscribe()
        try:
            poll_and_forward_operator_channel(config)
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_config_override_changes_admitted_set(self, tmp_events_dir: Path) -> None:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"session_id": "s1"},
        )
        config = OrchestratorConfig(
            operator_channel_forward=OperatorChannelForward(
                event_types=frozenset({OrchestratorEventType.TASK_DELETED})
            )
        )
        q = subscribe()
        try:
            poll_and_forward_operator_channel(config)
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_multiple_events_all_advance_cursor(self, tmp_events_dir: Path) -> None:
        record_event(OrchestratorEventType.TASK_DELETED, payload={"ticket_id": "T-4"})
        last = record_event(
            OrchestratorEventType.TASK_DELETED, payload={"ticket_id": "T-5"}
        )
        config = OrchestratorConfig()
        poll_and_forward_operator_channel(config)
        assert load_cursor(_OPERATOR_BRIDGE_CONSUMER) == last.id


# ---------------------------------------------------------------------------
# TestAppendEventOperatorChannel
# ---------------------------------------------------------------------------


class TestAppendEventOperatorChannel:
    def test_appends_to_operator_channel_events_jsonl(
        self, tmp_events_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _append_event

        _append_event({"notification_type": _NOTIFICATION_TYPE, "message": "m"})
        path = state_dir() / "operator-channel-events.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1

    def test_thread_safe_under_concurrent_appends(self, tmp_events_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _append_event

        def worker() -> None:
            _append_event({"notification_type": _NOTIFICATION_TYPE, "message": "x"})

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = state_dir() / "operator-channel-events.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) == 10
        records = [json.loads(line) for line in lines]
        offsets = {r["offset"] for r in records}
        assert len(offsets) == 10


# ---------------------------------------------------------------------------
# TestReadEventsFromOffsetOperatorChannel
# ---------------------------------------------------------------------------


class TestReadEventsFromOffsetOperatorChannel:
    def test_returns_empty_for_missing_file(self, tmp_events_dir: Path) -> None:
        from cw.cw_operator_events import _read_events_from_offset

        assert _read_events_from_offset(0) == []

    def test_skips_blank_and_malformed_lines(self, tmp_events_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import (
            _OPERATOR_EVENTS_FILE,
            _read_events_from_offset,
        )

        path = state_dir() / _OPERATOR_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"notification_type": _NOTIFICATION_TYPE, "offset": 0})
            + "\n"
            + "\n"
            + "{ partial torn line\n"
        )
        result = _read_events_from_offset(0)
        assert len(result) == 1

    def test_respects_offset_filter(self, tmp_events_dir: Path) -> None:
        from cw.cw_operator_events import _append_event, _read_events_from_offset

        _append_event({"notification_type": _NOTIFICATION_TYPE, "message": "a"})
        _append_event({"notification_type": _NOTIFICATION_TYPE, "message": "b"})
        _append_event({"notification_type": _NOTIFICATION_TYPE, "message": "c"})
        result = _read_events_from_offset(1)
        assert len(result) == 2
        assert result[0]["offset"] == 1
        assert result[1]["offset"] == 2


# ---------------------------------------------------------------------------
# TestLoadCursorsOperatorChannel
# ---------------------------------------------------------------------------


class TestLoadCursorsOperatorChannel:
    def test_returns_empty_for_missing_file(self, tmp_events_dir: Path) -> None:
        from cw.cw_operator_events import _load_cursors

        assert _load_cursors() == {}

    def test_returns_empty_on_corrupt_json(self, tmp_events_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _OPERATOR_CURSORS_FILE, _load_cursors

        path = state_dir() / _OPERATOR_CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json")
        assert _load_cursors() == {}

    def test_returns_empty_on_unexpected_shape(self, tmp_events_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _OPERATOR_CURSORS_FILE, _load_cursors

        path = state_dir() / _OPERATOR_CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]))
        assert _load_cursors() == {}


# ---------------------------------------------------------------------------
# TestLoadOffsetFromFileOperatorChannel
# ---------------------------------------------------------------------------


class TestLoadOffsetFromFileOperatorChannel:
    def test_returns_zero_when_file_missing(self, tmp_events_dir: Path) -> None:
        from cw.cw_operator_events import _load_offset_from_file

        assert _load_offset_from_file() == 0

    def test_returns_max_offset_plus_one_skipping_blank_and_malformed(
        self, tmp_events_dir: Path
    ) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _OPERATOR_EVENTS_FILE, _load_offset_from_file

        path = state_dir() / _OPERATOR_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"offset": 2, "message": "a"}),
            "",
            json.dumps({"offset": 5, "message": "b"}),
            "not-json",
            json.dumps({"offset": 3, "message": "c"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        assert _load_offset_from_file() == 6

    def test_ignores_non_int_offset(self, tmp_events_dir: Path) -> None:
        from cw.config import state_dir
        from cw.cw_operator_events import _OPERATOR_EVENTS_FILE, _load_offset_from_file

        path = state_dir() / _OPERATOR_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"offset": "bad", "message": "a"}),
            json.dumps({"offset": 4, "message": "b"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        assert _load_offset_from_file() == 5
