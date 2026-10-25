"""Operator resolutions recorded on a plan-parked row (GitHub #2702).

``cw dev-queue resolve`` records the operator's answers to a plan-stage park
on :attr:`TicketTask.operator_resolutions`, an append-only list no requeue
clears, so the next plan-stage spawn receives them in
``queue_metadata.operator_resolutions`` as binding resolutions. The tracker is
never read or written here; the CLI requeues the row as a separate step.

With ``approve=True`` it also stamps plan approval, through the same gate
helpers ``cw dev-queue approve`` runs, imported by name: the approval-gate
check, ``_stamp_plan_approval``, the #2311 body-drift check and the #968
plan-reviewed check, plus the pipeline and terminal-stage refusals. The
plan-reviewed result is reported, never routed on: the row always stays
parked at the plan stage, because advancing to IMPL would drop the
resolutions.

Event-first, like ``must_fix_override`` (#2394): the
``ticket.resolution_recorded`` event is recorded before the row is saved, so a
failed audit write leaves nothing recorded. A pure duplicate (the latest
entry's text, no ``--approve``) records neither event nor save (R4).
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from cw.config import get_client, load_state
from cw.dev_queue.approval import (
    _body_drift_message,
    _raise_if_not_at_approval_gate,
    _raise_if_terminal_stage,
    _raise_stage_not_in_pipeline,
    _record_body_drift,
    _stamp_plan_approval,
)
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.lifecycle import _plan_is_reviewed
from cw.dev_queue.next_commands import dev_queue_command
from cw.dev_queue.refusal import refusal_hint
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.models.enums import stage_position
from cw.models.operator_resolution import OperatorResolution
from cw.operator_identity import resolve_operator_login

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session, TicketTask

_log = logging.getLogger(__name__)

MAX_RESOLUTION_BYTES = 64 * 1024
"""Per-entry cap: UTF-8 bytes of the stripped decisions text."""


class OperatorResolutionRecord(TypedDict):
    """What :func:`record_operator_resolution` did."""

    # The ticket id as passed.
    ticket_id: str
    # The resolved client name.
    client: str
    # The 1-based ordinal of the entry THIS call appended; on a duplicate, the
    # existing latest entry's round (no ordinal is consumed).
    round: int
    # The login resolved for THIS call ("" when unresolved). A duplicate never
    # rewrites the existing entry's own actor.
    actor: str
    # True iff the stripped text equals the latest entry's; nothing appended.
    duplicate: bool
    # True iff THIS call stamped plan approval (never the row's earlier one).
    approved: bool
    # The draft digest THIS call bound, or None (not approving, or a sentinel
    # with no or a malformed fingerprint: an unbound approval).
    plan_approved_fingerprint: str | None
    # The #968 plan-reviewed result, or None when not approving. Reported only.
    plan_reviewed: bool | None
    # The #2311 body-drift warning text, or None. Advisory.
    body_drift_warning: str | None
    # The row's stage: always "plan", unchanged by the record.
    stage: str
    # The row's status: always "blocked_on_user"; the requeue is separate.
    status: str


class _GateStamp(TypedDict):
    """What :func:`_stamp_at_gate` stamped in memory."""

    plan_approved_fingerprint: str | None
    plan_reviewed: bool
    body_drift_warning: str | None


def record_operator_resolution(
    ticket_id: str,
    client_name: str,
    text: str,
    *,
    approve: bool = False,
    operator_initiated: bool = False,
    bounded: bool = False,
) -> OperatorResolutionRecord:
    """Append *text* to the row's ``operator_resolutions``, under one lock.

    ``operator_initiated`` is the caller's provenance, as in
    ``_approve_ticket_locked``: only the interactive CLI passes True, and
    ``approve=True`` without it is refused. The default is the fail-safe
    direction.

    Raises:
        ApproveGateError: the text is blank or over
            :data:`MAX_RESOLUTION_BYTES`, the row is not BLOCKED_ON_USER at
            the plan stage, ``approve`` is refused by the approve gate, or
            recording an audit event failed. Nothing is recorded on any of
            these paths.
        CwError: if no matching task is found.
    """
    with _lock(bounded=bounded):
        return _record_locked(
            ticket_id,
            client_name,
            text,
            approve=approve,
            operator_initiated=operator_initiated,
        )


def _record_locked(
    ticket_id: str,
    client_name: str,
    text: str,
    *,
    approve: bool,
    operator_initiated: bool,
) -> OperatorResolutionRecord:
    """Lock-free body of :func:`record_operator_resolution`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Every
    refusal and the audit event precede ``save_dev_queue``, and the store is
    local, so a raise persists nothing.
    """
    stripped = _validated_text(ticket_id, text)
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)
    _require_parked_plan_row(task)
    client_cfg = get_client(client_name)
    actor = _resolve_actor(client_cfg, client_name, ticket_id)

    existing = task.operator_resolutions
    duplicate = bool(existing) and existing[-1].text == stripped
    stamp = (
        _stamp_at_gate(
            task,
            ticket_id,
            client_name,
            client_cfg,
            operator_initiated=operator_initiated,
        )
        if approve
        else None
    )
    round_ = existing[-1].round if duplicate else len(existing) + 1
    record: OperatorResolutionRecord = {
        "ticket_id": ticket_id,
        "client": client_name,
        "round": round_,
        "actor": actor,
        "duplicate": duplicate,
        "approved": stamp is not None,
        "plan_approved_fingerprint": (
            stamp["plan_approved_fingerprint"] if stamp is not None else None
        ),
        "plan_reviewed": stamp["plan_reviewed"] if stamp is not None else None,
        "body_drift_warning": (
            stamp["body_drift_warning"] if stamp is not None else None
        ),
        "stage": task.stage.value,
        "status": task.status.value,
    }
    if duplicate and stamp is None:
        # R4: nothing new to record, so no event and no queue save.
        return record
    if not duplicate:
        existing.append(
            OperatorResolution(
                text=stripped,
                recorded_at=datetime.now(UTC),
                actor=actor,
                round=round_,
            )
        )
    _audit_event(record, stripped, operator_initiated=operator_initiated)
    try:
        save_dev_queue(store)
    except Exception:
        _log.error(
            "operator resolution save failed for %s/%s after %s was recorded;"
            " the row carries no new resolution and no new approval",
            client_name,
            ticket_id,
            OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED.value,
            exc_info=True,
        )
        raise
    return record


def _validated_text(ticket_id: str, text: str) -> str:
    """*text* stripped; refuse it when blank or over the per-entry cap."""
    stripped = text.strip()
    if not stripped:
        msg = (
            f"Cannot record a resolution for ticket '{ticket_id}': the decisions"
            " text is blank. Nothing was recorded."
        )
        raise ApproveGateError(msg)
    size = len(stripped.encode("utf-8"))
    if size > MAX_RESOLUTION_BYTES:
        msg = (
            f"Cannot record a resolution for ticket '{ticket_id}': the decisions"
            f" text is {size} UTF-8 bytes, over the {MAX_RESOLUTION_BYTES}-byte"
            " cap. Nothing was recorded."
        )
        raise ApproveGateError(msg)
    return stripped


def _require_parked_plan_row(task: TicketTask) -> None:
    """Raise unless *task* is parked BLOCKED_ON_USER at the plan stage."""
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot record a resolution for ticket '{task.ticket_id}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER.{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)
    if task.stage != Stage.PLAN:
        msg = (
            f"Cannot record a resolution for ticket '{task.ticket_id}': stage is"
            f" {task.stage.value!r}, expected the plan stage.{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)


def _resolve_actor(client_cfg: ClientConfig, client_name: str, ticket_id: str) -> str:
    """The operator login for this record; ``""`` plus a WARNING when unresolved."""
    actor = resolve_operator_login(client_cfg)
    if actor is None:
        _log.warning(
            "operator resolution for %s/%s: operator login unresolved; recording"
            " an empty actor",
            client_name,
            ticket_id,
        )
        return ""
    return actor


def _approve_sequence(task: TicketTask) -> str:
    """The record-then-re-park-then-approve sequence an off-gate row needs."""
    record = dev_queue_command("resolve", task, "--file", "<decisions.md>")
    approve = dev_queue_command("approve", task)
    return (
        f" Sequence: (1) record the resolutions without --approve (`{record}`);"
        " (2) let the plan stage fold them in and re-park at"
        f" plan_pending_approval; (3) approve that draft with `{approve}`."
    )


def _stamp_at_gate(
    task: TicketTask,
    ticket_id: str,
    client_name: str,
    client_cfg: ClientConfig,
    *,
    operator_initiated: bool,
) -> _GateStamp:
    """Stamp plan approval in memory through ``cw dev-queue approve``'s gate.

    Runs the approve gate's refusals in its order (pipeline, session, gate,
    terminal stage), then ``_stamp_plan_approval``, the #2311 body-drift
    check (operator-initiated and PLAN are guaranteed here) and the #968
    plan-reviewed check. ``_stamp_plan_approval`` is never called alone.
    """
    if not operator_initiated:
        msg = (
            f"Cannot use --approve on ticket '{ticket_id}': plan approval must be"
            " operator-initiated. Nothing was recorded."
        )
        raise ApproveGateError(msg)
    # Keep this cycle-breaking import deferred; ``cw.executor`` imports code
    # that reaches back through the ``cw.dev_queue`` package initializer.
    resolve_pipeline_stages = importlib.import_module(
        "cw.executor"
    ).resolve_pipeline_stages
    stages = resolve_pipeline_stages(task, client_cfg)
    if task.stage not in stages:
        _raise_stage_not_in_pipeline(ticket_id, task, stages, client_name)
    position = stage_position(task.stage, stages)
    session = _require_session(task, ticket_id)
    try:
        # Positional: the gate-helper spy in the operator-resolutions tests
        # records *args.
        _raise_if_not_at_approval_gate(ticket_id, session, task, position)
    except ApproveGateError as exc:
        msg = (
            f"Cannot use --approve on ticket '{ticket_id}': it is not at an"
            " approval gate, so there is no draft awaiting approval to stamp."
            f" Nothing was recorded.{_approve_sequence(task)} Gate check: {exc}"
        )
        raise ApproveGateError(msg) from exc
    _raise_if_terminal_stage(ticket_id, session, task, position)
    fingerprint = _stamp_plan_approval(task, task.stage.value, session)
    drift = _record_body_drift(task, client_cfg)
    return {
        "plan_approved_fingerprint": fingerprint,
        "plan_reviewed": _plan_is_reviewed(task, client_cfg),
        "body_drift_warning": (
            _body_drift_message(task, *drift) if drift is not None else None
        ),
    }


def _require_session(task: TicketTask, ticket_id: str) -> Session:
    """The row's bound session; refuse when it cannot be resolved."""
    session = (
        load_state().find_by_name_or_id(task.session_id)
        if task.session_id is not None
        else None
    )
    if session is None:
        msg = (
            f"Cannot use --approve on ticket '{ticket_id}': its session was not"
            f" found (session_id={task.session_id!r}), so the approval cannot be"
            " bound to a draft. Nothing was recorded."
        )
        raise ApproveGateError(msg)
    return session


def _audit_event(
    record: OperatorResolutionRecord, text: str, *, operator_initiated: bool
) -> None:
    """Record ``ticket.resolution_recorded``; any failure records nothing.

    The payload carries a digest and size of the text, never the text.
    """
    event_type = OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED
    encoded = text.encode("utf-8")
    payload: dict[str, object] = {
        "ticket_id": record["ticket_id"],
        "client": record["client"],
        "round": record["round"],
        "actor": record["actor"],
        "duplicate": record["duplicate"],
        "approved": record["approved"],
        "operator_initiated": operator_initiated,
        "plan_approved_fingerprint": record["plan_approved_fingerprint"],
        "plan_reviewed": record["plan_reviewed"],
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "text_bytes": len(encoded),
    }
    try:
        record_event(event_type, payload, correlation_id=record["ticket_id"])
    except Exception as exc:
        # Why: event-first (#2394). Nothing has been saved yet, so aborting
        # persists nothing; swallowing the failure would land a resolution
        # whose audit trail silently lacks its record.
        msg = (
            f"Cannot record a resolution for ticket '{record['ticket_id']}':"
            f" recording the {event_type.value} audit event failed"
            f" ({exc.__class__.__name__}: {exc}). Nothing was recorded; fix the"
            " events inbox, then re-run resolve."
        )
        raise ApproveGateError(msg) from exc
