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
   fix agent spawned, unless the row has drifted off RUNNING since the handoff
   was recorded, in which case the handoff is dropped instead (#2142; see
   ``_build_dispatch_jobs`` and ``_drop_stale_handoffs``). A row can also drift
   off RUNNING — or stay RUNNING but move to a *different* stage — *after* its
   job was already built, during the stale-handoff drop's own unlocked
   emission window — ``_revalidate_dispatch_jobs`` re-checks every built job's
   status AND stage immediately before dispatch to close that window (#2142
   rounds 5-6).

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
returns). The ordinary path is inert: it writes only RUNNING->PENDING (status
stays RUNNING for the whole handoff, per the paragraph above), while escalation
eligibility requires BLOCKED_ON_USER/AWAITING_OPERATOR_SIGNOFF/FAILED, and
neither RUNNING nor PENDING is ever escalation-eligible. The #2142
stale-handoff drop is the one other path that touches a non-RUNNING row, and it
only clears ``pending_fix_dispatch`` — never the status — so the same argument
holds there.

The ONE exception is ``_park_for_unresolved_ref`` (#2209), which does move a row
RUNNING->BLOCKED_ON_USER with a disposition that IS in
``escalation._ELIGIBLE_DISPOSITIONS``. Because this module now runs after the
sweep, a row parked on one tick is first observed by ``run_escalation_sweep`` on
the NEXT tick. That one-tick delay is harmless: the sweep only stamps
``escalation_parked_at`` on first sight and pages nothing until
``ESCALATION_PARK_MINUTES`` (45) have elapsed, so no operator can observe the
difference.

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
from cw.exceptions import CwError, HookContextConflictError, RemoteRefUnresolvedError
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile._shared import (
    _FIX_DISPATCH_REF_UNRESOLVED_REASON,
    ticket_id_for_session,
)
from cw.reconcile.review_recipes.fix_agent import dispatch_fix_agent

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        PendingFixDispatch,
        Stage,
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
    """Rows carrying a recorded, not-yet-dispatched fix-loop handoff.

    A row parked by ``_park_for_unresolved_ref`` still carries its handoff on
    purpose (#2209), so it must be filtered out HERE rather than later: detect
    runs before ``_act_on_pending_fix_dispatches``'s
    ``_MAX_FIX_DISPATCHES_PER_TICK`` slice, and a parked row left in the
    candidate list would consume a cap slot on every tick forever, starving
    healthy rows behind it.
    """
    return [
        _FixDispatchCandidate(ticket_id=t.ticket_id, client=t.client)
        for t in tasks
        if t.pending_fix_dispatch is not None and not _is_parked_for_unresolved_ref(t)
    ]


def _is_parked_for_unresolved_ref(task: TicketTask) -> bool:
    """True for a row this module parked on an unresolvable remote ref (#2209)."""
    return (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == _FIX_DISPATCH_REF_UNRESOLVED_REASON
    )


def _reported_branch(state: CwState, client: str, ticket_id: str) -> str | None:
    """The branch the ticket's newest auto-dev sentinel reported having pushed.

    The seam that lets ``dispatch_fix_agent`` find a branch pushed under a
    slugified name (#2209) without a persisted-schema change: IMPL, REVIEW and
    FINALIZE sentinels all carry the same pushed branch in
    ``AutoDevResult.branch``, and ``session_retention.prune_sessions`` exempts
    any terminal session whose ``(client, ticket_id)`` still has a dev-queue
    row — so the value is in hot state for as long as a fix-dispatch row exists.

    Sessions are scanned newest-first on ``started_at``, the same tie-break key
    ``concierge._find_session_for_ticket`` sorts on. A session whose sentinel
    carries no branch, a blank one, or a non-``str`` value is skipped rather
    than ending the scan: the newest session for a ticket is often the REVIEW
    session, whose payload may predate any push. ``Session.stage`` cannot narrow
    this — it is None for every claude-native session.
    """
    matches = [
        session
        for session in state.sessions
        if session.client == client and ticket_id_for_session(session.name) == ticket_id
    ]
    for session in sorted(matches, key=lambda s: s.started_at, reverse=True):
        result = session.last_result
        if result is None:
            continue
        branch = result.get("branch")
        if isinstance(branch, str) and branch.strip():
            return branch
    return None


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
    # Row's stage at build time (#2142 round 6). Captured here so
    # ``_revalidate_dispatch_jobs`` can require the row to still be at the
    # SAME stage immediately before dispatch, not just still RUNNING — see
    # ``_job_still_valid``.
    stage: Stage
    # Branch the impl sentinel reported having pushed (#2209), which need not
    # be the templated ``branch`` above. Defaulted so the existing constructors
    # — and any row whose sessions carry no sentinel branch — stay valid.
    remote_branch: str | None = None


def _emit_fix_dispatch_operator_signal(
    *,
    session_id: str,
    ticket_id: str,
    client: str,
    lane: str,
    error_kind: str,
    breadcrumbs: str,
) -> None:
    """Emit the STAGE_ERRORED + SESSION_NEEDS_ATTENTION event pair for a
    fix-dispatch problem — shared by the stale-handoff drop in
    ``_build_dispatch_jobs`` and
    ``_stamp_dispatch_failure``, which differ only in *error_kind*,
    *breadcrumbs*, and whether the row also gets unparked.
    """
    record_event(
        OrchestratorEventType.STAGE_ERRORED,
        {
            "session_id": session_id,
            "ticket_id": ticket_id,
            "stage": _STAGE_FIX_LOOP,
            "started_at": datetime.now(UTC).isoformat(),
            "error_kind": error_kind,
        },
        correlation_id=ticket_id,
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session_id,
            "session_name": "",
            "client": client,
            "ticket_id": ticket_id,
            "claude_session_id": None,
            "paused_status": error_kind,
            "breadcrumbs": breadcrumbs,
            "crashed": False,
            "lane": lane,
        },
        correlation_id=ticket_id,
    )


class _StaleHandoffSnapshot(NamedTuple):
    """Phase-1 identification of a handoff seen off RUNNING (#2142 round 3).

    Taken read-only under ``dev_queue_lock()`` inside ``_build_dispatch_jobs``
    — no clearing, no event I/O happens at that point. ``_drop_stale_handoffs``
    consumes this snapshot after the lock has released: it emits the operator
    signal unlocked, then re-acquires the lock once to re-validate that the row
    still carries this *exact* handoff (identity: requested_by_session_id +
    requested_at + cycle, the fields a fresh ``PendingFixDispatch`` cannot
    collide on by construction) before clearing it. A row that changed underneath
    — reclaimed back to RUNNING, or re-armed with a new handoff — is left alone.
    """

    session_id: str
    ticket_id: str
    client: str
    lane: str
    requested_at: datetime
    cycle: int
    breadcrumbs: str


def _stale_handoff_snapshot(task: TicketTask) -> _StaleHandoffSnapshot | None:
    """Describe a handoff whose row is no longer RUNNING (#2142).

    Pure: reads the row and returns the phase-1 snapshot. No mutation, no
    event I/O — see ``_StaleHandoffSnapshot``.
    """
    pending = task.pending_fix_dispatch
    if pending is None:  # pragma: no cover - caller checked
        return None
    return _StaleHandoffSnapshot(
        session_id=pending.requested_by_session_id,
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        requested_at=pending.requested_at,
        cycle=pending.cycle,
        breadcrumbs=f"row status={task.status.value} dropped_label={pending.label!r}",
    )


def _drop_stale_handoffs(snapshots: list[_StaleHandoffSnapshot]) -> None:
    """Page the operator for each stale handoff, then clear what's still stale.

    (#2142 round 3.)

    Binding two-phase contract (operator, round 3): phase 1 (inside
    ``_build_dispatch_jobs``, under the lock) only *identifies* a stale
    handoff. This function is phase 2+3, run strictly after that lock has
    released:

    - **Emit** (unlocked, per candidate): the STAGE_ERRORED + SESSION_NEEDS_ATTENTION
      pair is the only audit trail a dropped handoff ever gets. If emitting it
      raises for a candidate, that candidate is logged and dropped from this
      tick — not cleared, not re-raised. The handoff survives on disk and the
      next reconcile tick re-detects and re-pages it. One candidate's I/O
      failure must not block every other candidate's independent drop or
      dispatch in the same tick, which is exactly what letting the exception
      propagate out of a single shared lock (round 1's design) would do.
    - **Re-validate and clear** (a single fresh ``dev_queue_lock()`` acquisition):
      for each candidate whose event pair was actually persisted, re-resolve
      the row and clear ``pending_fix_dispatch`` only if the row is still
      non-RUNNING AND still carries the identical handoff the snapshot
      described. If the state moved while the lock was released for
      emission — the row was reclaimed back to RUNNING, or a fresh REVIEW
      round re-armed a new handoff — the clear is skipped, silently and
      without error: the emitted event already describes a real condition
      that held a moment ago, and is not clawed back after the fact.
    """
    if not snapshots:
        return
    confirmed: list[_StaleHandoffSnapshot] = []
    for snap in snapshots:
        try:
            _emit_fix_dispatch_operator_signal(
                session_id=snap.session_id,
                ticket_id=snap.ticket_id,
                client=snap.client,
                lane=snap.lane,
                error_kind=_ERROR_KIND_STALE_HANDOFF,
                breadcrumbs=snap.breadcrumbs,
            )
        except OSError:
            _log.warning(
                "fix_dispatch: stale-handoff audit event failed for ticket %s "
                "— handoff left in place, will retry next tick",
                snap.ticket_id,
                exc_info=True,
            )
            continue
        confirmed.append(snap)
    if not confirmed:
        return
    with dev_queue_lock():
        store = load_dev_queue()
        dirty = False
        for snap in confirmed:
            task = _find_task(store, snap.ticket_id, snap.client)
            if task is None or task.pending_fix_dispatch is None:
                continue  # already cleared or removed concurrently
            pending = task.pending_fix_dispatch
            if (
                task.status == QueueItemStatus.RUNNING
                or pending.requested_by_session_id != snap.session_id
                or pending.requested_at != snap.requested_at
                or pending.cycle != snap.cycle
            ):
                # Row reclaimed, or re-armed with a different handoff, while
                # the lock was released for emission — not ours to clear.
                continue
            task.pending_fix_dispatch = None
            dirty = True
        if dirty:
            save_dev_queue(store)


def _job_still_valid(
    task: TicketTask | None, expected_stage: Stage | None = None
) -> bool:
    """True if a row is still eligible to receive a fix-agent dispatch.

    The single definition of "eligible" — RUNNING, at ``expected_stage`` (if
    given), with no ``fix_dispatch_session_id`` already outstanding — shared by
    ``_build_dispatch_jobs`` (first check, under its own lock) and
    ``_revalidate_dispatch_jobs`` (second check, immediately before dispatch,
    #2142 rounds 5-6). Deliberately does NOT check ``pending_fix_dispatch``: the
    two call sites disagree on what a missing handoff means (build treats it
    as "already handled, skip"; revalidate treats a job it already built as
    carrying its own copy of the handoff, so the row losing it mid-tick is
    just another form of "no longer eligible").

    ``expected_stage`` defaults to ``None`` for ``_build_dispatch_jobs``'s own
    call: at that point there is no previously-captured stage to compare
    against yet — the build pass is what *captures* ``task.stage`` onto the
    job record for ``_revalidate_dispatch_jobs`` to compare against later
    (#2142 round 6). A row that left and re-entered RUNNING at a different
    stage during the unlocked stale-handoff window (reverted, then re-claimed
    for a later stage) would otherwise still pass the RUNNING-only check and
    spawn a fix agent built for its old stage.
    """
    return (
        task is not None
        and task.status == QueueItemStatus.RUNNING
        and task.fix_dispatch_session_id is None
        and (expected_stage is None or task.stage == expected_stage)
    )


def _revalidate_dispatch_jobs(jobs: list[_DispatchJob]) -> list[_DispatchJob]:
    """Re-check each already-built job's row immediately before dispatch.

    (#2142 round 5.)

    ``_build_dispatch_jobs`` snapshots ``jobs`` under one ``dev_queue_lock()``
    acquisition, then releases it. ``_drop_stale_handoffs`` — run on the
    *other* return value, ``stale`` — then spends real wall-clock time
    unlocked emitting audit events, during which a *different* row's
    non-sentinel RUNNING->PENDING revert (the same class ``_build_dispatch_jobs``'s
    own inline comment names) can land on a row this tick already built a job
    for. A row can also leave and re-enter RUNNING at a *different* stage
    during that same window (reverted, then re-claimed for a later stage,
    #2142 round 6) — RUNNING alone would not catch that. Re-running
    ``_job_still_valid`` here against the job's captured ``stage``, under one
    fresh lock acquisition, closes both windows: a row that no longer
    qualifies is dropped from this tick's dispatch rather than spawning an
    uncorrelated (or stage-stale) fix agent for it. Dropping is silent beyond
    a debug log — the row's own ``pending_fix_dispatch`` is left untouched, so
    the next reconcile tick's ``_build_dispatch_jobs`` re-detects it under
    whatever status/stage it now holds and routes it through the ordinary
    build-or-stale path from there.
    """
    if not jobs:
        return jobs
    with dev_queue_lock():
        store = load_dev_queue()
        survivors = []
        for job in jobs:
            task = _find_task(store, job.ticket_id, job.client)
            if _job_still_valid(task, expected_stage=job.stage):
                survivors.append(job)
            else:
                _log.debug(
                    "fix_dispatch: dropping ticket %s from this tick's dispatch "
                    "— row no longer eligible after the unlocked stale-handoff "
                    "phase (status=%s, stage=%s, expected_stage=%s)",
                    job.ticket_id,
                    task.status.value if task is not None else "row missing",
                    task.stage.value if task is not None else "row missing",
                    job.stage.value,
                )
        return survivors


def _build_dispatch_jobs(
    candidates: list[_FixDispatchCandidate],
    clients: dict[str, ClientConfig],
) -> tuple[list[_DispatchJob], list[_StaleHandoffSnapshot]]:
    """Re-validate each candidate under the lock; build jobs, snapshot stale rows.

    Read-only under the lock — unlike ``address_review``'s equivalent, no latch
    is stamped here. The latch IS ``pending_fix_dispatch`` itself, and it must
    survive until the dispatch actually succeeds so a transient conflict retries
    on the next tick instead of dropping the action list on the floor.

    A row that has drifted off RUNNING is only *snapshotted* here (#2142 round 3)
    — no clearing, no event I/O under this lock. The caller runs
    ``_drop_stale_handoffs`` on the returned snapshots strictly after this lock
    has released, then ``_revalidate_dispatch_jobs`` on the returned jobs
    (#2142 round 5) — this function itself does not re-check ``jobs`` again.
    """
    if not candidates:
        return [], []
    # Read once, before the lock: _reported_branch below needs session state,
    # and loading it per-candidate under the dev-queue lock would hold that
    # lock across unrelated I/O. Mirrors _act_on_fix_dispatch_completions.
    state = load_state()
    jobs: list[_DispatchJob] = []
    stale: list[_StaleHandoffSnapshot] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in candidates:
            task = _find_task(store, candidate.ticket_id, candidate.client)
            if task is None or task.pending_fix_dispatch is None:
                continue  # concurrently dispatched or removed — silent skip
            if _is_parked_for_unresolved_ref(task):
                # Re-checked under the lock, closing the detect-to-build race
                # (#2209). Silent, and the handoff is retained on purpose: the
                # operator's ``cw dev-queue requeue`` resumes this same fix
                # cycle rather than restarting REVIEW from scratch.
                continue
            if not _job_still_valid(task):
                if task.status != QueueItemStatus.RUNNING:
                    # #2142: some other non-sentinel RUNNING->PENDING revert
                    # (crash/phantom/stall/salvage sweep) moved the row while
                    # this handoff sat unconsumed. Dispatching now spawns a
                    # fix-agent session with no dev-queue correlation at all —
                    # dispatch_fix_agent passes no task= kwarg, so nothing
                    # transitions the row and the session becomes a
                    # roster-ACTIVE orphan holding a client-ceiling slot
                    # against a PENDING row. Drop the handoff instead: the row
                    # is already back in the normal lifecycle and claim.py's
                    # reclaim picks it up once _is_fix_dispatch_held stops
                    # matching it.
                    _log.warning(
                        "fix_dispatch: identified stale handoff for ticket %s — "
                        "row status is %s, not RUNNING",
                        task.ticket_id,
                        task.status.value,
                    )
                    snap = _stale_handoff_snapshot(task)
                    if snap is not None:
                        stale.append(snap)
                else:
                    # A prior fix session for this ticket hasn't been unparked
                    # yet (completion watcher hasn't cleared
                    # fix_dispatch_session_id). Dispatching a second one here
                    # would orphan the first — the two fields are meant to be
                    # mutually exclusive by convention, not enforced by the
                    # model, so guard it here defensively.
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
                    stage=task.stage,
                    remote_branch=_reported_branch(state, task.client, task.ticket_id),
                )
            )
    return jobs, stale


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
        _emit_fix_dispatch_operator_signal(
            session_id=job.pending.requested_by_session_id,
            ticket_id=job.ticket_id,
            client=job.client,
            lane=job.lane,
            error_kind=_ERROR_KIND_DISPATCH_FAILED,
            breadcrumbs=str(exc),
        )


def _park_for_unresolved_ref(job: _DispatchJob, exc: RemoteRefUnresolvedError) -> None:
    """Park the row BLOCKED_ON_USER, keeping its action list (#2209).

    The deliberate opposite of ``_stamp_dispatch_failure``'s clear-and-revert.
    That path assumes a fresh REVIEW session can re-derive the action list, but
    an unresolvable remote ref recurs identically for every REVIEW round —
    and since #2075 the revert charges no attempt, so nothing bounded the
    review/failed-dispatch/review loop at all. Parking ends it, and retaining
    ``pending_fix_dispatch`` means the operator's ``cw dev-queue requeue``
    resumes this same fix cycle rather than paying for a whole new review.

    ``_park_running_task_blocked_on_user`` is imported function-locally for the
    same import-cycle reason ``cw.reconcile.codex_boot`` does it: ``claim.py``
    imports ``cw.executor``, which imports ``cw.reconcile``. It matches only a
    still-RUNNING row under its own lock (so a row already reverted to PENDING
    is left to the ordinary stale-handoff drop), clears ``session_id`` after
    reading it for the attention event, and never touches
    ``pending_fix_dispatch``.

    ``unproductive=False`` for the same reason ``_stamp_dispatch_failure``
    passes it (#2075): the REVIEW round behind this handoff produced a real
    action list, and the dispatch failure is infra-side.
    """
    from cw.dispatch.claim import _park_running_task_blocked_on_user

    _park_running_task_blocked_on_user(
        ticket_id=job.ticket_id,
        client_name=job.client,
        disposition=_FIX_DISPATCH_REF_UNRESOLVED_REASON,
        breadcrumbs=str(exc),
        unproductive=False,
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
      ``dev_queue_lock()`` releases, AND after ``_revalidate_dispatch_jobs``'s
      own separate, later acquisition of it also releases (#2142 round 5) —
      three non-overlapping acquisitions of the same lock, never nested.
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
    jobs, stale = _build_dispatch_jobs(capped, clients)
    _drop_stale_handoffs(stale)
    jobs = _revalidate_dispatch_jobs(jobs)
    for job in jobs:
        try:
            session_id = dispatch_fix_agent(
                client=job.client_cfg,
                branch=job.branch,
                prompt=job.pending.prompt,
                label=job.pending.label,
                ticket_id=job.ticket_id,
                lane=job.lane,
                parent=job.pending.requested_by_session_id,
                remote_branch=job.remote_branch,
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
        except RemoteRefUnresolvedError as exc:
            # Must precede the broad CwError clause below — it is a subclass.
            _log.warning(
                "fix_dispatch_ref_unresolved ticket=%s", job.ticket_id, exc_info=True
            )
            _park_for_unresolved_ref(job, exc)
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
