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
that has not opted in. Invoked unconditionally as a post-pass from
``core.reconcile()``, strictly AFTER ``sessions_lock()`` releases (#2064) —
unlike its siblings called from inside ``core._run_terminal_backstops_and_sweeps``
(which run under that lock), this module's ``dispatch_fix_agent`` call reaches
``spawn_create_impl``'s own ``sessions_lock()`` acquisition, which cannot nest.
The detect/act/deferred-post-lock-dispatch *shape* is still borrowed from
``review_recipes.address_review``; only the gating and lock placement differ.

The #2064 hoist also runs this module one step later, per tick, relative to
``cw.reconcile.escalation.run_escalation_sweep`` (previously before it inside
``core._run_terminal_backstops_and_sweeps``, now after ``_reconcile_locked``
returns). Inert: the only status this module ever writes is
RUNNING->PENDING (status stays RUNNING for the whole handoff, per the
paragraph above), while escalation eligibility requires
BLOCKED_ON_USER/AWAITING_OPERATOR_SIGNOFF/FAILED. Neither RUNNING nor PENDING
is ever escalation-eligible, so this module cannot move a row into or out of
the set the sweep scans, in either order. The #2142 stale-handoff drop is the
one path that touches a non-RUNNING row at all, and it only clears
``pending_fix_dispatch`` — never the status — so the same argument holds.

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

# error_kind/paused_status for a handoff found on a row that is no longer
# RUNNING (#2142). Distinct from _ERROR_KIND_DISPATCH_FAILED: nothing was
# attempted and nothing failed — the row moved out from under an unconsumed
# record, and dispatching onto it would have spawned an uncorrelated session.
_ERROR_KIND_STALE_HANDOFF = "fix_dispatch_stale_row"

# How long a HookContextConflictError stays "transient by construction" (#2075).
# The expected conflict window is one tick or two: the REVIEW session that
# recorded the handoff is going terminal. A handoff still conflicting this long
# after ``requested_at`` means something ELSE holds the worktree (the observed
# case: a stray revert re-parked the row to PENDING and a fresh REVIEW session
# was claimed on top of the unconsumed record) — retrying silently forever is
# the failure mode #2075's silent variant reported, so past this age the
# conflict escalates through ``_stamp_dispatch_failure`` and pages instead.
_CONFLICT_ESCALATION_SECONDS = 15 * 60

# Cap on fix-agent spawns per _act_on_pending_fix_dispatches call (#2064).
# This loop bypasses dispatch/host_capacity.py and dispatch/claim.py's lane
# occupancy entirely, by design (dispatch_fix_agent's own docstring:
# "Passes NO task= kwarg ... lane occupancy are untouched") -- this cap is
# what bounds it instead, now that the #2064 hoist makes the loop execute
# for the first time (previously every call died on sessions_lock reentry).
# Candidates beyond the cap are left untouched (pending_fix_dispatch is
# never cleared) and are reconsidered on a later tick. Shape mirrors
# dispatch/pr_gate.py's _MAX_PROBES_PER_TICK. Picked conservatively small
# (vs. pr_gate's 20): each unit here is a full git fetch/merge +
# spawn_create_impl DAEMON process launch, not a cheap `gh pr list` probe.
_MAX_FIX_DISPATCHES_PER_TICK = 3


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


def _drop_stale_handoff(task: TicketTask) -> None:
    """Clear a handoff whose row is no longer RUNNING and page the operator (#2142).

    Called with ``dev_queue_lock()`` held; the caller owns the ``save_dev_queue``.
    Emits ``_stamp_dispatch_failure``'s event pair with its own error_kind, since
    the operator signal is identical in shape (nothing is running to carry a
    blocker.reason) even though nothing was actually attempted here.
    """
    pending = task.pending_fix_dispatch
    if pending is None:  # pragma: no cover - caller checked
        return
    task.pending_fix_dispatch = None
    record_event(
        OrchestratorEventType.STAGE_ERRORED,
        {
            "session_id": pending.requested_by_session_id,
            "ticket_id": task.ticket_id,
            "stage": _STAGE_FIX_LOOP,
            "started_at": datetime.now(UTC).isoformat(),
            "error_kind": _ERROR_KIND_STALE_HANDOFF,
        },
        correlation_id=task.ticket_id,
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": pending.requested_by_session_id,
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _ERROR_KIND_STALE_HANDOFF,
            "breadcrumbs": f"row status={task.status.value}",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )


def _build_dispatch_jobs(
    candidates: list[_FixDispatchCandidate],
    clients: dict[str, ClientConfig],
) -> list[_DispatchJob]:
    """Re-validate each candidate under the lock and build its deferred job.

    Mutates only to drop a stale handoff (#2142, see below); otherwise
    read-only under the lock — unlike ``address_review``'s equivalent, no latch
    is stamped here. The latch IS ``pending_fix_dispatch`` itself, and it must
    survive until the dispatch actually succeeds so a transient conflict retries
    on the next tick instead of dropping the action list on the floor.
    """
    if not candidates:
        return []
    jobs: list[_DispatchJob] = []
    dropped = False
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in candidates:
            task = _find_task(store, candidate.ticket_id, candidate.client)
            if task is None or task.pending_fix_dispatch is None:
                continue  # concurrently dispatched or removed — silent skip
            if task.status != QueueItemStatus.RUNNING:
                # #2142: some other non-sentinel RUNNING->PENDING revert
                # (crash/phantom/stall/salvage sweep) moved the row while this
                # handoff sat unconsumed. Dispatching now spawns a fix-agent
                # session with no dev-queue correlation at all —
                # dispatch_fix_agent passes no task= kwarg, so nothing
                # transitions the row and the session becomes a roster-ACTIVE
                # orphan holding a client-ceiling slot against a PENDING row.
                # Drop the handoff instead: the row is already back in the
                # normal lifecycle and claim.py's reclaim picks it up once
                # _is_fix_dispatch_held stops matching it.
                _log.warning(
                    "fix_dispatch: dropping stale handoff for ticket %s — "
                    "row status is %s, not RUNNING",
                    task.ticket_id,
                    task.status.value,
                )
                _drop_stale_handoff(task)
                dropped = True
                continue
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
        if dropped:
            save_dev_queue(store)
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
            # unproductive=False (#2075): the REVIEW round behind this handoff
            # completed and consolidated a real action list — the dispatch
            # failure is infra-side, not evidence the ticket is churning.
            # Charging it walked healthy tickets to attempt_cap_blocked at an
            # already-approved finalize. The loud STAGE_ERRORED +
            # SESSION_NEEDS_ATTENTION pair below remains the bound that pages
            # the operator on every recurrence.
            transition_task_status(task, QueueItemStatus.PENDING, unproductive=False)
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

    Two locks are in play, and only one is guaranteed here:

    - ``dev_queue_lock()`` is genuinely never nested: every ``dispatch_fix_agent``
      call below runs strictly AFTER ``_build_dispatch_jobs``'s own
      ``dev_queue_lock()`` releases.
    - ``sessions_lock()`` is NOT nested only because the call site
      (``core.reconcile()``) invokes ``run_fix_dispatch`` after its own
      ``sessions_lock()`` releases (#2064) — that guarantee lives at the call
      site, not in this function. ``address_review._dispatch_address_review``'s
      otherwise-similar claim does NOT hold for ``sessions_lock``; see that
      module's docstring.

    ``dispatch_fix_agent`` itself defers the ``cw.spawn`` import, so this module
    needs no function-local import of its own.

    Capped at ``_MAX_FIX_DISPATCHES_PER_TICK`` spawns per call (#2064): this
    loop bypasses host_capacity/lane admission by design (``dispatch_fix_agent``'s
    own docstring), so the cap is what bounds fan-out now that the sessions_lock
    fix makes the loop actually execute for the first time; overflow candidates
    are sliced off before ``_build_dispatch_jobs`` runs and are reconsidered next
    tick, latch untouched.
    """
    capped = candidates[:_MAX_FIX_DISPATCHES_PER_TICK]
    elided = len(candidates) - len(capped)
    if elided > 0:
        _log.info(
            "fix_dispatch: %d candidate(s) deferred to a later tick — "
            "per-tick spawn cap (%d) reached",
            elided,
            _MAX_FIX_DISPATCHES_PER_TICK,
        )
    acted: list[str] = []
    for job in _build_dispatch_jobs(capped, clients):
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
        except HookContextConflictError as exc:
            age_seconds = (datetime.now(UTC) - job.pending.requested_at).total_seconds()
            if age_seconds > _CONFLICT_ESCALATION_SECONDS:
                # No longer transient (#2075): the writing REVIEW session went
                # terminal long ago, so a persisting conflict means another
                # session holds the worktree and this handoff will never
                # dispatch. Silent per-tick retries emitted NO operator signal
                # while the ticket sat unconsumed — escalate through the loud
                # failure path instead (fix_dispatch_failed event pair +
                # unpark), which is the "or a fix_dispatch_failed event fires"
                # half of the contract.
                _log.warning(
                    "fix_dispatch: worktree held %ds for ticket %s — escalating",
                    int(age_seconds),
                    job.ticket_id,
                    exc_info=True,
                )
                _stamp_dispatch_failure(job, exc)
                continue
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
                # unproductive=False (#2075): this unpark is the routine
                # per-cycle handoff — a full review round ran AND its fix
                # session went terminal. Charging it (plus the respawned
                # REVIEW round's own claim) made every healthy fix cycle
                # count double against the attempt ceiling, blocking
                # fully-approved finalizes behind attempt_cap_blocked.
                transition_task_status(
                    task, QueueItemStatus.PENDING, unproductive=False
                )
            unparked.append(task.ticket_id)
        if unparked:
            save_dev_queue(store)
    return unparked


def run_fix_dispatch(*, config: OrchestratorConfig) -> list[str]:
    """Run both fix-dispatch phases for one reconcile tick.

    Runs UNCONDITIONALLY — there is no enablement gate, by design (see the
    module docstring). *config* is accepted for signature parity with its
    sibling sweeps in ``core._run_terminal_backstops_and_sweeps``, even though
    this call itself is sited in ``core.reconcile()`` post-lock (#2064).

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
