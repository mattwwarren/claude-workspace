"""Mid-turn usage-limit sweep for reconcile (GitHub #2324).

A headless worker whose turn hits the account usage limit does not crash or
leave the daemon roster: its transcript simply ends on an assistant text record
such as ``You've hit your weekly limit · resets Sep 26, 11pm`` and the session
goes quiet with no sentinel. The phantom sweep never sees it (the surface is
still live) and the liveness sweep only pages generically, so the row held its
lane slot as RUNNING and no ``usage_limited_until`` lockout was armed.

This sweep detects exactly that tail -- a DAEMON session present in the roster,
owning a RUNNING row, whose last content-bearing transcript record matches
``USAGE_LIMIT_RE`` with no sentinel anywhere in the transcript -- and acts on it
as positive evidence of a usage-limit stop, never on elapsed time (ADR-0014).

The act is gated by the lane's ``reap_policy`` (ADR-0006 invariant 2) and
runs in one fixed order, each step's failure stopping the ones after it:

1. Gate, with no side effects: under ``dev_queue_lock``, re-read the tail and
   check the row is still RUNNING and bound to the session. Either failing is
   a no-op this tick.
2. Arm the client's ``usage_limited_until`` lockout. A failure logs and
   continues -- the next tick re-arms.

Under ``auto`` the row is then requeued and the session closed:

3. Record the audit events -- ``session.needs_attention`` naming the reset
   instant, ``session.reap_proposed`` (ADR-0006 invariant 3) and
   ``session.completed`` -- before any effect. A failed write stops here. The
   ``session.completed`` carries ``reason="usage_limited_mid_turn"``, which
   the dispatch consumer skips: this sweep owns the row's disposition.
4. Re-read the tail immediately before the stop, since steps 2-3 ran after
   the gate: a changed tail is the gate's no-op. Then stop the daemon
   surface, so a live idle home cannot defer the re-claim, and verify it:
   ``stop()`` swallows its own failures, so the surface must leave the roster
   within a short bounded poll. A raised stop, or a surface still listed (or
   an unreadable roster), stops here: the session stays ACTIVE, the row
   RUNNING, and the next tick retries.
5. Persist the session COMPLETED/``usage_limited`` (not a crash).
6. Requeue the row RUNNING -> PENDING with ``next_eligible_at`` at the reset
   instant, through the identity-checked update, so the existing claim gate
   releases it with no new sweep. A lost race is logged; the closed session
   stays closed, since its process is gone. Step 6 is resumable: the session
   and queue stores cannot be written atomically, so if its write fails (the
   error aborts the tick) or the process dies after step 5, a later tick
   finds the row still RUNNING under a session already COMPLETED with this
   sweep's usage-limit reason and finishes the requeue, idempotently and
   without charge. It runs ahead of reconcile's COMPLETED-session backstop,
   so such a row never falls through to that backstop's charged revert.

Any other policy (``signal_only`` default) instead parks the row
BLOCKED_ON_USER with ``disposition="usage_limited_mid_turn"`` and only then
proposes and pages; the session, its surface and the row's ``session_id`` are
left untouched for an operator to clear. Neither branch charges
``unproductive_attempts`` -- a whole turn ran.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from cw.config import save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dispatch_state import (
    merge_and_save_usage_limited_until,
    record_usage_limit_armed,
    resolve_usage_limited_until,
)
from cw.events import record_event
from cw.exceptions import parse_usage_limit_reset
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import wait_for_roster_presence
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _USAGE_LIMITED_MID_TURN_REASON,
    ProposedAction,
    ReapCandidate,
    UsageLimitDetection,
    _emit_reap_proposed,
    _parse_any_sentinel_from_transcript,
    resolve_reap_policy,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from datetime import datetime

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# The limit record must itself be the transcript's last content-bearing record:
# nothing conversational may follow it. Trailing non-content records
# (cost-state, last-prompt) never advance transcript_tail_at.
_ZERO_GAP_SECONDS = 0.0

# Step 4's stop confirmation: how long to poll the daemon roster for the
# stopped surface to leave it. Short, because reconcile runs under
# sessions_lock and ``claude stop`` has already returned by then.
_STOP_CONFIRM_TIMEOUT_SECS = 5.0
_STOP_CONFIRM_INTERVAL_SECS = 0.5


def _mid_turn_limit_detection(session: Session) -> UsageLimitDetection | None:
    """Return the usage-limit detection iff *session*'s tail is a mid-turn stop.

    Fail-closed: a missing timestamp on either the limit record or the tail
    means no positive evidence, so no detection. A sentinel anywhere in the
    transcript also disqualifies the session -- the worker reported a result.
    """
    detection = _shared.detect_usage_limit(session)
    if not _shared.usage_limit_is_recent(
        detection, window_seconds=_ZERO_GAP_SECONDS, fail_open=False
    ):
        return None
    if _parse_any_sentinel_from_transcript(session) is not None:
        return None
    return detection


def _owned_running_row(
    tasks: Iterable[TicketTask], ticket_id: str, client: str, session_id: str
) -> TicketTask | None:
    """The row iff one is RUNNING under ``(ticket_id, client, session_id)``.

    That triple is the row's identity (see #2219): a ticket id alone can
    resolve to another client's same-numbered ticket or to a duplicate
    RUNNING row. This is the inline form of ``_find_running_row``'s
    ``session_id`` predicate; switch to that helper once #2219 lands.
    """
    return next(
        (
            task
            for task in tasks
            if task.ticket_id == ticket_id
            and task.client == client
            and task.session_id == session_id
            and task.status is QueueItemStatus.RUNNING
        ),
        None,
    )


def _detect_mid_turn_usage_limit_candidates(
    state: CwState,
    *,
    native_live: set[str],
    tasks: Sequence[TicketTask],
) -> list[ReapCandidate]:
    """Classify roster-present sessions stopped mid-turn by a usage limit.

    Pure: no writes. Gating mirrors the liveness sweep (DAEMON origin, status
    in ``_LIVE_STATUSES``, ``surface_ref`` in *native_live*) -- a roster-absent
    session belongs to the phantom sweep. The owning row must be RUNNING under
    this exact ``(ticket_id, client, session_id)``, so a row already parked or
    reclaimed never re-fires, an older session for the same ticket is never
    blamed, and another same-ticket row can never stand in for it.
    """
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = (
            _owned_running_row(tasks, ticket_id, session.client, session.id)
            if ticket_id
            else None
        )
        if task is None:
            continue
        if _mid_turn_limit_detection(session) is None:
            continue
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.REVERT_TASK,
                ticket_id=task.ticket_id,
                lane=task.lane,
                client=session.client,
                reap_reason=ReapReason.USAGE_LIMIT_MID_TURN,
                usage_limit_detected=True,
            )
        )
    return candidates


class _ActContext(NamedTuple):
    """One gated candidate's inputs, shared by every step of its act."""

    state: CwState
    session: Session
    candidate: ReapCandidate
    ticket_id: str
    until: datetime
    native_live: set[str]
    now: datetime


def _gate(session: Session, ticket_id: str) -> UsageLimitDetection | None:
    """Step 1: re-verify the evidence and the row's identity, with no side effects.

    Under ``dev_queue_lock``, re-reads the transcript (the limit text must
    still be the last content-bearing record) and checks the row is still
    RUNNING under ``(ticket_id, session.client, session.id)``. Either failing
    is a no-op this tick: nothing is written, emitted or stopped. The lock is
    released before any effect, so step 6 re-runs the identity check for its
    own write.
    """
    with dev_queue_lock():
        detection = _mid_turn_limit_detection(session)
        owned = _owned_running_row(
            load_dev_queue().tasks, ticket_id, session.client, session.id
        )
    if detection is None:
        _log.info(
            "usage_limit_mid_turn: tail changed since detect for ticket %s "
            "session %s; skipping this tick",
            ticket_id,
            session.id,
        )
        return None
    if owned is None:
        _log.info(
            "usage_limit_mid_turn: row for ticket %s is no longer RUNNING under "
            "session %s; skipping this tick",
            ticket_id,
            session.id,
        )
        return None
    return detection


def _mutate_owned_running_row(
    session: Session, ticket_id: str, mutate: Callable[[TicketTask], None]
) -> bool:
    """Apply *mutate* to *session*'s row iff it is still RUNNING and owned by it.

    Re-verified under ``dev_queue_lock`` because the gate's check has been
    released since: a row that moved off RUNNING or was reclaimed by another
    session in the meantime is left as found (returns False).
    """
    with dev_queue_lock():
        store = load_dev_queue()
        target = _owned_running_row(store.tasks, ticket_id, session.client, session.id)
        if target is None:
            return False
        mutate(target)
        save_dev_queue(store)
    return True


def _revert_to_pending(target: TicketTask, *, until: datetime) -> None:
    transition_task_status(target, QueueItemStatus.PENDING, unproductive=False)
    target.session_id = None
    target.next_eligible_at = until


def _park_blocked_on_user(target: TicketTask) -> None:
    # session_id deliberately stays set: a late sentinel still routes through
    # the #918 rescue, which re-finds the row by it (as the #2135 park does).
    transition_task_status(
        target,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=_USAGE_LIMITED_MID_TURN_REASON,
        unproductive=False,
    )


def _resolve_window(
    matched_text: str | None, *, now: datetime, config: OrchestratorConfig
) -> tuple[datetime | None, datetime]:
    """The limit message's parsed reset instant (if any) and the window's end.

    Deterministic in *now*, so finishing an interrupted requeue from the
    session's ``completed_at`` reproduces the ``until`` its act computed.
    """
    reset_at = (
        parse_usage_limit_reset(matched_text, now=now.astimezone(_deps.host_timezone()))
        if matched_text
        else None
    )
    until = resolve_usage_limited_until(
        now, reset_at, config.usage_limit_backoff_seconds
    )
    return reset_at, until


def _arm_lockout(
    client: str, ticket_id: str, *, until: datetime, reset_at: datetime | None
) -> None:
    """Step 2: arm the client's spawn lockout; a failure logs and continues.

    Idempotent and non-destructive, and what actually stops new spawns. The
    shared helper audits before it persists, so a failed audit write leaves
    no window saved; the next tick re-arms it.
    """
    try:
        record_usage_limit_armed(client, until=until, reset_at=reset_at)
    except OSError:
        _log.warning(
            "usage_limit_mid_turn: arming the %s lockout for ticket %s failed; "
            "continuing, the next tick re-arms",
            client,
            ticket_id,
            exc_info=True,
        )
        return
    merge_and_save_usage_limited_until({client: until})


def _record_needs_attention(ctx: _ActContext, *, auto: bool) -> None:
    """Record the usage-limit detection, naming the reset instant."""
    session = ctx.session
    disposition = (
        "parked without charge, will re-enter the queue automatically"
        if auto
        else "parked BLOCKED_ON_USER without charge; needs an operator to clear"
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ctx.ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _USAGE_LIMITED_MID_TURN_REASON,
            "breadcrumbs": (
                f"hit usage limit mid-turn; resets {ctx.until.isoformat()}; "
                f"{disposition}"
            ),
            "crashed": False,
            "lane": ctx.candidate.lane,
        },
        correlation_id=ctx.ticket_id,
    )


def _audit_auto_act(ctx: _ActContext) -> bool:
    """Step 3: record every audit event before any effect; False if one failed.

    The usage-limit detection, then the intent to stop and requeue (the reap
    proposal), then the completion. A failed write stops the act here -- no
    stop, no close, no requeue -- and the next tick retries.
    """
    session = ctx.session
    try:
        _record_needs_attention(ctx, auto=True)
        _emit_reap_proposed(
            ctx.state, [ctx.candidate], native_live=ctx.native_live, now=ctx.now
        )
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ctx.ticket_id,
                "claude_session_id": session.claude_session_id,
                "crashed": False,
                # Reconcile owns this row's disposition, including finishing
                # an interrupted requeue, so the dispatch consumer skips it.
                "reason": _USAGE_LIMITED_MID_TURN_REASON,
            },
            correlation_id=ctx.ticket_id,
        )
    except OSError:
        _log.warning(
            "usage_limit_mid_turn: audit emit failed for ticket %s session %s; "
            "no stop, close or requeue this tick",
            ctx.ticket_id,
            session.id,
            exc_info=True,
        )
        return False
    _deps.fire_push_notification(session.name, session.client)
    return True


def _tail_still_limited(ctx: _ActContext) -> bool:
    """Step 4's precondition: re-read the transcript tail right before the stop.

    Steps 2 and 3 ran since the step-1 gate read it, so a worker that resumed
    in between must not be stopped. A changed tail is the gate's no-op: no
    stop, close or requeue this tick.
    """
    if _mid_turn_limit_detection(ctx.session) is not None:
        return True
    _log.info(
        "usage_limit_mid_turn: tail changed before the stop for ticket %s "
        "session %s; skipping this tick",
        ctx.ticket_id,
        ctx.session.id,
    )
    return False


def _stop_surface(ctx: _ActContext) -> bool:
    """Step 4: stop the live daemon session and verify it left the roster.

    ``stop()`` swallows its own failures, so it is not trusted: the roster
    must stop listing the surface within a short bounded poll (an unreadable
    roster confirms nothing). A raised stop or a surface still listed is a
    step-4 failure -- the session stays ACTIVE and the row RUNNING, and the
    next tick retries with the lockout already armed. A session whose
    ``surface_ref`` is already cleared has nothing to stop.
    """
    surface_ref = ctx.session.surface_ref
    if surface_ref is None:
        return True
    daemon = _deps.get_native_daemon_client()
    try:
        daemon.stop(surface_ref)
    except OSError:
        _log.warning(
            "usage_limit_mid_turn: stopping surface %s for ticket %s session %s "
            "failed; the session stays ACTIVE and the row RUNNING for the next tick",
            surface_ref,
            ctx.ticket_id,
            ctx.session.id,
            exc_info=True,
        )
        return False
    if wait_for_roster_presence(
        daemon,
        surface_ref,
        present=False,
        timeout=_STOP_CONFIRM_TIMEOUT_SECS,
        interval=_STOP_CONFIRM_INTERVAL_SECS,
    ):
        return True
    _log.warning(
        "usage_limit_mid_turn: surface %s for ticket %s session %s is still in "
        "the daemon roster (or the roster is unreadable) %.1fs after stop; the "
        "session stays ACTIVE and the row RUNNING for the next tick",
        surface_ref,
        ctx.ticket_id,
        ctx.session.id,
        _STOP_CONFIRM_TIMEOUT_SECS,
    )
    return False


def _persist_completed(ctx: _ActContext) -> None:
    """Step 5: persist the session COMPLETED, only after its stop succeeded."""
    session = ctx.session
    session.status = SessionStatus.COMPLETED
    session.completed_at = ctx.now
    session.completed_reason = CompletionReason.USAGE_LIMITED
    session.reap_reason = ReapReason.USAGE_LIMIT_MID_TURN
    save_state(ctx.state)


def _act_auto(ctx: _ActContext) -> bool:
    """Steps 3-6 of the ``reap_policy: auto`` act; True iff the row was requeued.

    Step 6's identity-checked requeue can lose a race to another writer after
    the session is already closed. That is logged and nothing further is
    emitted; the session stays closed, which is right because its process is
    gone.
    """
    if (
        not _audit_auto_act(ctx)
        or not _tail_still_limited(ctx)
        or not _stop_surface(ctx)
    ):
        return False
    _persist_completed(ctx)
    requeued = _mutate_owned_running_row(
        ctx.session, ctx.ticket_id, partial(_revert_to_pending, until=ctx.until)
    )
    if not requeued:
        _log.info(
            "usage_limit_mid_turn: requeue of ticket %s lost a race after "
            "session %s was stopped and closed; row left as found",
            ctx.ticket_id,
            ctx.session.id,
        )
    return requeued


def _act_signal_only(ctx: _ActContext) -> None:
    """Park the row BLOCKED_ON_USER, then propose and page (non-auto policy).

    The reap proposal is emitted only after the park's identity check
    passes: a lost race proposes nothing and mutates nothing (#2285).
    """
    if not _mutate_owned_running_row(ctx.session, ctx.ticket_id, _park_blocked_on_user):
        _log.info(
            "usage_limit_mid_turn: park of ticket %s lost a race; row no longer "
            "RUNNING under session %s",
            ctx.ticket_id,
            ctx.session.id,
        )
        return
    _emit_reap_proposed(
        ctx.state, [ctx.candidate], native_live=ctx.native_live, now=ctx.now
    )
    _record_needs_attention(ctx, auto=False)
    _deps.fire_push_notification(ctx.session.name, ctx.session.client)


def _act_on_mid_turn_usage_limit_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    now: datetime,
) -> list[str]:
    """Gate, arm, and act on each candidate; return the ticket ids requeued.

    Every candidate runs step 1 (the side-effect-free gate) and step 2 (the
    client lockout). ``reap_policy: auto`` then runs steps 3-6 -- audit, stop,
    close, requeue -- and only a candidate whose requeue lands appears in the
    returned list. Any other policy parks the row BLOCKED_ON_USER instead.
    """
    session_by_id = {s.id: s for s in state.sessions}
    reverted: list[str] = []
    for candidate in candidates:
        session = session_by_id.get(candidate.session_id)
        ticket_id = candidate.ticket_id
        if session is None or ticket_id is None:
            continue
        detection = _gate(session, ticket_id)
        if detection is None:
            continue
        reset_at, until = _resolve_window(
            detection.matched_text, now=now, config=config
        )
        _arm_lockout(session.client, ticket_id, until=until, reset_at=reset_at)
        ctx = _ActContext(
            state=state,
            session=session,
            candidate=candidate,
            ticket_id=ticket_id,
            until=until,
            native_live=native_live,
            now=now,
        )
        if resolve_reap_policy(candidate, clients, config) is not ReapPolicy.AUTO:
            _act_signal_only(ctx)
        elif _act_auto(ctx):
            reverted.append(ticket_id)
    return reverted


def _closed_by_this_sweep(session: Session) -> bool:
    """Did step 5 of this sweep's ``auto`` act close *session*?

    Only that step stamps the pair ``usage_limited`` /
    ``usage_limit_mid_turn``; the phantom sweep and every other closer stamp
    something else.
    """
    return (
        session.origin is SessionOrigin.DAEMON
        and session.status is SessionStatus.COMPLETED
        and session.completed_reason is CompletionReason.USAGE_LIMITED
        and session.reap_reason is ReapReason.USAGE_LIMIT_MID_TURN
    )


def _finish_interrupted_requeues(
    state: CwState,
    *,
    tasks: Sequence[TicketTask],
    config: OrchestratorConfig,
    now: datetime,
) -> list[str]:
    """Finish a step-6 requeue interrupted after step 5 closed the session.

    The session store and the dev queue cannot be written atomically, so a
    failed or crashed step 6 leaves the session COMPLETED/``usage_limited``
    while its row is still RUNNING under it. That pair is recognised here as
    "usage-limit act interrupted after close" and finished exactly as step 6
    would have: PENDING with ``next_eligible_at`` recomputed from the session's
    ``completed_at`` (the act's own ``now``) and no attempt charged. The same
    identity-checked write makes it idempotent -- a finished row is no longer
    RUNNING under the session. Nothing is re-armed, re-emitted or re-stopped:
    steps 2-5 already ran, and ``reap_policy`` authorised the act they began.

    Reconcile runs this sweep before its COMPLETED-session backstop, and a
    failed write here raises out of the tick, so such a row never falls
    through to that backstop's attempt-charging revert. Returns the ticket
    ids requeued.
    """
    finished: list[str] = []
    for session in state.sessions:
        if not _closed_by_this_sweep(session):
            continue
        ticket_id = ticket_id_for_session(session.name)
        if (
            ticket_id is None
            or _owned_running_row(tasks, ticket_id, session.client, session.id) is None
        ):
            continue
        closed_at = session.completed_at or now
        _, until = _resolve_window(
            _shared.detect_usage_limit(session).matched_text,
            now=closed_at,
            config=config,
        )
        if _mutate_owned_running_row(
            session, ticket_id, partial(_revert_to_pending, until=until)
        ):
            _log.info(
                "usage_limit_mid_turn: finished the requeue of ticket %s "
                "interrupted after session %s was closed",
                ticket_id,
                session.id,
            )
            finished.append(ticket_id)
    return finished


def detect_and_park_mid_turn_usage_limits(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    clients: dict[str, ClientConfig],
    tasks: Sequence[TicketTask] | None = None,
) -> list[str]:
    """Detect and disposition mid-turn usage-limit stops (GitHub #2324).

    Combines the detect and act phases, mirroring
    ``record_session_liveness_changes``. ``tasks`` (every dev-queue row, not a
    ticket-keyed map, which would collapse same-ticket rows) may be pre-loaded
    by the caller to avoid a duplicate dev-queue read within the same reconcile
    tick; when omitted it is loaded here. First finishes any ``auto`` requeue
    interrupted after its session was closed. Returns the ticket ids reverted
    to PENDING (``reap_policy: auto`` only), finished ones included.
    """
    resolved_tasks = tasks if tasks is not None else load_dev_queue().tasks
    finished = _finish_interrupted_requeues(
        state, tasks=resolved_tasks, config=config, now=now
    )
    candidates = _detect_mid_turn_usage_limit_candidates(
        state, native_live=native_live, tasks=resolved_tasks
    )
    return finished + _act_on_mid_turn_usage_limit_candidates(
        state,
        candidates,
        native_live=native_live,
        clients=clients,
        config=config,
        now=now,
    )
