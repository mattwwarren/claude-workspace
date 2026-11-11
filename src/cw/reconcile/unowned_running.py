"""Recover RUNNING rows whose launched session was never stamped on them.

Dispatch claims a row to RUNNING, launches a worker, and only then stamps the
session id onto the row (``cw.dispatch.claim.claimed_row._stamp_spawn_success``).
When that stamp fails twice (#2502), the worker keeps running but the row stays
RUNNING with no ``session_id``. Nothing could then route its completion: the
dispatch consumer and the Stop-hook router both look a row up by the session
id, so the row stuck RUNNING, holding its lane slot, until an operator
released it by hand.

This sweep has three outcomes for such a row (#2591, #2615, #3328).

**ADOPT** (first, ungated). It binds the row to its session when the evidence
ties the two together. The evidence is the worktree's ``cw-context.json``,
which the spawn writes before the worker exists (``cw.spawn._write_hook_context``).
A row is adopted only when all of these hold:

- it is RUNNING, unbound, not backstop-exempt, the only RUNNING row for its
  (client, ticket), and its client is in this tick's client scope;
- the context's ``attempt`` equals the row's ``attempts`` (``attempts`` moves
  only at claim) and its ``ticket_id`` and ``client`` are the row's;
- the context's ``session_id`` is a recorded Session that no other row owns,
  of the same client, whose name parses to the same ticket;
- that Session did not start before the row's ``claimed_at``. A Session that
  did belongs to an earlier row for the same ticket, whose context survived a
  pre-launch crash of this claim. A naive ``started_at`` or ``claimed_at``
  fails closed rather than raising. A row with no ``claimed_at`` (claimed
  before schema v44) has no timing evidence and skips this check.

Adoption writes exactly the fields dispatch's own stamp writes
(``_apply_spawn_success_fields``, which also clears the launch marker), clears
an ``?unowned_unconfirmed`` note, and leaves ``stage_base_ref`` alone, so no
git runs. It is constructive: no revert, stop, park or removal, so it is not
gated by ``reap_policy`` (ADR-0006) and is not timer-driven (ADR-0014). It
applies to any Session status and any backend.

**NO_OWNER** (the only status mutation). The claim context ties nothing
(missing, no attempt, another attempt, another claim, or a session absent from
``sessions.json``), the launch marker proves the claiming process gone
(:func:`cw.launch_marker.observed_launch_owner_state`), the daemon roster is
readable and homes no worker on the worktree, the client is claude-native
only, and the roster absence is not unproven. The row is parked
BLOCKED_ON_USER (``signal_only``) or reverted PENDING (``auto``); see
:mod:`cw.reconcile.unowned_running_acts`. A claim age never triggers it.

**UNCONFIRMABLE** (signal only). A specific reason blocks both: a duplicate
RUNNING row, a session bound elsewhere or recorded for another claim, a stale
or naive timestamp, an unreadable context (each signalled when the marker
proves the owner gone, or when there is no marker and the claim is past the
spawn grace), a non-native executor, or an unproven roster absence. One page,
then a latch in ``advisory_note``; nothing else changes. A row with no
marker and no tying context, or a marker whose owner may be alive, waits:
``cw.reconcile.lost_session_rows``'s sessionless page is its only signal.

Runs from ``cw.reconcile.locked_sweeps._run_terminal_backstops_and_sweeps``,
under ``reconcile()``'s ``sessions_lock``, before the TIMED_OUT backstop. It
reads state and the queue fresh, nests ``dev_queue_lock`` for each write
(ADR-0019 order), runs no subprocess (the roster is a file read), and records
its audit events and pages after that lock releases, except NO_OWNER's
``session.reap_proposed``, which precedes the mutation (ADR-0006 invariant 3).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from cw.config import load_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.launch_marker import LaunchOwnerState, observed_launch_owner_state
from cw.models import OrchestratorEventType, QueueItemStatus, resolve_feature_branch
from cw.queue_rows import (
    _apply_spawn_success_fields,
    _find_running_row,
    _is_backstop_exempt,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _UNOWNED_UNCONFIRMED_NOTE_PREFIX,
    roster_absence_unproven,
    ticket_id_for_session,
)
from cw.reconcile.lost_session_rows import _past_spawn_grace
from cw.reconcile.unowned_running_acts import (
    CONTEXT_OK,
    CONTEXT_UNREADABLE,
    ContextTie,
    NoOwnerCandidate,
    SignalCandidate,
    act_signal,
    non_native_backend_names,
    read_context_tie,
    run_no_owner_acts,
    worker_homed,
)
from cw.reconcile.unowned_running_text import (
    CAUSE_BOUND_ELSEWHERE,
    CAUSE_CONFLICTING_SESSION,
    CAUSE_DUPLICATE,
    CAUSE_NAIVE_DATETIME,
    CAUSE_NON_NATIVE,
    CAUSE_ROSTER_UNPROVEN,
    CAUSE_STALE_SESSION,
    CAUSE_UNREADABLE_CONTEXT,
    CONTEXT_SESSION_ABSENT,
    backend_names_text,
    context_clause,
    unconfirmed_evidence,
    unconfirmed_note_head,
)
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        Session,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient, WorkerHome

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnownedCandidate:
    """One unbound RUNNING row and the recorded session its claim launched.

    ``created_at``, ``attempts`` and ``claimed_at`` are the row's identity at
    detect time; the act re-checks all three under ``dev_queue_lock`` before
    binding.
    """

    ticket_id: str
    client: str
    lane: str
    created_at: datetime
    attempts: int
    claimed_at: datetime | None
    session_id: str
    session_name: str


class UnownedFindings(NamedTuple):
    """:func:`detect_unowned_running`'s result.

    ``adopts``: rows the evidence ties to a session. ``no_owner``: rows whose
    claiming process is gone and that nothing owns. ``signals``: rows whose
    ownership cannot be confirmed and that are not yet latched for that cause.
    """

    adopts: list[UnownedCandidate]
    no_owner: list[NoOwnerCandidate]
    signals: list[SignalCandidate]


class _Evidence(NamedTuple):
    """A specific reason a row can neither be adopted nor released."""

    cause: str
    facts: dict[str, str]


@dataclass(frozen=True)
class _TickView:
    """Everything detect reads once per tick."""

    store: DevQueueStore
    state: CwState
    clients: dict[str, ClientConfig]
    now: datetime
    daemon: NativeDaemonClient
    native_live: AbstractSet[str]
    homes: frozenset[WorkerHome] | None
    sessions_by_id: dict[str, Session]
    bound_session_ids: set[str]
    running: Counter[tuple[str, str]]


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _timing_cause(session: Session, row: TicketTask) -> str | None:
    """Why *session*'s start cannot follow *row*'s claim, or None if it does.

    Never raises: ``Session.started_at`` and ``TicketTask.claimed_at`` are
    plain ``datetime`` fields, and comparing a naive one with an aware one
    raises ``TypeError``, which would abort the whole reconcile tick. Either
    being naive is ``naive_datetime``; a start before the claim is
    ``stale_session``. A row with no ``claimed_at`` has no timing evidence.
    """
    claimed_at = row.claimed_at
    if claimed_at is None:
        return None
    started_at = session.started_at
    if not (_is_aware(claimed_at) and _is_aware(started_at)):
        _log.debug(
            "unowned_running: %s/%s not adopted: naive datetime "
            "(claimed_at=%s, session %s started_at=%s)",
            row.client,
            row.ticket_id,
            claimed_at,
            session.id,
            started_at,
        )
        return CAUSE_NAIVE_DATETIME
    if started_at < claimed_at:
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s started at %s, "
            "before the claim at %s",
            row.client,
            row.ticket_id,
            session.id,
            started_at.isoformat(),
            claimed_at.isoformat(),
        )
        return CAUSE_STALE_SESSION
    return None


def _classify_session(
    row: TicketTask, tie: ContextTie, *, view: _TickView, facts: dict[str, str]
) -> UnownedCandidate | _Evidence | ContextTie:
    """Classify a context that ties *row* to session id ``tie.session_id``."""
    session_id = tie.session_id or ""
    facts = {**facts, "sid": session_id}
    if session_id in view.bound_session_ids:
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s is bound to another row",
            row.client,
            row.ticket_id,
            session_id,
        )
        return _Evidence(CAUSE_BOUND_ELSEWHERE, facts)
    session = view.sessions_by_id.get(session_id)
    if session is None:
        return tie._replace(state=CONTEXT_SESSION_ABSENT)
    session_ticket = ticket_id_for_session(session.name)
    if session.client != row.client or session_ticket != row.ticket_id:
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s is recorded "
            "for another client or ticket",
            row.client,
            row.ticket_id,
            session_id,
        )
        return _Evidence(
            CAUSE_CONFLICTING_SESSION,
            {
                **facts,
                "s_client": session.client,
                "s_ticket": session_ticket or "(no ticket)",
            },
        )
    timing = _timing_cause(session, row)
    if timing is not None:
        return _Evidence(timing, {**facts, "started": session.started_at.isoformat()})
    return UnownedCandidate(
        ticket_id=row.ticket_id,
        client=row.client,
        lane=row.lane,
        created_at=row.created_at,
        attempts=row.attempts,
        claimed_at=row.claimed_at,
        session_id=session.id,
        session_name=session.name,
    )


def _duplicate_evidence(
    row: TicketTask, *, view: _TickView, facts: dict[str, str]
) -> _Evidence:
    _log.debug(
        "unowned_running: %s/%s not adopted: duplicate RUNNING rows",
        row.client,
        row.ticket_id,
    )
    other = min(
        (
            t.created_at
            for t in view.store.tasks
            if t is not row
            and t.status is QueueItemStatus.RUNNING
            and t.client == row.client
            and t.ticket_id == row.ticket_id
        ),
        default=row.created_at,
    )
    return _Evidence(CAUSE_DUPLICATE, {**facts, "other_created": other.isoformat()})


def _classify_unbound(
    row: TicketTask, *, worktree: Path, view: _TickView
) -> UnownedCandidate | _Evidence | ContextTie:
    """Adoption, a specific blocking reason, or the no-tie context state."""
    facts = {"client": row.client, "ticket": row.ticket_id, "wt": str(worktree)}
    if view.running[(row.client, row.ticket_id)] > 1:
        return _duplicate_evidence(row, view=view, facts=facts)
    tie = read_context_tie(worktree, row)
    if tie.state == CONTEXT_UNREADABLE:
        return _Evidence(CAUSE_UNREADABLE_CONTEXT, facts)
    if tie.state != CONTEXT_OK:
        return tie
    return _classify_session(row, tie, view=view, facts=facts)


def _signal(
    row: TicketTask, cause: str, facts: dict[str, str]
) -> SignalCandidate | None:
    """The signal for *row* and *cause*, or None when already latched for it."""
    if (row.advisory_note or "").startswith(unconfirmed_note_head(cause)):
        return None
    return SignalCandidate(
        ticket_id=row.ticket_id,
        client=row.client,
        lane=row.lane,
        created_at=row.created_at,
        attempts=row.attempts,
        claimed_at=row.claimed_at,
        cause=cause,
        evidence=unconfirmed_evidence(cause, **facts),
    )


def _evidence_signal(
    row: TicketTask, evidence: _Evidence, *, now: datetime
) -> SignalCandidate | None:
    """Signal a specific reason when the owner is gone, or unknown and old."""
    verdict = observed_launch_owner_state(row.launch_marker)
    if verdict is LaunchOwnerState.OWNER_GONE or (
        verdict is LaunchOwnerState.UNKNOWN and _past_spawn_grace(row, now)
    ):
        return _signal(row, evidence.cause, evidence.facts)
    return None


def _no_tie_outcome(
    row: TicketTask,
    tie: ContextTie,
    *,
    client: ClientConfig,
    worktree: Path,
    view: _TickView,
) -> NoOwnerCandidate | SignalCandidate | None:
    """NO_OWNER, a signal, or None (wait) for a row no context ties."""
    marker = row.launch_marker
    if (
        marker is None
        or observed_launch_owner_state(marker) is not LaunchOwnerState.OWNER_GONE
    ):
        return None
    homes = view.homes
    if homes is None:
        return None
    facts = {"client": row.client, "ticket": row.ticket_id, "wt": str(worktree)}
    names = non_native_backend_names(client)
    if names:
        return _signal(
            row, CAUSE_NON_NATIVE, {**facts, "names": backend_names_text(names)}
        )
    if worker_homed(worktree, daemon=view.daemon, homes=homes):
        return None
    reason = roster_absence_unproven(
        view.state, view.store.tasks, homes, view.native_live, clients=view.clients
    )
    if reason is not None:
        return _signal(row, CAUSE_ROSTER_UNPROVEN, {**facts, "reason": reason})
    return NoOwnerCandidate(
        ticket_id=row.ticket_id,
        client=row.client,
        lane=row.lane,
        created_at=row.created_at,
        attempts=row.attempts,
        claimed_at=row.claimed_at,
        launch_marker=marker,
        worktree=worktree,
        context_state=tie.state,
        context_session_id=tie.session_id,
        context_clause=context_clause(
            tie.state,
            claim_attempt=row.attempts,
            context_attempt=tie.attempt,
            session_id=tie.session_id,
        ),
        homes_snapshot=homes,
    )


def _route_row(
    row: TicketTask, *, client: ClientConfig, view: _TickView, found: UnownedFindings
) -> None:
    """Append *row*'s outcome to *found*, if it has one."""
    worktree = worktree_path_for(
        client, resolve_feature_branch(row.ticket_id, client, row)
    )
    outcome = _classify_unbound(row, worktree=worktree, view=view)
    if isinstance(outcome, UnownedCandidate):
        found.adopts.append(outcome)
        return
    result = (
        _evidence_signal(row, outcome, now=view.now)
        if isinstance(outcome, _Evidence)
        else _no_tie_outcome(row, outcome, client=client, worktree=worktree, view=view)
    )
    if isinstance(result, NoOwnerCandidate):
        found.no_owner.append(result)
    elif result is not None:
        found.signals.append(result)


def _unbound_rows(
    store: DevQueueStore, clients: dict[str, ClientConfig]
) -> list[tuple[TicketTask, ClientConfig]]:
    """Every RUNNING, unbound, in-scope, non-exempt row with its client."""
    rows: list[tuple[TicketTask, ClientConfig]] = []
    for row in store.tasks:
        if row.status is not QueueItemStatus.RUNNING or row.session_id is not None:
            continue
        client = clients.get(row.client)
        if client is not None and not _is_backstop_exempt(row):
            rows.append((row, client))
    return rows


def detect_unowned_running(
    store: DevQueueStore,
    state: CwState,
    clients: dict[str, ClientConfig],
    *,
    now: datetime,
    daemon: NativeDaemonClient,
    native_live: AbstractSet[str],
) -> UnownedFindings:
    """Classify every unbound RUNNING row in scope. Pure.

    Reads the worktrees' ``cw-context.json`` files, the launch markers' pids
    and the daemon roster (once, and only when a candidate row exists);
    writes nothing (ADR-0006 invariant 1). *clients* is the tick's client
    scope: a row whose client is not in it (including an empty mapping) is
    skipped. *native_live* is the tick's ``claude agents --json`` roster.
    """
    found = UnownedFindings(adopts=[], no_owner=[], signals=[])
    rows = _unbound_rows(store, clients)
    if not rows:
        return found
    view = _TickView(
        store=store,
        state=state,
        clients=clients,
        now=now,
        daemon=daemon,
        native_live=native_live,
        homes=daemon.list_live_worker_homes(),
        sessions_by_id={s.id: s for s in state.sessions},
        bound_session_ids={
            t.session_id for t in store.tasks if t.session_id is not None
        },
        running=Counter(
            (t.client, t.ticket_id)
            for t in store.tasks
            if t.status is QueueItemStatus.RUNNING
        ),
    )
    for row, client in rows:
        _route_row(row, client=client, view=view, found=found)
    return found


def _still_adoptable(
    store: DevQueueStore, row: TicketTask, candidate: UnownedCandidate
) -> bool:
    """Re-check, under the bind's ``dev_queue_lock``, what detect saw.

    The row must still be unbound on the same claim, no other row may have
    taken the session, and no second RUNNING row may have appeared for the
    same (client, ticket) since detect read its snapshot.
    """
    if (
        row.session_id is not None
        or row.attempts != candidate.attempts
        or row.claimed_at != candidate.claimed_at
    ):
        return False
    return not any(
        other is not row
        and (
            other.session_id == candidate.session_id
            or (
                other.status is QueueItemStatus.RUNNING
                and other.client == row.client
                and other.ticket_id == row.ticket_id
            )
        )
        for other in store.tasks
    )


def _emit_adopted(candidate: UnownedCandidate) -> None:
    """Record the audit event; a failed write is logged, never raised.

    The bind is already saved, and the event is audit-only, so losing it
    must not abort the reconcile tick.
    """
    payload: dict[str, object] = {
        "client": candidate.client,
        "ticket_id": candidate.ticket_id,
        "lane": candidate.lane,
        "session_id": candidate.session_id,
        "session_name": candidate.session_name,
        "attempt": candidate.attempts,
        "claimed_at": (
            candidate.claimed_at.isoformat()
            if candidate.claimed_at is not None
            else None
        ),
    }
    try:
        record_event(
            OrchestratorEventType.TASK_SESSION_ADOPTED,
            payload,
            correlation_id=candidate.ticket_id,
        )
    except (CwError, OSError):
        _log.exception(
            "unowned_running: %s/%s adopted session %s, but the "
            "task.session_adopted event was not recorded",
            candidate.client,
            candidate.ticket_id,
            candidate.session_id,
        )


def _act_adopt(candidate: UnownedCandidate) -> bool:
    """Bind the candidate's row to its session; return whether it was bound."""
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_running_row(
            store,
            candidate.ticket_id,
            candidate.client,
            created_at=candidate.created_at,
        )
        if row is None or not _still_adoptable(store, row, candidate):
            return False
        _apply_spawn_success_fields(row, session_id=candidate.session_id)
        if (row.advisory_note or "").startswith(_UNOWNED_UNCONFIRMED_NOTE_PREFIX):
            row.advisory_note = None
        save_dev_queue(store)
    _log.info(
        "unowned_running: adopted %s/%s onto session %s (attempt %d)",
        candidate.client,
        candidate.ticket_id,
        candidate.session_id,
        candidate.attempts,
    )
    _emit_adopted(candidate)
    return True


def run_unowned_running_recovery(
    *,
    now: datetime,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    native_live: AbstractSet[str],
) -> list[str]:
    """Adopt, then release (NO_OWNER), then signal (UNCONFIRMABLE).

    Returns the adopted ticket ids. *clients* is the tick's client scope
    (empty: a no-op); *native_live* the tick's ``claude agents --json``
    roster; *config* resolves each lane's ``reap_policy``.
    """
    if not clients:
        return []
    daemon = _deps.get_native_daemon_client()
    found = detect_unowned_running(
        load_dev_queue(),
        load_state(),
        clients,
        now=now,
        daemon=daemon,
        native_live=native_live,
    )
    adopted = [c.ticket_id for c in found.adopts if _act_adopt(c)]
    run_no_owner_acts(
        found.no_owner, now=now, daemon=daemon, clients=clients, config=config
    )
    for signal in found.signals:
        act_signal(signal)
    return adopted
