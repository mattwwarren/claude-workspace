"""Scope, dedupe latch and line rendering for ``cw orchestrate watch`` (#2693).

The pure half of the attention stream; :mod:`cw.attention_stream` owns the
follower, the persisted cursor and the stuck-pending scan. Replaces the skill
script ``attention_watch.py``, which hand-copied ``cw`` constants because it
had to run under a bare ``python3``; the constants are imported here.

**Scope** (:func:`in_scope`), applied after :meth:`Latch.clear`:

1. ``payload.client`` must be the watched client (no client: dropped).
2. A whole-client (or ``--all-lanes``) scope keeps everything else.
3. A client-level page (no ticket and no lane, e.g. ``dispatch_loop_stale``)
   is kept under every scope: a dead dispatch loop affects every ticket.
4. A ticket in the ``--ticket`` set is kept.
5. A starved-lane page (no ticket, a lane) is kept iff its lane is in scope
   or a watched ticket's row sits in that lane.
6. Otherwise the lane is ``payload.lane``, else the row's lane (many watched
   events carry no lane -- the #382 class of silent drop), and the event is
   kept iff that lane is in scope. Only an unknowable lane fails open, and
   only when the lane set is non-empty.

**Latch** (:class:`Latch`): a page is keyed by (type, client, ticket or lane
or ``*``, paused_status / new_bucket / reason) with ``renotify_marker`` as its
fingerprint, and a repeat is suppressed until a resolving event clears the key
(``task.transition`` / ``ticket.requeued`` for the ticket, ``lane.resumed``
for the lane, a client-wide ``dispatch.tick`` for ``dispatch_loop_stale``, a
non-surfaced liveness bucket for the ticket's liveness pages). Clear events are
applied before scope filtering and never rendered.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.dev_queue import _find_ticket
from cw.dev_queue.next_commands import next_commands
from cw.dispatch import BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
from cw.dispatch.regress_repeat import _FINALIZE_REGRESS_REPEAT_REASON
from cw.exceptions import CwError
from cw.focus import get_focus
from cw.models import LivenessBucket, OrchestratorEventType
from cw.reconcile._shared import (
    SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
    SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from cw.models import DevQueueStore, OrchestratorEvent, Session, TicketTask
    from cw.stuck_pending import PendingBlock

WATCHED_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        OrchestratorEventType.OPERATOR_ESCALATION,
        OrchestratorEventType.SESSION_TIMED_OUT,
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED,
        OrchestratorEventType.SESSION_PHANTOM_REVERTED,
        OrchestratorEventType.SESSION_LIVENESS_CHANGED,
    }
)
CLEAR_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.TASK_TRANSITION,
        OrchestratorEventType.TICKET_REQUEUED,
        OrchestratorEventType.LANE_RESUMED,
        OrchestratorEventType.DISPATCH_TICK,
    }
)
# Only these buckets page (#2004): "live"/"stale_15m" flap during a legitimate
# quiet stretch. Per-stage thresholds mean stale_30m is not a fixed duration,
# so the line renders stale_minutes and never implies one.
SURFACED_LIVENESS: frozenset[str] = frozenset(
    {LivenessBucket.STALE_30M.value, LivenessBucket.STALE_45M.value}
)
# paused_status values whose breadcrumbs are rendered: the blocker.reason
# carriers (#1597) plus three diagnostics whose breadcrumbs are always
# actionable (#1717, #2490, #2513). Every other producer's breadcrumbs are
# polymorphic (paths, fixed strings), so this is an allowlist.
BREADCRUMB_PAUSED_STATUSES: frozenset[str] = BREADCRUMB_ELIGIBLE_PAUSED_STATUSES | {
    _FINALIZE_REGRESS_REPEAT_REASON,
    SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
    SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
}
LATCH_MAX_KEYS = 512
CONSUMER_PREFIX = "attention"
DISPATCH_LOOP_STALE_STATUS = "dispatch_loop_stale"

_ANY_SLOT = "*"
_NONE = "-"
_EVERY_LANE = "*"
_HASH_LEN = 8
_SECOND_PREFIX_LEN = len("YYYY-MM-DDTHH:MM:SS")
_SESSION_PREFIX_LEN = 8
_SECONDS_PER_MINUTE = 60
_STUCK_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"
_NO_CLIENT = (
    "No client to watch: pass --client, or point this session's focus at one"
    " with `cw focus set CLIENT[/LANE]`."
)


class ScopeSource(StrEnum):
    """Where a watch scope came from (the scope line's ``source=``)."""

    FLAGS = "flags"
    FOCUS = "focus"
    ALL_LANES = "all-lanes"
    CLIENT = "client"


@dataclass(frozen=True)
class WatchScope:
    """The client, lanes and tickets one watcher reports on.

    Empty *lanes* and *tickets* (or *all_lanes*) mean every lane; empty
    *lanes* with tickets is a ``--ticket``-only scope with no lane filter.
    """

    client: str
    lanes: frozenset[str]
    tickets: frozenset[str]
    all_lanes: bool
    source: ScopeSource

    @property
    def everything(self) -> bool:
        """True when every event for the client is in scope."""
        return self.all_lanes or not (self.lanes or self.tickets)

    @property
    def lanes_text(self) -> str:
        """``*`` for every lane, ``-`` for no lane filter, else sorted lanes."""
        if self.everything:
            return _EVERY_LANE
        return ",".join(sorted(self.lanes)) or _NONE

    @property
    def tickets_text(self) -> str:
        return ",".join(sorted(self.tickets)) or _NONE

    @property
    def consumer(self) -> str:
        """The cursor consumer name: one per (client, resolved scope)."""
        key = f"{self.lanes_text}|{self.tickets_text}"
        digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()
        client = self.client.replace("/", "-")
        return f"{CONSUMER_PREFIX}-{client}-{digest[:_HASH_LEN]}"

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "lanes": sorted(self.lanes),
            "tickets": sorted(self.tickets),
            "all_lanes": self.all_lanes,
        }


def resolve_scope(
    client: str | None,
    lanes: Iterable[str],
    tickets: Iterable[str],
    *,
    all_lanes: bool,
    session_id: str | None,
) -> WatchScope:
    """The watch scope from the flags, falling back to the focus pointer.

    The focus entry of *session_id* supplies the client when *client* is
    None, and the lane only when no ``--lane`` / ``--ticket`` /
    ``--all-lanes`` narrows or widens the scope and the focus names the same
    client. Without either, the whole client is watched.

    Raises:
        CwError: no *client* and no focus entry to take one from.
    """
    focus = get_focus(session_id) if session_id else None
    resolved = client if client is not None else (focus.client if focus else None)
    if resolved is None:
        raise CwError(_NO_CLIENT)
    lane_set = frozenset(lanes)
    ticket_set = frozenset(tickets)
    if all_lanes:
        source = ScopeSource.ALL_LANES
    elif lane_set or ticket_set:
        source = ScopeSource.FLAGS
    elif focus is not None and focus.client == resolved and focus.lane is not None:
        lane_set = frozenset({focus.lane})
        source = ScopeSource.FOCUS
    else:
        source = ScopeSource.CLIENT
    return WatchScope(
        client=resolved,
        lanes=lane_set,
        tickets=ticket_set,
        all_lanes=all_lanes,
        source=source,
    )


class RowIndex:
    """Lazy (client, ticket) -> dev-queue row lookups over one queue snapshot."""

    def __init__(self, store: DevQueueStore, client: str) -> None:
        self._store = store
        self._client = client
        self._rows: dict[str, TicketTask | None] = {}

    def row(self, ticket_id: str) -> TicketTask | None:
        """The row ``_find_ticket`` selects for *ticket_id*, or None."""
        if ticket_id not in self._rows:
            try:
                found: TicketTask | None = _find_ticket(
                    self._store, ticket_id, self._client, warn=False
                )
            except CwError:
                found = None
            self._rows[ticket_id] = found
        return self._rows[ticket_id]

    def lanes_of(self, ticket_ids: Iterable[str]) -> set[str]:
        """The lanes the rows of *ticket_ids* sit in (missing rows skipped)."""
        return {row.lane for t in ticket_ids if (row := self.row(t)) is not None}


def _text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def in_scope(event: OrchestratorEvent, scope: WatchScope, rows: RowIndex) -> bool:
    """Whether *event* belongs to *scope*; see the module docstring for the rule."""
    payload = event.payload
    if payload.get("client") != scope.client:
        return False
    if scope.everything:
        return True
    ticket = _text(payload, "ticket_id")
    lane = _text(payload, "lane")
    if ticket is None and lane is None:
        return True
    if ticket is None:
        return lane in scope.lanes or lane in rows.lanes_of(scope.tickets)
    return ticket in scope.tickets or _ticket_lane_in_scope(ticket, lane, scope, rows)


def _ticket_lane_in_scope(
    ticket: str, lane: str | None, scope: WatchScope, rows: RowIndex
) -> bool:
    """A ticket event outside the ticket set: judged by its (row) lane."""
    if lane is None:
        row = rows.row(ticket)
        lane = row.lane if row is not None else None
    if lane is None:
        return bool(scope.lanes)
    return lane in scope.lanes


def _status_slot(payload: dict[str, object]) -> str:
    return (
        _text(payload, "paused_status")
        or _text(payload, "new_bucket")
        or _text(payload, "reason")
        or ""
    )


def _key(event: OrchestratorEvent) -> str:
    payload = event.payload
    slot = _text(payload, "ticket_id") or _text(payload, "lane") or _ANY_SLOT
    client = _text(payload, "client") or ""
    return f"{event.type.value}|{client}|{slot}|{_status_slot(payload)}"


class Latch:
    """Suppress repeated pages until a resolving event clears them.

    Entries map a serialised key to its ``renotify_marker`` fingerprint and
    keep insertion order, so the oldest key is evicted past
    :data:`LATCH_MAX_KEYS`.
    """

    def __init__(self, entries: dict[str, str] | None = None) -> None:
        self._entries: dict[str, str] = dict(entries or {})

    @classmethod
    def from_dict(cls, raw: object) -> Latch:
        """Restore a latch from :meth:`to_dict` output; garbage gives an empty one."""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            {
                key: value
                for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        )

    def to_dict(self) -> dict[str, str]:
        return dict(self._entries)

    def admit(self, event: OrchestratorEvent) -> bool:
        """True when *event* should page; records it so a repeat will not."""
        key = _key(event)
        fingerprint = _text(event.payload, "renotify_marker") or ""
        if self._entries.get(key) == fingerprint:
            return False
        self._entries.pop(key, None)
        self._entries[key] = fingerprint
        while len(self._entries) > LATCH_MAX_KEYS:
            del self._entries[next(iter(self._entries))]
        return True

    def clear(self, event: OrchestratorEvent) -> bool:
        """Apply *event* as a resolving event; True when it is one (never render).

        A non-surfaced liveness bucket counts too: it clears that ticket's
        liveness pages, so a genuine re-stall into the same bucket pages again.
        """
        payload = event.payload
        client = _text(payload, "client") or ""
        ticket = _text(payload, "ticket_id")
        if event.type == OrchestratorEventType.SESSION_LIVENESS_CHANGED:
            if _text(payload, "new_bucket") in SURFACED_LIVENESS:
                return False
            if ticket is not None:
                self._drop(event.type.value, client, ticket)
            return True
        if event.type not in CLEAR_TYPES:
            return False
        if event.type == OrchestratorEventType.LANE_RESUMED:
            lane = _text(payload, "lane")
            if lane is not None:
                self._drop(None, client, lane)
        elif event.type == OrchestratorEventType.DISPATCH_TICK:
            if "ticket_id" not in payload:
                stale = (
                    f"{OrchestratorEventType.SESSION_NEEDS_ATTENTION.value}|{client}"
                    f"|{_ANY_SLOT}|{DISPATCH_LOOP_STALE_STATUS}"
                )
                self._entries.pop(stale, None)
        elif ticket is not None:
            self._drop(None, client, ticket)
        return True

    def _drop(self, etype: str | None, client: str, slot: str) -> None:
        for key in list(self._entries):
            k_type, k_client, k_slot, _status = key.split("|", 3)
            if k_client == client and k_slot == slot and etype in (None, k_type):
                del self._entries[key]


def first_next_command(row: TicketTask | None, session: Session | None) -> str | None:
    """The first legal next command for *row*, or None when there is no row."""
    if row is None:
        return None
    # Every rule of the next-command table yields at least one command.
    return next_commands(row, session=session)[0].command


def _why(payload: dict[str, object]) -> str:
    for key in ("paused_status", "reason", "proposed_action", "new_bucket"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _bits(payload: dict[str, object]) -> str:
    bits: list[str] = []
    if payload.get("stage"):
        bits.append(f"stage={payload['stage']}")
    if payload.get("attempts") is not None:
        bits.append(f"att={payload['attempts']}")
    stale = payload.get("stale_minutes")
    if isinstance(stale, int | float):
        bits.append(f"stale_m={float(stale):.1f}")
    if payload.get("lane"):
        bits.append(f"lane={payload['lane']}")
    crumbs = payload.get("breadcrumbs")
    if crumbs and payload.get("paused_status") in BREADCRUMB_PAUSED_STATUSES:
        bits.append(f"reason={crumbs}")
    return " ".join(bits)


def render_event(event: OrchestratorEvent, command: str | None) -> str:
    """One ``ATTENTION | ...`` line for *event*, ending in its next command."""
    payload = event.payload
    fields = [
        event.created_at.isoformat()[:_SECOND_PREFIX_LEN],
        event.type.value,
        f"#{_text(payload, 'ticket_id') or '?'}",
        _why(payload),
        _bits(payload),
        str(payload.get("session_id") or "")[:_SESSION_PREFIX_LEN],
        f"next: {command or _NONE}",
    ]
    return "ATTENTION | " + " | ".join(fields)


def render_stuck(
    now: datetime,
    *,
    ticket_id: str,
    lane: str,
    block: PendingBlock,
    age_seconds: float,
) -> str:
    """One ``STUCK | ...`` line for a PENDING row stuck *age_seconds*."""
    minutes = int(age_seconds // _SECONDS_PER_MINUTE)
    fields = [
        now.strftime(_STUCK_TS_FORMAT),
        f"#{ticket_id}",
        block.code,
        f"pending {minutes}m",
        block.detail,
        f"lane={lane}",
        f"next: {block.command or _NONE}",
    ]
    return "STUCK | " + " | ".join(fields)


def render_scope_line(scope: WatchScope, start: str) -> str:
    """The first stdout line: the resolved scope, start mode and cursor name."""
    return (
        f"WATCH | scope | client={scope.client} | lanes={scope.lanes_text}"
        f" | tickets={scope.tickets_text} | source={scope.source.value}"
        f" | start={start} | cursor={scope.consumer}"
    )


def exit_line(reason: str) -> str:
    """The last line a watcher prints: ``WATCH | exited: <reason>``."""
    return f"WATCH | exited: {reason}"


def refused_reason(pid: int, consumer: str) -> str:
    """The exit reason for a start refused by a live cursor owner."""
    return (
        f"another watcher (pid {pid}) owns cursor {consumer}; pass --force if it"
        " is stale (a recycled pid cannot be detected)"
    )


def write_failed_reason(exc: BaseException) -> str:
    """The exit reason for a failed stdout write."""
    return f"stdout write failed ({type(exc).__name__}); cursor not advanced"


def error_reason(exc: BaseException) -> str:
    """The exit reason for a fatal error."""
    return f"{type(exc).__name__}: {exc}"


def signal_reason(name: str) -> str:
    """The exit reason for a terminating signal."""
    return f"signal {name}"
