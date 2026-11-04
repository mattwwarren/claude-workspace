"""Tests for ``cw.events_tail.locate_after_event``: the backward cursor scan."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.events_tail import ANCHOR_BYTES, locate_after_event
from cw.models import OrchestratorEventType
from tests.conftest import _seed_inbox_with_cursor

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

    import pytest

_SMALL_BLOCK = 64


def _line_end(data: bytes, event_id: str) -> int:
    """Byte offset just past the newline ending the line with *event_id*."""
    needle = f'"id":"{event_id}"'.encode()
    start = data.index(needle)
    return data.index(b"\n", start) + 1


def _assert_located(path: Path, event_id: str) -> None:
    data = path.read_bytes()
    end = _line_end(data, event_id)
    assert locate_after_event(path, event_id) == (
        end,
        data[max(0, end - ANCHOR_BYTES) : end],
    )


def _raw_line(event_id: str, payload: object) -> bytes:
    event = {
        "id": event_id,
        "type": "pr.registered",
        "payload": payload,
        "correlation_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"


def test_cursor_in_middle(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(10, 5)
    path = tmp_events_dir / "inbox.jsonl"

    _assert_located(path, cursor)
    end, anchor = locate_after_event(path, cursor) or (0, b"")
    assert len(anchor) == ANCHOR_BYTES
    assert path.read_bytes()[:end].endswith(anchor)


def test_cursor_is_last_line(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(3, 0)
    path = tmp_events_dir / "inbox.jsonl"

    located = locate_after_event(path, cursor)

    assert located is not None
    assert located[0] == path.stat().st_size
    _assert_located(path, cursor)


def test_cursor_is_first_line(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(0, 4)
    path = tmp_events_dir / "inbox.jsonl"
    data = path.read_bytes()

    located = locate_after_event(path, cursor)

    assert located is not None
    end = data.index(b"\n") + 1
    assert located == (end, data[max(0, end - ANCHOR_BYTES) : end])


def test_unknown_id_returns_none(tmp_events_dir: Path) -> None:
    _seed_inbox_with_cursor(3, 3)

    assert locate_after_event(tmp_events_dir / "inbox.jsonl", "0" * 16) is None


def test_absent_inbox_returns_none(tmp_events_dir: Path) -> None:
    assert locate_after_event(tmp_events_dir / "inbox.jsonl", "abc") is None


def test_os_error_returns_none_silently(
    tmp_events_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory path raises ``IsADirectoryError``; it is swallowed, unlogged."""
    with caplog.at_level(logging.DEBUG, logger="cw.events_tail"):
        assert locate_after_event(tmp_events_dir, "abc") is None

    assert not [r for r in caplog.records if r.name == "cw.events_tail"]


def test_file_shrunk_after_sizing_returns_none(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short block read means the sized file changed; do not guess an offset."""
    import cw.events_tail as tail_mod

    cursor, _newer = _seed_inbox_with_cursor(2, 2)
    path = tmp_events_dir / "inbox.jsonl"
    real_read_block = tail_mod._read_block

    def truncate_then_read(handle: BinaryIO, start: int, end: int) -> bytes:
        # A concurrent rewrite shrinks the file between fstat and the read.
        with path.open("r+b") as writer:
            writer.truncate(start)
        return real_read_block(handle, start, end)

    monkeypatch.setattr(tail_mod, "_read_block", truncate_then_read)

    assert locate_after_event(path, cursor) is None


def test_unterminated_tail_is_ignored(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(2, 2)
    path = tmp_events_dir / "inbox.jsonl"
    complete = path.read_bytes()
    # A torn append that names the cursor id but has no newline yet.
    path.write_bytes(complete + _raw_line(cursor, {})[:-1])

    end = _line_end(complete, cursor)
    assert locate_after_event(path, cursor) == (
        end,
        complete[max(0, end - ANCHOR_BYTES) : end],
    )


def test_torn_tail_alone_is_not_a_match(tmp_events_dir: Path) -> None:
    path = tmp_events_dir / "inbox.jsonl"
    path.write_bytes(_raw_line("aaaaaaaaaaaaaaaa", {})[:-1])

    assert locate_after_event(path, "aaaaaaaaaaaaaaaa") is None


def test_false_needle_hit_in_payload_is_rejected(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(2, 1)
    path = tmp_events_dir / "inbox.jsonl"
    # A later event whose payload embeds the cursor id with the same spelling.
    record_event(OrchestratorEventType.PR_REGISTERED, {"nested": {"id": cursor}})
    data = path.read_bytes()
    # Precondition: the needle really does occur twice, once as a decoy.
    assert data.count(f'"id":"{cursor}"'.encode()) == 2

    _assert_located(path, cursor)


def test_malformed_candidate_is_skipped(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(2, 1)
    path = tmp_events_dir / "inbox.jsonl"
    # Interior garbage carrying the needle but not valid JSON.
    with path.open("ab") as handle:
        handle.write(f'{{"id":"{cursor}", broken\n'.encode())
    data = path.read_bytes()

    end = _line_end(data, cursor)
    assert locate_after_event(path, cursor) == (
        end,
        data[max(0, end - ANCHOR_BYTES) : end],
    )


def test_non_object_json_candidate_is_skipped(tmp_events_dir: Path) -> None:
    cursor, _newer = _seed_inbox_with_cursor(1, 0)
    path = tmp_events_dir / "inbox.jsonl"
    with path.open("ab") as handle:
        handle.write(f'["id":"{cursor}"]\n'.encode())
        handle.write(json.dumps([f'"id":"{cursor}"']).encode() + b"\n")

    _assert_located(path, cursor)


def test_small_blocks_reassemble_lines(
    tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines longer than a block, and lines straddling block edges, still match."""
    monkeypatch.setattr("cw.events_tail._BLOCK_BYTES", _SMALL_BLOCK)
    path = tmp_events_dir / "inbox.jsonl"
    cursor = "c" * 16
    lines = [_raw_line(f"{i:016d}", {"pad": "x" * (i * 7)}) for i in range(6)]
    lines.insert(3, _raw_line(cursor, {"pad": "y" * 300}))
    path.write_bytes(b"".join(lines))

    _assert_located(path, cursor)
    for i in range(6):
        _assert_located(path, f"{i:016d}")
