"""Per-row GitHub issue-state cache for the closed-ticket guard (#1605).

Persisted in the shared ``dispatch_state.json`` sidecar under the
``"ticket_state_probe"`` key, one entry per queue-row identity
``f"{client}/{ticket_id}::{created_at}"`` (the #2219 row identity: a requeue
keeps ``created_at``, a fresh enqueue gets a new one). Written by
``cw.dispatch.ticket_state_gate`` (lock-free resolve) and by the claim path's
post-release ``cancelled_at`` stamp; read lock-free by the concierge
cancelled-row restore and the ``auto_fix_ci`` recipe, which must never
un-cancel a guard-cancelled row.

Entry semantics (all transient, no schema version, same posture as
``OpenPrProbeCache``):

* ``closed=True``: positive evidence the issue is CLOSED, TTL-bounded by the
  caller like an OPEN verdict so a reopened issue is revalidated.
* ``closed=False`` with ``checked_at``: an OPEN verdict, TTL-bounded by the
  caller.
* ``attempted_at``: the last read that produced no verdict (timeout, gh
  missing, an unknown state). Never a verdict; the caller backs off on it.
* ``cancelled_at``: the guard cancelled this exact row. A row that is PENDING
  again with this stamp was requeued by the operator, so the guard leaves it.

Imports ``cw.dispatch_state``; that module never imports this one. The state
file path is read at call time as ``dispatch_state.DISPATCH_STATE_FILE``
because the suite isolates state by patching that module attribute.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from cw import dispatch_state
from cw.atomic import atomic_write_text
from cw.config import refuse_real_state_write
from cw.dispatch_state import _load_dispatch_state_raw, dispatch_state_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

logger = logging.getLogger(__name__)

TICKET_STATE_PROBE_KEY = "ticket_state_probe"


class TicketStateProbe(NamedTuple):
    """One row's cached issue-state read (see the module docstring)."""

    checked_at: datetime | None
    closed: bool
    cancelled_at: datetime | None = None
    attempted_at: datetime | None = None


def ticket_state_client_prefix(client: str) -> str:
    """The sidecar-key prefix shared by row keys and client pruning."""
    return f"{client}/"


def ticket_state_key(client: str, ticket_id: str, created_at: datetime) -> str:
    """The sidecar key for one queue-row identity."""
    return f"{ticket_state_client_prefix(client)}{ticket_id}::{created_at.isoformat()}"


class _MalformedEntryError(Exception):
    """Internal: one entry field failed validation."""


def _optional_instant(value: object) -> datetime | None:
    """Parse an optional aware ISO instant; raise when unusable.

    A naive instant is rejected: comparing it with an aware "now" would raise.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _MalformedEntryError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _MalformedEntryError from exc
    if parsed.tzinfo is None:
        raise _MalformedEntryError
    return parsed


def _parse_ticket_state_entry(entry: object) -> TicketStateProbe | None:
    """Validate one raw entry, or None when malformed (per entry, like
    ``_parse_open_pr_probe_entry``: one bad entry never blinds its siblings)."""
    if not isinstance(entry, dict):
        return None
    closed = entry.get("closed")
    if not isinstance(closed, bool):
        return None
    try:
        return TicketStateProbe(
            checked_at=_optional_instant(entry.get("checked_at")),
            closed=closed,
            cancelled_at=_optional_instant(entry.get("cancelled_at")),
            attempted_at=_optional_instant(entry.get("attempted_at")),
        )
    except _MalformedEntryError:
        return None


def _serialize(entry: TicketStateProbe) -> dict[str, object]:
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    return {
        "checked_at": _iso(entry.checked_at),
        "closed": entry.closed,
        "cancelled_at": _iso(entry.cancelled_at),
        "attempted_at": _iso(entry.attempted_at),
    }


def _raw_entries(payload: Mapping[str, object]) -> dict[str, object]:
    raw = payload.get(TICKET_STATE_PROBE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def load_ticket_state_probes() -> dict[str, TicketStateProbe]:
    """Every well-formed entry; ``{}`` for an absent or corrupt sidecar."""
    probes: dict[str, TicketStateProbe] = {}
    for key, entry in _raw_entries(_load_dispatch_state_raw()).items():
        parsed = _parse_ticket_state_entry(entry)
        if parsed is not None:
            probes[key] = parsed
    return probes


def guard_cancelled_row_keys() -> frozenset[str]:
    """Row keys the guard cancelled (lock-free read, like the other loaders)."""
    return frozenset(
        key
        for key, probe in load_ticket_state_probes().items()
        if probe.cancelled_at is not None
    )


def _merge_dispatch_state(
    label: str,
    keys: Collection[str],
    mutate: Callable[[dict[str, object]], bool],
) -> None:
    """Run one generic raw-sidecar read/merge/write operation.

    This is the leaf persistence primitive: callers own the sidecar-specific
    parsing and mutation, while the shared file path, lock, atomic write and
    fail-safe error policy stay in one place. *mutate* edits the full payload
    in place and reports whether anything changed; an unchanged payload is not
    rewritten.
    """
    path = dispatch_state.DISPATCH_STATE_FILE
    try:
        refuse_real_state_write(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            if not mutate(payload):
                return
            atomic_write_text(path, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to %s ticket_state_probe entry/entries (%s)",
            label,
            sorted(keys),
            exc_info=True,
        )


def save_ticket_state_probes(
    client: str,
    entries: Mapping[str, TicketStateProbe],
    *,
    prune_to_keys: Collection[str],
) -> None:
    """Merge *entries* for *client* and drop its entries absent from *prune_to_keys*.

    *prune_to_keys* is the key of EVERY row of *client* in the snapshot (not
    only the candidates), so a guard-cancelled row's stamp survives while the
    row exists. Other clients' entries and other sidecar keys are untouched
    (#1157). No write when nothing changes. ``OSError`` is logged and
    swallowed: a lost write only costs a re-read next tick.
    """
    prefix = ticket_state_client_prefix(client)
    keep = frozenset(prune_to_keys)

    def _merge(payload: dict[str, object]) -> bool:
        raw = _raw_entries(payload)
        stale = [k for k in raw if k.startswith(prefix) and k not in keep]
        for key in stale:
            del raw[key]
        for key, entry in entries.items():
            raw[key] = _serialize(entry)
        payload[TICKET_STATE_PROBE_KEY] = raw
        return bool(stale or entries)

    _merge_dispatch_state("persist", list(entries), _merge)


def mark_ticket_closed_cancelled(key: str, *, at: datetime) -> None:
    """Stamp *key* as cancelled by the guard at *at* (best effort, logged)."""

    def _stamp(payload: dict[str, object]) -> bool:
        raw = _raw_entries(payload)
        current = _parse_ticket_state_entry(raw.get(key))
        base = current if current is not None else TicketStateProbe(at, closed=True)
        raw[key] = _serialize(base._replace(closed=True, cancelled_at=at))
        payload[TICKET_STATE_PROBE_KEY] = raw
        return True

    _merge_dispatch_state("stamp", [key], _stamp)
