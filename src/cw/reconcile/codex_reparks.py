"""Reconcile-tick re-evaluation of live-writer codex-orphan parks (GitHub #2307).

``cw.reconcile.codex_boot`` runs once per ``serve`` boot. When the process scan
finds a codex writer still alive in an orphan's worktree, or cannot tell (an
unreadable candidate, a failed scan), it parks the task and deliberately
leaves the ``Session`` ACTIVE: closing it would free its ceiling slot and its
hook-context guard for a new spawn that races the writer. The park clears the
row's ``session_id``, so before #2307 nothing ever revisited that session once
the writer exited. It stayed ACTIVE and held a client-ceiling slot for good,
the same leak #2285 fixed for the common case.

The boot pass now stamps ``TicketTask.codex_orphan_session_id`` on exactly
those parks. On every reconcile tick this sweep follows that link and re-runs
the boot pass's own decision (``codex_boot._resolve_orphan_action``) against
the session's worktree:

- **Writer still live, or scan inconclusive** — nothing changes except the
  row's ``codex_orphan_rescan_next_eligible_at``, pushed out by a fixed
  interval so ``psutil.process_iter`` is not walked on every tick. No event,
  no signal (the boot pass never kills a writer and neither does this).
- **Scan affirmatively finds no writer** — #2285's clean path: the session
  closes with its ``SESSION_COMPLETED`` audit event, then the row is requeued
  (``TICKET_REQUEUED``) if the tree is provably clean under ``reap_policy:
  auto``, otherwise it stays parked with its link cleared.
- **Linked session gone, or resumed since the park** — the link is stale, so
  only it is cleared. A resumed session is a live claude process, not the
  orphan, and must never be closed on a codex-writer scan.

A crash between the session close and the row write leaves the row linked to
an already-terminal session. The next tick re-derives the same decision and
applies it without closing the session twice.

Runs from ``_run_terminal_backstops_and_sweeps``, inside ``reconcile()``'s
``sessions_lock`` hold, so it never takes that lock itself: sessions are
loaded and saved directly under the ambient lock, as
``concierge._close_confirmed_dead_session`` does, and ``dev_queue_lock`` nests
inside it exactly as in the boot pass.

Unconditional, like the boot pass: it acts only on the evidence bar the boot
pass already applies, and it never touches a row the boot pass did not link.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from cw.config import load_clients, load_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile._shared import _LIVE_STATUSES
from cw.reconcile.codex_boot import (
    _close_session_audited,
    _OrphanDisposition,
    _resolve_orphan_action,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        Session,
        Stage,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# SESSION_COMPLETED ``reason`` for an orphaned session this sweep closes —
# the reconcile-tick sibling of codex_boot.CODEX_ORPHAN_CLOSE_REASON.
CODEX_ORPHAN_CLOSE_REASON_AT_RECONCILE = "codex_orphaned_at_reconcile"

# TICKET_REQUEUED ``reason`` for a provably-clean orphan this sweep requeues —
# the sibling of codex_boot.CODEX_ORPHAN_CLEAN_REQUEUE_REASON.
CODEX_ORPHAN_CLEAN_REQUEUE_REASON_AT_RECONCILE = (
    "codex_orphan_clean_requeue_at_reconcile"
)

# Fixed, not exponential: the writer exits once, so there is no worsening
# signal to back off from — only a bound on how often a still-parked row costs
# a process-table walk. Same order as concierge's initial false-park backoff.
_LIVE_WRITER_RESCAN_BACKOFF_SECONDS = 300

_STALE_SESSION_GONE = "it is no longer in sessions.json"
_STALE_SESSION_RESUMED = "it was resumed after the park"


@dataclass(frozen=True)
class _ReparkCandidate:
    """One linked park the detect phase re-evaluated.

    ``disposition`` is the boot pass's decision re-derived now. It is None
    exactly when the link itself is stale (``stale_reason`` says why), in which
    case the act phase only clears the link.
    """

    ticket_id: str
    client: str
    orphan_session_id: str
    stage: Stage
    disposition: _OrphanDisposition | None
    stale_reason: str | None = None


def _rescan_due(task: TicketTask, now: datetime) -> bool:
    """A live-writer park whose rescan backoff has elapsed (or was never set)."""
    eligible_at = task.codex_orphan_rescan_next_eligible_at
    return task.status is QueueItemStatus.BLOCKED_ON_USER and (
        eligible_at is None or now >= eligible_at
    )


def _stale_link_reason(session: Session | None, task: TicketTask) -> str | None:
    """Why the linked session is no longer the orphan the park left, or None.

    Resumption keeps a session's id (``cw resume`` respawns a dead DAEMON
    session in place), so a session resumed after the park is a new live
    process under the old id. ``completed_at`` is the park time:
    ``transition_task_status`` stamps it on the BLOCKED_ON_USER transition.
    """
    if session is None:
        return _STALE_SESSION_GONE
    if session.resumed_at is not None and (
        task.completed_at is None or session.resumed_at > task.completed_at
    ):
        return _STALE_SESSION_RESUMED
    return None


def _detect_live_writer_repark_candidates(
    state: CwState,
    tasks: list[TicketTask],
    *,
    now: datetime,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[_ReparkCandidate]:
    """Classify every linked park due a rescan. Makes zero writes.

    The session is resolved by exact id, never by ticket: a fresh session for
    the same ticket may have superseded the orphan, and it must not be
    mistaken for it (the identity bug #2285 round 5 fixed). The rescan runs
    even when the session is already terminal, which is how a crash between
    a prior tick's session close and its row write is recovered.
    """
    sessions_by_id = {session.id: session for session in state.sessions}
    candidates: list[_ReparkCandidate] = []
    for task in tasks:
        orphan_session_id = task.codex_orphan_session_id
        if orphan_session_id is None or not _rescan_due(task, now):
            continue
        client = clients.get(task.client)
        if client is None:
            continue
        session = sessions_by_id.get(orphan_session_id)
        stale_reason = _stale_link_reason(session, task)
        disposition = (
            _resolve_orphan_action(session.worktree_path, task, client, clients, config)
            if session is not None and stale_reason is None
            else None
        )
        candidates.append(
            _ReparkCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                orphan_session_id=orphan_session_id,
                stage=task.stage,
                disposition=disposition,
                stale_reason=stale_reason,
            )
        )
    return candidates


def _find_linked_row(
    store: DevQueueStore, candidate: _ReparkCandidate
) -> TicketTask | None:
    """The row, only if it is still the park *candidate* was derived from."""
    return next(
        (
            task
            for task in store.tasks
            if task.ticket_id == candidate.ticket_id
            and task.client == candidate.client
            and task.status is QueueItemStatus.BLOCKED_ON_USER
            and task.codex_orphan_session_id == candidate.orphan_session_id
        ),
        None,
    )


def _clear_link(task: TicketTask) -> None:
    task.codex_orphan_session_id = None
    task.codex_orphan_rescan_next_eligible_at = None


def _close_if_still_live(
    session_id: str, ticket_id: str, disposition: _OrphanDisposition
) -> None:
    """Close the orphaned session with its audit event, unless already closed.

    Caller holds ``sessions_lock`` (ambient) and ``dev_queue_lock``. An
    already-terminal session is the crash-recovery case: its audit event was
    recorded when it closed, so it gets no second one.
    """
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None or session.status not in _LIVE_STATUSES:
        return
    _close_session_audited(
        state,
        session,
        ticket_id,
        disposition,
        close_reason=CODEX_ORPHAN_CLOSE_REASON_AT_RECONCILE,
    )


def _apply_decision(
    task: TicketTask, candidate: _ReparkCandidate, *, now: datetime
) -> bool:
    """Apply *candidate*'s decision to its re-verified row; True if requeued.

    Session before row, the boot pass's crash-recoverable order: a crash in
    between leaves the row linked to a closed session, which the next tick
    finishes.
    """
    disposition = candidate.disposition
    if disposition is None:
        _log.warning(
            "codex_reparks: clearing %s/%s's link to session %s: %s",
            candidate.client,
            candidate.ticket_id,
            candidate.orphan_session_id,
            candidate.stale_reason,
        )
        _clear_link(task)
        return False
    if not disposition.close_session:
        task.codex_orphan_rescan_next_eligible_at = now + timedelta(
            seconds=_LIVE_WRITER_RESCAN_BACKOFF_SECONDS
        )
        return False
    _close_if_still_live(candidate.orphan_session_id, candidate.ticket_id, disposition)
    if disposition.should_requeue:
        # Clears the link and backoff too (transition_task_status's
        # unconditional clear). BLOCKED_ON_USER -> PENDING is not a RUNNING
        # exit, so no unproductive attempt is charged.
        transition_task_status(task, QueueItemStatus.PENDING)
        return True
    _clear_link(task)
    return False


def _record_requeued(candidate: _ReparkCandidate) -> None:
    """Same-stage payload, mirroring ``codex_boot._requeue_clean_orphan``."""
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": candidate.ticket_id,
            "client": candidate.client,
            "from_stage": candidate.stage,
            "to_stage": candidate.stage,
            "reason": CODEX_ORPHAN_CLEAN_REQUEUE_REASON_AT_RECONCILE,
            "session_id": candidate.orphan_session_id,
        },
        correlation_id=candidate.ticket_id,
    )


def _act_on_live_writer_repark_candidates(
    candidates: list[_ReparkCandidate], *, now: datetime
) -> list[str]:
    """Act phase: re-verify each row under ``dev_queue_lock``, then apply.

    A row that changed since detect (unblocked, requeued, re-parked, or linked
    to another session) is left alone, and its session is not closed on the
    stale decision. ``TICKET_REQUEUED`` is recorded only once the requeue has
    been saved. Returns the ticket ids transitioned to PENDING.
    """
    requeued: list[str] = []
    for candidate in candidates:
        with dev_queue_lock():
            store = load_dev_queue()
            task = _find_linked_row(store, candidate)
            if task is None:
                _log.info(
                    "codex_reparks: %s/%s changed since detect; leaving it",
                    candidate.client,
                    candidate.ticket_id,
                )
                continue
            did_requeue = _apply_decision(task, candidate, now=now)
            save_dev_queue(store)
        if did_requeue:
            _record_requeued(candidate)
            requeued.append(candidate.ticket_id)
    return requeued


def run_codex_live_writer_reparks(
    *, now: datetime, config: OrchestratorConfig
) -> list[str]:
    """Re-evaluate every live-writer codex-orphan park due a rescan.

    Loads fresh state, dev-queue and client snapshots itself: by the time
    reconcile reaches this point, earlier sweeps this tick have already
    mutated and saved both files (``run_concierge_recoveries``' rationale).
    Safe under the caller's ``sessions_lock``; never acquires it.

    Returns only the ticket ids actually transitioned to PENDING by this call.
    A row merely touched is never listed: a stale link cleared, a rescan that
    still finds a writer (or cannot tell) and only moves the backoff, and a
    session closed while its row stays parked all leave the id out.
    """
    state = load_state()
    tasks = load_dev_queue().tasks
    candidates = _detect_live_writer_repark_candidates(
        state, tasks, now=now, clients=load_clients(), config=config
    )
    return _act_on_live_writer_repark_candidates(candidates, now=now)
