"""A non-blocking inbox follower and a richer consumer-cursor record (#2693).

:class:`InboxFollower` is the poll-at-will counterpart of
:func:`cw.events.tail_events_follow`: the same position bookkeeping
(``_resolve_follow_start`` / ``_poll_follow_state`` / ``_FollowState``), but
``poll()`` never sleeps, so a caller can interleave other periodic work (the
attention stream's stuck-pending scan) between polls. Rotation is handled by
those primitives: a ``prune_events`` rewrite is re-resolved from the last
delivered *unfiltered* event id, replaying the surviving suffix when that id
is gone (at-least-once).

:func:`save_cursor_record` / :func:`load_cursor_record` write and read the
same ``events_dir()/cursors/<consumer>.json`` file as
:func:`cw.events.advance_cursor` / :func:`cw.events.load_cursor`, as a
superset: ``{"cursor", "updated_at", "extra"}``. ``load_cursor`` only reads
``cursor``, so it keeps working on these files.

A sibling module rather than more code in ``cw.events`` (which is near the
module-size ceiling); it imports that module's private follow primitives, so
``cw.events`` must never import this module back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.events import (
    _cursor_path,
    _FollowState,
    _poll_follow_state,
    _resolve_follow_start,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import OrchestratorEvent


class InboxFollower:
    """Follow the event inbox from a cursor without ever blocking.

    The first :meth:`poll` returns the backlog after *since_cursor* (every
    event when it is None) and seeds the follow state from that one read;
    later polls return only what was appended since. Events are unfiltered:
    the caller filters, and :attr:`cursor` stays an unfiltered event id so a
    rotation re-resolves correctly.
    """

    def __init__(self, since_cursor: str | None) -> None:
        self._seed = since_cursor
        self._state: _FollowState | None = None

    @property
    def cursor(self) -> str | None:
        """Id of the last delivered event, else the seed cursor."""
        return self._seed if self._state is None else self._state.cursor

    def poll(self) -> list[OrchestratorEvent]:
        """Return the events that are new since the previous poll."""
        if self._state is None:
            events, read = _resolve_follow_start(self._seed)
            cursor = events[-1].id if events else self._seed
            self._state = _FollowState(read.offset, read.size, read.ino, cursor)
            return events
        events, self._state = _poll_follow_state(self._state)
        return events


def last_event_id() -> str | None:
    """The id of the newest event in the inbox, or None when it is empty."""
    events, _read = _resolve_follow_start(None)
    return events[-1].id if events else None


def save_cursor_record(
    consumer: str, event_id: str | None, extra: Mapping[str, object]
) -> None:
    """Persist *consumer*'s cursor plus *extra* state, atomically.

    *event_id* may be None (nothing delivered yet); ``load_cursor`` then
    reads None, exactly as for a consumer that never advanced.
    """
    path = _cursor_path(consumer)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "cursor": event_id,
        "updated_at": datetime.now(UTC).isoformat(),
        "extra": dict(extra),
    }
    atomic_write_text(path, json.dumps(data))


def load_cursor_record(consumer: str) -> dict[str, object] | None:
    """Return *consumer*'s whole cursor record, or None when unusable.

    None for a missing or unreadable file, corrupt JSON, or JSON that is not
    an object. A plain :func:`cw.events.advance_cursor` file loads as a
    record with no ``extra`` key.
    """
    try:
        data: object = json.loads(_cursor_path(consumer).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): value for key, value in data.items()}
