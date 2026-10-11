"""Tests for cw._lock_hold — the sessions / dev-queue lock-hold recorder (#2637).

Mirrors ``src/cw/_lock_hold.py``. The end-to-end seam cases for each lock live
beside their lock (``test_sessions_lock.py``, ``test_dev_queue_lock.py``) and
the doctor check's tests live in ``test_doctor.py``; this file covers the
recorder itself: the recording rule, the flush, the sidecar writer and its cap
policy, the reader, the aggregate decision rule, the formatter, the kill
switch, and failure isolation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn

import pytest
from freezegun import freeze_time

from cw import _lock_hold
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw._lock_guard import LockRank, held_locks, lock_guard
from cw._lock_hold import (
    LockHoldRecord,
    LockHoldSummary,
    caller_label,
    chunk_lines,
    day_path,
    flush_pending,
    format_longest,
    note_hold,
    read_window,
    record_enabled_from_env,
    select_lines,
    summarize,
    timed_state_io,
    write_batch,
)
from cw.config import dev_queue_lock as dev_queue_lock_file
from cw.config import (
    load_state,
    mutate_state,
    sessions_lock,
    state_dir,
    state_file,
)
from cw.dev_queue import storage as dev_queue_storage
from cw.exceptions import (
    DEV_QUEUE_LOCK_NAME,
    SESSIONS_LOCK_NAME,
    CwLockReentrancyError,
    LockTimeoutError,
)
from tests.conftest import _FakeClock, _hold_flock, _seed_lock_holds

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_SMALL_CAP = 2000
_FIELDS = {
    "at",
    "lock",
    "held_s",
    "waited_s",
    "caller",
    "pid",
    "state_io_s",
    "state_bytes",
}


def _flush() -> None:
    flush_pending(state_dir, state_file)


def _today_path() -> Path:
    return day_path(state_dir(), datetime.now(UTC).date())


def _today_lines() -> list[dict[str, object]]:
    path = _today_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _recorded() -> list[LockHoldRecord]:
    return read_window(state_dir(), now=datetime.now(UTC)).records


def _record(**overrides: object) -> LockHoldRecord:
    fields: dict[str, object] = {
        "at": _NOW,
        "lock": SESSIONS_LOCK_NAME,
        "held_s": 1.0,
        "waited_s": 0.0,
        "caller": "tests.fake:caller",
        "pid": 1,
    }
    fields.update(overrides)
    return LockHoldRecord.model_validate(fields)


def _marker_line(stage: str, at: datetime) -> str:
    return json.dumps(
        {
            "kind": "capped",
            "stage": stage,
            "at": at.isoformat(),
            "cap_bytes": 1,
            "ceiling_bytes": 2,
            "detail": "seeded",
        }
    )


def _hold_sessions() -> None:
    with sessions_lock():
        pass


@pytest.fixture
def record_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every hold, however short, so tests do not depend on timing."""
    monkeypatch.setattr("cw._lock_hold.RECORD_MIN_S", 0.0)


# ---------------------------------------------------------------------------
# Recording rule
# ---------------------------------------------------------------------------


class TestRecordingRule:
    @pytest.mark.parametrize(
        ("held", "waited", "recorded"),
        [
            (0.01, 0.01, False),
            (0.30, 0.0, True),
            (0.01, 0.30, True),
            (0.30, 0.30, True),
        ],
    )
    def test_held_or_waited_threshold(
        self, held: float, waited: float, recorded: bool
    ) -> None:
        note_hold(SESSIONS_LOCK_NAME, held_s=held, waited_s=waited)
        _flush()
        assert _today_path().exists() is recorded

    @pytest.mark.parametrize("lock_name", ["clients", "concurrency_override"])
    def test_untracked_locks_are_ignored(self, lock_name: str) -> None:
        note_hold(lock_name, held_s=30.0, waited_s=30.0)
        _flush()
        assert not _today_path().exists()


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


class TestFlush:
    def test_one_line_per_note_with_all_fields(self) -> None:
        state_file().write_text('{"sessions": []}')
        size = state_file().stat().st_size
        note_hold(SESSIONS_LOCK_NAME, held_s=0.5, waited_s=0.3)
        note_hold(DEV_QUEUE_LOCK_NAME, held_s=0.4, waited_s=0.0)
        _flush()

        lines = _today_lines()
        assert [line["lock"] for line in lines] == ["sessions", "dev_queue"]
        assert all(set(line) == _FIELDS for line in lines)
        sessions, dev_queue = lines
        assert sessions["held_s"] == 0.5
        assert sessions["waited_s"] == 0.3
        assert sessions["pid"] == os.getpid()
        assert sessions["state_bytes"] == size
        assert sessions["state_io_s"] == 0.0
        assert dev_queue["state_bytes"] is None
        assert dev_queue["state_io_s"] is None

    def test_state_bytes_none_when_state_file_absent(self) -> None:
        note_hold(SESSIONS_LOCK_NAME, held_s=0.5, waited_s=0.0)
        _flush()
        (line,) = _today_lines()
        assert line["state_bytes"] is None

    def test_floats_rounded_to_three_decimals(self) -> None:
        note_hold(SESSIONS_LOCK_NAME, held_s=0.123456, waited_s=0.98765)
        _flush()
        (line,) = _today_lines()
        assert line["held_s"] == 0.123
        assert line["waited_s"] == 0.988

    @pytest.mark.usefixtures("record_everything")
    def test_no_io_until_the_outermost_guarded_lock_releases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_flush = _lock_hold.flush_pending
        calls: list[tuple[tuple[str, ...], bool]] = []

        def spy(
            sink_dir: Callable[[], Path], state_file_fn: Callable[[], Path]
        ) -> None:
            calls.append((tuple(h.name for h in held_locks()), _today_path().exists()))
            real_flush(sink_dir, state_file_fn)

        monkeypatch.setattr("cw._lock_hold.flush_pending", spy)

        with sessions_lock():
            with dev_queue_storage.dev_queue_lock():
                pass
            assert calls == [(("sessions",), False)]
            assert not _today_path().exists()

        assert calls == [(("sessions",), False), ((), False)]
        assert sorted(line["lock"] for line in _today_lines()) == [
            "dev_queue",
            "sessions",
        ]

    def test_write_failure_is_swallowed_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="cw._lock_hold")
        not_a_dir = tmp_path / "regular-file"
        not_a_dir.write_text("")
        note_hold(SESSIONS_LOCK_NAME, held_s=1.0, waited_s=0.0)

        flush_pending(lambda: not_a_dir, state_file)

        assert any("flush_pending failed" in r.getMessage() for r in caplog.records)
        # The batch was taken before the failed write: nothing retries.
        _flush()
        assert not _today_path().exists()


# ---------------------------------------------------------------------------
# Caller label
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("record_everything")
class TestCallerLabel:
    def test_sessions_lock_from_a_helper(self) -> None:
        _hold_sessions()
        (record,) = _recorded()
        assert record.caller == "tests.test_lock_hold:_hold_sessions"

    def test_mutate_state_labels_the_outer_caller(self) -> None:
        mutate_state(lambda _state: None)
        (record,) = _recorded()
        assert record.caller == (
            "tests.test_lock_hold:"
            "TestCallerLabel.test_mutate_state_labels_the_outer_caller"
        )

    def test_dev_queue_lock_labels_the_caller(self) -> None:
        with dev_queue_storage.dev_queue_lock():
            pass
        (record,) = _recorded()
        assert record.lock == "dev_queue"
        assert record.caller == (
            "tests.test_lock_hold:TestCallerLabel.test_dev_queue_lock_labels_the_caller"
        )

    def test_depth_cap_falls_back_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw._lock_hold._FRAME_DEPTH_CAP", 1)
        assert caller_label() == "unknown"

    def test_long_label_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw._lock_hold.CALLER_MAX_CHARS", 10)
        label = caller_label()
        assert label == "tests.test_lock_hold:"[:10]

    def test_no_frame_falls_back_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw._lock_hold.inspect", SimpleNamespace(currentframe=lambda: None)
        )
        assert caller_label() == "unknown"


# ---------------------------------------------------------------------------
# Dated files and cleanup
# ---------------------------------------------------------------------------


class TestDatedFiles:
    @freeze_time("2026-10-11 12:00:00")
    def test_day_file_named_by_utc_date_and_old_files_unlinked(self) -> None:
        sink = state_dir()
        for day in ("2026-10-10", "2026-10-09", "2026-10-08"):
            (sink / f"lock_holds.{day}.jsonl").write_text("")
        (sink / "lock_holds.not-a-date.jsonl").write_text("")

        write_batch(sink, [_record()], datetime.now(UTC))

        assert sorted(p.name for p in sink.glob("lock_holds.*.jsonl")) == [
            "lock_holds.2026-10-10.jsonl",
            "lock_holds.2026-10-11.jsonl",
            "lock_holds.not-a-date.jsonl",
        ]

    @freeze_time("2026-10-11 12:00:00")
    def test_nothing_unlinked_when_day_file_already_exists(self) -> None:
        sink = state_dir()
        (sink / "lock_holds.2026-10-11.jsonl").write_text("")
        (sink / "lock_holds.2026-10-01.jsonl").write_text("")

        write_batch(sink, [_record()], datetime.now(UTC))

        assert (sink / "lock_holds.2026-10-01.jsonl").exists()


# ---------------------------------------------------------------------------
# Cap policy
# ---------------------------------------------------------------------------


def _parsed(lines: list[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in lines]


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """Shrink the per-day cap so a few records cross it; return the ceiling."""
    monkeypatch.setattr("cw._lock_hold.MAX_FILE_BYTES", _SMALL_CAP)
    return _SMALL_CAP * _lock_hold.CEILING_FACTOR


class TestCapPolicy:
    def test_below_cap_every_record_accepted(self, small_cap: int) -> None:
        lines = select_lines([_record(), _record()], 0, _NOW)
        assert [line["lock"] for line in _parsed(lines)] == ["sessions"] * 2

    def test_record_crossing_cap_is_followed_by_one_marker(
        self, small_cap: int
    ) -> None:
        lines = select_lines([_record(), _record()], _SMALL_CAP - 10, _NOW)
        parsed = _parsed(lines)
        assert parsed[0]["lock"] == "sessions"
        assert parsed[1]["kind"] == "capped"
        assert parsed[1]["stage"] == "cap"
        assert parsed[1]["cap_bytes"] == _SMALL_CAP
        assert parsed[1]["ceiling_bytes"] == small_cap
        # The second, short record is past the cap and dropped.
        assert len(parsed) == 2

    @pytest.mark.parametrize(
        ("held", "waited", "kept"),
        [
            (1.0, 0.0, False),
            (_lock_hold.WARN_HELD_S, 0.0, True),
            (0.01, 30.0, True),
        ],
    )
    def test_past_cap_only_warn_level_records_kept(
        self, small_cap: int, held: float, waited: float, kept: bool
    ) -> None:
        lines = select_lines(
            [_record(held_s=held, waited_s=waited)], _SMALL_CAP + 100, _NOW
        )
        assert bool(lines) is kept

    def test_record_crossing_ceiling_gets_final_marker(self, small_cap: int) -> None:
        warn = _record(held_s=30.0)
        lines = select_lines([warn, warn], small_cap - 10, _NOW)
        parsed = _parsed(lines)
        assert len(parsed) == 2
        assert parsed[1]["kind"] == "capped"
        assert parsed[1]["stage"] == "ceiling"
        assert "nothing more" in str(parsed[1]["detail"])

    def test_nothing_written_past_ceiling(self, small_cap: int) -> None:
        assert select_lines([_record(held_s=30.0)], small_cap, _NOW) == []

    def test_file_stays_bounded_with_one_marker_per_stage(self, small_cap: int) -> None:
        sink = state_dir()
        small = _record()
        warn = _record(held_s=30.0)
        for _ in range(40):
            write_batch(sink, [small], _NOW)
        for _ in range(40):
            write_batch(sink, [warn], _NOW)

        path = day_path(sink, _NOW.date())
        raw = path.read_text().splitlines()
        parsed = _parsed(raw)
        stages = [line["stage"] for line in parsed if line.get("kind") == "capped"]
        assert stages == ["cap", "ceiling"]
        record_len = len(warn.model_dump_json()) + 1
        marker_len = max(len(line) + 1 for line in raw if '"capped"' in line)
        assert path.stat().st_size < small_cap + record_len + marker_len

    def test_chunk_lines_respects_limit_and_preserves_bytes(self) -> None:
        lines = [f"{'x' * 99}\n"] * 100
        chunks = chunk_lines(lines)
        assert all(len(chunk) <= _lock_hold.WRITE_CHUNK_BYTES for chunk in chunks)
        assert b"".join(chunks) == "".join(lines).encode()
        assert len(chunk_lines(lines, limit=250)) == 50

    def test_chunk_lines_empty(self) -> None:
        assert chunk_lines([]) == []


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class TestReadWindow:
    @freeze_time("2026-10-11 00:30:00")
    def test_merges_yesterday_and_today_within_window(self) -> None:
        now = datetime.now(UTC)
        _seed_lock_holds(
            {"at": now - timedelta(minutes=40), "caller": "yesterday"},
            {"at": now - timedelta(minutes=10), "caller": "today"},
            {"at": now - timedelta(hours=3), "caller": "too-old"},
        )
        window = read_window(state_dir(), now=now)
        assert sorted(r.caller for r in window.records) == ["today", "yesterday"]
        assert window.capped is False
        assert window.ceiling is False

    def test_malformed_and_truncated_lines_are_skipped(self) -> None:
        now = datetime.now(UTC)
        _seed_lock_holds(
            {"caller": "good"},
            raw_lines=[
                "not json",
                '{"lock": "sessions", "held_s"',
                "[1, 2]",
                '{"kind": "capped", "stage": "bogus"}',
                json.dumps(
                    {**_record().model_dump(mode="json"), "at": "2026-10-11T00:00:00"}
                ),
            ],
        )
        window = read_window(state_dir(), now=now)
        assert [r.caller for r in window.records] == ["good"]
        assert window.capped is False

    def test_absent_files_give_empty_window(self) -> None:
        window = read_window(state_dir(), now=datetime.now(UTC))
        assert window.records == []
        assert window.capped is False

    def test_unreadable_file_gives_empty_window(self) -> None:
        now = datetime.now(UTC)
        day_path(state_dir(), now.date()).mkdir(parents=True)
        assert read_window(state_dir(), now=now).records == []

    @pytest.mark.parametrize(("stage", "ceiling"), [("cap", False), ("ceiling", True)])
    def test_todays_marker_sets_capped(self, stage: str, ceiling: bool) -> None:
        now = datetime.now(UTC)
        _seed_lock_holds(raw_lines=[_marker_line(stage, now)])
        window = read_window(state_dir(), now=now)
        assert window.capped is True
        assert window.ceiling is ceiling

    @freeze_time("2026-10-11 12:00:00")
    def test_yesterday_marker_ignored_when_window_starts_today(self) -> None:
        now = datetime.now(UTC)
        yesterday = day_path(state_dir(), (now - timedelta(days=1)).date())
        yesterday.write_text(_marker_line("ceiling", now - timedelta(hours=13)) + "\n")
        window = read_window(state_dir(), now=now)
        assert window.capped is False
        assert window.ceiling is False

    @freeze_time("2026-10-11 00:30:00")
    def test_yesterday_marker_applies_when_window_reaches_into_yesterday(
        self,
    ) -> None:
        now = datetime.now(UTC)
        yesterday = day_path(state_dir(), (now - timedelta(days=1)).date())
        yesterday.write_text(_marker_line("cap", now - timedelta(hours=2)) + "\n")
        assert read_window(state_dir(), now=now).capped is True


# ---------------------------------------------------------------------------
# Aggregate decision rule (R2)
# ---------------------------------------------------------------------------


def _sessions(held: float, io: float | None, waited: float = 0.0) -> LockHoldRecord:
    return _record(held_s=held, state_io_s=io, waited_s=waited)


class TestSummarize:
    def test_aggregate_fraction_not_the_longest_records_ratio(self) -> None:
        b = _sessions(12.0, 9.0)  # longest record carrying state_io_s; ratio 0.75
        e = _sessions(100.0, None)  # the longest record overall; no io figure
        records = [
            _sessions(10.0, 0.0),
            b,
            _sessions(10.0, 0.0),
            _sessions(8.0, 0.0),
            e,
        ]
        (summary,) = summarize(records)
        assert summary.lock == "sessions"
        assert summary.count == 5
        assert summary.state_io_fraction == pytest.approx(9 / 40)
        assert summary.state_io_dominates is False
        assert summary.longest == e

    def test_half_exactly_dominates(self) -> None:
        (summary,) = summarize([_sessions(10.0, 6.0), _sessions(10.0, 4.0)])
        assert summary.state_io_fraction == pytest.approx(0.5)
        assert summary.state_io_dominates is True

    def test_just_below_half_does_not_dominate(self) -> None:
        (summary,) = summarize([_sessions(10.0, 4.999), _sessions(10.0, 5.0)])
        assert summary.state_io_dominates is False

    def test_dev_queue_has_no_fraction(self) -> None:
        (summary,) = summarize([_record(lock=DEV_QUEUE_LOCK_NAME, held_s=3.0)])
        assert summary.state_io_fraction is None
        assert summary.state_io_dominates is None

    def test_zero_held_gives_no_fraction(self) -> None:
        (summary,) = summarize([_sessions(0.0, 0.0)])
        assert summary.state_io_fraction is None
        assert summary.state_io_dominates is False

    def test_longest_and_longest_wait_and_order(self) -> None:
        waiter = _sessions(0.01, None, waited=45.0)
        holder = _sessions(20.0, None, waited=1.0)
        dev_queue = _record(lock=DEV_QUEUE_LOCK_NAME, held_s=2.0)
        sessions, dq = summarize([dev_queue, waiter, holder])
        assert sessions.lock == "sessions"
        assert sessions.count == 2
        assert sessions.longest == holder
        assert sessions.longest_wait == waiter
        assert dq.lock == "dev_queue"
        assert dq.count == 1

    def test_no_records_no_summaries(self) -> None:
        assert summarize([]) == []


# ---------------------------------------------------------------------------
# timed_state_io
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("record_everything")
class TestTimedStateIo:
    def test_accumulates_only_under_the_sessions_lock(
        self, lock_hold_clock: _FakeClock
    ) -> None:
        with timed_state_io():
            lock_hold_clock.now += 5.0
        with dev_queue_storage.dev_queue_lock(), timed_state_io():
            lock_hold_clock.now += 4.0
        with sessions_lock():
            lock_hold_clock.now += 1.0
            with timed_state_io():
                lock_hold_clock.now += 2.0

        sessions = [r for r in _recorded() if r.lock == "sessions"]
        (record,) = sessions
        assert record.held_s == 3.0
        assert record.state_io_s == 2.0

    def test_resets_after_each_sessions_release(
        self, lock_hold_clock: _FakeClock
    ) -> None:
        with sessions_lock(), timed_state_io():
            lock_hold_clock.now += 2.0
        with sessions_lock():
            lock_hold_clock.now += 1.0

        first, second = _recorded()
        assert first.state_io_s == 2.0
        assert second.state_io_s == 0.0

    def test_clamped_to_held(self, lock_hold_clock: _FakeClock) -> None:
        guard_path = state_dir() / ".fake-sessions.lock"
        with (
            lock_guard(SESSIONS_LOCK_NAME, guard_path, LockRank.SESSIONS),
            timed_state_io(),
        ):
            lock_hold_clock.now += 50.0
        note_hold(SESSIONS_LOCK_NAME, held_s=3.0, waited_s=0.0)
        _flush()
        (record,) = _recorded()
        assert record.state_io_s == 3.0

    def test_exception_passes_through(self) -> None:
        msg = "boom"
        with pytest.raises(KeyError, match=msg), timed_state_io():
            raise KeyError(msg)
        with (
            pytest.raises(KeyError, match=msg),
            sessions_lock(),
            timed_state_io(),
        ):
            raise KeyError(msg)

    def test_load_state_under_lock_records_state_io(self) -> None:
        with sessions_lock():
            load_state()
        (record,) = _recorded()
        assert record.state_io_s is not None


# ---------------------------------------------------------------------------
# Debug log
# ---------------------------------------------------------------------------


class TestDebugLog:
    def test_one_lazy_debug_record_per_noted_hold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="cw._lock_hold")
        note_hold(SESSIONS_LOCK_NAME, held_s=0.5, waited_s=0.1)
        note_hold(DEV_QUEUE_LOCK_NAME, held_s=0.01, waited_s=0.01)
        _flush()

        (record,) = [r for r in caplog.records if r.name == "cw._lock_hold"]
        assert record.levelno == logging.DEBUG
        assert record.args == (
            "sessions",
            0.5,
            0.1,
            "tests.test_lock_hold:TestDebugLog.test_one_lazy_debug_record_per_noted_hold",
            os.getpid(),
            0.0,
            None,
        )

    def test_debug_log_is_uncapped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="cw._lock_hold")
        monkeypatch.setattr("cw._lock_hold.MAX_FILE_BYTES", 0)
        note_hold(SESSIONS_LOCK_NAME, held_s=0.5, waited_s=0.0)
        _flush()
        assert len(caplog.records) == 1
        assert _today_lines() == []


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadLocal:
    def test_other_threads_notes_stay_in_that_thread(self) -> None:
        def other() -> None:
            note_hold(SESSIONS_LOCK_NAME, held_s=1.0, waited_s=0.0)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        _flush()
        assert not _today_path().exists()

    def test_pending_drops_oldest_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw._lock_hold._PENDING_CAP", 3)
        for held in (1.0, 2.0, 3.0, 4.0, 5.0):
            note_hold(SESSIONS_LOCK_NAME, held_s=held, waited_s=0.0)
        _flush()
        assert [line["held_s"] for line in _today_lines()] == [3.0, 4.0, 5.0]


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatLongest:
    def test_sessions_and_dev_queue(self) -> None:
        sessions = _record(
            at=_NOW - timedelta(minutes=14),
            held_s=12.4,
            state_io_s=9.0,
            caller="cw.dispatch.tick:dispatch_tick",
        )
        dev_queue = _record(
            at=_NOW - timedelta(minutes=3),
            lock=DEV_QUEUE_LOCK_NAME,
            held_s=0.3,
            caller="cw.dev_queue.crud:add_ticket",
        )
        text = format_longest(summarize([dev_queue, sessions]), _NOW)
        assert text == (
            "sessions 12.4s (cw.dispatch.tick:dispatch_tick, 14m ago;"
            " aggregate state load/save 73%);"
            " dev_queue 0.3s (cw.dev_queue.crud:add_ticket, 3m ago)"
        )

    def test_sessions_without_state_io_figure(self) -> None:
        text = format_longest(summarize([_sessions(2.0, None)]), _NOW)
        assert "aggregate state load/save n/a" in text

    def test_empty(self) -> None:
        assert format_longest([], _NOW) == ""

    def test_summary_without_a_longest_record(self) -> None:
        summary = LockHoldSummary(
            lock=SESSIONS_LOCK_NAME,
            count=0,
            longest=None,
            longest_wait=None,
            state_io_fraction=None,
            state_io_dominates=None,
        )
        assert format_longest([summary], _NOW) == "sessions n/a"


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    @pytest.mark.parametrize(
        ("environ", "enabled"),
        [
            ({"CW_LOCK_HOLD_RECORD": "0"}, False),
            ({}, True),
            ({"CW_LOCK_HOLD_RECORD": "1"}, True),
            ({"CW_LOCK_HOLD_RECORD": "off"}, True),
        ],
    )
    def test_record_enabled_from_env(
        self, environ: dict[str, str], enabled: bool
    ) -> None:
        assert record_enabled_from_env(environ) is enabled

    @pytest.mark.usefixtures("record_everything")
    def test_disabled_records_nothing_and_lock_still_works(
        self, monkeypatch: pytest.MonkeyPatch, lock_hold_clock: _FakeClock
    ) -> None:
        monkeypatch.setattr("cw._lock_hold.RECORD_ENABLED", False)
        state = mutate_state(lambda _state: None)
        guard_path = state_dir() / ".fake-sessions.lock"
        with (
            lock_guard(SESSIONS_LOCK_NAME, guard_path, LockRank.SESSIONS),
            timed_state_io(),
        ):
            lock_hold_clock.now += 5.0

        monkeypatch.setattr("cw._lock_hold.RECORD_ENABLED", True)
        _flush()
        assert not _today_path().exists()
        assert state.sessions == []
        # The disabled timer accumulated nothing for the next sessions note.
        note_hold(SESSIONS_LOCK_NAME, held_s=10.0, waited_s=0.0)
        _flush()
        (record,) = _recorded()
        assert record.state_io_s == 0.0


# ---------------------------------------------------------------------------
# Failure isolation (R4)
# ---------------------------------------------------------------------------


def _raise_value_error(*_args: object, **_kwargs: object) -> NoReturn:
    msg = "injected recorder failure"
    raise ValueError(msg)


class _AccumulateFailsClock:
    """``monotonic`` answers each ``timed_state_io`` start, raises on its stop."""

    def __init__(self) -> None:
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        if self.calls % 2 == 0:
            _raise_value_error()
        return 0.0


def _install_failure(point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if point == "accumulate":
        monkeypatch.setattr("cw._lock_hold.time", _AccumulateFailsClock())
        return
    monkeypatch.setattr(f"cw._lock_hold.{point}", _raise_value_error)


def _outcome_returns() -> int:
    return 42


def _outcome_raises() -> int:
    msg = "body failure"
    raise KeyError(msg)


def _outcome_reentry() -> int:
    with sessions_lock():
        return 0


def _outcome_timeout() -> int:
    with (
        _hold_flock(dev_queue_lock_file()),
        dev_queue_storage.dev_queue_lock(bounded=True),
    ):
        return 0


_OUTCOMES: dict[str, tuple[Callable[[], int], type[BaseException] | None]] = {
    "returns": (_outcome_returns, None),
    "raises": (_outcome_raises, KeyError),
    "reentry": (_outcome_reentry, CwLockReentrancyError),
    "timeout": (_outcome_timeout, LockTimeoutError),
}

_FAILURE_POINTS = ["caller_label", "write_batch", "is_rank_held", "accumulate"]


def _run_body(action: Callable[[], int]) -> int:
    with sessions_lock():
        load_state()
        with dev_queue_storage.dev_queue_lock():
            pass
        return action()


def _outcome_params() -> list[object]:
    params: list[object] = []
    for outcome in _OUTCOMES:
        marks = (
            [pytest.mark.lock_violations_expected("reentry")]
            if outcome == "reentry"
            else []
        )
        params.append(pytest.param(outcome, marks=marks, id=outcome))
    return params


@pytest.mark.usefixtures("record_everything")
class TestFailureIsolation:
    @pytest.mark.parametrize("outcome", _outcome_params())
    @pytest.mark.parametrize("point", _FAILURE_POINTS)
    def test_recorder_failure_never_changes_the_outcome(
        self,
        point: str,
        outcome: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="cw._lock_hold")
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.5")
        monkeypatch.setattr("cw._flock.time", _FakeClock())
        _install_failure(point, monkeypatch)
        action, expected = _OUTCOMES[outcome]

        if expected is None:
            assert _run_body(action) == 42
        else:
            with pytest.raises(expected):
                _run_body(action)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("lock-hold recorder") and "failed" in m for m in messages
        )
        # The lock is released and usable afterwards.
        _hold_sessions()

    def test_keyboard_interrupt_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupt(*_args: object, **_kwargs: object) -> NoReturn:
            raise KeyboardInterrupt

        real_caller_label = _lock_hold.caller_label
        monkeypatch.setattr("cw._lock_hold.caller_label", interrupt)
        with pytest.raises(KeyboardInterrupt):
            _hold_sessions()
        monkeypatch.setattr("cw._lock_hold.caller_label", real_caller_label)
        _hold_sessions()
        assert len(_recorded()) == 1
