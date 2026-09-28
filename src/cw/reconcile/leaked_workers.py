"""Sweep for daemon roster workers whose owning cw session is gone (#2480).

A finished worker's daemon surface can outlive its cw session: the Stop
hook's ``rescued=None`` bail, ``cw done``, ``cw spawn close``, and
``cw spawn complete --force`` all had paths that flipped a session to
COMPLETED without ever calling ``NativeDaemonClient.stop()`` (see the
individual fixes in ``cw.session.done_session``, ``cw.cli.spawn.
_spawn_close_impl`` and ``_spawn_complete_impl``). Before this sweep, such a
worker sat live in ``roster.json`` forever, and
:func:`cw.worktree.live_home_reason` reported its ticket's worktree occupied
indefinitely -- the ticket could never be re-dispatched (the #2480 bug).

This module is the automatic cleanup for workers that already leaked before
the completion-path fixes landed (or that leak some other way in future):
it finds every roster worker whose ``surface_ref`` names a cw session already
in a TERMINAL status (:data:`~cw.models.TERMINAL_SESSION_STATUSES` --
COMPLETED/TIMED_OUT), or no cw session at all, stops it, and emits one
``daemon.leaked_worker_stopped`` audit event per stop. Consulted from two
call sites that must never disagree about what counts as leaked:

- :func:`sweep_leaked_daemon_workers`, called unconditionally from
  ``cw.reconcile.core._reconcile_locked`` every tick (mandatory, not gated by
  ``reap_policy`` -- stopping an already-finished worker's surface has no
  queue/session state to protect, unlike the phantom sweep's destructive
  acts).
- ``cw doctor --reap``'s wedge (``cw.doctor.wedge._check_wedge_leaked_daemon_
  worker`` / the matching reap branch), for an operator who wants to see and
  clear the leak by hand outside the reconcile loop.

Both are thin callers around :func:`find_leaked_daemon_workers` and
:func:`stop_leaked_daemon_worker` so the detection predicate and the audit
payload shape can never drift between them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.events import record_event
from cw.models import TERMINAL_SESSION_STATUSES, OrchestratorEventType
from cw.reconcile._shared import ticket_id_for_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import CwState, Session
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)


class LeakedWorker(NamedTuple):
    """One roster worker whose owning cw session is terminal or absent (#2480).

    ``session`` is the matching :class:`~cw.models.Session` when its status is
    in :data:`~cw.models.TERMINAL_SESSION_STATUSES`, or ``None`` when no cw
    session's ``surface_ref`` names this worker at all (never tracked, or the
    record is gone).
    """

    short_id: str
    cwd: Path
    session: Session | None


def find_leaked_daemon_workers(
    state: CwState, *, daemon: NativeDaemonClient
) -> list[LeakedWorker] | None:
    """Return every roster worker whose owning cw session is gone, or None.

    ``None`` means the roster could not be read this tick
    (:meth:`~cw.native_daemon.NativeDaemonClient.list_live_worker_homes`
    fails closed) -- there is nothing safe to enumerate, so the caller does
    nothing and the next tick retries once the roster is readable again.

    A worker whose ``surface_ref`` matches a session still in a NON-terminal
    status (ACTIVE/IDLE/BACKGROUNDED) is excluded -- it is legitimately live,
    the same predicate :func:`cw.worktree.live_home_reason` applies to decide
    worktree occupancy (#2480). Matching by ``surface_ref`` rather than by
    ``cwd``: a per-ticket worktree is reused by many sessions across its
    pipeline lifetime, so a path match alone cannot tell which session, if
    any, currently owns a given roster entry.
    """
    homes = daemon.list_live_worker_homes()
    if homes is None:
        return None
    surface_ref_session: dict[str, Session] = {
        s.surface_ref: s for s in state.sessions if s.surface_ref is not None
    }
    leaked: list[LeakedWorker] = []
    for home in homes:
        session = surface_ref_session.get(home.short_id)
        if session is not None and session.status not in TERMINAL_SESSION_STATUSES:
            continue
        leaked.append(LeakedWorker(home.short_id, home.cwd, session))
    return leaked


def stop_leaked_daemon_worker(
    worker: LeakedWorker, *, daemon: NativeDaemonClient
) -> None:
    """Stop *worker* and emit one ``daemon.leaked_worker_stopped`` audit event.

    ``NativeDaemonClient.stop()`` is best-effort (swallows a missing or
    already-gone surface), so this never raises on a worker that another
    caller already stopped between detection and this call.
    """
    daemon.stop(worker.short_id)
    session = worker.session
    ticket_id = ticket_id_for_session(session.name) if session is not None else None
    payload: dict[str, object] = {
        "short_id": worker.short_id,
        "cwd": str(worker.cwd),
        "session_id": session.id if session is not None else None,
        "session_status": session.status.value if session is not None else None,
        "ticket_id": ticket_id,
    }
    record_event(
        OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED,
        payload,
        correlation_id=ticket_id,
    )
    _log.info(
        "daemon.leaked_worker_stopped: short_id=%s cwd=%s session_id=%s status=%s",
        worker.short_id,
        worker.cwd,
        session.id if session is not None else None,
        session.status.value if session is not None else None,
    )


def sweep_leaked_daemon_workers(
    state: CwState, *, daemon: NativeDaemonClient
) -> list[str]:
    """Stop every leaked daemon worker found; return the short ids stopped.

    Unconditional -- not gated by ``reap_policy`` (ADR-0006): unlike the
    phantom/stalled/idle sweeps, there is no queue row or session state this
    stop could clobber -- the owning session, if any, is already terminal.
    Returns an empty list (no-op) when the roster is unreadable
    (:func:`find_leaked_daemon_workers` returns ``None``) or empty.
    """
    leaked = find_leaked_daemon_workers(state, daemon=daemon)
    if not leaked:
        return []
    for worker in leaked:
        stop_leaked_daemon_worker(worker, daemon=daemon)
    return [worker.short_id for worker in leaked]
