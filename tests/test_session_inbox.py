"""Tests for cw.session_inbox — the per-session operator mailbox (#2212)."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest

from cw import session_inbox
from cw.exceptions import CwError
from cw.models import SessionInboxMessage

if TYPE_CHECKING:
    from pathlib import Path

_SESSION = "abc12345"


class TestPaths:
    def test_inbox_path_is_session_scoped(self, tmp_config_dir: Path) -> None:
        path = session_inbox.inbox_path(_SESSION)
        assert path.parent == tmp_config_dir / ".local" / "share" / "cw" / "inboxes" / (
            _SESSION
        )
        assert path.name == "inbox.jsonl"

    def test_inbox_path_is_not_the_global_event_inbox(
        self, tmp_config_dir: Path
    ) -> None:
        from cw import events

        assert session_inbox.inbox_path(_SESSION) != events.inbox_path()

    def test_two_sessions_get_distinct_inboxes(self, tmp_config_dir: Path) -> None:
        assert session_inbox.inbox_path("aaaa1111") != session_inbox.inbox_path(
            "bbbb2222"
        )

    @pytest.mark.parametrize(
        "bad",
        ["../escape", "a/b", "", ".", "..", "with space", "semi;colon"],
    )
    def test_rejects_unsafe_session_id(self, tmp_config_dir: Path, bad: str) -> None:
        with pytest.raises(CwError):
            session_inbox.inbox_path(bad)


class TestAppendAndRead:
    def test_append_returns_validated_message(self, tmp_config_dir: Path) -> None:
        msg = session_inbox.append_message(_SESSION, author="matt", body="hello")
        assert isinstance(msg, SessionInboxMessage)
        assert msg.author == "matt"
        assert msg.body == "hello"
        assert msg.id
        assert msg.created_at.tzinfo is not None

    def test_round_trip_preserves_order(self, tmp_config_dir: Path) -> None:
        first = session_inbox.append_message(_SESSION, author="matt", body="one")
        second = session_inbox.append_message(_SESSION, author="matt", body="two")
        read = session_inbox.read_messages(_SESSION)
        assert [m.id for m in read] == [first.id, second.id]
        assert [m.body for m in read] == ["one", "two"]

    def test_message_ids_are_unique(self, tmp_config_dir: Path) -> None:
        ids = {
            session_inbox.append_message(_SESSION, author="m", body=str(i)).id
            for i in range(10)
        }
        assert len(ids) == 10

    def test_read_messages_on_missing_inbox_returns_empty(
        self, tmp_config_dir: Path
    ) -> None:
        assert session_inbox.read_messages("nosuchid") == []

    def test_append_is_jsonl(self, tmp_config_dir: Path) -> None:
        session_inbox.append_message(_SESSION, author="matt", body="hello")
        lines = session_inbox.inbox_path(_SESSION).read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["body"] == "hello"

    def test_concurrent_appends_do_not_corrupt(self, tmp_config_dir: Path) -> None:
        """Mirrors test_events.py's _inbox_lock coverage."""
        errors: list[BaseException] = []

        def _append(n: int) -> None:
            try:
                session_inbox.append_message(_SESSION, author="t", body=f"m{n}")
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=_append, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        read = session_inbox.read_messages(_SESSION)
        assert len(read) == 12
        assert {m.body for m in read} == {f"m{i}" for i in range(12)}

    def test_tolerates_malformed_trailing_line(self, tmp_config_dir: Path) -> None:
        session_inbox.append_message(_SESSION, author="matt", body="good")
        path = session_inbox.inbox_path(_SESSION)
        with path.open("a") as f:
            f.write('{"id": "torn", "body"')
        read = session_inbox.read_messages(_SESSION)
        assert [m.body for m in read] == ["good"]

    def test_interior_corruption_raises(self, tmp_config_dir: Path) -> None:
        session_inbox.append_message(_SESSION, author="matt", body="good")
        path = session_inbox.inbox_path(_SESSION)
        lines = path.read_text().splitlines()
        path.write_text("{not json\n" + lines[0] + "\n")
        with pytest.raises(json.JSONDecodeError):
            session_inbox.read_messages(_SESSION)


class TestCursor:
    def test_no_cursor_reads_everything(self, tmp_config_dir: Path) -> None:
        session_inbox.append_message(_SESSION, author="m", body="one")
        session_inbox.append_message(_SESSION, author="m", body="two")
        assert [m.body for m in session_inbox.read_unconsumed(_SESSION)] == [
            "one",
            "two",
        ]

    def test_advance_cursor_hides_consumed_messages(
        self, tmp_config_dir: Path
    ) -> None:
        first = session_inbox.append_message(_SESSION, author="m", body="one")
        session_inbox.append_message(_SESSION, author="m", body="two")
        session_inbox.advance_cursor(_SESSION, first.id)
        assert [m.body for m in session_inbox.read_unconsumed(_SESSION)] == ["two"]

    def test_load_cursor_round_trips(self, tmp_config_dir: Path) -> None:
        msg = session_inbox.append_message(_SESSION, author="m", body="one")
        assert session_inbox.load_cursor(_SESSION) is None
        session_inbox.advance_cursor(_SESSION, msg.id)
        assert session_inbox.load_cursor(_SESSION) == msg.id

    def test_replay_is_idempotent_via_cursor_not_truncation(
        self, tmp_config_dir: Path
    ) -> None:
        """Re-applying a consumed message changes the cursor file, not the inbox."""
        first = session_inbox.append_message(_SESSION, author="m", body="one")
        inbox_bytes = session_inbox.inbox_path(_SESSION).read_bytes()
        session_inbox.advance_cursor(_SESSION, first.id)
        assert session_inbox.read_unconsumed(_SESSION) == []
        # Inbox is append-only: consumption never rewrites it.
        assert session_inbox.inbox_path(_SESSION).read_bytes() == inbox_bytes
        session_inbox.advance_cursor(_SESSION, first.id)
        assert session_inbox.read_unconsumed(_SESSION) == []

    def test_unknown_cursor_replays_from_start(self, tmp_config_dir: Path) -> None:
        """At-least-once contract, mirroring events.read_events."""
        session_inbox.append_message(_SESSION, author="m", body="one")
        session_inbox.advance_cursor(_SESSION, "cursor-that-was-pruned")
        assert [m.body for m in session_inbox.read_unconsumed(_SESSION)] == ["one"]

    def test_cursor_is_session_scoped(self, tmp_config_dir: Path) -> None:
        first = session_inbox.append_message("aaaa1111", author="m", body="a")
        session_inbox.append_message("bbbb2222", author="m", body="b")
        session_inbox.advance_cursor("aaaa1111", first.id)
        assert session_inbox.read_unconsumed("aaaa1111") == []
        assert [m.body for m in session_inbox.read_unconsumed("bbbb2222")] == ["b"]


class TestSessionInboxMessageModel:
    def test_extra_fields_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SessionInboxMessage.model_validate(
                {
                    "id": "x",
                    "created_at": "2026-09-22T00:00:00Z",
                    "author": "m",
                    "body": "b",
                    "unexpected": 1,
                }
            )

    def test_naive_datetime_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SessionInboxMessage.model_validate(
                {
                    "id": "x",
                    "created_at": "2026-09-22T00:00:00",
                    "author": "m",
                    "body": "b",
                }
            )

    def test_empty_body_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SessionInboxMessage.model_validate(
                {
                    "id": "x",
                    "created_at": "2026-09-22T00:00:00Z",
                    "author": "m",
                    "body": "",
                }
            )

    def test_is_frozen(self) -> None:
        from pydantic import ValidationError

        msg = SessionInboxMessage.model_validate(
            {
                "id": "x",
                "created_at": "2026-09-22T00:00:00Z",
                "author": "m",
                "body": "b",
            }
        )
        with pytest.raises(ValidationError):
            msg.body = "changed"
