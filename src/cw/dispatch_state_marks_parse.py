"""Pure helpers for the operator usage-limit clear marks (#3547).

The ``usage_limit_cleared`` sidecar key maps a client name to the instant an
operator cleared its usage-limit window. A running dispatch loop compares each
mark with the one it last saw: a mark that differs is an operator clear it has
not observed yet. This is a LEAF module: it imports only the standard library,
so ``cw.dispatch_state`` can import it at top level, and
``cw.dispatch_state_marks`` (which imports ``cw.dispatch_state``) holds the
locked writer and the unlocked loader.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

CLEAR_MARKS_KEY = "usage_limit_cleared"

# A mark older than the longest possible window can no longer matter. Equal in
# value to cw.dispatch_state._MAX_PARSED_USAGE_LIMIT_WINDOW; mirrored rather
# than imported because this leaf imports nothing from cw.
CLEAR_MARK_RETENTION = timedelta(days=7)


def usage_limit_clear_marks_from_payload(
    payload: Mapping[str, object],
) -> dict[str, datetime]:
    """Parse the clear marks out of a raw sidecar payload.

    Only timezone-aware ISO strings count; a malformed, naive or non-string
    value is skipped, and an absent or non-object key gives ``{}``. No expiry
    filter: pruning happens when a mark is written.
    """
    raw = payload.get(CLEAR_MARKS_KEY)
    if not isinstance(raw, dict):
        return {}
    marks: dict[str, datetime] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            marks[str(name)] = parsed
    return marks


def unseen_clear_marks(
    marks: Mapping[str, datetime], seen: Mapping[str, datetime]
) -> set[str]:
    """The clients whose mark differs from the one in *seen* (or is new to it)."""
    return {client for client, mark in marks.items() if seen.get(client) != mark}


def drop_unseen_cleared(
    windows: Mapping[str, datetime],
    marks: Mapping[str, datetime],
    seen: Mapping[str, datetime],
) -> tuple[dict[str, datetime], set[str]]:
    """Split *windows* into the kept entries and the unseen-mark clients dropped.

    Only a client with an unseen mark is dropped; a client with no mark, or a
    mark equal to ``seen[client]``, is kept. The inputs are not mutated.
    """
    unseen = unseen_clear_marks(marks, seen)
    kept = {c: until for c, until in windows.items() if c not in unseen}
    return kept, set(windows) & unseen
