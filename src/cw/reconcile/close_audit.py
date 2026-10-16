"""The durable audit of an operator session close (ADR-0006 invariant 3, #2694).

One mechanism, shared by every close an explicit operator command performs:
the routed-orphan close (``cw dev-queue approve``/``requeue``, #2517), inline
``cw spawn close``, queued closes applied by reconcile, and the ``cw dev-queue
cancel`` session step. Each close goes through three steps:

1. :func:`queue_close_audit_intent` -- before ANY mutation (the session flip
   and any queue-row step), a durable intent is appended to the audit outbox
   (``events_dir()/routed_result_reap_audit.json``). No intent, no close.
2. :func:`record_close_audit` -- after the close is saved, a
   ``SESSION_COMPLETED`` history event with the session identity, the
   command and the prior status.
3. :func:`finalize_close_audit_intent` -- the intent is finalized and
   delivered as a ``session.reap_authorized`` event, then removed.

A crash between steps leaves the intent in the outbox: a queued close keeps
its retry entry until step 3 commits and its replay finalizes the intent
without a second history event; ``cw doctor --reap`` delivers any intent left
behind (``cw.doctor.routed_result_wedge._retry_pending_audits``). Delivery is
deduplicated per intent (``audit_id``), so a session closed again after a
resume is audited again.

The outbox primitives moved here from ``cw.doctor.routed_result_wedge`` (the
``cw doctor --reap`` stranded routed-result close still uses them): reconcile
cannot import ``cw.doctor``, whose ``linkage`` module imports ``reconcile``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.atomic import atomic_write_text
from cw.config import events_dir
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent
from cw.history import record_event as record_history_event
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    OrchestratorEventType,
    SessionStatus,
)
from cw.reconcile._shared import ProposedAction
from cw.reconcile.routed_result_sessions import StrandedRoutedSession

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from cw.models import Session

# ``detail``/``reason`` of an operator close's history event, and the
# ``proposed_action`` of its ``session.reap_authorized`` event.
OPERATOR_CLOSE_REASON = "operator_close"

_AUDIT_OUTBOX_NAME = "routed_result_reap_audit.json"
_AUDIT_OUTBOX_LOCK_NAME = ".routed_result_reap_audit.lock"
_logger = logging.getLogger(__name__)
_audit_lock_depth: ContextVar[int] = ContextVar("audit_lock_depth", default=0)


class CloseAuditError(CwError):
    """A close's durable audit could not be prepared or written."""


@dataclass(frozen=True, slots=True)
class CloseAudit:
    """The fixed context of one operator close: what its audit records.

    *command* is the operator command; *reason* the history ``detail``;
    *proposed_action* the ``session.reap_authorized`` action. *client*
    defaults to the session's. *stop_succeeded* is the caller's confirmed
    daemon stop, or ``None`` when the stop runs after the audit (best effort,
    backed by the leaked-worker sweep). *details* extend the history
    metadata.
    """

    command: str
    reason: str
    proposed_action: str = OPERATOR_CLOSE_REASON
    ticket_id: str | None = None
    client: str | None = None
    stop_succeeded: bool | None = None
    details: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The audit outbox
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _audit_outbox_lock() -> Iterator[None]:
    """Serialize every audit-outbox read/modify/write transaction."""
    depth = _audit_lock_depth.get()
    if depth:
        yield
        return
    events_dir().mkdir(parents=True, exist_ok=True)
    lock_path = events_dir() / _AUDIT_OUTBOX_LOCK_NAME
    fd = lock_path.open("w")
    token = _audit_lock_depth.set(1)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        _audit_lock_depth.reset(token)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def audit_outbox_path() -> Path:
    """Return the durable outbox path for close audits."""
    return events_dir() / _AUDIT_OUTBOX_NAME


def read_audit_outbox() -> list[dict[str, Any]]:
    """Read the close-audit outbox, failing closed on malformed durable data."""
    with _audit_outbox_lock():
        path = audit_outbox_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            msg = f"invalid routed-result audit outbox: {path}"
            raise ValueError(msg)
        return raw


def _write_audit_outbox(records: list[dict[str, Any]]) -> None:
    """Atomically persist close-audit records before a session close commits."""
    with _audit_outbox_lock():
        path = audit_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(records, indent=2, sort_keys=True) + "\n")


def queue_audit_intents(
    hits: Sequence[StrandedRoutedSession],
    *,
    proposed_action: str = ProposedAction.CLOSE_ROUTED_RESULT_SESSION.value,
) -> None:
    """Write pending audit intents before their corresponding state save.

    A session that already has an intent in the outbox keeps it (idempotent
    per session). Each intent carries an ``audit_id`` that identifies this
    close, so its delivery is deduplicated per intent, not per session.
    """
    with _audit_outbox_lock():
        records = read_audit_outbox()
        known = {record.get("session_id") for record in records}
        recorded_at = datetime.now(UTC).isoformat()
        for hit in hits:
            if hit.session.id in known:
                continue
            ticket_id = hit.ticket_id or None
            records.append(
                {
                    "session_id": hit.session.id,
                    # Immutable mutation intent: this ledger entry is appended
                    # before the session commit and retained until the audit event
                    # lands. Delivery status below may change; this record does not.
                    "mutation_record": {
                        "recorded_at": recorded_at,
                        "session_id": hit.session.id,
                        "session_name": hit.session.name,
                        "client": hit.session.client,
                        "ticket_id": ticket_id,
                        "lane": hit.lane,
                        "proposed_action": proposed_action,
                        "mutations": ["session_status_completed"],
                        "correlation_id": ticket_id or hit.session.id,
                    },
                    "surface_ref": hit.session.surface_ref,
                    "status": "pending_stop",
                    "payload": {
                        "audit_id": f"{hit.session.id}@{recorded_at}",
                        "session_id": hit.session.id,
                        "session_name": hit.session.name,
                        "client": hit.session.client,
                        "ticket_id": ticket_id,
                        "lane": hit.lane,
                        "authority": "operator",
                        "proposed_action": proposed_action,
                        "mutations": ["session_status_completed"],
                        "daemon_stop_succeeded": None,
                    },
                    "correlation_id": ticket_id or hit.session.id,
                }
            )
        _write_audit_outbox(records)


def finalize_audit_intent(
    session_id: str,
    *,
    mutations: list[str],
    stop_succeeded: bool | None,
    stop_error: str | None,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    """Persist the actual stop result and return the resulting event record.

    ``None`` when *session_id* has no intent; that is logged as an error
    unless *missing_ok* (a replay, where the intent may already be delivered).
    """
    with _audit_outbox_lock():
        records = read_audit_outbox()
        for record in records:
            if record.get("session_id") != session_id:
                continue
            payload = record["payload"]
            payload["mutations"] = mutations
            payload["daemon_stop_succeeded"] = stop_succeeded
            if stop_error is not None:
                payload["daemon_stop_error"] = stop_error
            record["status"] = "pending_event"
            _write_audit_outbox(records)
            return record
    if not missing_ok:
        _logger.error("missing durable audit intent for session %s", session_id)
    return None


def _already_delivered(record: dict[str, Any]) -> bool:
    """Whether *record*'s ``session.reap_authorized`` event is already in the inbox.

    Matched by ``audit_id``; a record written before ``audit_id`` existed
    falls back to its session id and proposed action.
    """
    payload = record.get("payload") or {}
    audit_id = payload.get("audit_id")
    for event in read_events(
        event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED]
    ):
        if audit_id is not None:
            if event.payload.get("audit_id") == audit_id:
                return True
        elif event.payload.get("session_id") == record.get(
            "session_id"
        ) and event.payload.get("proposed_action") == payload.get("proposed_action"):
            return True
    return False


def emit_audit_record(record: dict[str, Any]) -> bool:
    """Emit one outbox record, retaining it when the event inbox is unavailable."""
    # The event bus assigns random event ids, so use the intent's stable
    # audit_id when checking whether a prior successful delivery survived a
    # failed outbox cleanup. Holding the outbox lock across the check, append
    # and cleanup also prevents two processes from delivering the same intent
    # concurrently.
    with _audit_outbox_lock():
        try:
            already_delivered = _already_delivered(record)
        except Exception:
            _logger.exception(
                "cannot inspect audit delivery %s", record.get("session_id")
            )
            return False
        if not already_delivered:
            try:
                record_event(
                    OrchestratorEventType.SESSION_REAP_AUTHORIZED,
                    payload=record["payload"],
                    correlation_id=record["correlation_id"],
                )
            except Exception:
                _logger.exception(
                    "close audit is pending in %s",
                    audit_outbox_path(),
                )
                return False
        try:
            records = [
                item
                for item in read_audit_outbox()
                if item.get("session_id") != record.get("session_id")
            ]
            _write_audit_outbox(records)
        except Exception:
            _logger.exception(
                "close audit delivered but outbox cleanup failed for %s",
                record.get("session_id"),
            )
        return True


# ---------------------------------------------------------------------------
# One operator close's audit
# ---------------------------------------------------------------------------


def queue_close_audit_intent(session: Session, audit: CloseAudit) -> None:
    """Write *session*'s durable close intent; call it BEFORE any mutation.

    Raises :class:`CloseAuditError` when the outbox cannot be written: the
    caller must then not close the session.
    """
    hit = StrandedRoutedSession(
        session=session,
        ticket_id=audit.ticket_id or "",
        lane=DEFAULT_LANE,
        stage=DEFAULT_STAGE,
        row_status=None,
        stale_minutes=0.0,
        surface_ref=session.surface_ref or "",
    )
    try:
        queue_audit_intents([hit], proposed_action=audit.proposed_action)
    except (OSError, ValueError) as exc:
        _logger.exception(
            "close_audit_intent_failed: command=%s ticket_id=%s session_id=%s",
            audit.command,
            audit.ticket_id,
            session.id,
        )
        message = (
            f"Could not prepare the durable audit record for session"
            f" {session.id} before closing it: {exc}. Fix the audit outbox"
            f" ({audit_outbox_path()}) and retry the command."
        )
        raise CloseAuditError(message) from exc


def record_close_audit(
    session: Session, audit: CloseAudit, *, prior_status: SessionStatus
) -> None:
    """Record the close of *session* as a ``SESSION_COMPLETED`` history event.

    Call it after the close is saved. Raises :class:`CloseAuditError` (logged
    with its traceback) when the history cannot be written; the close stands.
    """
    metadata = {
        "actor": "operator",
        "command": audit.command,
        "ticket_id": audit.ticket_id or "",
        "client": audit.client or session.client,
        "session_id": session.id,
        "surface_ref": session.surface_ref or "",
        "prior_status": prior_status.value,
        "resulting_status": SessionStatus.COMPLETED.value,
        "reason": audit.reason,
        **audit.details,
    }
    event = HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client=session.client,
        session_id=session.id,
        session_name=session.name,
        purpose=session.purpose,
        detail=audit.reason,
        metadata=metadata,
    )
    try:
        record_history_event(session.client, event)
    except (OSError, ValueError) as exc:
        _logger.exception(
            "close_audit_failed: command=%s ticket_id=%s session_id=%s",
            audit.command,
            audit.ticket_id,
            session.id,
        )
        msg = (
            f"Session {session.id} was closed, but its durable audit record"
            f" could not be written: {exc}"
        )
        raise CloseAuditError(msg) from exc


def finalize_close_audit_intent(
    session: Session, audit: CloseAudit, *, replay: bool = False
) -> bool:
    """Finalize *session*'s intent and deliver it; call it after the close.

    Returns True once the intent is finalized in the durable outbox (its
    delivery is retried later if the event inbox is down), or, on a *replay*,
    when there was no intent left to finalize. False (logged, never raised)
    when the outbox cannot be read or written, or a non-replay call finds no
    intent: a queued close then keeps its retry entry.
    """
    mutations = ["session_status_completed"]
    if audit.stop_succeeded:
        mutations.append("daemon_stopped")
    try:
        record = finalize_audit_intent(
            session.id,
            mutations=mutations,
            stop_succeeded=audit.stop_succeeded,
            stop_error=None,
            missing_ok=replay,
        )
    except (OSError, ValueError):
        _logger.exception(
            "close_audit_finalize_failed: command=%s session_id=%s",
            audit.command,
            session.id,
        )
        return False
    if record is None:
        return replay
    emit_audit_record(record)
    return True
