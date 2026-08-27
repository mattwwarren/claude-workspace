"""Asynchronous fix-loop dispatch handoff (GitHub #2017 R21).

The review stage's fix loop used to dispatch its fix agent synchronously, from
inside the REVIEW session's own turn. That can never work: ``cw`` provisions one
worktree per ``(client, branch)``, so the fix session's worktree IS the review
session's worktree, and ``cw.spawn._write_hook_context`` refuses any DAEMON
spawn into a worktree whose ``cw-context.json`` names a still-live session. The
refusal is correct and stays unmodified (R21.1); what changes is who issues the
spawn.

This module is that second party. The REVIEW session records a
:class:`~cw.models.PendingFixDispatch` on its queue row and exits; a later
reconcile tick — running in a process resident in no worktree at all — finds the
record and dispatches the fix agent. By then the session named in the worktree's
hook context has gone terminal by construction, so the guard is satisfied
without an exemption.

Two phases, in this order:

1. **Completions** — a row whose ``fix_dispatch_session_id`` names a now-terminal
   session is unparked back to PENDING, so ``dispatch/claim.py`` dispatches a
   fresh REVIEW session that resumes at ``s3_fix_loop, cycle_{N+1}`` via the
   existing ``Auto-Dev-Fix-Cycle`` trailer detection.
2. **Pending dispatches** — a row carrying a ``pending_fix_dispatch`` gets its
   fix agent spawned.

Deliberately NOT an RFC-0010 review recipe, and deliberately not registered in
``run_review_recipes``: that family gates on ``review_recipes_enabled``, which
defaults off. These recipes are optional PR-attention automations; the fix loop
is not optional — gating it would silently disable the fix loop for every client
that has not opted in. Sited and called like ``run_escalation_sweep`` instead:
a sibling of ``gate_recipes``/``concierge``/``escalation``, invoked
unconditionally from ``core._run_terminal_backstops_and_sweeps``. The
detect/act/deferred-post-lock-dispatch *shape* is still borrowed from
``review_recipes.address_review``; only the gating differs.

Throughout, the row's ``status`` is left at RUNNING for the whole handoff. That
is load-bearing, not incidental: ``dispatch/claim.py`` only ever claims PENDING
rows, so a RUNNING row cannot be re-dispatched as a second REVIEW session while
the fix agent is still working.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.config import load_effective_clients, load_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError, HookContextConflictError
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        PendingFixDispatch,
        TicketTask,
    )

_log = logging.getLogger(__name__)

# blocker.reason the REVIEW session exits with after recording the handoff.
# Shared with dispatch/routing, whose narrow branch suppresses the generic
# blocked-sentinel handling for it -- this is routine per-cycle flow, not an
# operator-actionable park.
FIX_LOOP_PENDING_DISPATCH = "fix_loop_pending_dispatch"

_STAGE_FIX_LOOP = "s3_fix_loop"
_ERROR_KIND_DISPATCH_FAILED = "fix_dispatch_failed"


class _FixDispatchCandidate(NamedTuple):
    """One row a phase intends to act on, identified by its (ticket, client) key.

    A NamedTuple rather than the row itself: both act phases re-load the queue
    under their own lock and re-resolve the row from this key, so a snapshot
    taken during detect can never be written back over a concurrent mutation.
    """

    ticket_id: str
    client: str


def _find_task(store: DevQueueStore, ticket_id: str, client: str) -> TicketTask | None:
    """Resolve the (ticket_id, client) row — no status filter.

    Keyed on both fields because ticket_id is a per-repo issue number, not
    globally unique across this multi-tenant system's clients.
    """
    return next(
        (t for t in store.tasks if t.ticket_id == ticket_id and t.client == client),
        None,
    )


def _detect_pending_fix_dispatches(
    tasks: list[TicketTask],
) -> list[_FixDispatchCandidate]:
    """Rows carrying a recorded, not-yet-dispatched fix-loop handoff."""
    return [
        _FixDispatchCandidate(ticket_id=t.ticket_id, client=t.client)
        for t in tasks
        if t.pending_fix_dispatch is not None
    ]


def _detect_fix_dispatch_completions(
    tasks: list[TicketTask],
) -> list[_FixDispatchCandidate]:
    """Rows whose dispatched fix session is being waited on."""
    return [
        _FixDispatchCandidate(ticket_id=t.ticket_id, client=t.client)
        for t in tasks
        if t.fix_dispatch_session_id is not None
    ]


class _DispatchJob(NamedTuple):
    """Deferred dispatch job built inside dev_queue_lock(), run after release."""

    client_cfg: ClientConfig
    branch: str
    pending: PendingFixDispatch
    ticket_id: str
    client: str
    lane: str


def _build_dispatch_jobs(
    candidates: list[_FixDispatchCandidate],
    clients: dict[str, ClientConfig],
) -> list[_DispatchJob]:
    """Re-validate each candidate under the lock and build its deferred job.

    Read-only under the lock — unlike ``address_review``'s equivalent, no latch
    is stamped here. The latch IS ``pending_fix_dispatch`` itself, and it must
    survive until the dispatch actually succeeds so a transient conflict retries
    on the next tick instead of dropping the action list on the floor.
    """
    if not candidates:
        return []
    jobs: list[_DispatchJob] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in candidates:
            task = _find_task(store, candidate.ticket_id, candidate.client)
            if task is None or task.pending_fix_dispatch is None:
                continue  # concurrently dispatched or removed — silent skip
            if task.fix_dispatch_session_id is not None:
                # A prior fix session for this ticket hasn't been unparked yet
                # (completion watcher hasn't cleared fix_dispatch_session_id).
                # Dispatching a second one here would orphan the first — the
                # two fields are meant to be mutually exclusive by convention,
                # not enforced by the model, so guard it here defensively.
                _log.warning(
                    "fix_dispatch: skipping ticket %s — fix_dispatch_session_id "
                    "%r still set, prior fix session not yet unparked",
                    task.ticket_id,
                    task.fix_dispatch_session_id,
                )
                continue
            client_cfg = clients.get(task.client)
            if client_cfg is None:
                _log.warning(
                    "fix_dispatch: client %r not resolvable for ticket %s",
                    task.client,
                    task.ticket_id,
                )
                continue
            jobs.append(
                _DispatchJob(
                    client_cfg=client_cfg,
                    branch=f"{client_cfg.feature_branch_prefix}/{task.ticket_id}",
                    pending=task.pending_fix_dispatch,
                    ticket_id=task.ticket_id,
                    client=task.client,
                    lane=task.lane,
                )
            )
    return jobs


def _stamp_dispatch_success(job: _DispatchJob, session_id: str) -> None:
    """Consume the handoff record and point the completion watcher at the spawn."""
    with dev_queue_lock():
        store = load_dev_queue()
        task = _find_task(store, job.ticket_id, job.client)
        if task is None:
            return
        task.pending_fix_dispatch = None
        task.fix_dispatch_session_id = session_id
        save_dev_queue(store)


def _stamp_dispatch_failure(job: _DispatchJob, exc: CwError) -> None:
    """Clear the handoff, unpark the row, and emit the two operator signals.

    Clearing rather than retrying is the point: a hard dispatch failure (a real
    merge conflict, an unregistered spawn) recurs identically every tick, so
    keeping the latch would re-fail forever with no session anywhere to carry
    the signal. Reverting to PENDING lets the pipeline re-dispatch a REVIEW
    session that can re-derive the action list.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        task = _find_task(store, job.ticket_id, job.client)
        if task is None:
            return
        task.pending_fix_dispatch = None
        if task.status == QueueItemStatus.RUNNING:
            transition_task_status(task, QueueItemStatus.PENDING)
        save_dev_queue(store)
        # session_id degrades to the REVIEW session that recorded the handoff:
        # this tick owns no session of its own, and that is the closest thing to
        # the $CW_SESSION the equivalent in-session emissions carry.
        record_event(
            OrchestratorEventType.STAGE_ERRORED,
            {
                "session_id": job.pending.requested_by_session_id,
                "ticket_id": job.ticket_id,
                "stage": _STAGE_FIX_LOOP,
                "started_at": datetime.now(UTC).isoformat(),
                "error_kind": _ERROR_KIND_DISPATCH_FAILED,
            },
            correlation_id=job.ticket_id,
        )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": job.pending.requested_by_session_id,
                "session_name": "",
                "client": job.client,
                "ticket_id": job.ticket_id,
                "claude_session_id": None,
                "paused_status": _ERROR_KIND_DISPATCH_FAILED,
                "breadcrumbs": str(exc),
                "crashed": False,
                "lane": job.lane,
            },
            correlation_id=job.ticket_id,
        )


def _act_on_pending_fix_dispatches(
    candidates: list[_FixDispatchCandidate],
    *,
    clients: dict[str, ClientConfig],
) -> list[str]:
    """Dispatch each pending fix agent; return the ticket_ids actually spawned.

    Every ``dispatch_fix_agent`` call runs strictly AFTER ``dev_queue_lock()``
    releases (mirrors ``address_review._dispatch_address_review``), so the spawn
    never nests the flock. ``dispatch_fix_agent`` itself defers the ``cw.spawn``
    import, so this module needs no function-local import of its own.
    """
    acted: list[str] = []
    for job in _build_dispatch_jobs(candidates, clients):
        try:
            session_id = dispatch_fix_agent(
                client=job.client_cfg,
                branch=job.branch,
                prompt=job.pending.prompt,
                label=job.pending.label,
                ticket_id=job.ticket_id,
                lane=job.lane,
                parent=job.pending.requested_by_session_id,
            )
        except HookContextConflictError:
            # Transient by construction: the REVIEW session that wrote this
            # record is still going terminal. Leave the latch alone and retry
            # next tick -- this is the ONE failure this design expects to see.
            _log.warning(
                "fix_dispatch: worktree still held for ticket %s — retrying next tick",
                job.ticket_id,
                exc_info=True,
            )
            continue
        except CwError as exc:
            _log.warning("fix_dispatch_failed ticket=%s", job.ticket_id, exc_info=True)
            _stamp_dispatch_failure(job, exc)
            continue
        _stamp_dispatch_success(job, session_id)
        acted.append(job.ticket_id)
    return acted


def _act_on_fix_dispatch_completions(
    candidates: list[_FixDispatchCandidate],
) -> list[str]:
    """Unpark rows whose fix session has gone terminal; return their ticket_ids.

    A session cw cannot resolve at all counts as finished: the fix agent is a
    first-class DAEMON session, so an unresolvable id means it is gone. Leaving
    the row RUNNING on that evidence would strand the ticket forever, since
    nothing else clears this field.
    """
    if not candidates:
        return []
    state = load_state()
    unparked: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in candidates:
            task = _find_task(store, candidate.ticket_id, candidate.client)
            if task is None or task.fix_dispatch_session_id is None:
                continue
            session = state.find_by_name_or_id(task.fix_dispatch_session_id)
            if session is not None and session.status not in TERMINAL_SESSION_STATUSES:
                continue
            task.fix_dispatch_session_id = None
            if task.status == QueueItemStatus.RUNNING:
                transition_task_status(task, QueueItemStatus.PENDING)
            unparked.append(task.ticket_id)
        if unparked:
            save_dev_queue(store)
    return unparked


def run_fix_dispatch(*, config: OrchestratorConfig) -> list[str]:
    """Run both fix-dispatch phases for one reconcile tick.

    Runs UNCONDITIONALLY — there is no enablement gate, by design (see the
    module docstring). *config* is accepted for signature parity with its
    sibling sweeps in ``core._run_terminal_backstops_and_sweeps``.

    Completions run first, and the queue is re-loaded between the phases: a row
    unparked by the completions phase must not then be seen as a pending
    dispatch by a stale snapshot from before that write.

    Returns the ticket_ids acted on across both phases.
    """
    del config
    acted = _act_on_fix_dispatch_completions(
        _detect_fix_dispatch_completions(load_dev_queue().tasks)
    )
    acted += _act_on_pending_fix_dispatches(
        _detect_pending_fix_dispatches(load_dev_queue().tasks),
        clients=load_effective_clients(),
    )
    return acted
