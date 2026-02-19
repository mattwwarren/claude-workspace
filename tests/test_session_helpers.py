"""Tests for session helper functions (_relative_time)."""

from __future__ import annotations

from datetime import UTC, datetime

from freezegun import freeze_time

from cw.session import _relative_time


class TestRelativeTime:
    def test_none_returns_unknown(self) -> None:
        assert _relative_time(None) == "unknown"

    @freeze_time("2025-01-15 12:00:00", tz_offset=0)
    def test_just_now(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "just now"

    @freeze_time("2025-01-15 12:00:30", tz_offset=0)
    def test_30_seconds_is_just_now(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "just now"

    @freeze_time("2025-01-15 12:00:59", tz_offset=0)
    def test_59_seconds_is_just_now(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "just now"

    @freeze_time("2025-01-15 12:01:00", tz_offset=0)
    def test_60_seconds_is_1m(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "1m ago"

    @freeze_time("2025-01-15 12:05:00", tz_offset=0)
    def test_5_minutes(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "5m ago"

    @freeze_time("2025-01-15 12:59:59", tz_offset=0)
    def test_59_minutes(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "59m ago"

    @freeze_time("2025-01-15 13:00:00", tz_offset=0)
    def test_1_hour(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "1h ago"

    @freeze_time("2025-01-15 15:00:00", tz_offset=0)
    def test_3_hours(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "3h ago"

    @freeze_time("2025-01-16 12:00:00", tz_offset=0)
    def test_1_day(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "1d ago"

    @freeze_time("2025-01-18 12:00:00", tz_offset=0)
    def test_3_days(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert _relative_time(dt) == "3d ago"
