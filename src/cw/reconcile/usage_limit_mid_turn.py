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

Every candidate whose row survives the identity-checked queue transition is
proposed via ``session.reap_proposed`` (ADR-0006 invariant 3) and the act is
gated by the lane's ``reap_policy`` (ADR-0006 invariant 2):

- ``auto``: the row goes RUNNING -> PENDING with ``next_eligible_at`` set to the
  reset instant, so the existing claim gate releases it at the reset with no
  new sweep; the session closes COMPLETED/``usage_limited`` (not a crash) and
  its surface is stopped so a live idle home cannot defer the re-claim.
- any other policy (``signal_only`` default): the row parks BLOCKED_ON_USER
  with ``disposition="usage_limited_mid_turn"``; the session, its surface and
  the row's ``session_id`` are left untouched for an operator to clear.

Neither branch charges ``unproductive_attempts`` -- a whole turn ran. Both arm
the client's ``usage_limited_until`` lockout and emit
``session.needs_attention`` naming the reset instant. The evidence is re-read
at act time; a tail that changed since detect is a silent no-op this tick.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dispatch_state import (
    USAGE_LIMIT_SOURCE_FLAT_BACKOFF,
    USAGE_LIMIT_SOURCE_PARSED_RESET,
    merge_and_save_usage_limited_until,
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


def _mutate_owned_running_row(
    ticket_id: str, session_id: str, mutate: Callable[[TicketTask], None]
) -> bool:
    """Apply *mutate* to the row iff it is still RUNNING and owned by *session_id*.

    Re-verified under ``dev_queue_lock`` because the detect-phase snapshot may
    be stale: a row that moved off RUNNING or was reclaimed by another session
    since detect is a silent no-op (returns False).
    """
    with dev_queue_lock():
        store = load_dev_queue()
        lookup = _lookup_matching_task(store, ticket_id, session_id)
        target = lookup.target
        if target is None or lookup.target_status is not QueueItemStatus.RUNNING:
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


def _complete_usage_limited_session(
    state: CwState, session: Session, ticket_id: str, *, now: datetime
) -> None:
    """Record the completion event, then close *session* and stop its surface.

    Audit before effect, as ``_close_session_audited`` (#2285): a failed event
    write raises before the session is touched, so it stays ACTIVE for the
    next pass rather than being closed without its audit trail.
    """
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
        },
        correlation_id=ticket_id,
    )
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.USAGE_LIMITED
    session.reap_reason = ReapReason.USAGE_LIMIT_MID_TURN
    save_state(state)
    if session.surface_ref is not None:
        _deps.get_native_daemon_client().stop(session.surface_ref)


def _arm_lockout_and_notify(
    session: Session,
    candidate: ReapCandidate,
    ticket_id: str,
    *,
    until: datetime,
    reset_at: datetime | None,
    auto: bool,
) -> None:
    """Arm the client's spawn lockout and page the operator with the reset time."""
    merge_and_save_usage_limited_until({session.client: until})
    source = (
        USAGE_LIMIT_SOURCE_PARSED_RESET
        if until == reset_at
        else USAGE_LIMIT_SOURCE_FLAT_BACKOFF
    )
    record_event(
        OrchestratorEventType.USAGE_LIMIT_ARMED,
        {"client": session.client, "until": until.isoformat(), "source": source},
    )
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
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _USAGE_LIMITED_MID_TURN_REASON,
            "breadcrumbs": (
                f"hit usage limit mid-turn; resets {until.isoformat()}; {disposition}"
            ),
            "crashed": False,
            "lane": candidate.lane,
        },
        correlation_id=ticket_id,
    )
    _deps.fire_push_notification(session.name, session.client)


def _act_on_mid_turn_usage_limit_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    now: datetime,
) -> list[str]:
    """Propose, gate, and act on each candidate; return ticket ids reverted.

    Only ``reap_policy: auto`` reverts (and so appears in the returned list);
    any other policy parks the row BLOCKED_ON_USER. The transcript tail is
    re-read first -- if it no longer shows a mid-turn stop, nothing is
    proposed, mutated, or armed this tick. The identity-checked row transition
    runs before the reap proposal, so a row that moved or was reclaimed since
    detect is likewise a silent no-op.
    """
    session_by_id = {s.id: s for s in state.sessions}
    reverted: list[str] = []
    for candidate in candidates:
        session = session_by_id.get(candidate.session_id)
        ticket_id = candidate.ticket_id
        if session is None or ticket_id is None:
            continue
        detection = _mid_turn_limit_detection(session)
        if detection is None:
            _log.info(
                "usage_limit_mid_turn: tail changed since detect for ticket %s "
                "session %s; skipping this tick",
                ticket_id,
                session.id,
            )
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
        auto = resolve_reap_policy(candidate, clients, config) is ReapPolicy.AUTO
        mutate: Callable[[TicketTask], None] = (
            partial(_revert_to_pending, until=until) if auto else _park_blocked_on_user
        )
        # Race check first, as #2285 corrected: a lost race proposes nothing.
        if not _mutate_owned_running_row(ticket_id, session.id, mutate):
            continue
        _emit_reap_proposed(state, [candidate], native_live=native_live, now=now)
        if auto:
            _complete_usage_limited_session(state, session, ticket_id, now=now)
            reverted.append(ticket_id)
        _arm_lockout_and_notify(
            session, candidate, ticket_id, until=until, reset_at=reset_at, auto=auto
        )
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
