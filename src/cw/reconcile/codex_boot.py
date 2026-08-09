"""Boot-time pass over codex sessions orphaned by a crash (GitHub #1727).

Since ``CodexExecutor.spawn()`` hands its review to a background thread, an
ordinary process exit can land mid-review. ``run_dispatch_loop``'s shutdown
path covers the exits we control by bounded-joining those threads
(``cw.codex_background.join_outstanding_codex_threads``). A crash or ``SIGKILL``
is the case a join cannot reach at all: the process that owned the thread is
already gone, so there is nothing left to join and nothing recorded a failure.
What survives is a session still marked ``ACTIVE``, a task still ``RUNNING``,
and possibly a half-committed worktree.

This module is the other half of that pair: run once per process before the
first dispatch tick, it treats any live codex-origin headless ``DAEMON``
session as evidence of exactly that, and flags it for an operator rather than
guessing at recovery. It reuses two existing primitives verbatim rather than
inventing a disposition path:

- ``cw.dispatch.claim._park_running_task_blocked_on_user`` — the shared
  "park this task for operator inspection and emit SESSION_NEEDS_ATTENTION"
  primitive already used by the dirty-worktree guard and the codex capability
  gate (#1238, #1257).
- ``cw.executor.resolve_executor_config(...).backend != CODEX_BACKEND`` — the
  codex-origin test, lifted from ``claim.py``'s capability gate.

This is a blast-radius bound, not a liveness handle: it does not make codex
sessions crash-recoverable in the RFC 0005 F3 sense (no PID/surface_ref is
persisted for external harvest). See the ``StageExecutor`` Protocol invariant
comment in ``cw.executor`` for the accepted gap this bounds.
"""

from __future__ import annotations

import logging

from cw.config import load_clients, load_state
from cw.dev_queue import load_dev_queue
from cw.models import CODEX_BACKEND, SessionOrigin
from cw.reconcile._shared import _LIVE_STATUSES, _is_headless, ticket_id_for_session

_log = logging.getLogger(__name__)

# Short reason code stamped as the task's disposition and carried as the
# SESSION_NEEDS_ATTENTION payload's ``paused_status``.
CODEX_ORPHANED_AT_BOOT_DISPOSITION = "codex_review_orphaned_at_boot"

_ORPHAN_BREADCRUMBS = (
    "ACTIVE codex-origin session found at process start; its background review"
    " thread did not survive the prior process exit (crash/SIGKILL) — inspect"
    " the worktree for a partial commit or orphaned scratch dir before"
    " reclaiming."
)


def reap_orphaned_codex_sessions_at_boot() -> int:
    """Park every live codex-origin session found at process start.

    Returns the number of tasks parked. Never raises on an unresolvable
    session (unknown client, no matching dev-queue row, unparseable name) —
    this runs on the boot path, where refusing to start is strictly worse
    than skipping one ambiguous session.
    """
    # Deferred for import-cycle reasons, uniformly for both modules: claim.py
    # imports cw.executor at module level, and cw.executor imports cw.reconcile
    # at module level — so this module (inside the cw.reconcile package) must
    # not reach into either at import time. Mirrors _shared.py's own deferred
    # cw.dispatch import (#698).
    from cw.dispatch.claim import _park_running_task_blocked_on_user
    from cw.executor import resolve_executor_config

    state = load_state()
    # Keyed by (ticket_id, client), not ticket_id alone: ticket numbering is
    # per-client, so a claude-workspace ticket 21 and another client's ticket 21
    # are different tasks. Keying on ticket_id alone would let one client's row
    # shadow the other's and park the wrong client's live session. Matches
    # _park_running_task_blocked_on_user's own (ticket_id, client) key exactly.
    task_by_ticket = {
        (task.ticket_id, task.client): task for task in load_dev_queue().tasks
    }
    clients = load_clients()

    parked = 0
    for session in state.sessions:
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
        ):
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        task = task_by_ticket.get((ticket_id, session.client))
        client = clients.get(session.client)
        if task is None or client is None:
            continue
        # Identity, not coincidence of (ticket_id, client): an earlier boot's
        # orphan can linger in state as an ACTIVE record long after its task was
        # parked, recovered, and re-dispatched onto a fresh session. Without this
        # check that zombie re-matches on every later boot and parks whatever
        # healthy review now owns the row. This is a cheap early-exit against
        # the snapshot read above, not the safety guarantee itself — the row
        # could still be re-claimed between here and the park call below, so
        # the same identity is re-verified atomically under the lock via
        # expected_session_id (#1727 round 5).
        if task.session_id != session.id:
            continue
        if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
            continue
        _log.warning(
            "codex_boot: session %s (%s/%s) was still ACTIVE at process start;"
            " parking the task for operator inspection",
            session.id,
            session.client,
            ticket_id,
        )
        _park_running_task_blocked_on_user(
            ticket_id=ticket_id,
            client_name=session.client,
            expected_session_id=session.id,
            disposition=CODEX_ORPHANED_AT_BOOT_DISPOSITION,
            breadcrumbs=_ORPHAN_BREADCRUMBS,
        )
        parked += 1
    return parked
