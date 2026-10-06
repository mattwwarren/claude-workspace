"""Daemon roster drift, session liveness and session-id advisories.

Computes drift between cw state and the native daemon roster, backfills
``claude_session_id`` from it, resolves a dev-queue row to its session and
that session's daemon liveness, and stamps the #1762 session-id mismatch
advisory. Imports ``_constants``, ``_transcripts`` and ``_types``. Split out
of the flat ``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from cw.config import save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.models import QueueItemStatus, SessionOrigin, SessionPurpose, TicketTask
from cw.native_daemon import _is_native_surface_ref
from cw.reconcile._shared._constants import (
    _LIVE_STATUSES,
    _LOGGER_NAME,
    _SESSION_ID_MISMATCH_ADVISORY_NOTE,
    AUTO_DEV_LABEL_PREFIX,
    SPAWN_GRACE_SECONDS,
)
from cw.reconcile._shared._transcripts import _csid_from_transcript
from cw.reconcile._shared._types import ReconcileReport

if TYPE_CHECKING:
    from cw.models import CwState, Session

_log = logging.getLogger(_LOGGER_NAME)


def _claude_agents_json() -> list[dict[str, object]]:
    """Call ``claude agents --json`` and return the parsed list.

    Raises ``subprocess.CalledProcessError`` when the daemon is not running,
    or ``subprocess.TimeoutExpired`` if the call hangs past the timeout (#1230).
    """
    proc = subprocess.run(
        ["claude", "agents", "--json"],
        capture_output=True,
        text=True,
        check=True,
        # Why: bare literal (not a module constant) — single call site, matches
        # the RealNativeDaemonClient.stop timeout=10 precedent (native_daemon.py:352)
        # and keeps this fix minimal per #1230's scope fence (see .cw/plan.md).
        timeout=15,
    )
    data = json.loads(proc.stdout)
    return data if isinstance(data, list) else []


def compute_drift(
    state: CwState,
    native_live: set[str],
    *,
    now: datetime | None = None,
) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in *native_live*, AND
    - its ``started_at`` is older than :data:`SPAWN_GRACE_SECONDS` ago
      (newly-spawned sessions are still registering with the daemon).

    *native_live* is the set of short session IDs reported by
    ``claude agents --json``; callers obtain it via :func:`_claude_agents_json`.

    *now* is injected for testability; defaults to ``datetime.now(UTC)``.

    This function does not mutate state. It also does not distinguish
    "backend reports zero live entries" from "backend is unreachable";
    that guard lives in :func:`reconcile`.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref in native_live:
            continue
        if session.started_at > cutoff:
            continue
        if session.purpose is SessionPurpose.ORCHESTRATE:
            continue
        phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)


def ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(AUTO_DEV_LABEL_PREFIX):
        return tail[len(AUTO_DEV_LABEL_PREFIX) :]
    return None


def _looks_like_daemon_outage(
    state: CwState,
    daemon_errored: bool,
    native_live: set[str],
) -> bool:
    """True when the daemon appears unreachable and the state still has live refs.

    Fires when:
    - the daemon subprocess raised ``CalledProcessError`` (*daemon_errored*), OR
    - the daemon returned an empty roster while the persisted state has at
      least one ACTIVE/IDLE session with a ``surface_ref``.

    In either case, assume the daemon is transiently unreachable rather than
    "somehow every session died at once". Aborting here is the difference
    between a 5-second restart and permanent data loss.

    When *native_live* is non-empty the daemon is clearly reachable, so
    this returns False regardless of *daemon_errored*.
    """
    if not daemon_errored and native_live:
        return False
    return any(
        s.surface_ref is not None and s.status in _LIVE_STATUSES for s in state.sessions
    )


def _backfill_claude_session_ids(
    state: CwState, surface_to_full: dict[str, str]
) -> int:
    """Backfill claude_session_id from the daemon roster for DAEMON sessions.

    Called once per reconcile tick, after the outage guard. Returns the number
    of sessions updated; saves state when non-zero.
    """
    count = 0
    for session in state.sessions:
        if (
            session.claude_session_id is None
            and session.surface_ref is not None
            and session.status in _LIVE_STATUSES
            and session.origin is SessionOrigin.DAEMON
        ):
            from_agents = surface_to_full.get(session.surface_ref)
            resolved = from_agents or _csid_from_transcript(session)
            if resolved is not None:
                session.claude_session_id = resolved
                count += 1
    if count:
        _log.debug("Backfilled claude_session_id for %d session(s)", count)
        save_state(state)
    return count


class SessionLivenessForTask(NamedTuple):
    """A dev-queue row's owning cw ``Session`` plus that session's daemon liveness.

    ``native_surface`` distinguishes "this surface_ref is a daemon short id, and
    the roster genuinely does not list it" from "this surface_ref belongs to some
    other surface kind, so roster membership says nothing" -- the check
    ``cw dev-queue wait``'s ATTENTION predicate has always made and the reason
    ``in_roster`` alone is not a sufficient liveness answer.
    """

    session: Session
    surface_ref: str | None
    native_surface: bool
    in_roster: bool


def resolve_session_for_task(task: TicketTask, state: CwState) -> Session | None:
    """Resolve *task*'s owning cw ``Session`` in *state* (hops 1-2 of the chain).

    ``TicketTask.session_id`` is cw's own ``Session.id`` -- NOT the daemon
    roster's short id (that is ``Session.surface_ref``) and NOT a transcript
    filename (that is ``Session.claude_session_id``). See the session-id
    namespaces section in ARCHITECTURE.md. Returns ``None`` when *task* has no
    session_id or it resolves to no session in *state*.
    """
    if task.session_id is None:
        return None
    return next((s for s in state.sessions if s.id == task.session_id), None)


def session_daemon_liveness(
    session: Session, live_short_ids: set[str]
) -> SessionLivenessForTask:
    """Resolve *session*'s liveness against the daemon roster (hop 3 of the chain).

    Split from :func:`resolve_session_for_task` so a caller that already holds
    the ``Session`` (``cw dev-queue wait``'s ``_check_stale_attention``) does not
    re-resolve it, and a caller that only wants the lookup
    (``_blocked_on_user_exit_code``) does not pay for a roster query.

    *live_short_ids* is passed in rather than fetched here: ``reconcile`` already
    queries ``claude agents --json`` exactly once per tick, and a helper that
    re-queried per session would turn one subprocess into one per row.
    """
    surface_ref = session.surface_ref
    native_surface = surface_ref is not None and _is_native_surface_ref(surface_ref)
    return SessionLivenessForTask(
        session=session,
        surface_ref=surface_ref,
        native_surface=native_surface,
        in_roster=native_surface and surface_ref in live_short_ids,
    )


def resolve_session_liveness_for_task(
    task: TicketTask, state: CwState, live_short_ids: set[str]
) -> SessionLivenessForTask | None:
    """Resolve *task*'s owning session AND its liveness against the daemon roster.

    The full three-hop chain ``cw dev-queue wait`` already gets right (GitHub
    #1738/#1774/#1762): ``task.session_id`` -> a ``Session`` in *state* by
    ``.id`` -> that session's ``surface_ref`` -> the live daemon roster. Extracted
    here so a second consumer never re-derives it from a bare roster or
    transcript-filename comparison, which is what produced the "session_id
    mismatch" reports on #1738/#1774 (three namespaces compared as if they were
    one). Returns ``None`` when the task resolves to no session.
    """
    session = resolve_session_for_task(task, state)
    if session is None:
        return None
    return session_daemon_liveness(session, live_short_ids)


def find_live_sessions_for_ticket(
    state: CwState, ticket_id: str, client: str, live_short_ids: set[str]
) -> list[Session]:
    """Every session in *state* for (*ticket_id*, *client*) still live in the
    daemon roster (GitHub #2275).

    Unlike :func:`resolve_session_liveness_for_task`, does NOT key off any
    ``TicketTask.session_id`` -- derives the ticket id from each
    ``Session.name`` via :func:`ticket_id_for_session`, so a stray session no
    dev-queue row points at any more is still found.
    """
    live: list[Session] = []
    for session in state.sessions:
        if session.client != client or session.status not in _LIVE_STATUSES:
            continue
        if ticket_id_for_session(session.name) != ticket_id:
            continue
        liveness = session_daemon_liveness(session, live_short_ids)
        if liveness.native_surface and liveness.in_roster:
            live.append(session)
    return live


def _session_id_advisory_mismatch(
    liveness: SessionLivenessForTask | None, spawn_cutoff: datetime
) -> bool:
    """True when *liveness* is the advisory-worthy shape (#1762).

    Either the row's session_id resolved to nothing at all, or it resolved to a
    session whose native daemon surface is absent from the roster. A session
    still inside its spawn-grace window is never flagged: it may simply not have
    registered with the daemon yet, the same allowance :func:`compute_drift`
    makes before calling a surface phantom.
    """
    if liveness is None:
        return True
    if liveness.session.started_at > spawn_cutoff:
        return False
    return liveness.native_surface and not liveness.in_roster


def _stamp_session_id_mismatch_advisories(
    state: CwState, live_short_ids: set[str], *, now: datetime | None = None
) -> None:
    """Flag RUNNING rows whose session_id no longer resolves to a live session.

    GitHub #1762: makes the namespace-confusion signal operator-visible in
    ``cw dev-queue tasks``'s REASON column via ``TicketTask.advisory_note``,
    instead of leaving an operator to compare a row's session_id against a
    roster short id by hand and conclude, wrongly, that cw itself lost track.

    A resolvable, roster-live row is never flagged, nor is one still inside its
    spawn-grace window, and an existing note is cleared the moment the condition
    lifts -- this is a live re-derivation each tick, not a latch, so no history
    is kept. See :func:`_session_id_advisory_mismatch` for the predicate.

    Writes under ``dev_queue_lock`` through the same load/save path
    :func:`_apply_queue_mutations` above uses.
    """
    spawn_cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status is not QueueItemStatus.RUNNING or task.session_id is None:
                continue
            liveness = resolve_session_liveness_for_task(task, state, live_short_ids)
            is_mismatch = _session_id_advisory_mismatch(liveness, spawn_cutoff)
            new_note = _SESSION_ID_MISMATCH_ADVISORY_NOTE if is_mismatch else None
            if task.advisory_note == new_note:
                continue
            _log.warning(
                "session_id_mismatch_advisory_%s: ticket=%s task_session_id=%s "
                "surface_ref=%s",
                "set" if new_note else "cleared",
                task.ticket_id,
                task.session_id,
                liveness.surface_ref if liveness else None,
            )
            task.advisory_note = new_note
            changed = True
        if changed:
            save_dev_queue(store)
