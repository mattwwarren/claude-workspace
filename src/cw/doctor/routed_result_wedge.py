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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from cw.events import record_event
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    ReapReason,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile._shared import ProposedAction, _sentinel_partial_route_consumed
from cw.reconcile.routed_result_sessions import (
    find_stranded_routed_sessions,
    stranded_close_command,
)

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore
    from cw.native_daemon import NativeDaemonClient
    from cw.reconcile.routed_result_sessions import StrandedRoutedSession

WEDGE_ROUTED_RESULT_STRANDED = "wedge/active-routed-result-stranded"

_CLOSE_MUTATIONS: tuple[str, ...] = ("session_status_completed", "daemon_stopped")


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
        f"row), or {stranded_close_command(hit.session.id)} to close just this one."
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
    native_live = get_native_daemon_client().list_live_session_short_ids()
    hits = find_stranded_routed_sessions(
        state,
        queue.tasks,
        now=datetime.now(UTC),
        native_live=native_live,
        config=load_orchestrator_config(),
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


def _stop_and_audit(hit: StrandedRoutedSession, daemon: NativeDaemonClient) -> None:
    """Stop *hit*'s worker, then emit its ``session.reap_authorized`` audit event.

    Runs after every lock is released. A failed stop is swallowed, as in
    ``_reap_session_by_selector``: the session is already COMPLETED, so the
    #2481 leaked-worker sweep stops the worker on the next reconcile tick.
    """
    with contextlib.suppress(Exception):
        daemon.stop(hit.surface_ref)
    session = hit.session
    record_event(
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
        payload={
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": hit.ticket_id,
            "lane": hit.lane,
            "authority": "operator",
            "proposed_action": ProposedAction.CLOSE_ROUTED_RESULT_SESSION.value,
            "mutations": list(_CLOSE_MUTATIONS),
        },
        correlation_id=hit.ticket_id or session.id,
    )


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
    if not target_ids:
        return []
    daemon = get_native_daemon_client()
    native_live = daemon.list_live_session_short_ids()
    config = load_orchestrator_config()
    with dev_queue_lock():
        tasks = _deps.load_dev_queue().tasks
    # Why (#2491, operator decision D3): bounded=True because this is reached
    # only from `cw doctor --reap`, never from an unattended loop, and only
    # reads precede the acquisition here. Earlier steps of
    # _reap_wedge_findings may already have saved queue changes before this
    # lock -- the same partial-state window classes 6/8 accept (#2504) -- so
    # a SessionsLockTimeoutError leaves a state the next `cw doctor --reap`
    # re-detects idempotently.
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
            save_state(state)
    for hit in closed:
        _stop_and_audit(hit, daemon)
    return [hit.session.id for hit in closed]
