"""Last-real-block selection over a stream of sentinel text (GitHub #2490).

:func:`~cw.auto_dev_result.parse.parse_stdout` takes ONE worker stdout and
refuses a second block (§6 (6) ``multiple_result_blocks``). A caller that scans
a stream whose chunks can legitimately quote an earlier block -- an opencode
``text`` event echoing a prior stage's result, a sibling ticket's result the
worker read, the skill's worked example -- needs §3.1's "the LAST block wins"
instead, plus the guards that make "last" safe: placeholders and the documented
example are skipped, a block claiming a different ticket is ignored, and an
unusable last block is reported rather than replaced by an earlier one.

This module is the single owner of those rules; ``cw.opencode_runner`` (harvest
and ``queue_peek`` both) composes them over its event stream.
"""

from __future__ import annotations

import json
from typing import Any

from cw.auto_dev_result.parse import (
    _BLOCK_RE,
    _CLOSE_SENTINEL,
    _OPEN_SENTINEL,
    _is_placeholder_sentinel_text,
    _iter_loose_sentinel_json,
    _strip_code_fence,
    is_documented_example,
    parse_stdout,
)
from cw.auto_dev_result.schema import AutoDevResult, BlockedResult


def _claimed_ticket_id(raw: str) -> str | None:
    """Return the ``ticket_id`` a raw sentinel payload claims, or None.

    None means the claim is unreadable (the JSON does not decode, is not an
    object, or carries no string ``ticket_id``) -- the payload cannot be
    attributed to any ticket.
    """
    try:
        payload: Any = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    claimed = payload.get("ticket_id")
    return claimed if isinstance(claimed, str) else None


def _parse_real_block(
    raw: str, ticket_id: str | None
) -> AutoDevResult | BlockedResult | None:
    """Parse one raw sentinel payload, or None when it is not a real result.

    Not real: an unresolved doc-example placeholder, the documented
    ``PROJ-1234`` example, or -- when *ticket_id* is given -- a payload that
    claims a DIFFERENT ticket (a sibling ticket's result the worker read, an
    earlier ticket's block it quoted). A payload whose ticket cannot be read
    (:func:`_claimed_ticket_id` is None) is NOT discarded: it cannot be shown
    to belong to another ticket, so it stays the caller's final word and is
    reported as the :class:`BlockedResult` describing its failure.
    """
    if _is_placeholder_sentinel_text(raw):
        return None
    claimed = _claimed_ticket_id(raw)
    if ticket_id is not None and claimed is not None and claimed != ticket_id:
        return None
    result = parse_stdout(f"{_OPEN_SENTINEL}\n{raw}\n{_CLOSE_SENTINEL}")
    if isinstance(result, AutoDevResult) and is_documented_example(result):
        return None
    return result


def has_open_marker(text: str) -> bool:
    """True iff *text* contains the ``<<<AUTO_DEV_RESULT`` opening marker."""
    return _OPEN_SENTINEL in text


def has_unclosed_frame(text: str) -> bool:
    """True iff *text* ends in a sentinel frame that was opened but never closed.

    That is: the last ``<<<AUTO_DEV_RESULT`` marker is not part of a complete
    block (§6 (2): the producer was cut off mid-emit, or the frame continues in
    a later chunk). False when *text* has no open marker at all.
    """
    opened = text.rfind(_OPEN_SENTINEL)
    if opened < 0:
        return False
    complete = list(_BLOCK_RE.finditer(text))
    return not complete or opened >= complete[-1].end()


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
    trailing frame is ignored here -- see :func:`has_unclosed_frame`.
    """
    for match in reversed(list(_BLOCK_RE.finditer(text))):
        parsed = _parse_real_block(match.group(1), ticket_id)
        if parsed is not None:
            return parsed
    return None


def parse_last_loose_block(
    text: str, *, ticket_id: str | None = None
) -> AutoDevResult | BlockedResult | None:
    """Parse the last real bare-fenced auto-dev payload in *text* (GitHub #337).

    The loose counterpart of :func:`parse_last_block`, for a producer that
    emitted its result in a ```` ```json ```` fence without the
    ``AUTO_DEV_RESULT`` markers -- the shape :func:`parse_stdout` tolerates.
    Same skip rules as :func:`parse_last_block`; ``None`` when no fenced block
    carries ``schema_version`` and ``status``, or every one is skipped.
    """
    for candidate in _iter_loose_sentinel_json(text):
        parsed = _parse_real_block(candidate, ticket_id)
        if parsed is not None:
            return parsed
    return None
