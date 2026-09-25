"""Candidate screening and the atomic claim transaction.

Everything that decides, under ``dev_queue_lock()``, whether a PENDING row is
claimed: the precomputed worktree-occupancy screen (#2077), spawn-error
backoff, the fix-dispatch hold, the pre-dispatch open-PR gate (#1862), the
lane-resolved attempt ceiling, and :func:`_claim_next_pending` itself.
Extracted verbatim from the historical flat ``cw.dispatch.claim`` module by
the package split (#2378).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import (
    STALE_DISPATCH_GATE_DISPOSITION,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dev_queue.lifecycle import _PRE_DISPATCH_STALE_PR_REASON
from cw.dispatch.claim.events import (
    _emit_attempt_cap_attention_event,
    _emit_attempt_cap_blocked_event,
    _emit_stale_dispatch_attention_event,
    _emit_stale_dispatch_blocked_event,
    _emit_worktree_occupied_skip_event,
)
from cw.models import QueueItemStatus, Stage
from cw.reconcile import resolve_attempt_ceiling
from cw.worktree import live_home_reason, worktree_path_for

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree import UnresolvablePathWarningKey

_log = logging.getLogger("cw.dispatch")


def _park_stale_pr_task(
    task: TicketTask, client_name: str, lane: str, store: DevQueueStore
) -> None:
    """Park one gate-hit task BLOCKED_ON_USER and emit both signals (#1862).

    ``unproductive=False``: no session was ever spawned for this claim, so
    there is no RUNNING exit to charge — and charging it would eventually
    re-park the row at ``attempt_cap_blocked``, burying the specific,
    actionable ``stale_dispatch_gate`` signal behind a generic one.

    Extracted rather than inlined because both claim loops (priority and plain)
    need the identical five-step sequence; keeping it here also holds
    ``_claim_next_pending`` inside its PLR branch/statement budget.
    """
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=STALE_DISPATCH_GATE_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_STALE_PR_REASON,
        unproductive=False,
    )
    save_dev_queue(store)
    _emit_stale_dispatch_blocked_event(client_name, task.ticket_id)
    _emit_stale_dispatch_attention_event(task, client_name, lane)


def _is_stale_pr_gated(task: TicketTask, stale_pr_ticket_ids: frozenset[str]) -> bool:
    """True iff *task* is a PLAN/IMPL-stage row the open-PR gate holds (#1862).

    Stage-scoped at the point of use, not only at resolution time: a REVIEW or
    FINALIZE row legitimately has an open PR (that is the artifact under
    review), so the gate must never hold one even if a stale set from an
    earlier stage still names its ticket id.
    """
    return (
        task.stage in (Stage.PLAN, Stage.IMPL) and task.ticket_id in stale_pr_ticket_ids
    )


def _is_fix_dispatch_held(task: TicketTask) -> bool:
    """True iff *task* is mid-fix-loop handoff and owned by fix_dispatch (#2075).

    A row carrying an unconsumed ``pending_fix_dispatch`` (or a live
    ``fix_dispatch_session_id``) belongs to ``cw.reconcile.fix_dispatch``. By
    design such a row stays RUNNING for the whole handoff, but any of the
    codebase's many non-sentinel RUNNING→PENDING reverts (crash/phantom/stall
    sweeps) can re-park it with the record untouched. Claiming it then spawns
    a fresh REVIEW session whose live worktree makes every subsequent
    ``dispatch_fix_agent`` attempt raise ``HookContextConflictError`` — the
    silent never-spawns loop #2075 reported. Skipping here leaves the row for
    the fix-dispatch pass, which (as of #2142) checks ``task.status !=
    QueueItemStatus.RUNNING`` before dispatching and drops a stale handoff
    (clearing ``pending_fix_dispatch``, paging via ``SESSION_NEEDS_ATTENTION``)
    instead of spawning an orphaned session; a healthy RUNNING row still
    dispatches normally and unparks cleanly when the fix session completes.
    """
    return (
        task.pending_fix_dispatch is not None
        or task.fix_dispatch_session_id is not None
    )


def _is_backstop_exempt(task: TicketTask) -> bool:
    """True iff a generic RUNNING->PENDING backstop revert must not touch *task*.

    Composes the two known in-flight write-ahead intents a non-sentinel
    revert (crash/phantom/stall/timeout sweep) must never clobber: the
    mid-turn usage-limit act (#2324) and the fix-loop dispatch handoff
    (#2075/#2204, via _is_fix_dispatch_held). Each owns its own resume/
    consume seam elsewhere (usage_limit_mid_turn.py, fix_dispatch.py);
    reverting the row out from under either charges an attempt neither
    should ever cost, and in the fix-dispatch case strands the handoff --
    fix_dispatch.py's _build_dispatch_jobs classifies an unconsumed
    handoff on a non-RUNNING row as a stale handoff and drops it instead
    of dispatching the fix session.
    """
    return task.usage_limit_act is not None or _is_fix_dispatch_held(task)


def resolve_occupied_ticket_ids(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    daemon: NativeDaemonClient,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> dict[str, str]:
    """Ticket ids (with occupancy reason) whose per-ticket worktree a live
    session/worker already holds (#2077).

    Resolved once per client per tick, lock-free, BEFORE any row is
    claimed -- mirrors the #1862 stale-PR-ticket-ids precompute
    (cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids): _claim_next_pending
    runs under dev_queue_lock() and must make no I/O of its own. Unlike
    that sibling this makes no gh call -- only the same local reads (cw
    session state, the daemon roster) create_worktree's reuse-refresh and
    the stale-worktree claim handler already consult via
    live_home_reason. Its fleet-wide feature-flag escape hatch is
    ``OrchestratorConfig.occupancy_gate_enabled`` (#2396), with a staged
    per-client override on ``ClientConfig``; both are resolved in the
    ``cw.dispatch.lanes`` caller exactly as ``pr_gate_enabled`` gates
    ``resolve_stale_pr_ticket_ids`` -- this function's own body and
    signature are unaffected by the toggles.

    Scans every PENDING task for *client* across all lanes and stages
    (occupancy is per-ticket, not per-lane: a ticket's worktree is the
    same path across its whole pipeline). A task with no worktree
    created yet resolves to a nonexistent path that matches no live
    home, so it is never falsely reported occupied.

    Returns the occupancy REASON per ticket (not just membership) so the
    pre-claim screen's warning log can name it, matching the shape the
    post-claim occupancy handlers already log.
    """
    occupied: dict[str, str] = {}
    for task in queue_snapshot.tasks:
        if task.client != client.name or task.status != QueueItemStatus.PENDING:
            continue
        branch = f"{client.feature_branch_prefix}/{task.ticket_id}"
        wt_path = worktree_path_for(client, branch)
        reason = live_home_reason(
            wt_path, daemon=daemon, warned_unresolvable=warned_unresolvable
        )
        if reason is not None:
            occupied[task.ticket_id] = reason
    return occupied


# _screen_and_claim outcomes. "skipped" covers every held/parked case the two
# claim loops treat identically (move to the next candidate, no flag raised).
_CLAIM_CLAIMED = "claimed"
_CLAIM_BACKOFF = "backoff"
_CLAIM_SKIPPED = "skipped"


def _screen_and_claim(
    task: TicketTask,
    *,
    client_name: str,
    lane: str,
    client: ClientConfig,
    config: OrchestratorConfig,
    stale_pr_ticket_ids: frozenset[str],
    occupied_ticket_reasons: dict[str, str],
    store: DevQueueStore,
    now: datetime,
) -> str:
    """Run one PENDING candidate through the claim gauntlet; report the outcome.

    The identical screen-then-claim sequence both claim loops (priority and
    plain) run per candidate, extracted (#2075) so the fix-dispatch hold could
    be added without pushing ``_claim_next_pending`` past its PLR0912 branch
    budget. Screens in precedence order — worktree occupancy (#2077),
    spawn-error backoff, fix-dispatch hold, stale-PR gate, attempt ceiling —
    then claims. Parking paths save the store themselves
    (``_park_stale_pr_task`` and the ceiling park below), as does the
    successful claim; a screened-out candidate writes nothing.

    The occupancy screen runs first: it is the most fundamental precondition
    (a second worker must never be spawned into a live worktree) and needs no
    store write -- the row simply stays PENDING, unclaimed and uncharged, for
    the next tick's fresh precompute to reconsider.
    """
    occupied_reason = occupied_ticket_reasons.get(task.ticket_id)
    if occupied_reason is not None:
        branch = f"{client.feature_branch_prefix}/{task.ticket_id}"
        wt_path = worktree_path_for(client, branch)
        _emit_worktree_occupied_skip_event(client_name, task.ticket_id)
        _log.warning(
            "dispatch_tick: worktree %s for %s/%s is occupied (%s); leaving"
            " it PENDING for a later tick (not claimed)",
            wt_path,
            client_name,
            task.ticket_id,
            occupied_reason,
        )
        return _CLAIM_SKIPPED
    if task.next_eligible_at is not None and now < task.next_eligible_at:
        return _CLAIM_BACKOFF
    if _is_fix_dispatch_held(task):
        return _CLAIM_SKIPPED
    if _is_stale_pr_gated(task, stale_pr_ticket_ids):
        _park_stale_pr_task(task, client_name, lane, store)
        return _CLAIM_SKIPPED
    ceiling = resolve_attempt_ceiling(client, task, config)
    if ceiling is not None and task.unproductive_attempts >= ceiling:
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition="attempt_cap_blocked",
        )
        save_dev_queue(store)
        _emit_attempt_cap_blocked_event(client_name, task.ticket_id, ceiling)
        _emit_attempt_cap_attention_event(task, client_name, lane, ceiling)
        return _CLAIM_SKIPPED
    transition_task_status(task, QueueItemStatus.RUNNING)
    task.attempts += 1
    save_dev_queue(store)
    return _CLAIM_CLAIMED


def _claim_next_pending(
    client_name: str,
    *,
    lane: str,
    client: ClientConfig,
    config: OrchestratorConfig,
    priority_ticket_ids: list[str] | None = None,
    usage_limited_until: datetime | None = None,
    stale_pr_ticket_ids: frozenset[str] = frozenset(),
    occupied_ticket_reasons: dict[str, str] | None = None,
) -> tuple[TicketTask | None, bool]:
    """Atomically claim the next PENDING task for a client in a specific lane.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* in *lane* as RUNNING, saves, and returns it.
    Returns (None, spawn_backoff_skipped) if no pending task exists or all
    eligible tasks are in spawn_error backoff.

    If *priority_ticket_ids* is provided, prefer claiming PENDING tasks in
    that order (only those whose ticket_id appears in the list).  Tasks not
    referenced by the list are skipped at this stage; they will be claimed
    by subsequent ticks once the prioritised tasks are exhausted (the
    parameter is intentionally a *preference*, not a filter — see the
    fallback after the priority loop).

    Attempt ceiling: if task.unproductive_attempts >= the ceiling resolved for
    the task's lane, the task is parked BLOCKED_ON_USER instead of claimed. A
    dispatch.tick event with skip_reason=ATTEMPT_CAP_BLOCKED is emitted per
    parked task for observability. See GitHub #786.

    The ceiling is resolved per-lane by
    :func:`cw.reconcile.resolve_attempt_ceiling` (#1751), which takes *client*
    for that lookup and falls back to ``config.global_attempt_ceiling``.
    A lane that sets ``attempt_ceiling: false`` resolves to ``None``, meaning
    no ceiling at all — a supervised lane's operator answers every park and is
    itself the rate limiter, so the automated bound buys nothing there. The
    concierge's recovery recipes call the same resolver, so neither layer can
    refuse work the other would have allowed.

    The ceiling reads ``unproductive_attempts``, NOT raw ``attempts`` (GitHub
    #1750): every claim still increments ``attempts`` below, but only claims
    that exited RUNNING without evidence of progress are charged against the
    ceiling. A ticket doing real work across many stages (#1727) therefore no
    longer parks itself, while a crashloop that produces nothing (#1653) still
    reaches the cap at exactly the same rate as before.

    *usage_limited_until*: when set and still in the future, returns
    ``(None, False)`` immediately without claiming anything (#1346
    defense-in-depth — see the gate below for why this is a parameter, not a
    fresh read).

    *stale_pr_ticket_ids* (GitHub #1862): ticket ids whose feature branch
    already carries an open, unmerged PR. A PLAN/IMPL-stage task named here is
    parked BLOCKED_ON_USER with ``disposition=stale_dispatch_gate`` instead of
    claimed, so a dispatch whose PR landed but whose queue row was never
    advanced is not silently re-implemented by a second worker. Resolved once
    per client per tick by ``cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids``
    and passed in precomputed for the same reason *usage_limited_until* is:
    this function runs under ``dev_queue_lock()`` and must make no network
    call. Defaults to the empty set, so any caller that has not resolved the
    gate simply keeps today's behaviour.

    *occupied_ticket_reasons* (GitHub #2077): ticket id -> occupancy reason for
    every ticket whose per-ticket worktree a live session or daemon worker
    already holds. A task named here is left PENDING -- never claimed, no
    attempt charged, no backoff stamped -- instead of being claimed and then
    released when ``create_worktree`` or the hook-context write discovers the
    conflict. Resolved once per client per tick by
    :func:`resolve_occupied_ticket_ids` and passed in precomputed for the same
    no-I/O-under-the-lock reason as *stale_pr_ticket_ids*. ``None`` (the
    default) screens nothing, so any caller that has not resolved it keeps
    the post-claim occupancy handling as its only guard.

    Returns a tuple (task, spawn_backoff_skipped) where spawn_backoff_skipped
    is True when at least one PENDING task was skipped due to active
    spawn_error backoff (next_eligible_at in the future). See GitHub #868.
    """
    now = datetime.now(UTC)
    # Defense-in-depth (#1346): the caller (dispatch_tick, via
    # _dispatch_client_lanes) already gates the whole tick on this same
    # value at tick.py's top-of-tick early return -- this second check
    # protects any OTHER caller of this claim primitive (this function is
    # re-exported as part of cw.dispatch's private test surface and is not
    # guaranteed to always be reached only through dispatch_tick's gate).
    # Deliberately takes the value as a parameter rather than calling
    # load_usage_limited_until() here: this function is invoked per-client
    # per-tick (an in-function read would be N reads/tick instead of one),
    # and claim.py is pure state-transition logic under dev_queue_lock with
    # no I/O -- reading the file here would break that invariant.
    if usage_limited_until is not None and now < usage_limited_until:
        return None, False
    reasons = occupied_ticket_reasons if occupied_ticket_reasons is not None else {}
    with dev_queue_lock():
        store = load_dev_queue()
        spawn_backoff_skipped = False
        if priority_ticket_ids:
            for ticket_id in priority_ticket_ids:
                for task in store.tasks:
                    if (
                        task.client == client_name
                        and task.ticket_id == ticket_id
                        and task.lane == lane
                        and task.status == QueueItemStatus.PENDING
                    ):
                        outcome = _screen_and_claim(
                            task,
                            client_name=client_name,
                            lane=lane,
                            client=client,
                            config=config,
                            stale_pr_ticket_ids=stale_pr_ticket_ids,
                            occupied_ticket_reasons=reasons,
                            store=store,
                            now=now,
                        )
                        if outcome == _CLAIM_CLAIMED:
                            return task, spawn_backoff_skipped
                        if outcome == _CLAIM_BACKOFF:
                            spawn_backoff_skipped = True
                        break
        pending = sorted(
            [
                t
                for t in store.tasks
                if t.client == client_name
                and t.lane == lane
                and t.status == QueueItemStatus.PENDING
            ],
            key=lambda t: (-t.priority, t.created_at),
        )
        for task in pending:
            outcome = _screen_and_claim(
                task,
                client_name=client_name,
                lane=lane,
                client=client,
                config=config,
                stale_pr_ticket_ids=stale_pr_ticket_ids,
                occupied_ticket_reasons=reasons,
                store=store,
                now=now,
            )
            if outcome == _CLAIM_CLAIMED:
                return task, spawn_backoff_skipped
            if outcome == _CLAIM_BACKOFF:
                spawn_backoff_skipped = True
    return None, spawn_backoff_skipped
