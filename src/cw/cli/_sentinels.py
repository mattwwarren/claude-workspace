"""Transcript sentinel helpers shared across CLI commands.

Both ``signal-stop`` (headless DAEMON backstop) and ``dev-queue wait``
(sentinel-aware polling) need to detect and parse the AUTO_DEV_RESULT
sentinel inside a Claude session transcript. The logic lives here so both
command submodules import the same implementation.

:func:`_sentinel_frame_after` is the odd one out: it looks for the frame
*markers* rather than a parseable sentinel, and it exists to produce NEGATIVE
evidence only — its True answer can suppress the #2135 abandoned-exit park but
can never cause one. Any read or parse trouble therefore counts as evidence
that a frame may be present; only a clean read that finds none returns False.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from cw._util import (
    _iter_sentinel_text_blocks,
    _iter_sentinel_text_records,
    claude_project_dir,
)
from cw.auto_dev_result import (
    _CLOSE_SENTINEL,
    _OPEN_SENTINEL,
    AutoDevResult,
    BlockedResult,
    _is_placeholder_sentinel_text,
    extract_block,
    is_documented_example,
    parse_stdout,
)

if TYPE_CHECKING:
    from pathlib import Path


def _parse_sentinel_from_transcript(
    cwd: str,
    claude_session_id: str | None,
    *,
    warned_blocks: set[str] | None = None,
) -> AutoDevResult | BlockedResult | None:
    """Return the parsed sentinel from the transcript, or None if absent.

    Claude stores session transcripts at:
      ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``

    where the encoded path replaces both ``/`` and ``.`` with ``-``. The JSONL
    contains one event per line; ``assistant`` events carry ``message.content``
    blocks whose ``text`` fields hold the model output, JSON-escaped (real
    newlines become the two-character sequence ``\\n``). Running ``extract_block``
    against the raw file therefore misses sentinels that are valid in their
    decoded form, so this scans each candidate block individually after JSON
    decoding — assistant text blocks AND ``tool_result`` blocks, since a worker
    may emit the sentinel via ``cat <<EOF`` (landing it in Bash stdout rather
    than assistant text; GitHub #731). Returns None on any I/O error or when no
    complete sentinel pair is found — distinct from a BlockedResult, which means
    the sentinel framing was present but the inner payload was unusable.

    Used by ``signal_stop`` for headless DAEMON sessions, whose result must
    be captured here because they bypass session lifecycle tracking entirely.
    See GitHub issue #225 (capture gap) and issue #176 Layer 1 (transcript-walk origin).

    ``warned_blocks`` (issue #1247) is an optional caller-owned set forwarded
    unchanged into every ``parse_stdout`` call below, deduping repeated
    ``_log.warning`` calls for the same malformed block both across repeated
    calls to this function (e.g. a poll loop rescanning an unresolved
    transcript) and across multiple candidate blocks within one call. Left
    ``None`` (the default), every warning logs independently as before.
    """
    if not claude_session_id:
        return None
    transcript_path = claude_project_dir(cwd) / f"{claude_session_id}.jsonl"
    last_result: AutoDevResult | BlockedResult | None = None
    for text in _iter_sentinel_text_blocks(transcript_path):
        block = extract_block(text)
        if block is not None:
            if _is_placeholder_sentinel_text(block):
                continue
            result = parse_stdout(text, warned_blocks=warned_blocks)
            if isinstance(result, AutoDevResult) and is_documented_example(result):
                continue
            last_result = result
    return last_result


def _sentinel_present_in_transcript(
    cwd: str,
    claude_session_id: str | None,
) -> bool:
    """Return True if the AUTO_DEV_RESULT sentinel block appears in the transcript.

    Thin wrapper around :func:`_parse_sentinel_from_transcript` preserved for
    callers that only need the boolean (Layer 1 budget gate in signal_stop).
    A non-None return — including a BlockedResult for malformed payloads —
    means the agent emitted *something*; the result-capture path uses the
    full parsed value, but the budget path only cares "did it emit?"
    """
    return _parse_sentinel_from_transcript(cwd, claude_session_id) is not None


def _at_or_after(timestamp: str | None, pivot: datetime) -> bool:
    """Whether a record stamped *timestamp* is at/after *pivot*.

    An unorderable stamp — absent, unparseable, or naive (no offset, so not
    comparable to the aware *pivot*) — counts as AFTER. Every such record then
    suppresses a park rather than permitting one, which is the conservative
    direction: the cost is a silent non-park, never a false park.
    """
    if timestamp is None:
        return True
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    return parsed.tzinfo is None or parsed >= pivot


def _sentinel_frame_after(transcript_path: Path, pivot: datetime) -> bool:
    """True iff a sentinel frame marker appears at or after *pivot* (#2135).

    Either marker counts, complete pair or not: an open marker with no close
    never reaches ``parse_stdout`` (``extract_block`` needs the pair), so a
    truncated frame reads to the Stop hook as "no sentinel" — exactly the case
    that must NOT be parked as ``stopped_without_sentinel``, because doing so
    would hide the worker's real blocker reason behind the wrong disposition.

    Fails toward defer. It reads the transcript in ``strict`` mode, so an
    unreadable file, a torn final line, or any undecodable line raises and is
    caught here as True. Only a clean read that finds no frame returns False,
    which is what makes "this guard can only suppress a park" true of the whole
    function rather than only of the happy path. A missing file is the caller's
    decision, not this function's (it returns False here and the caller's own
    "no transcript found" branch defers).

    Reads no clock: *pivot* comes from the marker the worker recorded, and the
    comparison is ordering against record timestamps, never an age or expiry.
    """
    try:
        for timestamp, text in _iter_sentinel_text_records(
            transcript_path, strict=True
        ):
            if (_OPEN_SENTINEL in text or _CLOSE_SENTINEL in text) and _at_or_after(
                timestamp, pivot
            ):
                return True
    except (OSError, ValueError):
        return True
    return False
