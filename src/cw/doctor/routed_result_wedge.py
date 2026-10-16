"""Doctor wedge class for sessions stranded after their result was routed (#2524).

``wedge/active-routed-result-stranded``: an ACTIVE/IDLE DAEMON session whose
staged result a #2458 partial route already routed (the ticket's row
advanced), with no occupied row bound to it, a stale transcript, and its
worker still live in the daemon roster. The reconcile pass pages it once
(``cw.reconcile.routed_result_sessions``); this module is the operator side:

- :func:`_check_wedge_routed_result_session` reports it on every ``cw doctor``
  run, through the same shared detector the reconcile page uses.
- :func:`reap_routed_result_findings` closes it on ``cw doctor --reap`` only
  -- an explicit operator command (the orchestrator session counts, ADR-0020;
  ADR-0014 invariant 2), so it closes
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

The audit outbox it writes before the save is the shared operator-close audit
of :mod:`cw.reconcile.close_audit` (#2694); :func:`_retry_pending_audits`
delivers any intent a crashed close of any kind left behind.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import (
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
    state_file,
)
from cw.dev_queue import dev_queue_lock
from cw.doctor import _deps
from cw.doctor._shared import WedgeFinding
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    ReapReason,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile._shared import _sentinel_partial_route_consumed
from cw.reconcile.close_audit import (
    emit_audit_record,
    finalize_audit_intent,
    queue_audit_intents,
    read_audit_outbox,
)
from cw.reconcile.liveness_page import close_command
from cw.reconcile.routed_result_sessions import find_stranded_routed_sessions

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore
    from cw.native_daemon import NativeDaemonClient
    from cw.reconcile.routed_result_sessions import StrandedRoutedSession

WEDGE_ROUTED_RESULT_STRANDED = "wedge/active-routed-result-stranded"

_logger = logging.getLogger(__name__)


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


def has_pending_routed_result_audits() -> bool:
    """Whether a prior close still needs audit delivery (the shared outbox)."""
    try:
        return bool(read_audit_outbox())
    except (OSError, ValueError):
        _logger.exception("cannot inspect routed-result audit outbox")
        return True


def _retry_pending_audits(daemon: NativeDaemonClient) -> None:
    """Retry durable audits left by an earlier close or inbox failure."""
    try:
        records = read_audit_outbox()
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
            finalized = finalize_audit_intent(
                str(record["session_id"]),
                mutations=mutations,
                stop_succeeded=stop_succeeded,
                stop_error=stop_error,
            )
            if finalized is not None:
                emit_audit_record(finalized)
        elif record.get("status") == "pending_event":
            emit_audit_record(record)


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
    record = finalize_audit_intent(
        hit.session.id,
        mutations=mutations,
        stop_succeeded=stop_succeeded,
        stop_error=stop_error,
    )
    if record is not None:
        emit_audit_record(record)


def reap_routed_result_findings(findings: list[WedgeFinding]) -> list[str]:
    """Close every still-stranded session named by *findings*; return their ids.

    An explicit operator command (``cw doctor --reap``; the orchestrator session
    counts, ADR-0020). Re-detects on fresh state under the
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
            queue_audit_intents(closed)
            save_state(state)
    for hit in closed:
        _stop_and_audit(hit, daemon)
    return [hit.session.id for hit in closed]
