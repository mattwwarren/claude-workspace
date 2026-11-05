"""Binding operator constraints recorded on a dev-queue row (GitHub #2964).

``cw review constrain`` records the operator's binding constraint text on
:attr:`TicketTask.operator_constraints`, an append-only list no requeue or
regress clears. The codex review prompt and the codex fix loop read it from
the row, so it reaches them on every tracker, independent of any tracker
comment, provenance marker or operator login. The tracker is never read or
written here.

Append-only: there is no edit, supersede or revoke path (retraction is tracked
in #2968). Two caps are enforced at record time and refuse rather than
truncate, because the text is injected verbatim into every review prompt and
fix cycle: :data:`MAX_CONSTRAINT_BYTES` per entry and
:data:`MAX_TOTAL_CONSTRAINT_BYTES` over the row's whole list.

Event-first, like ``operator_resolutions`` (#2394): the
``ticket.constraint_recorded`` event is recorded before the row is saved, so a
failed audit write leaves nothing recorded. A duplicate (the stripped text of
ANY existing entry) records neither event nor save.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.models.operator_constraint import OperatorConstraint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import QueueItemStatus

_log = logging.getLogger(__name__)

MAX_CONSTRAINT_BYTES = 16 * 1024
"""Per-entry cap: UTF-8 bytes of the stripped constraint text."""

MAX_TOTAL_CONSTRAINT_BYTES = 64 * 1024
"""Total cap: UTF-8 bytes over the row's stored entries plus the new one."""

UNRESOLVED_ACTOR = "unresolved"
"""The actor stored when the operator's gh login could not be resolved."""

_APPEND_ONLY = "retraction is tracked in #2968"


class OperatorConstraintRecord(TypedDict):
    """What :func:`record_operator_constraint` did."""

    # The ticket id as passed.
    ticket_id: str
    # The resolved client name.
    client: str
    # The 1-based ordinal of the entry THIS call appended; on a duplicate, the
    # matching existing entry's round (no ordinal is consumed).
    round: int
    # The actor resolved for THIS call (UNRESOLVED_ACTOR when unresolved). A
    # duplicate never rewrites the existing entry's own actor.
    actor: str
    # False iff THIS call had no resolved login.
    actor_resolved: bool
    # True iff the stripped text equals an existing entry's; nothing appended.
    duplicate: bool
    # The row's status, unchanged by the record.
    row_status: QueueItemStatus
    # UTF-8 bytes across the row's entries after this call.
    total_bytes: int
    # The appended entry, or on a duplicate the existing matching entry.
    entry: OperatorConstraint


def record_operator_constraint(
    ticket_id: str,
    client_name: str,
    text: str,
    *,
    actor: str | None,
    bounded: bool = False,
) -> OperatorConstraintRecord:
    """Append *text* to the row's ``operator_constraints``, under one lock.

    *actor* is the operator's gh login, or ``None`` when it could not be
    resolved; the entry then records :data:`UNRESOLVED_ACTOR` and the audit
    event ``actor_resolved: false``. Authority comes from where the command
    runs, not from the login, so an unresolved login is not refused.

    Raises:
        CwError: the text is blank, over :data:`MAX_CONSTRAINT_BYTES`, or would
            take the row over :data:`MAX_TOTAL_CONSTRAINT_BYTES`; no matching
            row exists; or recording the audit event failed. Nothing is
            recorded on any of these paths.
        OSError: the queue save failed after the audit event was recorded
            (logged at ERROR, re-raised for the caller to report).
    """
    with _lock(bounded=bounded):
        return _record_locked(ticket_id, client_name, text, actor=actor)


def _record_locked(
    ticket_id: str, client_name: str, text: str, *, actor: str | None
) -> OperatorConstraintRecord:
    """Lock-free body of :func:`record_operator_constraint`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Every
    refusal and the audit event precede ``save_dev_queue``.
    """
    stripped = _validated_text(ticket_id, text)
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)
    existing = task.operator_constraints
    stored_bytes = _entries_bytes(existing)
    normalized_actor = actor.strip() if actor is not None else ""
    recorded_actor = normalized_actor or UNRESOLVED_ACTOR
    actor_resolved = bool(normalized_actor)
    match = next((entry for entry in existing if entry.text == stripped), None)
    if match is not None:
        # Nothing new to record, so no event and no queue save.
        return _result(
            ticket_id,
            client_name,
            entry=match,
            actor=recorded_actor,
            actor_resolved=actor_resolved,
            duplicate=True,
            row_status=task.status,
            total_bytes=stored_bytes,
        )
    size = len(stripped.encode("utf-8"))
    _check_total(ticket_id, stored_bytes, size)
    entry = OperatorConstraint(
        text=stripped,
        recorded_at=datetime.now(UTC),
        actor=recorded_actor,
        round=len(existing) + 1,
    )
    existing.append(entry)
    record = _result(
        ticket_id,
        client_name,
        entry=entry,
        actor=recorded_actor,
        actor_resolved=actor_resolved,
        duplicate=False,
        row_status=task.status,
        total_bytes=stored_bytes + size,
    )
    _audit_event(record, stripped)
    try:
        save_dev_queue(store)
    except Exception:
        _log.error(
            "operator constraint save failed for %s/%s after %s was recorded;"
            " the row carries no new constraint",
            client_name,
            ticket_id,
            OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED.value,
            exc_info=True,
        )
        raise
    return record


def _result(
    ticket_id: str,
    client_name: str,
    *,
    entry: OperatorConstraint,
    actor: str,
    actor_resolved: bool,
    duplicate: bool,
    row_status: QueueItemStatus,
    total_bytes: int,
) -> OperatorConstraintRecord:
    return {
        "ticket_id": ticket_id,
        "client": client_name,
        "round": entry.round,
        "actor": actor,
        "actor_resolved": actor_resolved,
        "duplicate": duplicate,
        "row_status": row_status,
        "total_bytes": total_bytes,
        "entry": entry,
    }


def _entries_bytes(entries: Sequence[OperatorConstraint]) -> int:
    """UTF-8 bytes across *entries*' texts."""
    return sum(len(entry.text.encode("utf-8")) for entry in entries)


def _validated_text(ticket_id: str, text: str) -> str:
    """*text* stripped; refuse it when blank or over the per-entry cap."""
    stripped = text.strip()
    if not stripped:
        msg = (
            f"Cannot record a constraint for ticket '{ticket_id}': the constraint"
            " text is blank. Nothing was recorded."
        )
        raise CwError(msg)
    size = len(stripped.encode("utf-8"))
    if size > MAX_CONSTRAINT_BYTES:
        msg = (
            f"Cannot record a constraint for ticket '{ticket_id}': the text is"
            f" {size} UTF-8 bytes, over the {MAX_CONSTRAINT_BYTES}-byte per-entry"
            " cap. Nothing was recorded. Constraints are append-only and cannot"
            f" be edited or removed ({_APPEND_ONLY}), so state only what is"
            " binding: the forbidden mechanisms and why, not the surrounding"
            " discussion."
        )
        raise CwError(msg)
    return stripped


def _check_total(ticket_id: str, existing: int, size: int) -> None:
    """Refuse an entry that takes the row over the total cap."""
    if existing + size <= MAX_TOTAL_CONSTRAINT_BYTES:
        return
    msg = (
        f"Cannot record a constraint for ticket '{ticket_id}': the row already"
        f" holds {existing} UTF-8 bytes of constraints and this entry adds"
        f" {size}, over the {MAX_TOTAL_CONSTRAINT_BYTES}-byte total cap. Nothing"
        " was recorded. Constraints are append-only and cannot be consolidated"
        f" or removed ({_APPEND_ONLY}); shorten this entry to what is binding."
    )
    raise CwError(msg)


def _audit_event(record: OperatorConstraintRecord, text: str) -> None:
    """Record ``ticket.constraint_recorded``; any failure records nothing.

    The payload carries a digest and size of the text, never the text.
    """
    event_type = OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED
    encoded = text.encode("utf-8")
    payload: dict[str, object] = {
        "ticket_id": record["ticket_id"],
        "client": record["client"],
        "round": record["round"],
        "actor": record["actor"],
        "actor_resolved": record["actor_resolved"],
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "text_bytes": len(encoded),
        "total_bytes": record["total_bytes"],
        "row_status": record["row_status"].value,
    }
    try:
        record_event(event_type, payload, correlation_id=record["ticket_id"])
    except Exception as exc:
        # Why: event-first (#2394). Nothing has been saved yet, so aborting
        # persists nothing; swallowing the failure would land a constraint
        # whose audit trail silently lacks its record.
        msg = (
            f"Cannot record a constraint for ticket '{record['ticket_id']}':"
            f" recording the {event_type.value} audit event failed"
            f" ({exc.__class__.__name__}: {exc}). Nothing was recorded; fix the"
            " events inbox, then re-run constrain."
        )
        raise CwError(msg) from exc
