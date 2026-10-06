"""Doctor wedge class for sessions stranded after their result was routed (#2524).

``wedge/active-routed-result-stranded``: an ACTIVE/IDLE DAEMON session whose
staged result a #2458 partial route already routed (the ticket's row
advanced), with no occupied row bound to it, a stale transcript, and its
worker still live in the daemon roster. The reconcile pass pages it once
(``cw.reconcile.routed_result_sessions``); this module is the operator side:

- :func:`_check_wedge_routed_result_session` reports it on every ``cw doctor``
  run, through the same shared detector the reconcile page uses.
- :func:`reap_routed_result_findings` closes it on ``cw doctor --reap`` only
  -- an explicit operator command (ADR-0014 invariant 2), so it closes
  regardless of ``reap_policy``, as classes 6 and 8 do.

The close flips the session alone (COMPLETED, ``completed_reason=USER``,
``reap_reason=ROUTED_RESULT_STRANDED``) and stops its daemon worker. It never
touches a queue row -- the row already advanced past this session -- and never
emits ``session.completed``, whose consumer would apply this session's
``last_result`` to a later-claimed row of the same ticket. That is also why it
does not reuse ``cw.doctor.loop_health._reap_session_by_selector``: that
helper reverts a RUNNING row and falls through to collapsing the ticket's
BLOCKED_ON_USER rows. Its lock ordering is mirrored instead: flip and save
under ``sessions_lock``, release, stop the daemon, then emit the audit event.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.config import (
    events_dir,
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
    state_file,
)
from cw.dev_queue import dev_queue_lock
from cw.doctor import _deps
from cw.doctor._shared import WedgeFinding
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    ReapReason,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile._shared import ProposedAction, _sentinel_partial_route_consumed
from cw.reconcile.liveness_page import close_command
from cw.reconcile.routed_result_sessions import find_stranded_routed_sessions

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import CwState, DevQueueStore
    from cw.native_daemon import NativeDaemonClient
    from cw.reconcile.routed_result_sessions import StrandedRoutedSession

WEDGE_ROUTED_RESULT_STRANDED = "wedge/active-routed-result-stranded"

_AUDIT_OUTBOX_NAME = "routed_result_reap_audit.json"
_AUDIT_OUTBOX_LOCK_NAME = ".routed_result_reap_audit.lock"
_logger = logging.getLogger(__name__)
_audit_lock_depth: ContextVar[int] = ContextVar("audit_lock_depth", default=0)


@contextlib.contextmanager
def _audit_outbox_lock() -> Any:
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


def _stop_daemon_best_effort(
    daemon: NativeDaemonClient, surface_ref: str
) -> tuple[bool, str | None]:
    """Stop a worker and return a durable success/error summary."""
    with contextlib.suppress(Exception):
        daemon.stop(surface_ref)
        return True, None
    return False, "daemon stop failed"


def _routed_result_recipe(hit: StrandedRoutedSession) -> str:
    """Recipe text for one finding; names the session id and both remedies."""
    row = (
        f"{hit.stage.value}/{hit.row_status.value}"
        if hit.row_status is not None
        else "absent from the queue"
    )
    return (
        f"Session {hit.session.id} already routed its result and never "
        f"completed; ticket {hit.ticket_id}'s row is {row}. It holds a ceiling "
        "slot and its worktree, and nothing acts on it automatically. Run: "
        "cw doctor --reap (closes every session of this class, never a queue "
        f"row), or {close_command(hit.session.id)} to close just this one."
    )


def _check_wedge_routed_result_session(
    state: CwState, queue: DevQueueStore
) -> list[WedgeFinding]:
    """Detect live sessions stranded after their result was routed (#2524).

    Returns immediately when no session carries the #2458 consumed marker, so
    an ordinary run reads neither the daemon roster nor the orchestrator
    config for this class. Otherwise one finding per
    :func:`~cw.reconcile.routed_result_sessions.find_stranded_routed_sessions`
    hit. Report-only: the close is :func:`reap_routed_result_findings`.
    """
    if not any(_sentinel_partial_route_consumed(s) for s in state.sessions):
        return []
    try:
        config = load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        # Skip rather than detect with default thresholds the operator did not
        # set; the failed orchestrator.yaml check already reports the cause.
        return []
    native_live = get_native_daemon_client().list_live_session_short_ids()
    hits = find_stranded_routed_sessions(
        state,
        queue.tasks,
        now=datetime.now(UTC),
        native_live=native_live,
        config=config,
    )
    return [
        WedgeFinding(
            wedge_class=WEDGE_ROUTED_RESULT_STRANDED,
            session_id=hit.session.id,
            ticket_id=hit.ticket_id,
            recipe=_routed_result_recipe(hit),
            state_file=str(state_file()),
        )
        for hit in hits
    ]


def _audit_outbox_path() -> Path:
    """Return the durable outbox path for class-11 close audits."""
    return events_dir() / _AUDIT_OUTBOX_NAME


def _read_audit_outbox() -> list[dict[str, Any]]:
    """Read the close-audit outbox, failing closed on malformed durable data."""
    with _audit_outbox_lock():
        path = _audit_outbox_path()
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
        path = _audit_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(records, indent=2, sort_keys=True) + "\n")


def has_pending_routed_result_audits() -> bool:
    """Whether a prior class-11 close still needs audit delivery."""
    try:
        return bool(_read_audit_outbox())
    except (OSError, ValueError):
        _logger.exception("cannot inspect routed-result audit outbox")
        return True


def _queue_audit_intents(hits: list[StrandedRoutedSession]) -> None:
    """Write pending audit intents before their corresponding state save."""
    with _audit_outbox_lock():
        records = _read_audit_outbox()
        known = {record.get("session_id") for record in records}
        recorded_at = datetime.now(UTC).isoformat()
        for hit in hits:
            if hit.session.id in known:
                continue
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
                        "ticket_id": hit.ticket_id,
                        "lane": hit.lane,
                        "proposed_action": (
                            ProposedAction.CLOSE_ROUTED_RESULT_SESSION.value
                        ),
                        "mutations": ["session_status_completed"],
                        "correlation_id": hit.ticket_id or hit.session.id,
                    },
                    "surface_ref": hit.session.surface_ref,
                    "status": "pending_stop",
                    "payload": {
                        "session_id": hit.session.id,
                        "session_name": hit.session.name,
                        "client": hit.session.client,
                        "ticket_id": hit.ticket_id,
                        "lane": hit.lane,
                        "authority": "operator",
                        "proposed_action": (
                            ProposedAction.CLOSE_ROUTED_RESULT_SESSION.value
                        ),
                        "mutations": ["session_status_completed"],
                        "daemon_stop_succeeded": None,
                    },
                    "correlation_id": hit.ticket_id or hit.session.id,
                }
            )
        _write_audit_outbox(records)


def _finalize_audit_intent(
    session_id: str,
    *,
    mutations: list[str],
    stop_succeeded: bool,
    stop_error: str | None,
) -> dict[str, Any] | None:
    """Persist the actual stop result and return the resulting event record."""
    with _audit_outbox_lock():
        records = _read_audit_outbox()
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
    _logger.error(
        "missing durable audit intent for routed-result session %s", session_id
    )
    return None


def _emit_audit_record(record: dict[str, Any]) -> bool:
    """Emit one outbox record, retaining it when the event inbox is unavailable."""
    # The event bus assigns random event ids, so use this stable logical id
    # when checking whether a prior successful delivery survived a failed
    # outbox cleanup. Holding the outbox lock across the check, append and
    # cleanup also prevents two doctor processes from delivering the same
    # authorization concurrently.
    audit_event_id = f"routed-result-session-reap:{record.get('session_id')}"
    with _audit_outbox_lock():
        try:
            already_delivered = any(
                event.payload.get("session_id") == record.get("session_id")
                and event.payload.get("proposed_action")
                == ProposedAction.CLOSE_ROUTED_RESULT_SESSION.value
                and event.type is OrchestratorEventType.SESSION_REAP_AUTHORIZED
                for event in read_events(
                    event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED]
                )
            )
        except Exception:
            _logger.exception("cannot inspect audit delivery %s", audit_event_id)
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
                    "routed-result close audit is pending in %s",
                    _audit_outbox_path(),
                )
                return False
        try:
            records = [
                item
                for item in _read_audit_outbox()
                if item.get("session_id") != record.get("session_id")
            ]
            _write_audit_outbox(records)
        except Exception:
            _logger.exception(
                "routed-result audit delivered but outbox cleanup failed for %s",
                record.get("session_id"),
            )
        return True


def _retry_pending_audits(daemon: NativeDaemonClient) -> None:
    """Retry durable audits left by an earlier close or inbox failure."""
    try:
        records = _read_audit_outbox()
    except (OSError, ValueError):
        _logger.exception("cannot read routed-result audit outbox")
        return
    for record in records:
        if record.get("status") == "pending_stop":
            try:
                state = load_state()
                session = next(
                    (s for s in state.sessions if s.id == record.get("session_id")),
                    None,
                )
            except (OSError, ValueError):
                _logger.exception("cannot inspect pending routed-result audit")
                continue
            if session is None or session.status is not SessionStatus.COMPLETED:
                _logger.warning(
                    "routed-result audit intent remains pending for open session %s",
                    record.get("session_id"),
                )
                continue
            stop_succeeded, stop_error = _stop_daemon_best_effort(
                daemon, record["surface_ref"]
            )
            mutations = ["session_status_completed"]
            if stop_succeeded:
                mutations.append("daemon_stopped")
            finalized = _finalize_audit_intent(
                str(record["session_id"]),
                mutations=mutations,
                stop_succeeded=stop_succeeded,
                stop_error=stop_error,
            )
            if finalized is not None:
                _emit_audit_record(finalized)
        elif record.get("status") == "pending_event":
            _emit_audit_record(record)


def _stop_and_audit(hit: StrandedRoutedSession, daemon: NativeDaemonClient) -> None:
    """Stop *hit*'s worker, then emit its ``session.reap_authorized`` audit event.

    Runs after every lock is released. A failed stop is swallowed, as in
    ``_reap_session_by_selector``: the session is already COMPLETED, so the
    #2481 leaked-worker sweep stops the worker on the next reconcile tick.
    """
    stop_succeeded, stop_error = _stop_daemon_best_effort(daemon, hit.surface_ref)
    if not stop_succeeded:
        _logger.warning("failed to stop routed-result worker %s", hit.session.id)
    mutations = ["session_status_completed"]
    if stop_succeeded:
        mutations.append("daemon_stopped")
    record = _finalize_audit_intent(
        hit.session.id,
        mutations=mutations,
        stop_succeeded=stop_succeeded,
        stop_error=stop_error,
    )
    if record is not None:
        _emit_audit_record(record)


def reap_routed_result_findings(findings: list[WedgeFinding]) -> list[str]:
    """Close every still-stranded session named by *findings*; return their ids.

    Operator-only (``cw doctor --reap``). Re-detects on fresh state under the
    lock, so a session that gained a bound row or fresh transcript activity
    since the finding was collected is skipped. Lock ordering: roster and
    config reads outside every lock; a queue snapshot under
    ``dev_queue_lock``, released before ``sessions_lock`` is taken (the two
    are never nested, and nothing writes the queue); the flip and one
    ``save_state`` under ``sessions_lock``; the daemon stop and the audit
    event after it is released.
    """
    target_ids = {
        f.session_id
        for f in findings
        if f.session_id and f.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
    }
    daemon = get_native_daemon_client()
    _retry_pending_audits(daemon)
    if not target_ids:
        return []
    native_live = daemon.list_live_session_short_ids()
    config = load_orchestrator_config()
    with dev_queue_lock():
        tasks = _deps.load_dev_queue().tasks
    # Why (#2491, operator decision D3): bounded=True because this is reached
    # only from `cw doctor --reap`, never from an unattended loop, and only
    # reads precede the acquisition here. On the _reap_wedge_findings path,
    # earlier steps may already have saved queue changes before this lock --
    # the partial-state window classes 6/8 share (#2504) -- and a
    # SessionsLockTimeoutError there is caught and reported as a failing
    # `wedge-reap` check, so the window is reported rather than aborted. The
    # direct caller cli/maintenance.py::_reap_routed_report does NOT catch it,
    # so the error propagates there. Either way the next `cw doctor --reap`
    # re-detects the state idempotently.
    with sessions_lock(bounded=True):
        state = load_state()
        now = datetime.now(UTC)
        closed = [
            hit
            for hit in find_stranded_routed_sessions(
                state, tasks, now=now, native_live=native_live, config=config
            )
            if hit.session.id in target_ids
        ]
        for hit in closed:
            hit.session.status = SessionStatus.COMPLETED
            hit.session.completed_at = now
            hit.session.completed_reason = CompletionReason.USER
            hit.session.reap_reason = ReapReason.ROUTED_RESULT_STRANDED
        if closed:
            _queue_audit_intents(closed)
            save_state(state)
    for hit in closed:
        _stop_and_audit(hit, daemon)
    return [hit.session.id for hit in closed]
