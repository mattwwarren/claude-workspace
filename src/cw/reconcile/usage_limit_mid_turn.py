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
   ``session.completed`` -- before any effect. A failed write stops here.
4. Stop the daemon surface, so a live idle home cannot defer the re-claim. A
   failed stop stops here: the session stays ACTIVE, the row RUNNING.
5. Persist the session COMPLETED/``usage_limited`` (not a crash).
6. Requeue the row RUNNING -> PENDING with ``next_eligible_at`` at the reset
   instant, through the identity-checked update, so the existing claim gate
   releases it with no new sweep. A lost race is logged; the closed session
   stays closed, since its process is gone.

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
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _USAGE_LIMITED_MID_TURN_REASON,
    ProposedAction,
    ReapCandidate,
    UsageLimitDetection,
    _emit_reap_proposed,
    _lookup_matching_task,
    _parse_any_sentinel_from_transcript,
    resolve_reap_policy,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# The limit record must itself be the transcript's last content-bearing record:
# nothing conversational may follow it. Trailing non-content records
# (cost-state, last-prompt) never advance transcript_tail_at.
_ZERO_GAP_SECONDS = 0.0


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


def _detect_mid_turn_usage_limit_candidates(
    state: CwState,
    *,
    native_live: set[str],
    task_by_ticket: dict[str, TicketTask],
) -> list[ReapCandidate]:
    """Classify roster-present sessions stopped mid-turn by a usage limit.

    Pure: no writes. Gating mirrors the liveness sweep (DAEMON origin, status
    in ``_LIVE_STATUSES``, ``surface_ref`` in *native_live*) -- a roster-absent
    session belongs to the phantom sweep. The owning row must be RUNNING and
    owned by this exact session, so a row already parked or reclaimed never
    re-fires and an older session for the same ticket is never blamed.
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
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        if (
            task is None
            or task.status is not QueueItemStatus.RUNNING
            or task.session_id != session.id
        ):
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


def _owned_running_row(
    store: DevQueueStore, ticket_id: str, session_id: str
) -> TicketTask | None:
    """The row iff it is still RUNNING and bound to *session_id*, else None."""
    lookup = _lookup_matching_task(store, ticket_id, session_id)
    if lookup.target_status is not QueueItemStatus.RUNNING:
        return None
    return lookup.target


def _gate(session: Session, ticket_id: str) -> UsageLimitDetection | None:
    """Step 1: re-verify the evidence and the row's identity, with no side effects.

    Under ``dev_queue_lock``, re-reads the transcript (the limit text must
    still be the last content-bearing record) and checks the row is still
    RUNNING and bound to *session*. Either failing is a no-op this tick:
    nothing is written, emitted or stopped. The lock is released before any
    effect, so step 6 re-runs the identity check for its own write.
    """
    with dev_queue_lock():
        detection = _mid_turn_limit_detection(session)
        owned = _owned_running_row(load_dev_queue(), ticket_id, session.id)
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
    ticket_id: str, session_id: str, mutate: Callable[[TicketTask], None]
) -> bool:
    """Apply *mutate* to the row iff it is still RUNNING and owned by *session_id*.

    Re-verified under ``dev_queue_lock`` because the gate's check has been
    released since: a row that moved off RUNNING or was reclaimed by another
    session in the meantime is left as found (returns False).
    """
    with dev_queue_lock():
        store = load_dev_queue()
        target = _owned_running_row(store, ticket_id, session_id)
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


def _stop_surface(ctx: _ActContext) -> bool:
    """Step 4: stop the live daemon session; False if the stop raised.

    A session whose ``surface_ref`` is already cleared has nothing to stop.
    On failure the session stays ACTIVE and the row RUNNING; the next tick
    retries with the lockout already armed.
    """
    surface_ref = ctx.session.surface_ref
    if surface_ref is None:
        return True
    try:
        _deps.get_native_daemon_client().stop(surface_ref)
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
    return True


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
    if not _audit_auto_act(ctx) or not _stop_surface(ctx):
        return False
    _persist_completed(ctx)
    requeued = _mutate_owned_running_row(
        ctx.ticket_id,
        ctx.session.id,
        partial(_revert_to_pending, until=ctx.until),
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
    if not _mutate_owned_running_row(
        ctx.ticket_id, ctx.session.id, _park_blocked_on_user
    ):
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
        text = detection.matched_text
        reset_at = (
            parse_usage_limit_reset(text, now=now.astimezone(_deps.host_timezone()))
            if text
            else None
        )
        until = resolve_usage_limited_until(
            now, reset_at, config.usage_limit_backoff_seconds
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


def detect_and_park_mid_turn_usage_limits(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    clients: dict[str, ClientConfig],
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Detect and disposition mid-turn usage-limit stops (GitHub #2324).

    Combines the detect and act phases, mirroring
    ``record_session_liveness_changes``. ``task_by_ticket`` may be pre-loaded by
    the caller to avoid a duplicate dev-queue read within the same reconcile
    tick; when omitted it is loaded here. Returns the ticket ids reverted to
    PENDING (``reap_policy: auto`` only).
    """
    resolved_task_by_ticket = (
        task_by_ticket
        if task_by_ticket is not None
        else {t.ticket_id: t for t in load_dev_queue().tasks}
    )
    candidates = _detect_mid_turn_usage_limit_candidates(
        state, native_live=native_live, task_by_ticket=resolved_task_by_ticket
    )
    return _act_on_mid_turn_usage_limit_candidates(
        state,
        candidates,
        native_live=native_live,
        clients=clients,
        config=config,
        now=now,
    )
