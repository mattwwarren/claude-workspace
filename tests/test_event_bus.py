"""Tests for the generic EventBus core (src/cw/event_bus.py).

Exercises the append/subscribe/broadcast/cursor machinery directly, without
going through cw_queue_events_server. Each test constructs a fresh, local
EventBus so there is no module-level shared state to reset between tests.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import TYPE_CHECKING, Any

from cw.config import state_dir
from cw.event_bus import EventBus

if TYPE_CHECKING:
    import pytest

_EVENTS_FILE = "queue-channel-events.jsonl"
_CURSORS_FILE = "queue-channel-cursors.json"
_LABEL = "queue-events"


def _make_bus() -> EventBus:
    """Build a fresh EventBus wired to the queue channel's file names."""
    return EventBus(
        events_file=_EVENTS_FILE,
        cursors_file=_CURSORS_FILE,
        log_label=_LABEL,
    )


class TestEventBusAppendAndRead:
    def test_append_persists_with_monotonic_offset(self) -> None:
        bus = _make_bus()
        bus.append_event({"message": "a"})
        bus.append_event({"message": "b"})

        path = state_dir() / _EVENTS_FILE
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["offset"] == 0
        assert second["offset"] == 1
        assert first["message"] == "a"
        assert bus.event_offset[0] == 2

    def test_concurrent_appends_are_thread_safe(self) -> None:
        bus = _make_bus()

        def _appender() -> None:
            for _ in range(20):
                bus.append_event({"message": "x"})

        threads = [threading.Thread(target=_appender) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert bus.event_offset[0] == 80
        records = bus.read_events_from_offset(0)
        offsets = sorted(r["offset"] for r in records)
        assert offsets == list(range(80))

    def test_read_from_offset_empty_when_no_file(self) -> None:
        bus = _make_bus()
        assert bus.read_events_from_offset(0) == []

    def test_read_from_offset_filters_below_cursor(self) -> None:
        bus = _make_bus()
        for i in range(5):
            bus.append_event({"message": str(i)})
        results = bus.read_events_from_offset(3)
        assert [r["offset"] for r in results] == [3, 4]

    def test_read_from_offset_skips_malformed_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": 0, "message": "a"})
            + "\n"
            + "not-json\n"
            + "\n"
            + json.dumps({"offset": 1, "message": "b"})
            + "\n"
        )
        with caplog.at_level(logging.WARNING):
            results = bus.read_events_from_offset(0)
        assert [r["offset"] for r in results] == [0, 1]
        assert any("skipping malformed line" in rec.message for rec in caplog.records)

    def test_locked_read_does_not_reacquire_file_lock(self) -> None:
        """read_events_from_offset_locked must run while caller holds file_lock.

        The public read acquires ``file_lock``; the locked variant must NOT,
        or ``subscribe_with_cursor`` (which calls it under ``file_lock``) would
        deadlock on the plain, non-reentrant ``threading.Lock``.
        """
        bus = _make_bus()
        bus.append_event({"message": "a"})
        with bus.file_lock:
            results = bus.read_events_from_offset_locked(0)
        assert [r["offset"] for r in results] == [0]


class TestEventBusSubscribeBroadcast:
    def test_subscribe_registers_queue(self) -> None:
        bus = _make_bus()
        q = bus.subscribe()
        assert q in bus.subscribers
        assert len(bus.subscribers) == 1

    def test_unsubscribe_removes_queue(self) -> None:
        bus = _make_bus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        assert q not in bus.subscribers

    def test_unsubscribe_unknown_queue_is_suppressed(self) -> None:
        bus = _make_bus()
        stray: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        # Must not raise ValueError even though stray was never subscribed.
        bus.unsubscribe(stray)
        assert stray not in bus.subscribers

    def test_broadcast_fans_out_to_all_subscribers(self) -> None:
        bus = _make_bus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.broadcast({"message": "hi"})
        assert q1.get_nowait()["message"] == "hi"
        assert q2.get_nowait()["message"] == "hi"

    def test_broadcast_persists_before_fanout(self) -> None:
        """broadcast() must append to disk BEFORE it fans out to subscribers."""
        bus = _make_bus()

        seen_on_disk: list[int] = []

        class _Recorder(queue.SimpleQueue[dict[str, Any]]):
            def put_nowait(self, item: dict[str, Any]) -> None:
                # When fan-out fires, the event must already be persisted.
                seen_on_disk.append(len(bus.read_events_from_offset(0)))
                super().put_nowait(item)

        rec = _Recorder()
        with bus.lock:
            bus.subscribers.append(rec)
        bus.broadcast({"message": "ordered"})
        assert seen_on_disk == [1]

    def test_broadcast_logs_subscriber_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        bus.subscribe()
        with caplog.at_level(logging.DEBUG):
            bus.broadcast({"message": "hi"})
        assert any(
            "queue-events broadcast to 1 subscribers" in rec.message
            for rec in caplog.records
        )


class TestEventBusSubscribeWithCursor:
    def test_replays_missed_events(self) -> None:
        bus = _make_bus()
        for i in range(3):
            bus.append_event({"offset_seed": i})
        q = bus.subscribe_with_cursor("client-a")
        replayed = []
        while not q.empty():
            replayed.append(q.get_nowait())
        assert [r["offset"] for r in replayed] == [0, 1, 2]

    def test_no_replay_when_caught_up(self) -> None:
        bus = _make_bus()
        for i in range(3):
            bus.append_event({"offset_seed": i})
        bus.cursors["client-a"] = 3
        q = bus.subscribe_with_cursor("client-a")
        assert q.empty()

    def test_registers_for_future_events(self) -> None:
        bus = _make_bus()
        q = bus.subscribe_with_cursor("client-a")
        assert q.empty()
        bus.broadcast({"message": "future"})
        assert q.get_nowait()["message"] == "future"

    def test_no_double_delivery_when_broadcast_races_subscribe(self) -> None:
        """Event broadcast during subscribe+replay gap must NOT be double-delivered.

        Direct port of
        ``TestSubscribeWithCursorTOCTOUQueueChannel
        .test_no_double_delivery_when_broadcast_races_subscribe`` (#433).
        """
        bus = _make_bus()

        # Pre-append one event at offset 0; cursor starts at 0 (full replay).
        bus.append_event({"message": "pre", "title": "pre"})

        results: list[list[dict[str, Any]]] = []
        errors: list[Exception] = []

        def _subscriber() -> None:
            import time

            try:
                q = bus.subscribe_with_cursor("toctou-queue-sub")
                items = []
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        items.append(q.get_nowait())
                    except Exception:  # noqa: BLE001
                        time.sleep(0.01)
                results.append(items)
                bus.unsubscribe(q)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        sub_thread = threading.Thread(target=_subscriber)
        sub_thread.start()

        bus.broadcast({"message": "concurrent", "title": "c"})

        sub_thread.join(timeout=5)
        assert not errors, f"Subscriber thread errored: {errors}"
        assert results, "Subscriber produced no results"

        items = results[0]
        offsets = [item.get("offset") for item in items]
        assert len(offsets) == len(set(offsets)), (
            f"Duplicate delivery detected: offsets={offsets}"
        )


class TestEventBusAckOffset:
    def test_persists_via_atomic_write(self) -> None:
        bus = _make_bus()
        bus.ack_offset("client-a", 7)
        path = state_dir() / _CURSORS_FILE
        assert json.loads(path.read_text()) == {"client-a": 7}

    def test_updates_in_memory_cursors_in_place(self) -> None:
        bus = _make_bus()
        cursors_ref = bus.cursors
        bus.ack_offset("client-a", 7)
        # Same object mutated, not replaced.
        assert bus.cursors is cursors_ref
        assert bus.cursors["client-a"] == 7

    def test_overwrites_previous_cursor(self) -> None:
        bus = _make_bus()
        bus.ack_offset("client-a", 3)
        bus.ack_offset("client-a", 9)
        assert bus.cursors["client-a"] == 9
        path = state_dir() / _CURSORS_FILE
        assert json.loads(path.read_text()) == {"client-a": 9}


class TestEventBusLoaders:
    def test_load_cursors_missing_returns_empty(self) -> None:
        bus = _make_bus()
        assert bus.load_cursors() == {}

    def test_load_cursors_reads_persisted(self) -> None:
        bus = _make_bus()
        path = state_dir() / _CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"client-a": 5, "client-b": 2}))
        assert bus.load_cursors() == {"client-a": 5, "client-b": 2}

    def test_load_cursors_corrupt_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        with caplog.at_level(logging.WARNING):
            assert bus.load_cursors() == {}
        assert any("corrupt" in rec.message for rec in caplog.records)

    def test_load_cursors_non_dict_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]))
        with caplog.at_level(logging.WARNING):
            assert bus.load_cursors() == {}
        assert any("unexpected shape" in rec.message for rec in caplog.records)

    def test_load_offset_missing_returns_zero(self) -> None:
        bus = _make_bus()
        assert bus.load_offset_from_file() == 0

    def test_load_offset_returns_max_plus_one(self) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": 2, "message": "a"})
            + "\n"
            + json.dumps({"offset": 5, "message": "b"})
            + "\n"
        )
        assert bus.load_offset_from_file() == 6


class TestEventBusPathsResolvedAtCallTime:
    def test_paths_resolved_fresh_each_call(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4: EventBus must resolve state_dir() at call time, never cache it.

        Construct the bus, THEN re-point STATE_DIR, and prove the append lands
        under the new directory — which only holds if __init__ stored the file
        *name* and each method recomputes the directory.
        """
        bus = _make_bus()
        new_state = tmp_path / "relocated"
        new_state.mkdir(parents=True)
        monkeypatch.setattr("cw.config.STATE_DIR", new_state)

        bus.append_event({"message": "relocated"})

        assert (new_state / _EVENTS_FILE).exists()
        records = bus.read_events_from_offset(0)
        assert records[0]["message"] == "relocated"


class TestEventBusTwoLocksNeverCollapsed:
    def test_lock_and_file_lock_are_distinct_plain_locks(self) -> None:
        """R5: subscriber lock and file lock are two separate, non-reentrant Locks."""
        bus = _make_bus()
        assert bus.lock is not bus.file_lock
        lock_type = type(threading.Lock())
        assert isinstance(bus.lock, lock_type)
        assert isinstance(bus.file_lock, lock_type)
