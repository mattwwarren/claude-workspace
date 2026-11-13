"""The operator usage-limit clear: its locked primitive and mark loader (#3547).

``clear_usage_limited_until`` is the one writer that removes a persisted
``usage_limited_until`` entry. ``merge_and_save_usage_limited_until`` only ever
merges later-of and so cannot shorten a window. The clear also records a
per-client clear mark (``usage_limit_cleared``) in the same lock hold, which a
running dispatch loop observes on its next tick to drop its own in-memory copy
of the window.

Imports ``cw.dispatch_state``; that module never imports this one (it imports
only the stdlib leaf ``cw.dispatch_state_marks_parse``), so there is no cycle.
The state-file path is read at call time as ``dispatch_state.DISPATCH_STATE_FILE``
because the suite isolates state by patching that module attribute.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from cw import dispatch_state
from cw.atomic import atomic_write_text
from cw.config import refuse_real_state_write
from cw.dispatch_state import (
    _load_dispatch_state_raw,
    _usage_limited_until_from_payload,
    dispatch_state_lock,
)
from cw.dispatch_state_marks_parse import (
    CLEAR_MARK_RETENTION,
    CLEAR_MARKS_KEY,
    usage_limit_clear_marks_from_payload,
)
from cw.exceptions import CwError

if TYPE_CHECKING:
    from datetime import datetime


_EXPECTED_UNSET = object()


def load_usage_limit_clear_marks() -> dict[str, datetime]:
    """Load the per-client operator clear marks, unlocked like the other loaders.

    An absent, unreadable or corrupt sidecar yields ``{}``.
    """
    return usage_limit_clear_marks_from_payload(_load_dispatch_state_raw())


def clear_usage_limited_until(
    client: str,
    *,
    cleared_at: datetime,
    expected_until: datetime | None | object = _EXPECTED_UNSET,
) -> datetime | None:
    """Remove *client*'s usage-limit window and record its clear mark.

    One :func:`~cw.dispatch_state.dispatch_state_lock` acquisition covers the
    read, the pop, the mark and the write, so no other writer lands between
    them. Marks older than ``CLEAR_MARK_RETENTION`` before *cleared_at* are
    pruned. The mark is written even when no window was persisted, so a
    running loop still drops a window it holds only in memory. Returns the
    removed deadline, or None. ``OSError`` propagates: unlike the merge-save
    helper, the operator must know whether the clear landed.
    """
    path = dispatch_state.DISPATCH_STATE_FILE
    refuse_real_state_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with dispatch_state_lock():
        payload = _load_dispatch_state_raw(strict=True)
        windows = _usage_limited_until_from_payload(payload)
        old_until = windows.pop(client, None)
        if expected_until is not _EXPECTED_UNSET and old_until != expected_until:
            expected_dt = cast("datetime | None", expected_until)
            expected = "none" if expected_dt is None else expected_dt.isoformat()
            actual = "none" if old_until is None else old_until.isoformat()
            msg = (
                f"usage-limit window for {client} changed after planning "
                f"(expected {expected}, found {actual}); re-run the clear "
                "to refresh the plan and account probe"
            )
            raise CwError(msg)
        raw_windows = payload.get("usage_limited_until")
        if isinstance(raw_windows, dict):
            preserved_windows = dict(raw_windows)
            preserved_windows.pop(client, None)
            payload["usage_limited_until"] = preserved_windows
        horizon = cleared_at - CLEAR_MARK_RETENTION
        marks = {
            name: mark
            for name, mark in usage_limit_clear_marks_from_payload(payload).items()
            if mark >= horizon
        }
        marks[client] = cleared_at
        payload[CLEAR_MARKS_KEY] = {
            name: mark.isoformat() for name, mark in marks.items()
        }
        atomic_write_text(path, json.dumps(payload))
    return old_until
