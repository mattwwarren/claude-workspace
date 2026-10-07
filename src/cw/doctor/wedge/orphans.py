"""Orphaned-session and leaked-worker wedge detectors for ``cw doctor``.

ACTIVE/IDLE DAEMON sessions carrying neither liveness channel past their spawn
grace (class-9, ``wedge/active-null-liveness-orphan``, advisory only) with the
backend resolution and recipe helpers it uses, and daemon roster workers whose
owning cw session is terminal or absent (class-10,
``wedge/leaked-daemon-worker``). Imports ``_constants``. Split out of the
flat ``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import state_file
from cw.dispatch.claim import _find_running_row
from cw.doctor import _deps
from cw.doctor._shared import WedgeFinding
from cw.doctor.wedge._constants import (
    _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
    _WEDGE_LEAKED_DAEMON_WORKER,
)
from cw.exceptions import CwError
from cw.executor import resolve_executor_config
from cw.models import (
    CODEX_BACKEND,
    QueueItemStatus,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import SPAWN_GRACE_SECONDS, ticket_id_for_session
from cw.reconcile.leaked_workers import find_leaked_daemon_workers

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session


def _resolve_backend_for_orphan_check(
    session: Session,
    ticket_id: str | None,
    queue: DevQueueStore,
    clients: dict[str, ClientConfig],
) -> tuple[str | None, str | None]:
    """Resolve *session*'s executor backend: ``(backend, None)`` or ``(None, why)``.

    Threads ``(ticket_id, client) -> RUNNING task -> backend`` the way
    ``cw.reconcile.codex_boot`` does, including its identity check: the RUNNING
    row must carry ``session_id == session.id`` (via
    :func:`~cw.dispatch.claim._find_running_row`), so a lingering zombie can
    never borrow the backend of a row since re-dispatched onto a fresh session.
    Every miss is a *resolution failure* (the "backend unresolved" advisory
    variant), never a silent skip. A ``None`` *ticket_id* short-circuits first,
    before any client or queue lookup -- mirroring class-8's
    ``task_by_ticket.get(ticket_id) if ticket_id else None`` convention.
    """
    if ticket_id is None:
        return None, "session name does not encode a ticket id"
    client = clients.get(session.client)
    if client is None:
        return None, f"no clients.yaml entry for client {session.client!r}"
    if not any(
        t.ticket_id == ticket_id
        and t.client == session.client
        and t.status == QueueItemStatus.RUNNING
        for t in queue.tasks
    ):
        return None, f"no RUNNING task for ticket {ticket_id!r}"
    task = _find_running_row(queue, ticket_id, session.client, session_id=session.id)
    if task is None:
        return None, "RUNNING task belongs to a different session"
    return resolve_executor_config(task.stage, task, client).backend, None


def _is_null_liveness_candidate(session: Session, cutoff: datetime) -> bool:
    """True iff *session* is a live DAEMON row with neither liveness channel.

    ``local_liveness is None`` keeps this disjoint from
    ``cw.reconcile.local``'s harvest, which owns every null-surface_ref row
    that DOES carry a local-process handle. ORCHESTRATE is excluded as in
    ``compute_drift``. *cutoff* only debounces reporting (ADR-0014 allows a
    threshold to delay a signal); a row younger than it yields no finding.
    """
    return (
        session.origin is SessionOrigin.DAEMON
        and session.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        and session.purpose is not SessionPurpose.ORCHESTRATE
        and session.surface_ref is None
        and session.local_liveness is None
        and session.started_at <= cutoff
    )


def _null_liveness_orphan_recipe(
    session_id: str, backend: str | None, reason: str | None
) -> str:
    """Recipe text for a class-9 finding; names the session id, never its name."""
    if backend is not None:
        return (
            f"ACTIVE session {session_id} has no daemon surface and no liveness "
            "record past its spawn grace — it holds a ceiling slot. "
            f"Run: cw spawn close {session_id}"
        )
    return (
        f"ACTIVE session {session_id} has no daemon surface; could not resolve "
        f"its executor backend ({reason}). "
        f"Run: cw spawn close {session_id} if it is not running."
    )


def _check_wedge_active_null_liveness_orphan(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect DAEMON ACTIVE/IDLE sessions invisible to every reaper (#2237).

    A row with no ``surface_ref`` AND no ``local_liveness`` past
    ``SPAWN_GRACE_SECONDS`` is skipped by ``compute_drift`` (so class-6 and
    the reconcile phantom sweep never see it) and by class-8 (which needs a
    roster-present ref), while ``dispatch/tick.py``'s ``running_count`` still
    counts it against the client ceiling.

    Advisory only (ADR-0014): eligibility is absence plus elapsed time, with
    no roster, dead-PID, or terminal-result evidence, so
    :func:`_reap_wedge_findings` never mutates on it. The recipe names the
    per-session operator command, ``cw spawn close <id>``.

    ``CODEX_BACKEND`` sessions are excluded silently: CodexExecutor never sets
    either liveness channel by design (``executor.py``, #1727) and its orphans
    are owned by ``cw.reconcile.codex_boot`` / ``codex_reparks``
    (#2285/#2307). A backend that cannot be resolved at all still yields a
    finding, using the "backend unresolved" recipe variant, so a drifted queue
    row or a removed client entry cannot make the session invisible again.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    candidates = [s for s in state.sessions if _is_null_liveness_candidate(s, cutoff)]
    if not candidates:
        return []
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients, which drives every candidate through the "no clients.yaml
    # entry" advisory variant (mirrors _check_wedge_repo_ahead's guard).
    try:
        clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}

    findings: list[WedgeFinding] = []
    for session in candidates:
        ticket_id = ticket_id_for_session(session.name)
        backend, reason = _resolve_backend_for_orphan_check(
            session, ticket_id, queue, clients
        )
        if backend == CODEX_BACKEND:
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
                session_id=session.id,
                ticket_id=ticket_id,
                recipe=_null_liveness_orphan_recipe(session.id, backend, reason),
                state_file=str(state_file()),
            )
        )
    return findings


def _check_wedge_leaked_daemon_worker(state: CwState) -> list[WedgeFinding]:
    """Detect daemon roster workers whose owning cw session is gone (#2480).

    Surfaces every :class:`~cw.reconcile.leaked_workers.LeakedWorker`
    (:func:`~cw.reconcile.leaked_workers.find_leaked_daemon_workers`) as a
    wedge finding, even on a plain ``cw doctor`` run with no ``--reap`` --
    an operator asking "why won't this ticket re-dispatch" can see the leak
    named here before deciding to clear it. ``daemon_short_id`` carries the
    roster id the ``--reap`` remedy (:func:`_reap_wedge_findings`) needs,
    since a leaked worker with no matching cw session has no ``session_id``
    to key off.

    Returns no findings when the roster cannot be read
    (:func:`~cw.reconcile.leaked_workers.find_leaked_daemon_workers` returns
    ``None``) -- there is nothing to safely report or act on this run.
    """
    leaked = find_leaked_daemon_workers(state, daemon=get_native_daemon_client())
    if not leaked:
        return []
    findings: list[WedgeFinding] = []
    for worker in leaked:
        session = worker.session
        if session is not None:
            owner = f"owning session {session.id} is {session.status.value}"
        else:
            owner = "no matching cw session"
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_LEAKED_DAEMON_WORKER,
                session_id=session.id if session is not None else None,
                ticket_id=(
                    ticket_id_for_session(session.name) if session is not None else None
                ),
                recipe=(
                    f"Daemon worker {worker.short_id} at {worker.cwd} is leaked"
                    f" ({owner}). Run: cw doctor --reap to stop it."
                ),
                state_file=str(state_file()),
                daemon_short_id=worker.short_id,
            )
        )
    return findings
