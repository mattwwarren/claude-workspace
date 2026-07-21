"""Deduped WARNING logging helper for the AutoDevResult parse boundary.

Extracted from cw.auto_dev_result.parse (issue #1325) so the new
premises-resolution coercion submodule can log through the same deduped
path without creating a parse.py <-> _premises_resolution.py import cycle
(parse.py imports the coercion function FROM the new submodule; the new
submodule needs this helper). No behavior change -- parse.py re-imports
this symbol so every existing internal `_warn_once(...)` call site is
untouched.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("cw.auto_dev_result")


def _warn_once(
    message: str,
    *args: object,
    warned_blocks: set[str] | None,
    block_key: str | None,
) -> None:
    """Log *message* at WARNING, deduped per (block_key, rendered message) pair.

    Issue #1247: ``cw dev-queue wait``'s poll loop re-parses the full
    transcript every 5s, so an unresolved malformed sentinel re-triggers the
    identical warning on every poll for the life of the wait. Callers that
    opt in by passing a caller-owned ``warned_blocks`` set (content-hash
    keyed on the sentinel text via ``block_key``) get each distinct warning
    logged exactly once per block; every other caller (``warned_blocks`` or
    ``block_key`` left ``None``, the default) gets today's un-deduped
    behavior. Keyed on ``(block_key, rendered message)`` — the message
    formatted with its args, not the bare template — so two independent
    warnings about the same block that happen to share a log template but
    carry different args (e.g. ``_filter_empty_string_items`` called for
    both ``commits`` and ``friction_highlights`` on the same payload) each
    still surface once rather than the first suppressing the second.
    """
    if warned_blocks is None or block_key is None:
        _log.warning(message, *args)
        return
    rendered = message % args if args else message
    entry_key = f"{block_key}:{rendered}"
    if entry_key not in warned_blocks:
        _log.warning(message, *args)
        warned_blocks.add(entry_key)
