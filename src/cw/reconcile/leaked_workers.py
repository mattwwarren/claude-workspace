"""Sweep for daemon roster workers that no live cw session owns (#2480, #2670).

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
the completion-path fixes landed (or that leak some other way in future).
A roster worker is stopped only on one of two arms:

- **Terminal arm.** Its ``surface_ref`` names a cw session already in a
  TERMINAL status (:data:`~cw.models.TERMINAL_SESSION_STATUSES` --
  COMPLETED/TIMED_OUT). A recorded terminal session is itself the evidence,
  so this arm is unconditional.
- **Absent arm (#2670).** No cw session names it at all, AND it is evidenced
  orphaned: its roster ``startedAt`` is at least
  :data:`LEAKED_WORKER_GRACE_SECONDS` old, and its ``cwd`` is strictly inside
  a cw worktree base of a configured client. A worker that fails either test,
  or whose age cannot be read, is spared. A ``spawn_bg`` worker is on the
  roster before cw records its session, so an unevidenced absence is not a
  leak. A hand-launched non-cw ``claude --bg`` worker is not in a cw base, so
  it is also spared.

The grace is a debounce, never the trigger (ADR-0014 invariant 1): the trigger
is absence evidence, and the clock only delays a stop that absence already
justifies.

Consulted from two call sites that must never disagree about what counts as
leaked:

- :func:`sweep_leaked_daemon_workers`, called unconditionally from
  ``cw.reconcile.locked_sweeps._run_pre_guard_sweeps`` (called from
  ``cw.reconcile.core._reconcile_locked``) every tick (mandatory, not gated by
  ``reap_policy`` -- the terminal arm stops an already-finished worker's
  surface, and the absent arm stops only evidenced orphans, so neither has
  queue/session state to protect, unlike the phantom sweep's destructive
  acts). That call passes ``reconcile()``'s post-lock sink, so each stop is
  queued and runs after ``sessions_lock`` releases (#1232).
- ``cw doctor --reap``'s wedge (``cw.doctor.wedge._check_wedge_leaked_daemon_
  worker`` / the matching reap branch), for an operator who wants to see and
  clear the leak by hand outside the reconcile loop. It holds no lock and
  stops inline (no sink).

Both are thin callers around :func:`find_leaked_daemon_workers` and
:func:`stop_leaked_daemon_worker` so the detection predicate and the audit
payload shape can never drift between them. Both pass the same ``now`` and
``clients``.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from cw.events import record_event
from cw.models import TERMINAL_SESSION_STATUSES, OrchestratorEventType
from cw.reconcile._shared import ticket_id_for_session
from cw.reconcile.deferred import PostLockJob, StopKind, is_surface_stop_queued
from cw.worktree import effective_worktree_bases

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from cw.models import CwState, Session
    from cw.models.client import ClientConfig
    from cw.native_daemon import NativeDaemonClient, WorkerHome
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger(__name__)

# Spawn-to-record debounce for the absent arm (#2670). It must exceed the
# window in which a freshly ``spawn_bg``'d worker is on the roster but not yet
# named by a recorded session: the roster poll (``_ROSTER_POLL_TIMEOUT_SECS``,
# 10 s) plus the transcript lookup plus the sessions-lock wait, and it must
# exceed ``SPAWN_GRACE_SECONDS``. It must stay below
# ``fix_dispatch_hold._ATTENTION_GRACE`` (5 min), because the #2590 hold relies
# on this sweep stopping an unrecorded fix worker before that page fires.
LEAKED_WORKER_GRACE_SECONDS: int = 180


class LeakedWorker(NamedTuple):
    """One roster worker whose owning cw session is terminal or absent (#2480).

    ``session`` is the matching :class:`~cw.models.Session` when its status is
    in :data:`~cw.models.TERMINAL_SESSION_STATUSES`, or ``None`` when no cw
    session's ``surface_ref`` names this worker at all and the worker is
    evidenced orphaned (#2670).
    """

    short_id: str
    cwd: Path
    session: Session | None


def _cw_worktree_bases(clients: Mapping[str, ClientConfig]) -> frozenset[Path]:
    """Return the union of every configured client's cw worktree bases.

    Path arithmetic and ``Path.resolve()`` only, no subprocess, so it is safe
    under ``sessions_lock`` (ADR-0019).
    """
    bases: set[Path] = set()
    for client in clients.values():
        bases |= effective_worktree_bases(client)
    return frozenset(bases)


def _is_evidenced_orphan(
    home: WorkerHome, *, now_ms: int, bases: frozenset[Path]
) -> bool:
    """Return True when *home* is old enough and homed in a cw worktree base.

    ``started_at_ms`` of ``None`` (absent, wrong-typed or non-positive in the
    roster) cannot be aged, so it is spared (fail closed). A ``cwd`` equal to a
    base, or a sibling-prefix path, is spared: only a strict descendant counts.
    """
    if home.started_at_ms is None:
        return False
    if now_ms - home.started_at_ms < LEAKED_WORKER_GRACE_SECONDS * 1000:
        return False
    return any(base in home.cwd.parents for base in bases)


def find_leaked_daemon_workers(
    state: CwState,
    *,
    daemon: NativeDaemonClient,
    now: datetime,
    clients: Mapping[str, ClientConfig],
) -> list[LeakedWorker] | None:
    """Return every roster worker that is terminal or absent-and-evidenced, or None.

    ``None`` means the roster could not be read this tick
    (:meth:`~cw.native_daemon.NativeDaemonClient.list_live_worker_homes`
    fails closed) -- there is nothing safe to enumerate, so the caller does
    nothing and the next tick retries once the roster is readable again.

    A worker whose ``surface_ref`` matches a session still in a NON-terminal
    status (ACTIVE/IDLE/BACKGROUNDED) is excluded -- it is legitimately live,
    the same predicate :func:`cw.worktree.live_home_reason` applies to decide
    worktree occupancy (#2480). A worker whose ``surface_ref`` matches a
    TERMINAL session is returned unconditionally. A worker that no session
    names is returned only when it is evidenced orphaned (#2670): older than
    :data:`LEAKED_WORKER_GRACE_SECONDS` at *now* and homed strictly inside a
    cw worktree base of one of *clients*. Matching by ``surface_ref`` rather
    than by ``cwd``: a per-ticket worktree is reused by many sessions across
    its pipeline lifetime, so a path match alone cannot tell which session, if
    any, currently owns a given roster entry.

    *now* must be timezone-aware (``ValueError`` otherwise); *clients* is the
    configured client map, and an empty map spares every absent worker.
    """
    if now.tzinfo is None:
        msg = "find_leaked_daemon_workers: now must be timezone-aware"
        raise ValueError(msg)
    homes = daemon.list_live_worker_homes()
    if homes is None:
        return None
    now_ms = int(now.timestamp() * 1000)
    bases = _cw_worktree_bases(clients)
    surface_ref_session: dict[str, Session] = {
        s.surface_ref: s for s in state.sessions if s.surface_ref is not None
    }
    leaked: list[LeakedWorker] = []
    for home in homes:
        session = surface_ref_session.get(home.short_id)
        if session is not None:
            if session.status not in TERMINAL_SESSION_STATUSES:
                continue
        elif not _is_evidenced_orphan(home, now_ms=now_ms, bases=bases):
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
    state: CwState,
    *,
    daemon: NativeDaemonClient,
    now: datetime,
    clients: Mapping[str, ClientConfig],
    deferred: DeferredReconcileJobs | None = None,
) -> list[str]:
    """Stop every leaked daemon worker found; return the leaked short ids.

    Unconditional -- not gated by ``reap_policy`` (ADR-0006): the terminal arm
    stops a worker whose owning session is already terminal, and the absent
    arm stops only a worker that is evidenced orphaned (see the module
    docstring). Neither clobbers queue or session state. Returns an empty list
    (no-op) when the roster is unreadable (:func:`find_leaked_daemon_workers`
    returns ``None``) or nothing is leaked.

    The absent arm's evidence is what closes the ``spawn_bg`` race: a worker
    registered on the roster whose session is not yet recorded is young (it
    is inside :data:`LEAKED_WORKER_GRACE_SECONDS`), so it is spared and not
    stopped. An unevidenced absent worker (no ``startedAt``, a non-cw cwd, or
    no configured base) is also spared, and it stays on the roster until
    stopped by hand (``claude stop <short_id>``).

    With *deferred* ``None`` (the lock-free ``cw doctor --reap`` path) each
    worker is stopped and audited inline. With a sink (``reconcile()``, which
    holds ``sessions_lock`` here) each stop-and-audit is queued as a
    ``leaked_worker_stop:<short_id>`` job and runs after the lock releases
    (#1232); a worker whose ``surface_stop`` is already queued this tick (the
    stalled sweep runs first and queues a stop for every session it
    completes) is skipped, so it is not stopped twice nor audited as leaked.
    Either way every leaked short id found is returned, including skipped ones.
    """
    leaked = find_leaked_daemon_workers(state, daemon=daemon, now=now, clients=clients)
    if not leaked:
        return []
    for worker in leaked:
        if deferred is None:
            stop_leaked_daemon_worker(worker, daemon=daemon)
        elif not is_surface_stop_queued(deferred, worker.short_id):
            deferred.post_lock.append(
                PostLockJob(
                    label=f"leaked_worker_stop:{worker.short_id}",
                    run=partial(stop_leaked_daemon_worker, worker, daemon=daemon),
                    # RECOVERABLE, and explicit: an untagged job is uncapped (#2551).
                    stop=StopKind.RECOVERABLE,
                )
            )
    return [worker.short_id for worker in leaked]
