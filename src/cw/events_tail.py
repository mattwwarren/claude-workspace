"""Locate an event in the inbox by scanning backward from EOF (#2937).

Event ids are random (``uuid4().hex[:16]``), so there is no ordered lookup
from an id to a byte offset. Resolving a cursor by reading the whole inbox
makes every cursor resolution cost O(history). :func:`locate_after_event`
instead reads fixed-size blocks backward from the end, so the cost is the
distance from EOF to the cursor line: cheap for the common "what happened
since I last looked" case. The worst case is a cursor that is absent from the
inbox: the scan reads the whole file backward and returns None, and the caller
(``_resolve_follow_start``) then does its full read and parse, so an absent
cursor costs one extra raw backward scan on top of that full read.

This module takes a :class:`~pathlib.Path` and deliberately does not import
:mod:`cw.events`, so ``cw.events`` can import it without a cycle. It takes no
lock: ``record_event`` appends whole lines under the inbox lock, and a file
replaced mid-scan is caught by the caller's anchor check
(``_read_lines_after_offset``).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

_BLOCK_BYTES = 65536
# cw.events derives _FOLLOW_ANCHOR_BYTES from this constant.
ANCHOR_BYTES = 256


def _read_block(handle: BinaryIO, start: int, end: int) -> bytes:
    """Read bytes ``[start, end)``; the module's single seek+read site.

    A short read means the file shrank after it was sized, so the scan's
    positions no longer describe it. That raises ``OSError``, which
    :func:`locate_after_event` turns into "not located".
    """
    handle.seek(start)
    data = handle.read(end - start)
    if len(data) != end - start:
        msg = f"short read at {start}: wanted {end - start} bytes, got {len(data)}"
        raise OSError(msg)
    return data


def _is_event_line(line: bytes, event_id: str) -> bool:
    """True when *line* parses as a JSON object whose ``id`` is *event_id*."""
    try:
        parsed: object = json.loads(line)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("id") == event_id


def _match_in_lines(body: bytes, needle: bytes, event_id: str) -> int | None:
    """Return the end (past ``\\n``) of the last line in *body* naming the event.

    *body* is a run of complete, newline-terminated lines. A needle hit is
    only a candidate, since the same text can appear inside another event's
    payload, so each hit's line is confirmed by parsing it.
    """
    hi = len(body)
    while (hit := body.rfind(needle, 0, hi)) != -1:
        line_start = body.rfind(b"\n", 0, hit) + 1
        line_end = body.index(b"\n", hit)
        if _is_event_line(body[line_start:line_end], event_id):
            return line_end + 1
        hi = line_start
    return None


def _scan_backward(
    handle: BinaryIO, size: int, needle: bytes, event_id: str
) -> int | None:
    """Return the byte offset just past the event's line, else None.

    Walks ``_BLOCK_BYTES`` blocks from *size* toward 0. ``carry`` holds the
    partial line at the front of the previous block, joined onto the next
    one, so a line straddling a block edge is matched whole. Bytes after the
    last newline are an unterminated (torn) append and are never matched.
    """
    pos = size
    carry = b""
    seen_complete_line = False
    while pos > 0:
        start = max(0, pos - _BLOCK_BYTES)
        data = _read_block(handle, start, pos) + carry
        pos = start
        if not seen_complete_line:
            last_newline = data.rfind(b"\n")
            if last_newline == -1:
                carry = data
                continue
            data = data[: last_newline + 1]
            seen_complete_line = True
        # At pos > 0 the first line may continue into the next block: hold it.
        split = data.index(b"\n") + 1 if pos > 0 else 0
        carry = data[:split]
        found = _match_in_lines(data[split:], needle, event_id)
        if found is not None:
            return pos + split + found
    return None


def locate_after_event(path: Path, event_id: str) -> tuple[int, bytes] | None:
    """Locate *event_id*'s line in the JSONL inbox at *path*.

    Returns ``(offset, anchor)``: *offset* is the byte position just past the
    newline ending that line, and *anchor* is the up-to-256 bytes immediately
    before *offset*, which is exactly the pair ``_read_lines_after_offset``
    takes to read the events after the cursor and verify the file did not
    change in between.

    Returns None when the inbox is absent, unreadable, or does not contain
    the event. That is silent by design: the caller falls back to a full
    read, which logs any real I/O failure itself. The size is taken once by
    ``fstat`` on the open descriptor, so appends during the scan are ignored.

    The match is on the raw JSON ``id`` of a line and does not require the
    line to pass model validation. The scan returns the LAST line carrying
    that id, whereas ``_apply_cursor`` on the full-read path takes the FIRST.
    An id that is duplicated, or that sits on a model-invalid line, may
    therefore resolve differently than on the full-read path. Ids are random,
    so this is theoretical.
    """
    needle = b'"id":' + json.dumps(event_id).encode()
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            end = _scan_backward(handle, size, needle, event_id)
            if end is None:
                return None
            anchor = _read_block(handle, max(0, end - ANCHOR_BYTES), end)
    except OSError:
        return None
    return end, anchor
