"""Constraints recorded on a dev-queue row (GitHub #2964, #2984).

``cw review constrain`` records the operator's binding constraint text on
:attr:`TicketTask.operator_constraints`, an append-only list no requeue or
regress clears. The codex review prompt and the codex fix loop read it from
the row, so it reaches them on every tracker, independent of any tracker
comment, provenance marker or operator login. The tracker is never read or
written here.

:func:`record_plan_constraints` (#2984) appends the approved plan's decided
sections to the same list under a plan-derived ``kind``, with the fixed actor
:data:`~cw.models.operator_constraint.PLAN_ACTOR`. It refuses unless the
dispatcher-owned row ``stage`` is ``plan``: that is the trust boundary that
keeps a review or impl worker from minting decided input for its own reviewer.

Append-only: there is no edit, supersede or revoke path (retraction is tracked
in #2968). Three caps are enforced at record time and refuse rather than
truncate, because the text is injected verbatim into every review prompt and
fix cycle: :data:`MAX_CONSTRAINT_BYTES` per entry,
:data:`MAX_TOTAL_CONSTRAINT_BYTES` over the row's whole list, and
:data:`MAX_PLAN_CONSTRAINT_BYTES` over its plan-derived entries, so plan
entries can never crowd out the operator's.

Event-first, like ``operator_resolutions`` (#2394): the
``ticket.constraint_recorded`` event is recorded before the row is saved, so a
failed audit write leaves nothing recorded. A duplicate records neither event
nor save. An operator entry is a duplicate only of an earlier operator entry
with the same stripped text, so an operator constraint is never folded into a
plan-derived one; a plan-derived entry is a duplicate of ANY earlier entry.
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
from cw.models import OrchestratorEventType, Stage
from cw.models.operator_constraint import (
    OPERATOR_CONSTRAIN_COMMAND,
    OPERATOR_KIND,
    PLAN_ACTOR,
    OperatorConstraint,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import QueueItemStatus, TicketTask
    from cw.models.operator_constraint import ConstraintKind, PlanConstraintKind

_log = logging.getLogger(__name__)

MAX_CONSTRAINT_BYTES = 16 * 1024
"""Per-entry cap: UTF-8 bytes of the stripped constraint text."""

MAX_TOTAL_CONSTRAINT_BYTES = 64 * 1024
"""Total cap: UTF-8 bytes over the row's stored entries plus the new one."""

MAX_PLAN_CONSTRAINT_BYTES = 32 * 1024
"""Plan sub-budget (#2984): UTF-8 bytes over the plan-derived entries plus the
new one. Operator entries always keep the rest of the total."""

UNRESOLVED_ACTOR = "unresolved"
"""The actor stored when the operator's gh login could not be resolved."""

_APPEND_ONLY = "retraction is tracked in #2968"


class ConstraintCapError(CwError):
    """A size-cap refusal (#2984): nothing was recorded for that entry.

    Its own class so :func:`record_plan_constraints` can stop a batch on a cap
    and report the rest as unrecorded, while every other refusal propagates.
    """


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


class PlanRecordResult(TypedDict):
    """What :func:`record_plan_constraints` did (#2984)."""

    # The entries THIS call appended, in order.
    recorded: list[OperatorConstraint]
    # How many items matched an existing entry and appended nothing.
    duplicates: int
    # The items from the first cap breach on, in order; ``[]`` when none.
    unrecorded: list[tuple[PlanConstraintKind, str]]
    # The cap refusal that stopped the batch, or ``None``.
    unrecorded_reason: str | None


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


def record_plan_constraints(
    ticket_id: str,
    client_name: str,
    items: Sequence[tuple[PlanConstraintKind, str]],
    *,
    bounded: bool = False,
) -> PlanRecordResult:
    """Append the plan's decided *items* to the row, in order, under one lock.

    Each item is ``(kind, text)``, recorded with actor
    :data:`~cw.models.operator_constraint.PLAN_ACTOR` (``actor_resolved``
    true: the actor is a fixed label, not a login), deduped and event-first
    like an operator entry. The batch stops at the first cap breach and
    returns that item and every later one as ``unrecorded``; it never
    truncates and never raises mid-batch for a cap.

    Raises:
        CwError: no matching row; the row is not at the plan stage (checked
            once, before any item, so nothing is recorded); a blank item; or
            recording an item's audit event failed (earlier items stay
            recorded).
        OSError: a queue save failed after that item's audit event.
    """
    with _lock(bounded=bounded):
        _require_plan_stage(_find_ticket(load_dev_queue(), ticket_id, client_name))
        return _record_plan_locked(ticket_id, client_name, list(items))


def _record_plan_locked(
    ticket_id: str,
    client_name: str,
    items: list[tuple[PlanConstraintKind, str]],
) -> PlanRecordResult:
    """Lock-free body of :func:`record_plan_constraints`."""
    recorded: list[OperatorConstraint] = []
    duplicates = 0
    for index, (kind, text) in enumerate(items):
        try:
            record = _record_locked(
                ticket_id, client_name, text, actor=PLAN_ACTOR, kind=kind
            )
        except ConstraintCapError as exc:
            return {
                "recorded": recorded,
                "duplicates": duplicates,
                "unrecorded": [(k, t) for k, t in items[index:]],
                "unrecorded_reason": str(exc),
            }
        if record["duplicate"]:
            duplicates += 1
        else:
            recorded.append(record["entry"])
    return {
        "recorded": recorded,
        "duplicates": duplicates,
        "unrecorded": [],
        "unrecorded_reason": None,
    }


def _require_plan_stage(task: TicketTask) -> None:
    """Refuse a plan-derived entry unless the row is at the plan stage (#2984).

    The row's ``stage`` is dispatcher-owned, so a review or impl worker cannot
    move it to mint decided input for the lane that reviews it.
    """
    if task.stage == Stage.PLAN:
        return
    msg = (
        f"Cannot record plan decisions for ticket '{task.ticket_id}': plan"
        " decisions are recorded only while the row is in the plan stage, and"
        f" the row is at {task.stage.value}. Nothing was recorded. Use"
        f" `{OPERATOR_CONSTRAIN_COMMAND}` to record an operator constraint."
    )
    raise CwError(msg)


def _duplicate_of(
    existing: Sequence[OperatorConstraint], text: str, kind: ConstraintKind
) -> OperatorConstraint | None:
    """The existing entry *text* duplicates, or ``None`` (kind-aware, #2984).

    An operator entry dedupes against operator entries only, so an operator's
    constraint is never silently downgraded into a plan-derived one; a
    plan-derived entry dedupes against every entry.
    """
    return next(
        (
            entry
            for entry in existing
            if entry.text == text
            and (kind != OPERATOR_KIND or entry.kind == OPERATOR_KIND)
        ),
        None,
    )


def _record_locked(
    ticket_id: str,
    client_name: str,
    text: str,
    *,
    actor: str | None,
    kind: ConstraintKind = OPERATOR_KIND,
) -> OperatorConstraintRecord:
    """Lock-free body of :func:`record_operator_constraint`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Every
    refusal and the audit event precede ``save_dev_queue``. A plan-derived
    *kind* is refused unless the row is at the plan stage, before the
    duplicate check, and is held to the plan sub-budget too.
    """
    stripped = _validated_text(ticket_id, text)
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)
    if kind != OPERATOR_KIND:
        _require_plan_stage(task)
    existing = task.operator_constraints
    stored_bytes = _entries_bytes(existing)
    normalized_actor = actor.strip() if actor is not None else ""
    recorded_actor = normalized_actor or UNRESOLVED_ACTOR
    actor_resolved = bool(normalized_actor)
    match = _duplicate_of(existing, stripped, kind)
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
    if kind != OPERATOR_KIND:
        plan_bytes = _entries_bytes([e for e in existing if e.kind != OPERATOR_KIND])
        _check_plan_budget(ticket_id, plan_bytes, size)
    entry = OperatorConstraint(
        text=stripped,
        recorded_at=datetime.now(UTC),
        actor=recorded_actor,
        round=len(existing) + 1,
        kind=kind,
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
        raise ConstraintCapError(msg)
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
    raise ConstraintCapError(msg)


def _check_plan_budget(ticket_id: str, existing: int, size: int) -> None:
    """Refuse a plan-derived entry that takes the plan entries over budget."""
    if existing + size <= MAX_PLAN_CONSTRAINT_BYTES:
        return
    msg = (
        f"Cannot record a plan decision for ticket '{ticket_id}': the row"
        f" already holds {existing} UTF-8 bytes of plan-derived constraints and"
        f" this entry adds {size}, over the {MAX_PLAN_CONSTRAINT_BYTES}-byte plan"
        " sub-budget. Nothing was recorded for this entry; the rest of the"
        f" {MAX_TOTAL_CONSTRAINT_BYTES}-byte total stays reserved for operator"
        f" constraints ({_APPEND_ONLY})."
    )
    raise ConstraintCapError(msg)


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
        "kind": record["entry"].kind,
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
