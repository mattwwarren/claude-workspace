"""Tests for the generic EventBus core (src/cw/event_bus.py).

Exercises the append/subscribe/broadcast/cursor machinery directly, without
going through cw_queue_events_server. Each test constructs a fresh, local
EventBus so there is no module-level shared state to reset between tests.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

import cw.event_bus as event_bus_mod
from cw.config import state_dir
from cw.event_bus import EventBus

if TYPE_CHECKING:
    from pathlib import Path

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

        def _subscriber() -> list[dict[str, Any]]:
            q = bus.subscribe_with_cursor("toctou-queue-sub")
            items: list[dict[str, Any]] = []
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    items.append(q.get_nowait())
                except queue.Empty:
                    time.sleep(0.01)
            bus.unsubscribe(q)
            return items

        # ThreadPoolExecutor + future.result() lets any subscriber-thread
        # exception propagate to the test naturally, so no exception handling
        # is needed here at all (R10: zero suppressions).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_subscriber)
            bus.broadcast({"message": "concurrent", "title": "c"})
            items = future.result(timeout=5)

        assert items, "Subscriber produced no results"

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


def _write_padded_jsonl(path: Path, target_bytes: int) -> int:
    """Write monotonically-offset JSON lines until at least target_bytes on disk.

    Returns the expected ``load_offset_from_file()`` result (the offset of the
    last line written, plus one).
    """
    written = 0
    offset = 0
    with path.open("w") as f:
        while written < target_bytes:
            line = json.dumps({"offset": offset, "pad": "x" * 200}) + "\n"
            f.write(line)
            written += len(line)
            offset += 1
    return offset


class TestEventBusLoadOffsetBoundedReverseRead:
    """#1986: load_offset_from_file must not full-parse the channel log.

    These tests exercise the bounded reverse-read path directly through the
    public ``load_offset_from_file()`` API, and (for the I/O-bound tests)
    through the byte-counting-wrapper technique established in
    ``tests/test_events.py`` (``test_wait_for_event_does_not_reread_history``,
    ``test_tail_events_follow_does_not_reread_history``).
    """

    def test_falls_back_on_truncated_trailing_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": 0, "message": "a"})
            + "\n"
            + json.dumps({"offset": 1, "message": "b"})
            + "\n"
            + '{"offset": 2, "mess'  # truncated, no trailing newline
        )
        with caplog.at_level(logging.WARNING):
            result = bus.load_offset_from_file()
        assert result == 2
        assert any("skipping malformed line" in rec.message for rec in caplog.records)

    def test_falls_back_through_multiple_malformed_trailing_lines(self) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": 0, "message": "a"}) + "\n" + "garbage1\n" + "garbage2\n"
        )
        assert bus.load_offset_from_file() == 1

    def test_all_lines_malformed_returns_zero(self) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json\n\n   \nalso not json\n")
        assert bus.load_offset_from_file() == 0

    def test_last_valid_json_missing_offset_field_falls_back(self) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": 3, "message": "a"})
            + "\n"
            + json.dumps({"offset": "not-an-int", "message": "b"})
            + "\n"
            + json.dumps({"message": "c"})  # no offset field at all
            + "\n"
        )
        assert bus.load_offset_from_file() == 4

    def test_handles_line_spanning_chunk_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(event_bus_mod, "_REVERSE_READ_CHUNK_BYTES", 16)
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": 7, "message": "x" * 300}) + "\n")
        assert bus.load_offset_from_file() == 8

    def test_trailing_blank_lines_and_whitespace_are_skipped(self) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": 4, "message": "a"}) + "\n\n   \n\n")
        assert bus.load_offset_from_file() == 5

    def test_falls_back_on_truncated_mid_multibyte_utf8_trailing_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        second = json.dumps({"offset": 1, "note": "café"}, ensure_ascii=False)
        second_bytes = second.encode("utf-8")
        # "é" encodes as 0xC3 0xA9; keep only the leading byte so the trailing
        # line ends mid-character, as a crash mid-append_event would leave it.
        cut_at = second_bytes.index("é".encode("utf-8")) + 1
        truncated = second_bytes[:cut_at]
        with path.open("wb") as f:
            f.write((json.dumps({"offset": 0, "message": "a"}) + "\n").encode("utf-8"))
            f.write(truncated)  # no trailing newline
        with caplog.at_level(logging.WARNING):
            result = bus.load_offset_from_file()
        assert result == 1
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_bounded_io_not_proportional_to_file_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = _make_bus()
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        bytes_read: list[int] = []
        real_reader = event_bus_mod._read_reverse_chunk

        def counting_reader(f: Any, end: int, size: int) -> bytes:
            chunk = real_reader(f, end, size)
            bytes_read.append(len(chunk))
            return chunk

        monkeypatch.setattr(event_bus_mod, "_read_reverse_chunk", counting_reader)

        for target_bytes in (1_000_000, 20_000_000):
            bytes_read.clear()
            expected = _write_padded_jsonl(path, target_bytes)
            result = bus.load_offset_from_file()
            total = sum(bytes_read)
            assert result == expected
            assert total <= 4 * event_bus_mod._REVERSE_READ_CHUNK_BYTES, (
                f"read {total} bytes for a {path.stat().st_size}-byte file "
                f"(target {target_bytes})"
            )

    def test_bounded_io_against_live_sized_channel_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded I/O against synthetic files sized like real channel logs.

        ~27MB / ~15MB approximate the observed on-disk sizes of
        queue-channel-events.jsonl and operator-channel-events.jsonl — the
        two live logs this loader runs against every daemon startup.
        """
        bytes_read: list[int] = []
        real_reader = event_bus_mod._read_reverse_chunk

        def counting_reader(f: Any, end: int, size: int) -> bytes:
            chunk = real_reader(f, end, size)
            bytes_read.append(len(chunk))
            return chunk

        monkeypatch.setattr(event_bus_mod, "_read_reverse_chunk", counting_reader)

        cases = [
            (_make_bus(), 27_000_000),
            (
                EventBus(
                    events_file="operator-channel-events.jsonl",
                    cursors_file="operator-channel-cursors.json",
                    log_label="operator-events",
                ),
                15_000_000,
            ),
        ]
        for bus, target_bytes in cases:
            path = state_dir() / bus.events_file
            path.parent.mkdir(parents=True, exist_ok=True)
            expected = _write_padded_jsonl(path, target_bytes)
            bytes_read.clear()
            result = bus.load_offset_from_file()
            total = sum(bytes_read)
            assert result == expected
            # Before/after evidence for #1986: pre-fix cost == file size
            # (full parse); post-fix cost is bounded by chunk size regardless
            # of file size.
            assert total <= 4 * event_bus_mod._REVERSE_READ_CHUNK_BYTES, (
                f"{bus.events_file}: read {total} bytes for a "
                f"{path.stat().st_size}-byte file (target {target_bytes})"
            )


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
