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

The act spans four stores that cannot commit together -- the daemon, the
session state, the dev queue and the event inbox -- so it is a write-ahead
intent, resumable from any point, rather than an ordering of steps:

1. Gate, with no side effects: under ``dev_queue_lock``, re-read the tail and
   check the row is RUNNING under ``(ticket_id, client, session_id)`` with no
   act already in flight. Either failing is a no-op this tick.
2. Decide: in the same lock hold, write ``TicketTask.usage_limit_act``. This
   is the only write that decides. Its branch is ``auto`` when the lane's
   ``reap_policy`` is AUTO (ADR-0006 invariant 2), else ``park``.
3. Resume: this tick, and every later tick that finds the intent, performs
   each remaining step idempotently, checking state before each one:

   - arm the client's ``usage_limited_until`` lockout, unless the sidecar
     already holds a window at least as long;
   - record the audit events -- ``session.needs_attention``,
     ``session.reap_proposed`` (ADR-0006 invariant 3) and, under ``auto``,
     ``session.completed`` -- unless the intent is marked audited. They
     carry the intent's ``started_at`` so a re-emit reads as the same act:
     a crash before the mark re-emits them, and a duplicate is accepted;
   - ``auto`` only: while the surface is live, re-read the tail immediately
     before stopping it. New content abandons the act: the intent is
     cleared and the session and row are left as they are. Otherwise stop
     it and confirm via the roster (bounded poll) that it left; a surface
     still listed is retried next tick. Then persist the session
     COMPLETED/``usage_limited`` (not a crash) unless it is already
     terminal;
   - transition the row -- ``auto``: RUNNING -> PENDING with
     ``next_eligible_at`` at the window's end, so the existing claim gate
     releases it; ``park``: RUNNING -> BLOCKED_ON_USER /
     ``usage_limited_mid_turn`` -- which clears the intent in the same row
     write.

A step that fails ends this tick's attempt and the next tick resumes from the
intent. While it is set, the generic phantom, completed-session backstop and
liveness sweeps leave the row alone, so an interrupted act is never charged an
attempt. Neither branch charges ``unproductive_attempts`` -- a whole turn ran.
Under ``park`` the session, its surface and the row's ``session_id`` are left
untouched for an operator to clear.
"""

from __future__ import annotations

import logging
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Literal, NamedTuple

from cw.config import save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dispatch_state import (
    load_usage_limited_until,
    merge_and_save_usage_limited_until,
    record_usage_limit_armed,
    resolve_usage_limited_until,
)
from cw.events import record_event
from cw.exceptions import parse_usage_limit_reset
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    UsageLimitAct,
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

# The stop confirmation: how long to poll the daemon roster for the stopped
# surface to leave it. Short, because reconcile runs under sessions_lock and
# ``claude stop`` has already returned by then.
_STOP_CONFIRM_TIMEOUT_SECS = 5.0
_STOP_CONFIRM_INTERVAL_SECS = 0.5

_AUTO: Literal["auto"] = "auto"
_PARK: Literal["park"] = "park"

# Payload key carrying the intent's started_at on the act's audit events, so a
# re-emit after a crash is recognisably the same act as the first emit.
ACT_STARTED_AT_KEY = "act_started_at"


class _Stop(Enum):
    DONE = "done"
    RETRY = "retry"
    ABANDONED = "abandoned"


def sessions_with_act_in_flight(tasks: Iterable[TicketTask]) -> frozenset[str]:
    """The sessions whose row carries an unfinished mid-turn usage-limit act.

    The generic phantom sweep skips these: a session this sweep has stopped
    but not yet closed is not a crash, and reverting its row there would
    charge the attempt the act exists to spare.
    """
    return frozenset(
        task.usage_limit_act.session_id
        for task in tasks
        if task.usage_limit_act is not None
    )


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
    blamed, and another same-ticket row can never stand in for it. A row
    already carrying an act is resumed from its intent, never decided again.
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
        if task is None or task.usage_limit_act is not None:
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


def _resolve_window(
    matched_text: str | None, *, now: datetime, config: OrchestratorConfig
) -> tuple[datetime | None, datetime]:
    """The limit message's parsed reset instant (if any) and the window's end."""
    reset_at = (
        parse_usage_limit_reset(matched_text, now=now.astimezone(_deps.host_timezone()))
        if matched_text
        else None
    )
    until = resolve_usage_limited_until(
        now, reset_at, config.usage_limit_backoff_seconds
    )
    return reset_at, until


def _decide(
    session: Session,
    ticket_id: str,
    *,
    branch: Literal["auto", "park"],
    config: OrchestratorConfig,
    now: datetime,
) -> UsageLimitAct | None:
    """Gate the candidate and persist its intent: the act's only deciding write.

    Under ``dev_queue_lock``, re-reads the transcript (the limit text must
    still be the last content-bearing record) and checks the row is still
    RUNNING under ``(ticket_id, session.client, session.id)`` with no act in
    flight. Either failing is a no-op: nothing is written, emitted or
    stopped. Otherwise the intent is written to the row in the same lock
    hold, and returned.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        target = _owned_running_row(store.tasks, ticket_id, session.client, session.id)
        detection = _mid_turn_limit_detection(session)
        intent: UsageLimitAct | None = None
        if (
            target is not None
            and target.usage_limit_act is None
            and detection is not None
        ):
            reset_at, until = _resolve_window(
                detection.matched_text, now=now, config=config
            )
            intent = UsageLimitAct(
                session_id=session.id,
                branch=branch,
                started_at=now,
                reset_at=reset_at,
                until=until,
            )
            target.usage_limit_act = intent
            save_dev_queue(store)
    if detection is None:
        _log.info(
            "usage_limit_mid_turn: tail changed since detect for ticket %s "
            "session %s; skipping this tick",
            ticket_id,
            session.id,
        )
    elif intent is None:
        _log.info(
            "usage_limit_mid_turn: row for ticket %s is no longer RUNNING under "
            "session %s without an act in flight; skipping this tick",
            ticket_id,
            session.id,
        )
    return intent


class _ActRow(NamedTuple):
    """The row an act is bound to, and the intent it carries."""

    ticket_id: str
    client: str
    lane: str
    intent: UsageLimitAct

    @property
    def auto(self) -> bool:
        return self.intent.branch == _AUTO


class _Act(NamedTuple):
    """One act's inputs, shared by every step of its resume."""

    row: _ActRow
    state: CwState
    session: Session
    native_live: set[str]
    now: datetime


def _row_carrying(tasks: Iterable[TicketTask], row: _ActRow) -> TicketTask | None:
    """The RUNNING row still carrying *row*'s exact intent, else None.

    Any transition clears the intent, so a row another writer dispositioned
    in the meantime no longer matches and is left as found.
    """
    intent = row.intent
    return next(
        (
            task
            for task in tasks
            if task.ticket_id == row.ticket_id
            and task.client == row.client
            and task.status is QueueItemStatus.RUNNING
            and task.session_id == intent.session_id
            and task.usage_limit_act is not None
            and task.usage_limit_act.session_id == intent.session_id
            and task.usage_limit_act.started_at == intent.started_at
        ),
        None,
    )


def _mutate_act_row(row: _ActRow, mutate: Callable[[TicketTask], None]) -> bool:
    """Apply *mutate* under ``dev_queue_lock`` iff the row still carries the act."""
    with dev_queue_lock():
        store = load_dev_queue()
        target = _row_carrying(store.tasks, row)
        if target is None:
            return False
        mutate(target)
        save_dev_queue(store)
    return True


def _lockout_covers(client: str, until: datetime, *, now: datetime) -> bool:
    """Is the client's lockout already armed through *until* (or moot)?"""
    if until <= now:
        return True
    armed = load_usage_limited_until().get(client)
    return armed is not None and armed >= until


def _arm_lockout(act: _Act) -> bool:
    """Arm the client's spawn lockout unless the sidecar already covers it.

    The shared helper audits before it persists, so a failed audit write
    raises with no window saved. The sidecar write swallows its own errors,
    so the window is read back: one that did not land is retried next tick.
    """
    row = act.row
    until = row.intent.until
    if _lockout_covers(row.client, until, now=act.now):
        return True
    record_usage_limit_armed(row.client, until=until, reset_at=row.intent.reset_at)
    merge_and_save_usage_limited_until({row.client: until})
    if _lockout_covers(row.client, until, now=act.now):
        return True
    _log.warning(
        "usage_limit_mid_turn: the %s lockout for ticket %s did not persist; "
        "the act resumes next tick",
        row.client,
        row.ticket_id,
    )
    return False


def _audit_payload(act: _Act) -> dict[str, object]:
    session = act.session
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": act.row.ticket_id,
        "claude_session_id": session.claude_session_id,
        "crashed": False,
        ACT_STARTED_AT_KEY: act.row.intent.started_at.isoformat(),
    }


def _record_needs_attention(act: _Act) -> None:
    """Record the usage-limit detection, naming the reset instant."""
    disposition = (
        "parked without charge, will re-enter the queue automatically"
        if act.row.auto
        else "parked BLOCKED_ON_USER without charge; needs an operator to clear"
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            **_audit_payload(act),
            "paused_status": _USAGE_LIMITED_MID_TURN_REASON,
            "breadcrumbs": (
                f"hit usage limit mid-turn; resets "
                f"{act.row.intent.until.isoformat()}; {disposition}"
            ),
            "lane": act.row.lane,
        },
        correlation_id=act.row.ticket_id,
    )


def _record_completed(act: _Act) -> None:
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            **_audit_payload(act),
            # Reconcile owns this row's disposition, including resuming an
            # interrupted act, so the dispatch consumer skips it.
            "reason": _USAGE_LIMITED_MID_TURN_REASON,
        },
        correlation_id=act.row.ticket_id,
    )


def _propose(act: _Act) -> None:
    candidate = ReapCandidate(
        session_id=act.session.id,
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id=act.row.ticket_id,
        lane=act.row.lane,
        client=act.row.client,
        reap_reason=ReapReason.USAGE_LIMIT_MID_TURN,
        usage_limit_detected=True,
    )
    _emit_reap_proposed(
        act.state, [candidate], native_live=act.native_live, now=act.now
    )


def _mark_audited(target: TicketTask, *, at: datetime) -> None:
    intent = target.usage_limit_act
    if intent is not None:
        target.usage_limit_act = intent.model_copy(update={"audited_at": at})


def _audit(act: _Act) -> bool:
    """Record the act's audit events once, before any effect.

    Skipped once the intent is marked audited. The detection page, then the
    reap proposal, then (``auto``) the completion; the push notification
    follows them. A failed write raises and the whole set is re-recorded next
    tick: at-least-once, never lost. The proposal's dedup stamp is rolled
    back on failure so a later ``save_state`` cannot persist a proposal that
    was never recorded.
    """
    if act.row.intent.audited_at is not None:
        return True
    session = act.session
    prior_proposed_at = session.reap_proposed_at
    try:
        _record_needs_attention(act)
        _propose(act)
        if act.row.auto:
            _record_completed(act)
    except OSError:
        session.reap_proposed_at = prior_proposed_at
        raise
    _deps.fire_push_notification(session.name, session.client)
    return _mutate_act_row(act.row, partial(_mark_audited, at=act.now))


def _clear_intent(target: TicketTask) -> None:
    target.usage_limit_act = None


def _stop_surface(act: _Act) -> _Stop:
    """``auto``: stop the live daemon surface and verify it left the roster.

    Nothing to stop once the session is terminal or its surface is no longer
    live -- a stop that landed before a crash is not repeated. Otherwise the
    tail is re-read immediately before the stop: new content abandons the act
    (the intent is cleared; the session and row are left as they are).
    ``stop()`` swallows its own failures, so the roster must stop listing the
    surface within a short bounded poll (an unreadable roster confirms
    nothing); a surface still listed is retried next tick.
    """
    session = act.session
    surface_ref = session.surface_ref
    if (
        session.status in TERMINAL_SESSION_STATUSES
        or surface_ref is None
        or surface_ref not in act.native_live
    ):
        return _Stop.DONE
    if _mid_turn_limit_detection(session) is None:
        _mutate_act_row(act.row, _clear_intent)
        _log.info(
            "usage_limit_mid_turn: tail changed before the stop for ticket %s "
            "session %s; act abandoned, session and row left as they are",
            act.row.ticket_id,
            session.id,
        )
        return _Stop.ABANDONED
    daemon = _deps.get_native_daemon_client()
    daemon.stop(surface_ref)
    if wait_for_roster_presence(
        daemon,
        surface_ref,
        present=False,
        timeout=_STOP_CONFIRM_TIMEOUT_SECS,
        interval=_STOP_CONFIRM_INTERVAL_SECS,
    ):
        return _Stop.DONE
    _log.warning(
        "usage_limit_mid_turn: surface %s for ticket %s session %s is still in "
        "the daemon roster (or the roster is unreadable) %.1fs after stop; the "
        "act resumes next tick",
        surface_ref,
        act.row.ticket_id,
        session.id,
        _STOP_CONFIRM_TIMEOUT_SECS,
    )
    return _Stop.RETRY


def _persist_completed(act: _Act) -> None:
    """``auto``: persist the session COMPLETED, unless it is already terminal."""
    session = act.session
    if session.status in TERMINAL_SESSION_STATUSES:
        return
    session.status = SessionStatus.COMPLETED
    session.completed_at = act.now
    session.completed_reason = CompletionReason.USAGE_LIMITED
    session.reap_reason = ReapReason.USAGE_LIMIT_MID_TURN
    save_state(act.state)


def _requeue(target: TicketTask, *, until: datetime) -> None:
    transition_task_status(target, QueueItemStatus.PENDING, unproductive=False)
    target.session_id = None
    target.next_eligible_at = until


def _park(target: TicketTask) -> None:
    # session_id deliberately stays set: a late sentinel still routes through
    # the #918 rescue, which re-finds the row by it (as the #2135 park does).
    transition_task_status(
        target,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=_USAGE_LIMITED_MID_TURN_REASON,
        unproductive=False,
    )


def _finish(row: _ActRow) -> bool:
    """Transition the row, clearing the intent in the same write.

    Returns True iff an ``auto`` act requeued the row to PENDING.
    """
    mutate = partial(_requeue, until=row.intent.until) if row.auto else _park
    if _mutate_act_row(row, mutate):
        return row.auto
    _log.info(
        "usage_limit_mid_turn: row for ticket %s no longer carries the act for "
        "session %s; left as found",
        row.ticket_id,
        row.intent.session_id,
    )
    return False


def _resume(act: _Act) -> bool:
    """Perform every step the act has not done yet; True iff it requeued."""
    if not _arm_lockout(act) or not _audit(act):
        return False
    if act.row.auto:
        if _stop_surface(act) is not _Stop.DONE:
            return False
        _persist_completed(act)
    return _finish(act.row)


def _resume_contained(
    row: _ActRow,
    *,
    state: CwState,
    native_live: set[str],
    now: datetime,
) -> bool:
    """Resume one act; a failed step ends this tick's attempt, not the tick.

    The intent makes every step resumable, so a write that fails is logged
    and retried next tick rather than aborting the other reconcile sweeps.
    """
    session = next((s for s in state.sessions if s.id == row.intent.session_id), None)
    try:
        if session is None:
            # No session left to audit, stop or close: only the row remains.
            _log.warning(
                "usage_limit_mid_turn: session %s of the act on ticket %s is "
                "gone; finishing the row",
                row.intent.session_id,
                row.ticket_id,
            )
            return _finish(row)
        act = _Act(
            row=row, state=state, session=session, native_live=native_live, now=now
        )
        return _resume(act)
    except OSError:
        _log.warning(
            "usage_limit_mid_turn: a step of the act on ticket %s session %s "
            "failed; the act resumes next tick",
            row.ticket_id,
            row.intent.session_id,
            exc_info=True,
        )
        return False


def _resume_open_acts(
    state: CwState,
    *,
    tasks: Sequence[TicketTask],
    native_live: set[str],
    now: datetime,
) -> list[str]:
    """Resume every act whose intent a RUNNING row carries; return those requeued."""
    requeued: list[str] = []
    for task in tasks:
        intent = task.usage_limit_act
        if intent is None or task.status is not QueueItemStatus.RUNNING:
            continue
        row = _ActRow(
            ticket_id=task.ticket_id, client=task.client, lane=task.lane, intent=intent
        )
        if _resume_contained(row, state=state, native_live=native_live, now=now):
            requeued.append(task.ticket_id)
    return requeued


def _act_on_mid_turn_usage_limit_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    now: datetime,
) -> list[str]:
    """Decide each candidate's act, then resume it; return the ticket ids requeued.

    ``reap_policy: auto`` decides the ``auto`` branch (stop, close, requeue);
    any other policy decides ``park`` (BLOCKED_ON_USER). A candidate whose
    gate fails decides nothing.
    """
    session_by_id = {s.id: s for s in state.sessions}
    requeued: list[str] = []
    for candidate in candidates:
        session = session_by_id.get(candidate.session_id)
        ticket_id = candidate.ticket_id
        if session is None or ticket_id is None:
            continue
        branch: Literal["auto", "park"] = (
            _AUTO
            if resolve_reap_policy(candidate, clients, config) is ReapPolicy.AUTO
            else _PARK
        )
        try:
            intent = _decide(session, ticket_id, branch=branch, config=config, now=now)
        except OSError:
            _log.warning(
                "usage_limit_mid_turn: persisting the act for ticket %s session "
                "%s failed; nothing was decided",
                ticket_id,
                session.id,
                exc_info=True,
            )
            continue
        if intent is None:
            continue
        row = _ActRow(
            ticket_id=ticket_id,
            client=session.client,
            lane=candidate.lane,
            intent=intent,
        )
        if _resume_contained(row, state=state, native_live=native_live, now=now):
            requeued.append(ticket_id)
    return requeued


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

    First resumes every act already in flight, then decides and runs new ones,
    mirroring ``record_session_liveness_changes``'s combined shape. ``tasks``
    (every dev-queue row, not a ticket-keyed map, which would collapse
    same-ticket rows) may be pre-loaded by the caller to avoid a duplicate
    dev-queue read within the same reconcile tick; when omitted it is loaded
    here. Returns the ticket ids requeued to PENDING (``auto`` acts only).
    """
    resolved_tasks = tasks if tasks is not None else load_dev_queue().tasks
    resumed = _resume_open_acts(
        state, tasks=resolved_tasks, native_live=native_live, now=now
    )
    candidates = _detect_mid_turn_usage_limit_candidates(
        state, native_live=native_live, tasks=resolved_tasks
    )
    return resumed + _act_on_mid_turn_usage_limit_candidates(
        state,
        candidates,
        native_live=native_live,
        clients=clients,
        config=config,
        now=now,
    )
