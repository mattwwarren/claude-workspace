"""Tests for ``cw.events_follow``: the non-blocking follower and cursor records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.events import (
    _cursor_path,
    advance_cursor,
    inbox_path,
    load_cursor,
    prune_events,
    record_event,
)
from cw.events_follow import (
    InboxFollower,
    last_event_id,
    load_cursor_record,
    save_cursor_record,
)
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TYPE = OrchestratorEventType.PR_REGISTERED


def _record(n: int) -> OrchestratorEvent:
    return record_event(_TYPE, {"n": n})


def _ids(events: list[OrchestratorEvent]) -> list[str]:
    return [e.id for e in events]


class TestInboxFollower:
    def test_backlog_after_cursor_then_only_appended(
        self, tmp_events_dir: Path
    ) -> None:
        first = _record(1)
        second = _record(2)
        third = _record(3)
        follower = InboxFollower(since_cursor=first.id)

        assert _ids(follower.poll()) == [second.id, third.id]
        assert follower.cursor == third.id
        assert follower.poll() == []

        fourth = _record(4)
        assert _ids(follower.poll()) == [fourth.id]
        assert follower.cursor == fourth.id

    def test_cursor_is_seed_until_something_is_delivered(
        self, tmp_events_dir: Path
    ) -> None:
        only = _record(1)
        follower = InboxFollower(since_cursor=only.id)

        assert follower.cursor == only.id
        assert follower.poll() == []
        assert follower.cursor == only.id

    def test_torn_trailing_line_is_not_consumed(self, tmp_events_dir: Path) -> None:
        seed = _record(1)
        follower = InboxFollower(since_cursor=seed.id)
        assert follower.poll() == []

        torn = OrchestratorEvent(type=_TYPE, payload={"n": 2})
        with inbox_path().open("a") as handle:
            handle.write(torn.model_dump_json())
        assert follower.poll() == []

        with inbox_path().open("a") as handle:
            handle.write("\n")
        assert _ids(follower.poll()) == [torn.id]

    def test_inode_replace_with_surviving_cursor_does_not_replay(
        self, tmp_events_dir: Path
    ) -> None:
        _record(1)
        _record(2)
        third = _record(3)
        follower = InboxFollower(since_cursor=third.id)
        assert follower.poll() == []

        prune_events(keep=2, archive=False)
        assert follower.poll() == []

        fourth = _record(4)
        assert _ids(follower.poll()) == [fourth.id]

    def test_pruned_cursor_replays_survivors_exactly_once(
        self, tmp_events_dir: Path
    ) -> None:
        _record(1)
        second = _record(2)
        follower = InboxFollower(since_cursor=second.id)
        assert follower.poll() == []

        third = _record(3)
        fourth = _record(4)
        prune_events(keep=2, archive=False)

        assert _ids(follower.poll()) == [third.id, fourth.id]
        assert follower.poll() == []

    def test_failed_stat_changes_nothing(
        self, tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = _record(1)
        follower = InboxFollower(since_cursor=seed.id)
        assert follower.poll() == []
        _record(2)

        monkeypatch.setattr("cw.events._stat_inbox", lambda: None)

        assert follower.poll() == []
        assert follower.cursor == seed.id

    def test_no_cursor_on_empty_inbox_delivers_first_append(
        self, tmp_events_dir: Path
    ) -> None:
        follower = InboxFollower(since_cursor=None)
        assert follower.poll() == []
        assert follower.cursor is None

        first = _record(1)
        assert _ids(follower.poll()) == [first.id]
        assert follower.cursor == first.id


class TestLastEventId:
    def test_empty_inbox_is_none(self, tmp_events_dir: Path) -> None:
        assert last_event_id() is None

    def test_returns_newest_event_id(self, tmp_events_dir: Path) -> None:
        _record(1)
        newest = _record(2)
        assert last_event_id() == newest.id


class TestCursorRecord:
    def test_round_trip_with_extra(self, tmp_events_dir: Path) -> None:
        extra: dict[str, object] = {"latch": {"k": "v"}, "owner_pid": 42}
        save_cursor_record("attention-x", "abc", extra)

        record = load_cursor_record("attention-x")

        assert record is not None
        assert record["cursor"] == "abc"
        assert record["extra"] == extra
        assert isinstance(record["updated_at"], str)

    def test_existing_load_cursor_reads_a_record_file(
        self, tmp_events_dir: Path
    ) -> None:
        save_cursor_record("attention-x", "abc", {"owner_pid": 1})
        assert load_cursor("attention-x") == "abc"

    def test_plain_advance_cursor_file_loads_without_extra(
        self, tmp_events_dir: Path
    ) -> None:
        advance_cursor("plain", "def")

        record = load_cursor_record("plain")

        assert record is not None
        assert record["cursor"] == "def"
        assert "extra" not in record

    def test_missing_file_is_none(self, tmp_events_dir: Path) -> None:
        assert load_cursor_record("nobody") is None

    def test_corrupt_json_is_none(self, tmp_events_dir: Path) -> None:
        path = _cursor_path("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert load_cursor_record("broken") is None

    def test_non_object_json_is_none(self, tmp_events_dir: Path) -> None:
        path = _cursor_path("listy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["cursor"]))
        assert load_cursor_record("listy") is None

    def test_null_event_id_is_accepted(self, tmp_events_dir: Path) -> None:
        save_cursor_record("attention-empty", None, {"armed_empty": True})

        record = load_cursor_record("attention-empty")

        assert record is not None
        assert record["cursor"] is None
        assert load_cursor("attention-empty") is None
