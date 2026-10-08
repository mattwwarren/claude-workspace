"""Signal-only detector for ACTIVE sessions whose result was already routed (#2524).

A #2458 ``complete_session=False`` partial route (a Stop whose
``background_tasks`` is still non-empty) routes the session's staged
``cw result emit`` result -- the ticket's row advances -- but leaves the
session ACTIVE, expecting a later Stop to complete it once the background work
drains. When that later Stop never fires, nothing else completes the session:

- the idle sweep skips it, because ``holds_staged_emit_result`` is False once
  ``sentinel_partial_route_consumed`` is stamped;
- the stalled sweep skips EMIT_CLI results outright (#2435).

The session then holds a client-ceiling slot and its worktree indefinitely,
with its daemon worker still live in the roster.

This module finds that shape (:func:`find_stranded_routed_sessions`) and pages
it exactly once (:func:`sweep_routed_result_sessions`). ADR-0014 governs it:
the transcript-age bucket only debounces a signal (invariant 1), the new
heuristic lands as a signal first (invariant 3), and the only actors that ever
close such a session are explicit operator commands -- ``cw doctor --reap``
(``cw.doctor.routed_result_wedge``), ``cw spawn close --confirmed-dead``, and
the operator-run ``cw dev-queue approve`` / ``cw dev-queue requeue`` (#2517;
they stop the worker, confirm it left the roster, then close the session;
invariant 2). The finders below (:func:`routed_marker_candidates`,
:func:`split_resolvable_routed_sessions`) are called only from those commands.
Accordingly, nothing here ever mutates a session's status or a
queue row, emits ``session.completed``, stops a daemon worker, runs a
subprocess, or calls ``gh``; the only state written is the existing
``Session.reap_proposed_at`` page-once latch, through the shared proposal
emitter. An automatic close under ``reap_policy: auto`` is deliberately not
built here: it is deferred to a separate ticket that starts with a
superseding-ADR decision.

The reconcile caller passes the already-loaded client set as a per-client
rollout gate. Sessions for an unconfigured client remain fail-open and are
left for the operator's doctor view; removing a client from that existing
configuration rolls back new pages. Existing page latches can be cleared
explicitly with :func:`rollback_routed_result_latches` while holding the
normal sessions lock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    OCCUPIED_LANE_STATUSES,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionOrigin,
    SessionPurpose,
)
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ProposedAction,
    ReapCandidate,
    _emit_reap_proposed,
    _looks_like_daemon_outage,
    _sentinel_partial_route_consumed,
    _transcript_age_seconds,
    stage_refusal_latched,
    ticket_id_for_session,
)
from cw.reconcile.idle._detect import _background_work_still_draining
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.reconcile.liveness_page import close_command

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from cw.models import (
        CwState,
        OrchestratorConfig,
        Session,
        Stage,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# The ``paused_status`` of the once-only ``session.needs_attention`` page.
ROUTED_RESULT_STRANDED_REASON = "routed_result_session_stranded"
# Transcript-staleness buckets past the grace window. The existing liveness
# ladder, recomputed with the advanced row's stage -- a debounce on a signal
# only (ADR-0014 invariant 1), never a disposition timer. A stage whose floor
# sits above the 30m threshold effectively waits for the 45m bucket.
ROUTED_RESULT_GRACE_BUCKETS: frozenset[LivenessBucket] = frozenset(
    {LivenessBucket.STALE_30M, LivenessBucket.STALE_45M}
)
_SECONDS_PER_MINUTE = 60
# The ticket's own rows a requeue is about to release (#2517): every
# lane-occupying status except RUNNING. Derived from the public constant so it
# tracks any future occupied status.
_PARKED_ROW_STATUSES: frozenset[QueueItemStatus] = OCCUPIED_LANE_STATUSES - {
    QueueItemStatus.RUNNING
}


class StrandedRoutedSession(NamedTuple):
    """One live session whose already-routed result left it stranded (#2524).

    ``lane``/``stage``/``row_status`` describe the ticket's advanced row (the
    last row for ``(client, ticket_id)``), or ``DEFAULT_LANE``/
    ``DEFAULT_STAGE``/``None`` when the queue holds no row for the ticket.
    ``surface_ref`` is the session's roster short id, always present on a hit.
    """

    session: Session
    ticket_id: str
    lane: str
    stage: Stage
    row_status: QueueItemStatus | None
    stale_minutes: float
    surface_ref: str


def session_pins_occupied_row(tasks: Iterable[TicketTask], session_id: str) -> bool:
    """True when any lane-occupying row (RUNNING, BLOCKED_ON_USER,
    AWAITING_OPERATOR_SIGNOFF) is bound to *session_id*.

    A strict superset of "a RUNNING row owns it": a parked row pins its
    session too (ADR-0001). Raw status membership rather than
    ``occupies_lane_slot``, so a terminal_sibling BLOCKED row still pins --
    erring toward protecting the session.
    """
    return _first_pinning_row(tasks, session_id) is not None


def _first_pinning_row(
    tasks: Iterable[TicketTask], session_id: str
) -> TicketTask | None:
    """The first lane-occupying row bound to *session_id*, else None.

    :func:`session_pins_occupied_row` with the row itself, so a caller that
    skips a pinned session can name what pins it (#2517).
    """
    return next(
        (
            task
            for task in tasks
            if task.session_id == session_id and task.status in OCCUPIED_LANE_STATUSES
        ),
        None,
    )


def _is_routed_marker_candidate(session: Session) -> bool:
    """The roster-independent half of the routed-marker filter.

    A live (ACTIVE/IDLE) non-orchestrator DAEMON session whose
    ``last_result`` carries the #2458 consumed marker (exactly ``True``),
    with no latched stage refusal (that shape belongs to the refusal pages)
    and a non-null surface (a null one belongs to doctor class 9).
    """
    return not (
        session.origin is not SessionOrigin.DAEMON
        or session.status not in _LIVE_STATUSES
        or session.purpose is SessionPurpose.ORCHESTRATE
        or not _sentinel_partial_route_consumed(session)
        or stage_refusal_latched(session)
        or session.surface_ref is None
    )


def _live_routed_surface_ref(session: Session, native_live: set[str]) -> str | None:
    """Return the roster short id of a routed-marker candidate, else None.

    The cheap first-pass filter: :func:`_is_routed_marker_candidate`, plus a
    surface still in the daemon roster (an absent one belongs to the phantom
    sweep and doctor class 6).
    """
    surface_ref = session.surface_ref
    if (
        not _is_routed_marker_candidate(session)
        or surface_ref is None
        or surface_ref not in native_live
    ):
        return None
    return surface_ref


def routed_marker_candidates(
    state: CwState, *, client: str, ticket_id: str
) -> list[Session]:
    """Every routed-marker session for (*client*, *ticket_id*) (#2517).

    Matched by session name (``ticket_id_for_session``), never by a row's
    ``session_id``: after approve/requeue no row points at the orphan any
    more. Needs no roster and no queue, so an operator command with nothing
    to close can return before it resolves a daemon client.
    """
    return [
        session
        for session in state.sessions
        if session.client == client
        and ticket_id_for_session(session.name) == ticket_id
        and _is_routed_marker_candidate(session)
    ]


class PinnedOrphan(NamedTuple):
    """A marker candidate an occupied row still binds, with that row."""

    session: Session
    pinned_by: TicketTask


class RoutedOrphanSplit(NamedTuple):
    """:func:`split_resolvable_routed_sessions`'s five disjoint outcomes.

    ``stop``: live in the roster, unpinned, not draining -- stop, confirm
    gone, then close. ``flip_only``: absent from a readable, trustworthy
    roster -- no worker to stop, close by the status flip alone.
    ``roster_untrusted``: absent from an empty roster while state records
    another live session (the daemon-restart signature) -- never closed.
    ``draining``: background work still draining. ``pinned``: an occupied
    row binds it.
    """

    stop: list[Session]
    flip_only: list[Session]
    roster_untrusted: list[Session]
    draining: list[Session]
    pinned: list[PinnedOrphan]


def _roster_outage_shaped(
    state: CwState, native_live: set[str], *, exclude_ids: Iterable[str]
) -> bool:
    """True when *native_live* looks like a daemon outage, not proof of death.

    :func:`_looks_like_daemon_outage` (an empty roster while state records a
    live session with a surface) over the sessions NOT in *exclude_ids*. The
    caller excludes its own candidates: each of them always counts as "live
    with a surface", so without the exclusion every empty-roster close would
    be refused and a lone orphan whose worker is gone could never be closed.
    """
    excluded = set(exclude_ids)
    others = [s for s in state.sessions if s.id not in excluded]
    return _looks_like_daemon_outage(
        state.model_copy(update={"sessions": others}), False, native_live
    )


def _pin_rows(
    tasks: Iterable[TicketTask], releasing: tuple[str, str] | None
) -> list[TicketTask]:
    """*tasks* minus the parked rows of the ticket a requeue is releasing."""
    if releasing is None:
        return list(tasks)
    return [
        task
        for task in tasks
        if (task.client, task.ticket_id) != releasing
        or task.status not in _PARKED_ROW_STATUSES
    ]


def split_resolvable_routed_sessions(
    candidates: list[Session],
    tasks: Iterable[TicketTask],
    *,
    state: CwState,
    native_live: set[str],
    now: datetime,
    config: OrchestratorConfig,
    releasing: tuple[str, str] | None = None,
) -> RoutedOrphanSplit:
    """Classify each marker candidate an operator command resolved (#2517).

    Pure: no writes, no events, no daemon call. Per candidate, in order:
    pinned (an occupied row binds it); absent from *native_live* --
    ``roster_untrusted`` when the roster is outage-shaped (excluding the whole
    candidate set), else ``flip_only``; ``draining``; otherwise ``stop``.

    *releasing* is ``(client, ticket_id)`` for a requeue only: that ticket's
    own parked rows do not pin, because ``requeue_ticket`` is about to release
    exactly those rows and a parked row always binds its own orphan
    (ADR-0001). An approve passes None: its transition already released the
    row, so whatever still binds the session (the REVIEW signoff park) pins.

    No transcript-staleness gate: an explicit operator command resolved the
    ticket (ADR-0014 invariant 2); the draining veto still applies.
    """
    pin_rows = _pin_rows(tasks, releasing)
    outage = _roster_outage_shaped(
        state, native_live, exclude_ids=[c.id for c in candidates]
    )
    # Local lists, built by keyword at the end: this module never spells a
    # ``.stop`` attribute (the #2524 no-daemon-stop AST guard).
    to_stop: list[Session] = []
    flip_only: list[Session] = []
    untrusted: list[Session] = []
    draining: list[Session] = []
    pinned: list[PinnedOrphan] = []
    for session in candidates:
        row = _first_pinning_row(pin_rows, session.id)
        if row is not None:
            pinned.append(PinnedOrphan(session, row))
        elif session.surface_ref not in native_live:
            (untrusted if outage else flip_only).append(session)
        elif _background_work_still_draining(session, now=now, config=config):
            draining.append(session)
        else:
            to_stop.append(session)
    return RoutedOrphanSplit(
        stop=to_stop,
        flip_only=flip_only,
        roster_untrusted=untrusted,
        draining=draining,
        pinned=pinned,
    )


def _stranded_hit(
    session: Session,
    surface_ref: str,
    *,
    tasks: list[TicketTask],
    task_by_key: dict[tuple[str, str], TicketTask],
    now: datetime,
    config: OrchestratorConfig,
) -> StrandedRoutedSession | None:
    """Build the hit for one marker candidate, or None when it is not stranded.

    Skips (fail-open) a session with no resolvable ticket id, one an occupied
    row still binds, one with no locatable transcript, one inside the grace
    window, and one whose Stop hook is still legitimately waiting on
    background work.
    """
    ticket_id = ticket_id_for_session(session.name)
    if ticket_id is None or session_pins_occupied_row(tasks, session.id):
        return None
    age_seconds = _transcript_age_seconds(session, now)
    if age_seconds is None:
        return None
    task = task_by_key.get((session.client, ticket_id))
    stage = task.stage if task is not None else DEFAULT_STAGE
    stale_minutes = age_seconds / _SECONDS_PER_MINUTE
    bucket = _classify_liveness_bucket(stale_minutes, stage=stage, config=config)
    if bucket not in ROUTED_RESULT_GRACE_BUCKETS:
        return None
    if _background_work_still_draining(session, now=now, config=config):
        return None
    return StrandedRoutedSession(
        session=session,
        ticket_id=ticket_id,
        lane=task.lane if task is not None else DEFAULT_LANE,
        stage=stage,
        row_status=task.status if task is not None else None,
        stale_minutes=stale_minutes,
        surface_ref=surface_ref,
    )


def find_stranded_routed_sessions(
    state: CwState,
    tasks: Iterable[TicketTask],
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    enabled_clients: Iterable[str] | None = None,
) -> list[StrandedRoutedSession]:
    """Return every live session stranded after its result was routed (#2524).

    Pure: no writes, no events, no daemon call, no subprocess. When supplied,
    ``enabled_clients`` is the reconcile caller's per-client rollout gate.
    The one
    detector both the reconcile page (:func:`sweep_routed_result_sessions`)
    and the doctor class (``cw.doctor.routed_result_wedge``) consult, so the
    two can never disagree about what is stranded. The ``(client, ticket_id)``
    row lookup is last-row-wins, the same convention as reconcile's
    ``shared_task_by_ticket``.
    """
    task_list = list(tasks)
    client_gate = set(enabled_clients) if enabled_clients is not None else None
    task_by_key = {(task.client, task.ticket_id): task for task in task_list}
    hits: list[StrandedRoutedSession] = []
    for session in state.sessions:
        if client_gate is not None and session.client not in client_gate:
            continue
        surface_ref = _live_routed_surface_ref(session, native_live)
        if surface_ref is None:
            continue
        hit = _stranded_hit(
            session,
            surface_ref,
            tasks=task_list,
            task_by_key=task_by_key,
            now=now,
            config=config,
        )
        if hit is not None:
            hits.append(hit)
    return hits


def rollback_routed_result_latches(state: CwState, session_ids: Iterable[str]) -> int:
    """Clear page-once latches for an explicit rollout rollback.

    The caller must hold the existing ``sessions_lock`` and persist *state*
    after this function returns. This deliberately does not broaden the
    reconcile sweep: rollback is an explicit operator/control-plane action,
    and clearing a latch only permits a later signal to be emitted again.
    """
    target_ids = set(session_ids)
    cleared = 0
    for session in state.sessions:
        if session.id in target_ids and session.reap_proposed_at is not None:
            session.reap_proposed_at = None
            cleared += 1
    return cleared


def _page_breadcrumbs(hit: StrandedRoutedSession) -> str:
    """Operator-facing text naming the stranded state and its exact remedy."""
    row = (
        f"row {hit.stage.value}/{hit.row_status.value}"
        if hit.row_status is not None
        else "no queue row"
    )
    return (
        f"ticket {hit.ticket_id} result already routed ({row}); session "
        f"{hit.session.id} still {hit.session.status.value} with transcript flat "
        f"{hit.stale_minutes:.0f}m and no occupied row bound to it -- it holds a "
        "ceiling slot and its worktree. Nothing acts automatically; close it "
        f"with: {close_command(hit.session.id)} (or cw doctor --reap)"
    )


def _page_stranded_session(hit: StrandedRoutedSession) -> bool:
    """Emit the once-only ``session.needs_attention`` page; True when it landed.

    No push notification (operator decision D1): this runs inside reconcile's
    ``sessions_lock`` hold, which must never start a thread or subprocess.
    An ``OSError`` from the inbox write is logged and reported as False, so
    the caller leaves the session unstamped and the next tick retries
    (at-least-once, the #2490 lesson).
    """
    session = hit.session
    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": hit.ticket_id,
        "claude_session_id": session.claude_session_id,
        "paused_status": ROUTED_RESULT_STRANDED_REASON,
        "breadcrumbs": _page_breadcrumbs(hit),
        "crashed": False,
        "stage": hit.stage.value,
        "stale_minutes": hit.stale_minutes,
        "lane": hit.lane,
    }
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=hit.ticket_id or session.id,
        )
    except OSError:
        _log.warning(
            "stranded routed-result page for session %s not written; "
            "retrying next tick",
            session.id,
            exc_info=True,
        )
        return False
    return True


def _reap_candidate(hit: StrandedRoutedSession, *, now: datetime) -> ReapCandidate:
    """The proposal-only candidate the shared emitter stamps and publishes."""
    session = hit.session
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.CLOSE_ROUTED_RESULT_SESSION,
        ticket_id=hit.ticket_id,
        reap_reason=ReapReason.ROUTED_RESULT_STRANDED,
        lane=hit.lane,
        client=session.client,
        stage=hit.stage,
        elapsed_seconds=(now - session.started_at).total_seconds(),
        worktree_path=session.worktree_path,
    )


def sweep_routed_result_sessions(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    tasks: Iterable[TicketTask],
    enabled_clients: Iterable[str] | None = None,
) -> list[StrandedRoutedSession]:
    """Page every newly stranded routed session once; return those paged.

    Runs inside ``_reconcile_locked``'s existing ``sessions_lock`` hold and
    takes no lock of its own. Policy-independent: under every ``reap_policy``
    it only detects, pages and proposes -- it never reads clients or
    ``reap_policy`` and never closes anything (ADR-0014). The reconcile caller
    supplies its configured-client rollout gate through ``enabled_clients``.
    Sessions already
    carrying ``reap_proposed_at`` are skipped (the page-once latch). The page
    is emitted before the latch is stamped, so a failed page write leaves the
    session unstamped for the next tick. A failure writing the proposal
    itself is logged and swallowed, so it cannot abort the rest of the tick.
    """
    hits = [
        hit
        for hit in find_stranded_routed_sessions(
            state,
            tasks,
            now=now,
            native_live=native_live,
            config=config,
            enabled_clients=enabled_clients,
        )
        if hit.session.reap_proposed_at is None
    ]
    paged = [hit for hit in hits if _page_stranded_session(hit)]
    if not paged:
        return []
    try:
        _emit_reap_proposed(
            state,
            [_reap_candidate(hit, now=now) for hit in paged],
            native_live=native_live,
            now=now,
        )
    except OSError:
        _log.warning(
            "stranded routed-result proposal for session(s) %s not written",
            ", ".join(hit.session.id for hit in paged),
            exc_info=True,
        )
    return paged
