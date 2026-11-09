"""Per-cause breakdown of worker sessions that end without a result (#2840).

Read-only report over the event inbox, the per-client history log and the
``sessions.json`` snapshot. Every ``session.spawned`` worker in a window is
either paired with a ``session.result_emitted`` or lands in exactly one
no-result bucket. Buckets are tried in :class:`NoResultCause` order and the
first match wins.

Audit-only: nothing here routes, dispatches or writes state. Do not import
this module from ``cw.reconcile`` or ``cw.dispatch`` (``docs/events.md``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple

from cw.events import read_events
from cw.history import EventType as HistoryEventType
from cw.history import load_history
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    CwState,
    OrchestratorEvent,
    OrchestratorEventType,
    ReapReason,
)

SCHEMA_VERSION = 1

# Literal values read from payloads. Not imported from cw.reconcile, which
# this module must not depend on (audit-only rule).
_REASON_USAGE_MID_TURN = "usage_limited_mid_turn"
_CAUSE_USAGE_LIMIT_CUTOFF = "usage_limit_cutoff"
_PAUSED_USAGE_MID_TURN = "usage_limited_mid_turn"
_REASON_PHANTOM_REAP_MERGED = "phantom_reap_merged"
_OPERATOR_CLOSE_DETAIL = "operator_close"

_USAGE_REAP_REASONS = frozenset(
    {ReapReason.USAGE_LIMIT_CUTOFF.value, ReapReason.USAGE_LIMIT_MID_TURN.value}
)
_PHANTOM_REAP_REASONS = frozenset({ReapReason.PHANTOM_SURFACE.value})

_READ_TYPES: tuple[OrchestratorEventType, ...] = (
    OrchestratorEventType.SESSION_SPAWNED,
    OrchestratorEventType.SESSION_COMPLETED,
    OrchestratorEventType.SESSION_RESULT_EMITTED,
    OrchestratorEventType.SESSION_REAP_PROPOSED,
    OrchestratorEventType.SESSION_REAP_AUTHORIZED,
    OrchestratorEventType.SESSION_PHANTOM_REVERTED,
    OrchestratorEventType.SESSION_NEEDS_ATTENTION,
    OrchestratorEventType.SESSION_TIMED_OUT,
    OrchestratorEventType.TICKET_REQUEUED,
)

# Events that count as "A was released" for the late-sentinel rule.
_RELEASE_TYPES = frozenset(
    {
        OrchestratorEventType.SESSION_PHANTOM_REVERTED,
        OrchestratorEventType.SESSION_TIMED_OUT,
        OrchestratorEventType.SESSION_REAP_PROPOSED,
    }
)
# Events that end a session's lifecycle, so it is not parked.
_TERMINAL_TYPES = frozenset(
    {
        OrchestratorEventType.SESSION_COMPLETED,
        OrchestratorEventType.SESSION_TIMED_OUT,
        OrchestratorEventType.SESSION_PHANTOM_REVERTED,
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
    }
)


class NoResultCause(StrEnum):
    """No-result buckets. Enum order is both precedence and report order."""

    LATE_SENTINEL_SUPERSEDED = "late_sentinel_superseded"
    OPERATOR_CLOSE = "operator_close"
    USAGE_LIMIT = "usage_limit"
    PHANTOM = "phantom"
    CRASH = "crash"
    REAP = "reap"
    PARKED_NEEDS_ATTENTION = "parked_needs_attention"
    COMPLETED_NO_RESULT = "completed_no_result"
    IN_FLIGHT = "in_flight"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class CauseBucket:
    """One no-result bucket: its count and the first sample session ids."""

    cause: NoResultCause
    count: int
    sample_session_ids: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeReport:
    """Spawned-versus-result breakdown for one window.

    ``oldest_event_at`` is the oldest event of the types this report reads,
    not of the whole inbox. ``window_truncated`` is therefore a heuristic: a
    young inbox can report truncation with no pruning.
    """

    window_start: datetime
    window_end: datetime
    clients: tuple[str, ...]
    spawned: int
    with_result: int
    without_result: int
    buckets: tuple[CauseBucket, ...]
    oldest_event_at: datetime | None
    window_truncated: bool

    def count(self, cause: NoResultCause) -> int:
        """Return the count for *cause*, or 0 when absent."""
        for bucket in self.buckets:
            if bucket.cause == cause:
                return bucket.count
        return 0

    @property
    def no_result_rate(self) -> float:
        """Share of spawned sessions with no result, in-flight included."""
        return self.without_result / self.spawned if self.spawned else 0.0

    @property
    def failure_rate_excl_in_flight(self) -> float:
        """No-result share after removing live (in-flight) sessions."""
        in_flight = self.count(NoResultCause.IN_FLIGHT)
        denominator = self.spawned - in_flight
        numerator = self.without_result - in_flight
        return numerator / denominator if denominator else 0.0

    @property
    def largest_cause(self) -> NoResultCause | None:
        """Largest failure bucket, excluding in-flight. Ties go to enum order."""
        best: CauseBucket | None = None
        for bucket in self.buckets:
            if bucket.cause == NoResultCause.IN_FLIGHT or bucket.count == 0:
                continue
            if best is None or bucket.count > best.count:
                best = bucket
        return best.cause if best is not None else None


class _Event(NamedTuple):
    at: datetime
    etype: OrchestratorEventType
    payload: Mapping[str, Any]


class _Spawn(NamedTuple):
    session_id: str
    at: datetime
    client: str | None
    ticket_id: str


_TicketKey = tuple[str | None, str]


@dataclass(frozen=True)
class _Index:
    spawns: dict[str, _Spawn]
    spawns_by_ticket: dict[_TicketKey, list[_Spawn]]
    by_session: dict[str, list[_Event]]
    requeues: dict[_TicketKey, list[datetime]]
    operator_closed_ids: frozenset[str]
    live_session_ids: frozenset[str]


_Predicate = Callable[[str, _Index], bool]


def _as_str(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _index_spawn(
    spawns: dict[str, _Spawn],
    spawns_by_ticket: dict[_TicketKey, list[_Spawn]],
    event: OrchestratorEvent,
    session_id: str,
) -> None:
    """Record the first spawn for *session_id*; duplicates are ignored."""
    ticket_id = _as_str(event.payload.get("ticket_id"))
    if ticket_id is None or session_id in spawns:
        return
    spawn = _Spawn(
        session_id=session_id,
        at=event.created_at,
        client=_as_str(event.payload.get("client")),
        ticket_id=ticket_id,
    )
    spawns[session_id] = spawn
    spawns_by_ticket.setdefault((spawn.client, ticket_id), []).append(spawn)


def _index_requeue(
    requeues: dict[_TicketKey, list[datetime]], event: OrchestratorEvent
) -> None:
    ticket_id = _as_str(event.payload.get("ticket_id"))
    if ticket_id is None:
        return
    key: _TicketKey = (_as_str(event.payload.get("client")), ticket_id)
    requeues.setdefault(key, []).append(event.created_at)


def _index_events(
    events: list[OrchestratorEvent],
) -> tuple[
    dict[str, _Spawn],
    dict[_TicketKey, list[_Spawn]],
    dict[str, list[_Event]],
    dict[_TicketKey, list[datetime]],
]:
    spawns: dict[str, _Spawn] = {}
    spawns_by_ticket: dict[_TicketKey, list[_Spawn]] = {}
    by_session: dict[str, list[_Event]] = {}
    requeues: dict[_TicketKey, list[datetime]] = {}
    for event in events:
        if event.type == OrchestratorEventType.TICKET_REQUEUED:
            _index_requeue(requeues, event)
            continue
        session_id = _as_str(event.payload.get("session_id"))
        if session_id is None:
            continue
        by_session.setdefault(session_id, []).append(
            _Event(event.created_at, event.type, event.payload)
        )
        if event.type == OrchestratorEventType.SESSION_SPAWNED:
            _index_spawn(spawns, spawns_by_ticket, event, session_id)
    return spawns, spawns_by_ticket, by_session, requeues


def _events_for(session_id: str, index: _Index) -> list[_Event]:
    return index.by_session.get(session_id, [])


def _has_result(session_id: str, index: _Index) -> bool:
    return any(
        ev.etype == OrchestratorEventType.SESSION_RESULT_EMITTED
        for ev in _events_for(session_id, index)
    )


def _released_before(predecessor: _Spawn, at: datetime, index: _Index) -> bool:
    """True when *predecessor* was released before *at* (a successor's spawn)."""
    if predecessor.session_id in index.operator_closed_ids:
        return True
    key: _TicketKey = (predecessor.client, predecessor.ticket_id)
    if any(predecessor.at <= requeued < at for requeued in index.requeues.get(key, [])):
        return True
    return any(
        ev.etype in _RELEASE_TYPES and ev.at < at
        for ev in _events_for(predecessor.session_id, index)
    )


def _is_superseding_predecessor(
    predecessor: _Spawn, successor: _Spawn, index: _Index
) -> bool:
    if predecessor.at >= successor.at:
        return False
    late_result = any(
        ev.etype == OrchestratorEventType.SESSION_RESULT_EMITTED
        and ev.at >= successor.at
        for ev in _events_for(predecessor.session_id, index)
    )
    return late_result and _released_before(predecessor, successor.at, index)


def _is_superseded(session_id: str, index: _Index) -> bool:
    """#2407: a released predecessor's late result landed after this spawn."""
    successor = index.spawns.get(session_id)
    if successor is None:
        return False
    key: _TicketKey = (successor.client, successor.ticket_id)
    return any(
        _is_superseding_predecessor(candidate, successor, index)
        for candidate in index.spawns_by_ticket.get(key, [])
        if candidate.session_id != session_id
    )


def _is_operator_close(session_id: str, index: _Index) -> bool:
    return session_id in index.operator_closed_ids


def _usage_signal(ev: _Event) -> bool:
    if ev.etype == OrchestratorEventType.SESSION_COMPLETED:
        return ev.payload.get("reason") == _REASON_USAGE_MID_TURN
    if ev.etype == OrchestratorEventType.SESSION_TIMED_OUT:
        return ev.payload.get("cause") == _CAUSE_USAGE_LIMIT_CUTOFF
    if ev.etype == OrchestratorEventType.SESSION_REAP_PROPOSED:
        return ev.payload.get("reason") in _USAGE_REAP_REASONS
    if ev.etype == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
        return ev.payload.get("paused_status") == _PAUSED_USAGE_MID_TURN
    return False


def _is_usage_limit(session_id: str, index: _Index) -> bool:
    return any(_usage_signal(ev) for ev in _events_for(session_id, index))


def _phantom_signal(ev: _Event) -> bool:
    if ev.etype == OrchestratorEventType.SESSION_PHANTOM_REVERTED:
        return True
    if ev.etype == OrchestratorEventType.SESSION_REAP_PROPOSED:
        return ev.payload.get("reason") in _PHANTOM_REAP_REASONS
    if ev.etype == OrchestratorEventType.SESSION_COMPLETED:
        return ev.payload.get("reason") == _REASON_PHANTOM_REAP_MERGED
    return False


def _is_phantom(session_id: str, index: _Index) -> bool:
    return any(_phantom_signal(ev) for ev in _events_for(session_id, index))


def _is_crash(session_id: str, index: _Index) -> bool:
    return any(
        ev.etype == OrchestratorEventType.SESSION_COMPLETED
        and ev.payload.get("crashed") is True
        for ev in _events_for(session_id, index)
    )


def _is_reap(session_id: str, index: _Index) -> bool:
    reap_types = {
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
        OrchestratorEventType.SESSION_TIMED_OUT,
    }
    return any(ev.etype in reap_types for ev in _events_for(session_id, index))


def _is_parked(session_id: str, index: _Index) -> bool:
    events = _events_for(session_id, index)
    parked = any(
        ev.etype == OrchestratorEventType.SESSION_NEEDS_ATTENTION for ev in events
    )
    return parked and not any(ev.etype in _TERMINAL_TYPES for ev in events)


def _is_completed(session_id: str, index: _Index) -> bool:
    return any(
        ev.etype == OrchestratorEventType.SESSION_COMPLETED
        for ev in _events_for(session_id, index)
    )


def _is_in_flight(session_id: str, index: _Index) -> bool:
    return session_id in index.live_session_ids


_CLASSIFIERS: tuple[tuple[NoResultCause, _Predicate], ...] = (
    (NoResultCause.LATE_SENTINEL_SUPERSEDED, _is_superseded),
    (NoResultCause.OPERATOR_CLOSE, _is_operator_close),
    (NoResultCause.USAGE_LIMIT, _is_usage_limit),
    (NoResultCause.PHANTOM, _is_phantom),
    (NoResultCause.CRASH, _is_crash),
    (NoResultCause.REAP, _is_reap),
    (NoResultCause.PARKED_NEEDS_ATTENTION, _is_parked),
    (NoResultCause.COMPLETED_NO_RESULT, _is_completed),
    (NoResultCause.IN_FLIGHT, _is_in_flight),
)


def _classify_one(session_id: str, index: _Index) -> NoResultCause:
    for cause, predicate in _CLASSIFIERS:
        if predicate(session_id, index):
            return cause
    return NoResultCause.UNCLASSIFIED


def _bucket(
    cause: NoResultCause, entries: list[tuple[datetime, str]], samples: int
) -> CauseBucket:
    return CauseBucket(
        cause=cause,
        count=len(entries),
        sample_session_ids=tuple(session_id for _, session_id in entries[:samples]),
    )


def classify_sessions(
    events: Iterable[OrchestratorEvent],
    *,
    operator_closed_ids: frozenset[str],
    live_session_ids: frozenset[str],
    window_start: datetime,
    window_end: datetime,
    samples: int,
) -> OutcomeReport:
    """Classify the spawned sessions in ``[window_start, window_end]``.

    Pure: no I/O. ``events`` should include pre-window spawns and results so
    the late-sentinel rule can see a predecessor spawned before the window.
    """
    ordered = sorted(events, key=lambda ev: ev.created_at)
    spawns, spawns_by_ticket, by_session, requeues = _index_events(ordered)
    index = _Index(
        spawns=spawns,
        spawns_by_ticket=spawns_by_ticket,
        by_session=by_session,
        requeues=requeues,
        operator_closed_ids=operator_closed_ids,
        live_session_ids=live_session_ids,
    )
    entries: dict[NoResultCause, list[tuple[datetime, str]]] = {
        cause: [] for cause in NoResultCause
    }
    spawned = 0
    with_result = 0
    clients: set[str] = set()
    for spawn in spawns.values():
        if not window_start <= spawn.at <= window_end:
            continue
        spawned += 1
        if spawn.client is not None:
            clients.add(spawn.client)
        if _has_result(spawn.session_id, index):
            with_result += 1
            continue
        cause = _classify_one(spawn.session_id, index)
        entries[cause].append((spawn.at, spawn.session_id))

    buckets = tuple(
        _bucket(cause, sorted(entries[cause]), samples) for cause in NoResultCause
    )
    oldest = ordered[0].created_at if ordered else None
    return OutcomeReport(
        window_start=window_start,
        window_end=window_end,
        clients=tuple(sorted(clients)),
        spawned=spawned,
        with_result=with_result,
        without_result=spawned - with_result,
        buckets=buckets,
        oldest_event_at=oldest,
        window_truncated=oldest is not None and oldest > window_start,
    )


def _window_clients(
    events: list[OrchestratorEvent], window_start: datetime
) -> list[str]:
    clients = {
        client
        for ev in events
        if ev.type == OrchestratorEventType.SESSION_SPAWNED
        and ev.created_at >= window_start
        for client in [_as_str(ev.payload.get("client"))]
        if client is not None
    }
    return sorted(clients)


def _operator_closed_ids(clients: list[str]) -> frozenset[str]:
    """Session ids of operator closes, read from each client's history log."""
    closed: set[str] = set()
    for client in clients:
        for entry in load_history(
            client, event_types=[HistoryEventType.SESSION_COMPLETED]
        ):
            session_id = entry.metadata.get("session_id")
            if entry.detail == _OPERATOR_CLOSE_DETAIL and session_id:
                closed.add(session_id)
    return frozenset(closed)


def build_report(
    *,
    days: int,
    now: datetime,
    client_names: frozenset[str] | None,
    live_session_ids: frozenset[str],
    samples: int,
) -> OutcomeReport:
    """Read the inbox and history for *days* back from *now* and classify.

    Read-only: consumer ``None`` advances no cursor. ``OSError`` and
    ``ValueError`` (corrupt interior inbox line) propagate unchanged; the
    callers own the error boundary.
    """
    window_start = now - timedelta(days=days)
    events = read_events(event_types=list(_READ_TYPES), client_names=client_names)
    clients = _window_clients(events, window_start)
    return classify_sessions(
        events,
        operator_closed_ids=_operator_closed_ids(clients),
        live_session_ids=live_session_ids,
        window_start=window_start,
        window_end=now,
        samples=samples,
    )


def live_ids_from_state(state: CwState) -> frozenset[str]:
    """Session ids whose status is not terminal in *state*."""
    return frozenset(
        session.id
        for session in state.sessions
        if session.status not in TERMINAL_SESSION_STATUSES
    )


def _window_label(report: OutcomeReport) -> str:
    start = report.window_start.isoformat(timespec="minutes")
    end = report.window_end.isoformat(timespec="minutes")
    return f"window {start} to {end}"


def format_report(report: OutcomeReport) -> str:
    """Render the breakdown as plain text for the terminal."""
    lines: list[str] = []
    clients = ", ".join(report.clients) or "none"
    if report.spawned == 0:
        lines.append(f"no worker sessions spawned in window ({_window_label(report)})")
    else:
        lines.extend(_headline_lines(report, clients))
        lines.extend(_table_lines(report))
    if report.window_truncated:
        lines.append(
            "note: window starts before the oldest inbox event; events may be pruned"
        )
    return "\n".join(lines)


def _headline_lines(report: OutcomeReport, clients: str) -> list[str]:
    largest = report.largest_cause
    largest_line = (
        f"Largest failure cause: {largest.value} ({report.count(largest)})"
        if largest is not None
        else "Largest failure cause: none"
    )
    return [
        f"Worker sessions spawned: {report.spawned} ({_window_label(report)};"
        f" clients: {clients})",
        f"With a result: {report.with_result}",
        f"Without a result: {report.without_result}"
        f" (no-result rate {report.no_result_rate:.1%};"
        f" failure rate excl. in-flight {report.failure_rate_excl_in_flight:.1%})",
        largest_line,
        "",
    ]


def _table_lines(report: OutcomeReport) -> list[str]:
    lines = [f"{'cause':<28} {'count':>6} {'share':>7}  sample session ids"]
    for bucket in report.buckets:
        if bucket.count == 0:
            continue
        share = bucket.count / report.without_result
        samples = ", ".join(bucket.sample_session_ids) or "-"
        lines.append(
            f"{bucket.cause.value:<28} {bucket.count:>6} {share:>7.1%}  {samples}"
        )
    return lines


def report_to_json(report: OutcomeReport) -> dict[str, object]:
    """Serialise the report with a stable, versioned key set."""
    without = report.without_result
    largest = report.largest_cause
    return {
        "schema_version": SCHEMA_VERSION,
        "window": {
            "start": report.window_start.isoformat(),
            "end": report.window_end.isoformat(),
        },
        "clients": list(report.clients),
        "spawned": report.spawned,
        "with_result": report.with_result,
        "without_result": without,
        "no_result_rate": report.no_result_rate,
        "failure_rate_excl_in_flight": report.failure_rate_excl_in_flight,
        "largest_cause": largest.value if largest is not None else None,
        "oldest_event_at": (
            report.oldest_event_at.isoformat()
            if report.oldest_event_at is not None
            else None
        ),
        "window_truncated": report.window_truncated,
        "buckets": [
            {
                "cause": bucket.cause.value,
                "count": bucket.count,
                "share_of_no_result": (bucket.count / without if without else 0.0),
                "sample_session_ids": list(bucket.sample_session_ids),
            }
            for bucket in report.buckets
        ],
    }
