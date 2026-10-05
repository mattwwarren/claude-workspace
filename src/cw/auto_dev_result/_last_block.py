"""Last-real-block selection over a stream of sentinel text (GitHub #2490).

:func:`~cw.auto_dev_result.parse.parse_stdout` takes ONE worker stdout and
refuses a second block (§6 (6) ``multiple_result_blocks``). A caller that scans
a stream whose chunks can legitimately quote an earlier block -- an opencode
``text`` event echoing a prior stage's result, a sibling ticket's result the
worker read, the skill's worked example -- needs §3.1's "the LAST block wins"
instead, plus the guards that make "last" safe: placeholders and the documented
example are skipped, a block claiming a different ticket is ignored, and an
unusable last block is reported rather than replaced by an earlier one.

This module is the single owner of those rules, including how they apply across
a stream of chunks (:func:`parse_last_block_in_chunks`: a frame split across
chunks, a truncated final frame, the bare-fence fallback). ``cw.opencode_runner``
(harvest and ``queue_peek`` both) only feeds it its event stream. The
single-text primitive :func:`parse_last_block` is public for callers that hold
one string.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result.parse import (
    _BLOCK_RE,
    _CLOSE_SENTINEL,
    _OPEN_SENTINEL,
    _is_placeholder_sentinel_text,
    _iter_loose_sentinel_json,
    _strip_code_fence,
    is_documented_example,
    parse_stdout,
    unclosed_frame_blocked,
)
from cw.auto_dev_result.schema import AutoDevResult, BlockedResult

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = logging.getLogger("cw.auto_dev_result")

# What can follow the open marker for it to START a payload: an optional
# ``>>>`` (the finalize prompt spells the marker ``<<<AUTO_DEV_RESULT>>>`` in
# prose, so a worker may echo that spelling), optional whitespace, an optional
# code fence, then the JSON object's ``{``. Anything else after the marker --
# prose ("I emitted the <<<AUTO_DEV_RESULT>>> sentinel above"), a bare ``>>>``,
# nothing -- is a mention of the marker, not the start of a frame.
_PAYLOAD_START_RE = re.compile(r"(?:>>>)?\s*(?:```(?:json)?\s*)?\{")

# A claim read straight out of a payload that does not decode as JSON (a quoted
# sibling block with a ``...`` elision, a cut-off one): the first
# ``"ticket_id": "<value>"`` pair.
_RAW_TICKET_CLAIM_RE = re.compile(r'"ticket_id"\s*:\s*"([^"]*)"')

# The forms an LLM may echo a bare numeric task id in: ``940``, ``#940``,
# ``GH-940`` / ``gh-940``. Group 1 is the number.
_NUMERIC_TICKET_FORM_RE = re.compile(r"(?:#|GH-|gh-)?([0-9]+)")


def _claimed_ticket_id(raw: str) -> str | None:
    """Return the ``ticket_id`` a raw sentinel payload claims, or None.

    A payload that decodes to a JSON object is authoritative: its string
    ``ticket_id``, or None when it carries none. A payload that does not decode
    (malformed, elided, truncated) still names its ticket when the text holds a
    ``"ticket_id": "<value>"`` pair, so the claim is read from the raw text. None
    means no claim can be read at all -- the payload cannot be attributed to any
    ticket.
    """
    try:
        payload: Any = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        claimed = payload.get("ticket_id")
        return claimed if isinstance(claimed, str) else None
    found = _RAW_TICKET_CLAIM_RE.search(raw)
    return found.group(1) if found else None


def _ticket_ids_match(claimed: str, expected: str) -> bool:
    """True iff a sentinel's *claimed* ``ticket_id`` names the *expected* ticket.

    Exact string equality, plus one tolerance: when *expected* is all digits (a
    bare numeric task id, e.g. ``"940"``) a claim equal to it after stripping a
    single leading ``#`` or ``GH-`` / ``gh-`` prefix also matches. The prompt
    hands the worker only ``ticket 940`` in prose and the sentinel template says
    ``"ticket_id": "<ticket-id>"``, so an LLM may echo ``#940`` or ``GH-940``;
    dropping a real result over that is worse than accepting the common forms.
    Any other id (non-numeric task ids, a different number, ``0940``) compares
    exactly.
    """
    if claimed == expected:
        return True
    if not (expected.isascii() and expected.isdigit()):
        return False
    form = _NUMERIC_TICKET_FORM_RE.fullmatch(claimed)
    return form is not None and form.group(1) == expected


def _parse_real_block(
    raw: str, ticket_id: str | None
) -> AutoDevResult | BlockedResult | None:
    """Parse one raw sentinel payload, or None when it is not a real result.

    Not real: an unresolved doc-example placeholder, the documented
    ``PROJ-1234`` example, or -- when *ticket_id* is given -- a payload that
    claims a DIFFERENT ticket (a sibling ticket's result the worker read, an
    earlier ticket's block it quoted; see :func:`_ticket_ids_match`). A discarded
    block is logged at WARNING naming both ids: the rule is a premise about what
    a worker echoes, and a silent drop would hide it being wrong. A payload whose
    ticket cannot be read (:func:`_claimed_ticket_id` is None) is NOT discarded:
    it cannot be shown to belong to another ticket, so it stays the caller's
    final word and is reported as the :class:`BlockedResult` describing its
    failure.
    """
    if _is_placeholder_sentinel_text(raw):
        return None
    claimed = _claimed_ticket_id(raw)
    if (
        ticket_id is not None
        and claimed is not None
        and not _ticket_ids_match(claimed, ticket_id)
    ):
        _log.warning(
            "auto-dev sentinel block discarded: it claims ticket_id=%r but this"
            " session is for ticket %r",
            claimed,
            ticket_id,
        )
        return None
    result = parse_stdout(f"{_OPEN_SENTINEL}\n{raw}\n{_CLOSE_SENTINEL}")
    if isinstance(result, AutoDevResult) and is_documented_example(result):
        return None
    return result


def parse_last_block(
    text: str, *, ticket_id: str | None = None
) -> AutoDevResult | BlockedResult | None:
    """Parse the LAST real sentinel block in *text*, tolerating earlier ones.

    Blocks are walked last to first and the first REAL one decides (see
    :func:`_parse_real_block`: placeholders, the documented example and, when
    *ticket_id* is given, other tickets' blocks are skipped). The decision is
    final: a real last block whose payload is unusable comes back as the
    :class:`BlockedResult` describing why, never swapped for an earlier valid
    block. ``None`` means *text* has no complete real block. An unclosed
    trailing frame is ignored here -- :func:`parse_last_block_in_chunks` owns it.
    """
    for match in reversed(list(_BLOCK_RE.finditer(text))):
        parsed = _parse_real_block(match.group(1), ticket_id)
        if parsed is not None:
            return parsed
    return None


def _parse_last_loose_block(
    text: str, ticket_id: str | None
) -> AutoDevResult | BlockedResult | None:
    """Parse the last real bare-fenced auto-dev payload in *text* (GitHub #337).

    The loose counterpart of :func:`parse_last_block`, for a producer that
    emitted its result in a ```` ```json ```` fence without the
    ``AUTO_DEV_RESULT`` markers -- the shape :func:`parse_stdout` tolerates.
    Same skip rules; ``None`` when no fenced block carries ``schema_version`` and
    ``status``, or every one is skipped.
    """
    for candidate in _iter_loose_sentinel_json(text):
        parsed = _parse_real_block(candidate, ticket_id)
        if parsed is not None:
            return parsed
    return None


def _latest_frame_start(chunks: list[str]) -> tuple[int, int, str] | None:
    """Locate the latest chunk whose last open marker STARTS a payload.

    Walks the chunks newest first. A chunk qualifies when its last
    ``<<<AUTO_DEV_RESULT`` marker, read together with everything after it in the
    stream (a payload may begin in the next chunk), is followed by a payload
    start (:data:`_PAYLOAD_START_RE`); a chunk whose last marker is a prose
    mention is passed over, so a later closing summary quoting the marker can
    neither become the frame nor hide an earlier one. Returns ``(chunk index,
    marker offset in that chunk, the stream from the marker onward)``, or None.
    """
    for index in range(len(chunks) - 1, -1, -1):
        offset = chunks[index].rfind(_OPEN_SENTINEL)
        if offset < 0:
            continue
        tail = chunks[index][offset:] + "".join(chunks[index + 1 :])
        if _PAYLOAD_START_RE.match(tail, len(_OPEN_SENTINEL)):
            return index, offset, tail
    return None


def parse_last_block_in_chunks(
    chunks: Iterable[str], *, ticket_id: str | None
) -> AutoDevResult | BlockedResult | None:
    """Return the last real result for *ticket_id* in a stream of text chunks.

    The chunks are an opencode log's ``text`` events, in order. Guarantee
    (#2490): the last COMPLETE frame wins. Each chunk is parsed on its own
    (:func:`parse_last_block`), newest last, so an earlier chunk that quotes
    another stage's sentinel -- or the skill's worked example -- can neither
    shadow nor poison the final one, and a chunk that quotes the open marker in
    prose changes nothing. Placeholders, the documented example and blocks that
    claim another ticket are skipped (:func:`_parse_real_block`, which also
    states the ticket-identity rule). ``None`` for *ticket_id* disables the
    identity check.

    A real last block that is unusable comes back as the ``BlockedResult``
    describing why; an earlier valid block must not resurrect a stale stage's
    result in place of the worker's actual final word. The same holds for a
    TRUNCATED final frame: the latest chunk whose open marker starts a payload
    (:func:`_latest_frame_start`) that its own text does not close is joined with
    the chunks after it. If the frame then closes (a block split across chunks)
    the real last block of the joined text decides, falling back to the chunk
    scan's pick when that block is a placeholder or another ticket's; if it
    still has no close it is the unusable ``BlockedResult``.

    With no framed block anywhere, the joined text gets one last try through the
    bare-fenced-JSON path :func:`~cw.auto_dev_result.parse.parse_stdout`
    tolerates (GitHub #337). ``None`` means no real block anywhere.
    """
    events = list(chunks)
    last: AutoDevResult | BlockedResult | None = None
    for text in events:
        parsed = parse_last_block(text, ticket_id=ticket_id)
        if parsed is not None:
            last = parsed
    frame = _latest_frame_start(events)
    if frame is not None:
        index, offset, tail = frame
        if not _BLOCK_RE.match(events[index], offset):
            if not _BLOCK_RE.match(tail):
                return unclosed_frame_blocked(tail)
            resolved = parse_last_block(tail, ticket_id=ticket_id)
            return resolved if resolved is not None else last
    if last is not None:
        return last
    return _parse_last_loose_block("".join(events), ticket_id)
