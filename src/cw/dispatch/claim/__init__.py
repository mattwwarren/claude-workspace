"""Task claim + spawn primitives for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the atomic claim step, the
per-lane occupant/stat snapshots, and the worktree-provision + spawn path."""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.dev_queue import (
    STALE_DISPATCH_GATE_DISPOSITION,
    _impl_bypass_plan_available,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dev_queue.lifecycle import (
    _PRE_DISPATCH_STALE_PR_REASON,
    _advance_stage,
)
from cw.dispatch.claim.claimed_row import (
    _SPAWN_ERROR_BACKOFF_CAP_SECONDS,
    _SPAWN_ERROR_BACKOFF_INITIAL_SECONDS,
    _find_running_row,
    _park_running_task_blocked_on_user,
    _revert_claimed_task_to_pending,
    _stamp_spawn_success,
)
from cw.dispatch.claim.events import (
    _emit_attempt_cap_attention_event,
    _emit_attempt_cap_blocked_event,
    _emit_stale_dispatch_attention_event,
    _emit_stale_dispatch_blocked_event,
    _emit_worktree_occupied_skip_event,
)
from cw.dispatch.claim.outcome import (
    _SpawnOutcome,
)
from cw.events import record_event
from cw.exceptions import (
    HookContextConflictError,
    StaleWorktreeError,
    UsageLimitError,
    WorktreeError,
    WorktreeOccupiedError,
)
from cw.executor import (
    CodexCapabilityDiagnosis,
    codex_capability_diagnosis,
    resolve_executor,
    resolve_executor_config,
    resolve_pipeline_stages,
)
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    occupies_lane_slot,
)
from cw.reconcile import resolve_attempt_ceiling
from cw.worktree import (
    check_not_main_checkout,
    create_worktree,
    live_home_reason,
    remove_worktree,
    unsaved_work_reason,
    worktree_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree import UnresolvablePathWarningKey

_log = logging.getLogger("cw.dispatch")


# How long a claim that found its reused worktree occupied by a live session or
# daemon worker (``WorktreeOccupiedError``, #2213) is held off before the row is
# eligible again. Short: the occupant is typically the prior stage's worker
# still leaving the roster, and pickup after it goes should be prompt. Long
# enough that the SAME tick does not re-claim the row it just released and burn
# every remaining slot on one head-of-line ticket, starving the free tickets
# queued behind it. Fixed rather than exponential because nothing is failing:
# this is a wait, not a retry of a broken operation.
_OCCUPIED_DEFER_SECONDS: int = 30


# TTL (seconds) for the in-process codex-capability probe cache (#1238). Codex
# CLI presence/version essentially never changes between dispatch ticks, so a
# short process-lifetime cache avoids re-shelling `codex --version` on every
# codex-backed spawn attempt. Unlike gating.py's _AVAILABILITY_PROBE_TTL_SECONDS
# this has no fleet-wide sidecar persistence or latch semantics -- it's a
# per-task gate, not a fleet-wide outage signal, so a plain in-memory cache is
# sufficient.
_CODEX_CAPABILITY_PROBE_TTL_SECONDS = 60

# Timeout for the codex-capability probe's own `codex --version` subprocess
# call when invoked from this hot path (#1238). `_spawn_claimed_task` runs
# synchronously inside dispatch_tick's per-client, per-lane loop, so a stuck
# `codex` binary would otherwise stall that entire tick for up to
# executor.py's one-shot-appropriate 10s default; use a much smaller budget
# here since `codex --version` is a trivial local command expected to return
# near-instantly on a healthy install.
_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS = 3

# Consecutive codex-capability parks (across any client/task sharing this
# process) tolerated before the gate also raises the generic `spawn_error`
# signal, as a bounded backstop (#1238). Below this count, a park stays
# decoupled from the per-lane circuit breaker (see _codex_capability_gate's
# docstring — an isolated park must not durably pause an unrelated lane).
# At/above it, the condition has stopped looking like an isolated blip and
# started looking systemic (e.g. a wrong probe verdict, per the TTL-cached
# result being shared across every codex-backed task in the process), so the
# existing circuit-breaker/operator-visible-pause machinery is allowed to
# engage rather than letting every codex-backed task in the queue drain into
# BLOCKED_ON_USER with no self-limiting mechanism at all.
_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD = 3

# In-process cache for the codex-capability probe (#1238). A single-element
# list is used as a mutable slot -- updates mutate its contents in place
# rather than rebinding the module-level name, so no `global` statement (and
# no PLW0603 suppression) is needed. Populated lazily by
# _cached_codex_capability_diagnosis; reset via _reset_codex_capability_cache
# (test support only -- production code never needs to invalidate early since
# codex CLI presence/version doesn't change mid-process).
_codex_capability_cache: list[tuple[CodexCapabilityDiagnosis, datetime]] = []

# Consecutive-park counter backing _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD
# (#1238). Same mutable-single-element-list-as-slot idiom as the cache above.
# Incremented on every park; reset to 0 as soon as the probe reports capable
# again (see _codex_capability_gate), so this genuinely tracks a *consecutive*
# streak of parks, not a lifetime total -- a long-lived dispatch-loop process
# that recovers between incidents must not have old, unrelated parks silently
# combine with a later isolated one to trip the circuit breaker.
_codex_capability_park_count: list[int] = [0]


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


def _lane_occupants_for_client(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, list[dict[str, str]]]:
    """Per-lane occupant ``{ticket_id, status}`` list for dispatch.tick payloads.

    Sibling of :func:`_lane_stats_for_client` -- same :func:`occupies_lane_slot`
    join over ``client``/``lane``, but returns identifying detail instead
    of counts, so a ``lane_cap_blocked`` reader can name the occupant
    instead of inferring a (possibly phantom) cross-client cap. See #1243.

    Deliberately a NEW top-level dispatch.tick payload key, never nested
    inside ``lanes`` -- orchestrate.py's ``_extract_lanes`` hard-filters
    ``lanes`` values to numerics, so a nested ticket-id string would be
    silently stripped downstream.
    """
    occupants: dict[str, list[dict[str, str]]] = {}
    for lane_cfg in client.effective_lanes:
        occupants[lane_cfg.name] = [
            {"ticket_id": t.ticket_id, "status": t.status.value}
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and occupies_lane_slot(t)
        ]
    return occupants


def _lane_stats_for_client(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    occupants: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, dict[str, int]]:
    """Per-lane ``{claimed, running, blocked, signoff, pending}`` counts for
    event payloads.

    Why task-based running: RUNNING/BLOCKED_ON_USER tasks carry ``lane``;
    sessions carry ``lane`` as of #594, but occupancy counting stays task-join
    based per ADR-0006 / Phase 4a scope (stamped-but-not-read by the
    scheduler). BLOCKED_ON_USER occupies its lane slot per ADR-0006, so
    ``running + blocked + signoff`` is the total occupied count. ``blocked``
    and ``signoff`` are split out so operators can see at a glance why
    claimed=0 when pending>0 (#588, #990). Derives running/blocked/signoff
    from :func:`_lane_occupants_for_client` -- see #1243.

    *occupants* lets a caller that already computed the occupant lookup (e.g.
    to also emit ``lane_occupants``/``occupied`` on the same dispatch.tick
    payload) pass it in and avoid a second full scan of ``queue_snapshot.tasks``.
    """
    if occupants is None:
        occupants = _lane_occupants_for_client(client, queue_snapshot)
    stats: dict[str, dict[str, int]] = {}
    for lane_cfg in client.effective_lanes:
        lane_occupants = occupants.get(lane_cfg.name, [])
        running = sum(
            1 for o in lane_occupants if o["status"] == QueueItemStatus.RUNNING.value
        )
        blocked = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.BLOCKED_ON_USER.value
        )
        signoff = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF.value
        )
        pending = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        stats[lane_cfg.name] = {
            "claimed": 0,
            "running": running,
            "blocked": blocked,
            "signoff": signoff,
            "pending": pending,
        }
    return stats


def _cached_codex_capability_diagnosis() -> CodexCapabilityDiagnosis:
    """TTL-cached wrapper over :func:`codex_capability_diagnosis` (#1238).

    Mirrors ``gating._resolve_availability``'s cache-and-reuse shape at a
    smaller scope: within ``_CODEX_CAPABILITY_PROBE_TTL_SECONDS`` of the last
    probe, reuse the cached verdict instead of re-shelling ``codex --version``
    on every codex-backed spawn attempt. Process-lifetime only (no sidecar
    persistence) -- unlike the fleet-wide gh-availability latch, this gate has
    no cross-process coordination requirement.
    """
    now = datetime.now(UTC)
    if _codex_capability_cache:
        probe, checked_at = _codex_capability_cache[0]
        if (now - checked_at).total_seconds() < _CODEX_CAPABILITY_PROBE_TTL_SECONDS:
            return probe
    probe = codex_capability_diagnosis(
        timeout_seconds=_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS
    )
    _codex_capability_cache[:] = [(probe, now)]
    return probe


def _reset_codex_capability_cache() -> None:
    """Clear the in-process codex-capability cache/park counter. Test support only.

    (#1238)
    """
    _codex_capability_cache.clear()
    _codex_capability_park_count[0] = 0


def _codex_capability_gate(
    task: TicketTask, client: ClientConfig
) -> _SpawnOutcome | None:
    """Pre-spawn codex capability gate (#1238).

    Returns a parked ``_SpawnOutcome`` (``capability_parked=True``, the
    RUNNING task moved to BLOCKED_ON_USER, session_id cleared, ``disposition``
    set to the probe diagnosis) when the task's stage is codex-backed and the
    ``codex`` CLI is not usable; returns ``None`` to proceed (non-codex
    backend, or codex capable). Reads only cheap, TTL-cached facts — binary
    presence + ``codex --version`` — via the shared
    :func:`_cached_codex_capability_diagnosis` probe; never a live review.

    Below :data:`_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD` consecutive parks,
    does NOT set ``spawn_error`` on the returned outcome: a codex-incapable
    host is a deterministic condition that recurs every tick, not the
    sporadic transient failure the generic spawn-error path (and its per-lane
    circuit breaker) is designed for. Signaling it as a generic spawn_error
    on every occurrence would trip the lane's circuit breaker on the very
    first park (durably pausing the lane, requiring a manual ``cw lane
    resume``) and would abort the rest of that tick's lane/client loop for
    unrelated, non-codex-backed tasks sharing the same lane or client.

    At/above the threshold, the outcome ALSO sets ``spawn_error=True`` — a
    bounded backstop against a systemically wrong probe verdict (the TTL
    cache's result is shared across every codex-backed task in the process,
    so a single bad verdict could otherwise park an unbounded number of tasks
    with no self-limiting mechanism at all).
    """
    if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
        return None
    probe = _cached_codex_capability_diagnosis()
    if probe.diagnosis is None:
        # Capable again -- clear the streak so a fully-recovered condition
        # doesn't leave stale park credit sitting on the counter (#1238
        # review: without this, the counter is a lifetime total, not a
        # consecutive-parks count, and a long-lived dispatch-loop process
        # would eventually treat every future isolated park as
        # breaker-worthy once 3 total parks had *ever* occurred).
        _codex_capability_park_count[0] = 0
        return None
    _log.warning(
        "dispatch: codex capability gate parked %s/%s — %s",
        client.name,
        task.ticket_id,
        probe.detail,
    )
    _park_running_task_blocked_on_user(
        ticket_id=task.ticket_id,
        client_name=client.name,
        disposition=probe.diagnosis,
        breadcrumbs=probe.detail,
        unproductive=False,
        created_at=task.created_at,
    )
    _codex_capability_park_count[0] += 1
    breaker_engaged = (
        _codex_capability_park_count[0] >= _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD
    )
    return _SpawnOutcome(
        capability_parked=True,
        spawn_error=breaker_engaged,
        error=probe.diagnosis,
    )


def _apply_plan_bypass_if_available(
    task: TicketTask, client: ClientConfig, worktree_path: Path
) -> None:
    """Advance a PLAN-stage task straight to IMPL if a plan is already there.

    GitHub #1286: closes the gap where every automatic re-entry at
    ``Stage.PLAN`` re-ran a full Stage 1 planning pass from scratch even when
    a valid, signed-off ``.cw/plan.md`` was already sitting in the ticket's
    reused worktree. ``allow_tracker_fallback=False`` keeps this hot
    per-claim-path check network-free -- Stage 1 is about to run and post the
    plan anyway if the local check misses.

    Extracted from :func:`_spawn_claimed_task` to keep that function inside
    the PLR branch/statement budget, mirroring :func:`_codex_capability_gate`
    and :func:`_stamp_spawn_success`'s extractions for the same reason. Sole
    caller; ``task.stage`` must already be ``Stage.PLAN`` on entry.

    Mutates the STORED row under :func:`dev_queue_lock`, mirroring
    :func:`_stamp_spawn_success`'s load->find->mutate->save shape (with one
    addition: the find also matches on ``created_at`` so a duplicate RUNNING
    row for the same ticket and client is never advanced by mistake) plus
    :func:`~cw.dev_queue.requeue._apply_requeue_stage`'s
    old_stage/``_raise_stage_high_water``/``_emit_stage_change`` trio -- a
    bare in-memory ``task.stage = Stage.IMPL`` would never reach
    ``dev_queue.json``, so the next dispatch tick would re-read stage=PLAN
    and re-run this whole check from scratch. Only mirrored onto the
    caller-held ``task`` (so this tick's ``executor.spawn(stage=task.stage,
    ...)`` builds the IMPL prompt, not PLAN) once the stored row is found and
    persisted.

    Two guards run before the (file-I/O) plan check, cheapest first:

    - #1286 Fix B: ``task.regressed_into_stage == Stage.PLAN`` means an
      operator explicitly regressed this ticket back to PLAN (``cw dev-queue
      requeue --stage plan --regress``, via ``_stage_regress``). That call
      clears the approval markers but does NOT delete the worktree's stale,
      still-signed-off ``.cw/plan.md`` -- so without this guard the bypass
      would silently defeat the operator's explicit re-plan request.
    - #1286 Fix C: the pipeline resolved by
      :func:`~cw.executor.resolve_pipeline_stages` (lane override, then
      client default) may not contain ``Stage.IMPL`` at all (pipelines are
      user-configurable per client/lane). Attempting the advance anyway
      would raise ``ValueError`` out of ``_raise_stage_high_water``'s
      ``stages.index()`` call.
    """
    if task.regressed_into_stage == Stage.PLAN:
        _log.debug(
            "dispatch: %r's approved-plan auto-bypass skipped -- task was"
            " deliberately regressed into PLAN, honoring the operator's"
            " re-plan request over the (possibly stale) signed-off plan.md"
            " (#1286)",
            task.ticket_id,
        )
        return

    stages = resolve_pipeline_stages(task, client)
    if Stage.IMPL not in stages:
        _log.debug(
            "dispatch: %r's approved-plan auto-bypass skipped -- the"
            " resolved pipeline (lane=%s) has no IMPL stage (#1286)",
            task.ticket_id,
            task.lane,
        )
        return

    bypass = _impl_bypass_plan_available(task, client, allow_tracker_fallback=False)
    if not bypass.available:
        return

    stored_task = None
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in store.tasks:
            # created_at disambiguates duplicate RUNNING rows for one
            # (ticket_id, client) -- reachable via add-after-terminal plus
            # ``requeue --from-completed`` (see ``_find_ticket``'s own
            # newest-created_at tie-break). session_id is not stamped yet
            # (this runs before executor.spawn / _stamp_spawn_success), so it
            # cannot serve here; created_at is never reassigned.
            if (
                candidate.ticket_id == task.ticket_id
                and candidate.client == client.name
                and candidate.status == QueueItemStatus.RUNNING
                and candidate.created_at == task.created_at
            ):
                stored_task = candidate
                break
        if stored_task is not None:
            _advance_stage(stored_task, stages, Stage.IMPL)
            save_dev_queue(store)

    if stored_task is not None:
        # Mirror onto the in-memory task so this tick's
        # executor.spawn(stage=task.stage, ...) below builds the IMPL
        # prompt, not PLAN.
        task.stage = stored_task.stage
        _log.info(
            "dispatch: %r has an approved, signed-off plan already"
            " on disk (%s) -- bypassing Stage 1 and spawning at"
            " IMPL directly (#1286)",
            task.ticket_id,
            worktree_path / ".cw" / "plan.md",
        )
    else:
        # Race guard: _claim_next_pending persisted this row as RUNNING
        # (still at Stage.PLAN) just before this function ran, so it should
        # always be found here. If it is missing or no longer RUNNING
        # (reaped/requeued/removed between claim and spawn), do NOT advance
        # and do NOT spawn with a mutated stage -- leave task.stage untouched
        # so this tick spawns /auto-dev-plan as normal.
        _log.warning(
            "dispatch: %r's approved-plan auto-bypass found no"
            " matching RUNNING row in the dev queue (client=%s)"
            " -- leaving stage at PLAN (#1286)",
            task.ticket_id,
            client.name,
        )


def _defer_occupied_claim(
    task: TicketTask,
    client: ClientConfig,
    exc: WorktreeOccupiedError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Release a claim whose reused worktree is occupied; spawn nothing (#2213).

    ``create_worktree(refresh_on_reuse=True)`` raised
    :exc:`~cw.exceptions.WorktreeOccupiedError`: a live cw session, a live
    daemon-roster worker, or an indeterminate read of either (fail closed) may be
    operating in the ticket's per-ticket worktree. Spawning a second worker into
    it is the hazard the ticket exists to prevent, so nothing is spawned and the
    worktree is never removed -- though HEAD may already have moved if a
    fast-forward landed just before the occupant was found (#2233).

    The stale-worktree path (a wrong-branch tree that ``create_worktree`` refuses
    with ``StaleWorktreeError``) routes here too when an occupant is present: it
    consults liveness first and defers through this helper. It removes the tree
    only when the tree is BOTH unoccupied and clean; an occupied tree is left
    for a later tick, and a dirty one is parked for the operator instead.

    The row goes back to PENDING for a later tick as a RELEASE, not a failure
    (:func:`_revert_claimed_task_to_pending` with ``defer_for``): no attempt is
    charged and no spawn error is stamped. The returned outcome sets ``occupied``
    and NOT ``spawn_error``, so the lane circuit breaker never counts it -- a
    long-lived occupant would otherwise pause the whole lane over a per-ticket
    condition. The reason is recorded in the log, in ``_SpawnOutcome.error`` and
    on the operator's emit line, and a ``dispatch.tick`` event with
    ``skip_reason=worktree_occupied`` is emitted through the emitter the #2077
    pre-claim screen shares.
    """
    _log.warning(
        "dispatch_tick: worktree %s for %s/%s is occupied (%s); not spawning, "
        "returning the task to PENDING for a later tick",
        exc.path,
        client.name,
        task.ticket_id,
        exc.reason,
    )
    _emit_worktree_occupied_skip_event(client.name, task.ticket_id)
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        defer_for=timedelta(seconds=_OCCUPIED_DEFER_SECONDS),
        created_at=task.created_at,
    )
    if emit is not None:
        emit(
            f"OCCUPIED {client.name}/{task.ticket_id} worktree={exc.path}"
            f" ({exc.reason}); deferred, not spawned"
        )
    return _SpawnOutcome(occupied=True, error=exc.reason)


def _defer_genuinely_live_hook_conflict(
    task: TicketTask,
    client: ClientConfig,
    exc: HookContextConflictError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Release a claim whose hook context is held by a genuinely live
    session (#2077) -- sibling of _defer_occupied_claim for the
    DAEMON-conflict branch of HookContextConflictError.

    The residual race the pre-claim occupancy screen cannot close: the
    screen's precompute found the worktree free, but by the time the
    executor wrote its hook context a live session was homed there. Same
    no-charge, no-breaker release as _defer_occupied_claim.

    Deliberately does NOT stamp hook_context_conflict_session_id (unlike
    the non-live branch in _spawn_claimed_task): that field's only consumer
    (reconcile/concierge.py recipe 1) reads it as "only an operator
    closing the session clears this" -- wrong semantics for a condition
    that resolves itself. Recipe 1 only evaluates BLOCKED_ON_USER rows;
    this revert leaves the row PENDING, so the stamp would only matter if a
    LATER, unrelated park inherited it -- which omitting it here prevents.
    """
    _log.warning(
        "dispatch_tick: hook context for %s/%s is held by genuinely live"
        " session %s; not spawning, returning the task to PENDING for a"
        " later tick",
        client.name,
        task.ticket_id,
        exc.conflicting_session_id,
    )
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        defer_for=timedelta(seconds=_OCCUPIED_DEFER_SECONDS),
        created_at=task.created_at,
    )
    _emit_worktree_occupied_skip_event(client.name, task.ticket_id)
    if emit is not None:
        emit(
            f"OCCUPIED {client.name}/{task.ticket_id} hook_context held"
            f" by live session={exc.conflicting_session_id}; deferred,"
            " not spawned"
        )
    return _SpawnOutcome(occupied=True, error=str(exc))


def _handle_hook_context_conflict(
    task: TicketTask,
    client: ClientConfig,
    exc: HookContextConflictError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Route a ``HookContextConflictError`` from the spawn path.

    Must be called from inside the ``except`` clause (``_log.exception``
    below relies on the active exception). Extracted from
    :func:`_spawn_claimed_task` to keep it inside its PLR0911 return budget
    once the #2077 genuinely-live branch was added.

    A genuinely live occupant (#2077) is a self-resolving wait, not a spawn
    failure: :func:`_defer_genuinely_live_hook_conflict` releases it without
    a charge or a breaker increment. Otherwise behaviour is deliberately
    identical to the broad spawn-failure path (revert to PENDING with the
    #868 backoff); the ONLY addition is recording WHICH session's live
    cw-context.json blocked the worktree, so concierge recipe 1 can stop
    requeuing a row that cannot spawn until that session is closed (GitHub
    #1674). The refusal itself lives there, not here.
    """
    if exc.genuinely_live:
        return _defer_genuinely_live_hook_conflict(task, client, exc, emit=emit)
    _log.exception(
        "dispatch_tick: hook-context conflict for %s/%s"
        " (blocking session=%s); reverting task to PENDING",
        client.name,
        task.ticket_id,
        exc.conflicting_session_id,
    )
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        stamp_backoff=True,
        hook_context_conflict_session_id=exc.conflicting_session_id,
        created_at=task.created_at,
    )
    return _SpawnOutcome(spawn_error=True, error=str(exc))


def _raise_if_stale_tree_occupied(
    client: ClientConfig,
    branch: str,
    *,
    daemon: NativeDaemonClient,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> None:
    """Raise :exc:`WorktreeOccupiedError` if the stale tree must not be removed.

    Guard 1 of the stale-worktree handler in :func:`_spawn_claimed_task`
    (#2213): consults :func:`cw.worktree.live_home_reason` -- the same predicate
    the same-branch reuse refresh uses, failing closed -- on the branch's
    canonical worktree path. *daemon* is the caller's own resolved
    :class:`~cw.native_daemon.NativeDaemonClient` (#2213 round 7), passed
    straight through rather than letting ``live_home_reason`` default to the
    real client -- a test injecting :class:`~cw.native_daemon.FakeNativeDaemonClient`
    into this claim path must be the thing consulted, not the host's real
    roster. A live cw session or daemon-roster worker homed there, or an
    unreadable state or roster, means the tree is not ours to remove. The
    raised error is caught by ``_spawn_claimed_task``'s
    ``except WorktreeOccupiedError`` and reaches :func:`_defer_occupied_claim`.
    Returns normally (no occupant) so the dirty check and removal may follow.
    *warned_unresolvable* is forwarded to :func:`~cw.worktree.live_home_reason`
    unchanged (#2240) -- the dispatch loop threads a process-lifetime set
    through here so a poisoned session/worker record does not re-warn every
    tick.
    """
    stale_tree = worktree_path_for(client, branch)
    occupant = live_home_reason(
        stale_tree, daemon=daemon, warned_unresolvable=warned_unresolvable
    )
    if occupant is None:
        return
    msg = (
        f"Refusing to remove stale worktree at {stale_tree} for branch "
        f"{branch!r}: another worker may be operating in it ({occupant}). "
        "The worktree was not touched."
    )
    raise WorktreeOccupiedError(msg, path=stale_tree, reason=occupant)


def _spawn_claimed_task(
    task: TicketTask,
    client: ClientConfig,
    *,
    resolved_native_daemon: NativeDaemonClient,
    parent: str | None,
    emit: Callable[[str], None] | None,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> _SpawnOutcome:
    """Spawn a Claude session for one already-claimed (RUNNING) task.

    Creates the worktree, spawns the session, stamps session_id +
    stage_base_ref, and emits SESSION_SPAWNED. On :class:`UsageLimitError` or
    any other spawn failure, reverts the task to PENDING and returns an outcome
    flagging the caller to break out of the slot/lane loops.
    """
    try:
        # Codex capability gate (#1238): a codex-backed stage that cannot reach
        # a usable `codex` CLI parks BLOCKED_ON_USER before any real per-task
        # work runs (worktree creation included) — extracted to a helper to
        # keep this function within the PLR branch/statement budget; no-op for
        # non-codex backends. Runs first so a codex-incapable host never pays
        # for worktree provisioning on a task that's about to be parked.
        parked = _codex_capability_gate(task, client)
        if parked is not None:
            return parked

        # Provision the worktree on the feature branch the auto-dev
        # skills push to (`<feature_branch_prefix>/<id>`, e.g.
        # dev/662) so cw and the worker agree on one branch — no
        # mid-pipeline rename that would trip the reuse guard (#712).
        # The session NAME still uses AUTO_DEV_LABEL_PREFIX (set in
        # the executor), which reconcile parses for the ticket id.
        branch = f"{client.feature_branch_prefix}/{task.ticket_id}"
        # Create a real git worktree (idempotent — returns existing
        # path if already created). Replaces a previous bug where
        # dispatch made an empty directory and relied on
        # ``claude -w`` to turn it into a worktree, which never
        # worked because that flag takes a name rather than a path.
        try:
            # allow_dirty_reuse: staged stages reuse one per-ticket
            # worktree and legitimately leave cross-stage churn (#712).
            # refresh_on_reuse (#2213): a reused per-ticket worktree can sit
            # behind origin/<branch>, so ask for a best-effort refresh. NOTE
            # this does a network `git fetch` (can be slow) and fast-forwards
            # only an unoccupied (no live cw session or daemon-roster worker),
            # clean, strictly-behind worktree. The refresh has two outcomes,
            # handled oppositely -- OCCUPIED is not "did not move it": a
            # fast-forward (and, #2233, its post-ff submodule sync) can land
            # for real before an occupant is found:
            #   * NOT refreshed (dirty, diverged, failed fetch, branch absent):
            #     the tree is ours, just not up to date. create_worktree
            #     returns the path and we spawn on it; the reason is logged
            #     (cw.worktree) -- there is no friction-notes surface here.
            #   * OCCUPIED (a live session or worker may be using the tree, or
            #     that cannot be ruled out): create_worktree RAISES
            #     WorktreeOccupiedError. HEAD may already have moved (a
            #     fast-forward, and #2233's post-ff submodule sync, can land
            #     before the occupancy re-check finds the occupant) -- that
            #     move is never undone. The raise must never fall through to
            #     a spawn, so it is handled by its own narrow ``except``
            #     below (not the StaleWorktreeError branch, which removes a
            #     tree: an occupied one is never removed). A stale
            #     (wrong-branch) tree gets its own occupancy check inside
            #     that branch and reaches the same deferral.
            # ticket_id: names the ticket on the worktree.fast_forwarded audit
            # event a refresh that moves HEAD records; no other effect.
            worktree_path = create_worktree(
                client,
                branch,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                ticket_id=task.ticket_id,
                native_daemon=resolved_native_daemon,
            )
        except StaleWorktreeError:
            # A stale worktree (wrong branch / not a worktree) refused
            # reuse (#404). No session exists yet, so reconcile's
            # TIMED_OUT cleanup will never fire for it — without
            # removing it here the task reverts to PENDING and re-hits
            # the same stale tree every tick (an infinite spin). Force-
            # remove it (best-effort) so the next claim rebuilds fresh,
            # then re-raise into the handler below to revert to PENDING.
            # Caught narrowly as StaleWorktreeError (not WorktreeError)
            # so the main-checkout guard never triggers a removal.
            #
            # Three guards stand between a stale tree and that removal, in
            # this order:
            #
            # 1. Liveness (#2213): if a live cw session or daemon-roster
            #    worker is homed on the tree, or that cannot be ruled out
            #    (unreadable state or roster: fail closed), leave it alone
            #    and defer the claim through _defer_occupied_claim -- the
            #    same OCCUPIED_BY_LIVE_SESSION handling the reuse-refresh
            #    refusal reaches. A wrong branch does not mean an idle tree:
            #    a worker may be running in it. It comes BEFORE the dirty
            #    check because it is the stronger reason to keep hands off
            #    (removing a live worker's tree destroys its working state),
            #    and because unsaved_work_reason shells out to git inside a
            #    directory that worker may be mutating concurrently, so the
            #    liveness answer must not depend on that read.
            # 2. Dirty-check guard (#425): if the stale tree contains
            #    unsaved work, skip the removal and park the task as
            #    BLOCKED_ON_USER instead of PENDING so the operator can
            #    inspect. The outer except handler will not overwrite
            #    BLOCKED_ON_USER (it checks status == RUNNING before
            #    reverting).
            # 3. Only a tree that is both unoccupied and clean is removed
            #    (the #404 spin fix above).
            #
            # Guard 1 raises WorktreeOccupiedError from inside this handler;
            # the sibling ``except WorktreeOccupiedError`` below (an exception
            # raised here propagates to the enclosing try, so it does catch
            # it) hands it to _defer_occupied_claim. Raising rather than
            # returning that helper's outcome inline keeps this function within
            # the PLR0911 return budget and gives the reuse-refresh refusal and
            # this one a single exit.
            _raise_if_stale_tree_occupied(
                client,
                branch,
                daemon=resolved_native_daemon,
                warned_unresolvable=warned_unresolvable,
            )
            unsaved = unsaved_work_reason(client, branch)
            if unsaved is not None:
                _log.warning(
                    "dispatch: stale worktree %s/%s has unsaved work (%s)"
                    " — leaving for operator inspection; parking as"
                    " BLOCKED_ON_USER",
                    client.name,
                    branch,
                    unsaved,
                )
                # #2114: the breadcrumb names WHICH predicate fired and the
                # base ref it measured against -- `dirty_worktree` on a
                # visibly clean tree is otherwise undiagnosable.
                _park_running_task_blocked_on_user(
                    ticket_id=task.ticket_id,
                    client_name=client.name,
                    disposition="dirty_worktree",
                    breadcrumbs=f"{worktree_path_for(client, branch)}: {unsaved}",
                    unproductive=False,
                    created_at=task.created_at,
                )
            else:
                with contextlib.suppress(WorktreeError, OSError):
                    remove_worktree(client, branch, force=True)
            raise

        # Guard against the #300 regression: if create_worktree
        # returns the main checkout path (degenerate path-computation
        # or symlink indirection), refuse the spawn.  create_worktree
        # normally catches this itself, but a mocked or buggy
        # implementation could still return the same path.
        check_not_main_checkout(worktree_path, client)

        if task.stage == Stage.PLAN:
            _apply_plan_bypass_if_available(task, client, worktree_path)

        # Function-level import breaks the gating<->claim import cycle:
        # cw.dispatch.gating imports this module at top level, so claim.py
        # must defer its own reach back into gating (mirrors the #698
        # reconcile/_shared -> cw.dispatch precedent the ticket cites).
        from cw.dispatch.gating import _invalidate_stale_context_json

        _invalidate_stale_context_json(task, client, worktree_path)

        executor = resolve_executor(task, client, native_daemon=resolved_native_daemon)
        # wall_clock_budget_seconds is always None since the process-kill-
        # timeout removal: no executor is handed a kill deadline. The codex
        # backend treats None as unlimited (no proc.kill on a timer), and the
        # value written into cw-context.json is informational only.
        session_id = executor.spawn(
            stage=task.stage,
            task=task,
            worktree=worktree_path,
            client=client,
            parent=parent,
            wall_clock_budget_seconds=None,
        )

        _stamp_spawn_success(
            task,
            client_name=client.name,
            session_id=session_id,
            worktree_path=worktree_path,
        )

        record_event(
            OrchestratorEventType.SESSION_SPAWNED,
            {
                "ticket_id": task.ticket_id,
                "client": client.name,
                "session_id": session_id,
                "lane": task.lane,
            },
        )

        if emit is not None:
            emit(
                f"SPAWN {client.name}/{task.ticket_id}"
                f" session={session_id}"
                f" worktree={worktree_path}"
            )
    except UsageLimitError as exc:
        # Narrow catch for fleet-wide usage limits. Raised by
        # executor.spawn → NativeDaemonClient.spawn_bg when the
        # claude output matches USAGE_LIMIT_RE. The task was claimed
        # to RUNNING but no session_id was assigned (spawn failed);
        # revert it explicitly to PENDING below, then break so no
        # further slots are tried this tick.
        #
        # The raw spawn-time message is NOT logged here: native_daemon
        # ._usage_limit_error already logs it exactly once per raise (#1409),
        # and repeating it would give two records for one event.
        _log.warning(
            "dispatch_tick: usage limit detected for %s/%s; setting back-off"
            " (reset_at=%s)",
            client.name,
            task.ticket_id,
            exc.reset_at,
        )
        # Revert the claimed task back to PENDING — spawn never succeeded.
        _revert_claimed_task_to_pending(
            client.name, task.ticket_id, created_at=task.created_at
        )
        return _SpawnOutcome(
            usage_limit_detected=True, usage_limit_reset_at=exc.reset_at
        )
    except HookContextConflictError as exc:
        # Narrow catch ahead of the broad handler below (order matters —
        # HookContextConflictError is a plain CwError subclass and would
        # otherwise fall through). See _handle_hook_context_conflict.
        return _handle_hook_context_conflict(task, client, exc, emit=emit)
    except WorktreeOccupiedError as exc:
        # Narrow catch ahead of the broad handler (order matters -- this is a
        # WorktreeError and would otherwise be reverted WITH a spawn-error
        # backoff and trip the circuit breaker). See _defer_occupied_claim.
        return _defer_occupied_claim(task, client, exc, emit=emit)
    except Exception as exc:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331.
        # Paired tests: TestDispatchTickSpawnErrors in
        # tests/test_dispatch.py:1097+ (asserts the loop survives
        # spawn failures and the task is reverted to PENDING).
        #
        # Catch broad like the reconcile guard above: a backend
        # outage (tmux pane exhaustion, transient daemon failure,
        # OSError from the adapter) must NOT kill the loop. The
        # task was just claimed RUNNING by _claim_next_pending; it
        # would otherwise be left in a half-state (status=RUNNING,
        # session_id=None) requiring manual repair. Revert to
        # PENDING + clear session_id so the next tick (or
        # reconcile) can retry. Break to skip this client's
        # remaining slots this tick — re-trying the same failing
        # backend immediately would just spin. See GitHub issue
        # #149.
        _log.exception(
            "dispatch_tick: spawn failed for %s/%s; reverting task to PENDING",
            client.name,
            task.ticket_id,
        )
        _revert_claimed_task_to_pending(
            client.name,
            task.ticket_id,
            stamp_backoff=True,
            created_at=task.created_at,
        )
        return _SpawnOutcome(spawn_error=True, error=str(exc))

    return _SpawnOutcome(spawned=True)


__all__ = [
    "_CLAIM_BACKOFF",
    "_CLAIM_CLAIMED",
    "_CLAIM_SKIPPED",
    "_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS",
    "_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD",
    "_CODEX_CAPABILITY_PROBE_TTL_SECONDS",
    "_OCCUPIED_DEFER_SECONDS",
    "_SPAWN_ERROR_BACKOFF_CAP_SECONDS",
    "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS",
    "_SpawnOutcome",
    "_apply_plan_bypass_if_available",
    "_cached_codex_capability_diagnosis",
    "_claim_next_pending",
    "_codex_capability_cache",
    "_codex_capability_gate",
    "_codex_capability_park_count",
    "_defer_genuinely_live_hook_conflict",
    "_defer_occupied_claim",
    "_emit_attempt_cap_attention_event",
    "_emit_attempt_cap_blocked_event",
    "_emit_stale_dispatch_attention_event",
    "_emit_stale_dispatch_blocked_event",
    "_emit_worktree_occupied_skip_event",
    "_find_running_row",
    "_handle_hook_context_conflict",
    "_is_backstop_exempt",
    "_is_fix_dispatch_held",
    "_is_stale_pr_gated",
    "_lane_occupants_for_client",
    "_lane_stats_for_client",
    "_park_running_task_blocked_on_user",
    "_park_stale_pr_task",
    "_raise_if_stale_tree_occupied",
    "_reset_codex_capability_cache",
    "_revert_claimed_task_to_pending",
    "_screen_and_claim",
    "_spawn_claimed_task",
    "_stamp_spawn_success",
    "resolve_occupied_ticket_ids",
]
