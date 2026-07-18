"""Dispatch tick orchestration, sentinel/stage routing, and the event loop.

The ``core.py``-equivalent leftover of the ``cw.dispatch`` package split
(#1310): everything not yet carved into gating/claim/lanes (targets of parts
2-3)."""

from __future__ import annotations

import contextlib
import importlib.metadata
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple

import yaml

from cw.auto_dev_result import (
    _STAGE_REACHED_CANONICAL,
    FINALIZE_REGRESS_BLOCKER_REASONS,
    FINALIZE_REGRESS_CAP,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    PLAN_SOURCE_NONE,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_LARGE,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    AutoDevResult,
    parse_stdout,
)
from cw.collision import detect_wave_collisions
from cw.config import (
    load_clients,
    load_effective_clients,
    load_effective_config,
    load_state,
    load_usage_limited_until,
    save_state,
    save_usage_limited_until,
    sessions_lock,
)
from cw.dev_queue import (
    SIGNOFF_GATE_DISPOSITION,
    _advance_task_pointer,
    _derive_disposition,
    _extract_pr_url,
    _stage_regress,
    dev_queue_lock,
    load_dev_queue,
    load_plan,
    save_dev_queue,
    transition_task_status,
)
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import (
    ConfigValidationError,
    VersionDriftError,
)
from cw.executor_diagnostics import cleanup_expired_diagnostics
from cw.models import (
    OCCUPIED_LANE_STATUSES,
    ClientConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    Stage,
)
from cw.native_daemon import get_native_daemon_client
from cw.pr_hydrate import hydrate_pr_states
from cw.reconcile import (
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        OrchestratorEvent,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient
from cw.dispatch.gating import (
    _emit_availability_skip,
    _emit_stale_skip,
    _emit_usage_limit_skip_events,
    _reconcile_usage_limited,
    _resolve_availability_once,
    _resolve_freshness,
)
from cw.dispatch.lanes import (
    _dispatch_client_lanes,
    _record_client_freshness_block,
    _reset_client_freshness_blocks,
)

_log = logging.getLogger("cw.dispatch")


_DISPATCH_CONSUMER = "dispatch"


# Duplicated from cw.doctor; importing from there creates a circular dep.
# A future cw.const cleanup can consolidate these.
_CW_PACKAGE_NAME: str = "claude-workspace"


# paused_status written to SESSION_NEEDS_ATTENTION when a session parks at plan
# stage (ambiguities_pending_resolution / premises_pending_verification).
_PLAN_PARKED_REASON = "plan_parked"


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 1 parks a
# non-small-tier scope-gated approval status (plan_pending_approval /
# review_pending_approval) to BLOCKED_ON_USER. Deliberately distinct from
# _PLAN_PARKED_REASON -- that constant is scoped to the v4
# ambiguities/premises statuses, an unrelated park reason -- per the
# operator's R1 resolution. See GitHub #1302.
_APPROVAL_GATE_REASON = "approval_gate"


# Disposition stamped by _stage_advance_unchecked when the task's client is
# absent from the effective clients dict — a config error, not a
# transient/recoverable state. Deliberately excluded from both
# concierge._FALSE_PARK_ELIGIBLE_DISPOSITIONS and
# escalation._ELIGIBLE_DISPOSITIONS. See GitHub #976.
_UNKNOWN_CLIENT_REASON = "unknown_client"


# Disposition stamped by _stage_advance_unchecked when task.stage is not in
# the client's configured pipeline stages — a config error, not a
# transient/recoverable state. Same exclusion as _UNKNOWN_CLIENT_REASON. See
# GitHub #976.
_INVALID_STAGE_REASON = "invalid_stage_config"


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 5's blocked
# status carries a blocker reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
# (RFC 0011 A1). Deliberately distinct from QueueItemStatus.
# AWAITING_OPERATOR_SIGNOFF -- that enum member is a REVIEW-stage signoff
# gate; this constant is a paused_status string for an operator/dependency
# unavailability, an unrelated concept that happens to share the "awaiting
# operator" phrase. See GitHub #1155.
_AWAITING_OPERATOR_REASON = "awaiting_operator_availability"


def _resolve_loaded_version() -> str:
    """Capture the installed version at import time for drift detection."""
    try:
        return importlib.metadata.version(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


# Captured at import time; remains the version that was actually loaded.
_LOADED_VERSION: str = _resolve_loaded_version()


@dataclass(frozen=True)
class DispatchTickResult:
    """Return value of :func:`dispatch_tick`.

    ``spawned`` — number of sessions started this tick.
    ``usage_limit_detected`` — True if a usage limit was detected this tick
    (either from spawn-time :class:`~cw.exceptions.UsageLimitError` or from
    :attr:`~cw.reconcile.ReconcileReport.usage_limited`). The caller
    (:func:`run_dispatch_loop`) uses this to set the back-off window.
    ``--once`` mode intentionally does not back off (single tick, no loop state).
    """

    spawned: int
    usage_limit_detected: bool = False


def _sweep_expired_diagnostics(config: OrchestratorConfig) -> None:
    """Best-effort retention sweep of per-session diagnostics bundles (#1239).

    Runs OUTSIDE ``sessions_lock`` (pure filesystem rmtree, no state mutation)
    and swallows every error, mirroring :func:`_reconcile_usage_limited`'s
    broad-catch posture: a cleanup failure (permission, race with a concurrent
    tick) must never abort the tick — the sweep just retries next tick.
    Extracted from ``dispatch_tick`` to keep it under the PLR branch/statement
    caps. Paired test: tests/test_dispatch.py
    test_dispatch_tick_cleanup_failure_does_not_abort_tick.
    """
    try:
        cleanup_expired_diagnostics(retention_hours=config.diagnostics_retention_hours)
    except Exception:  # noqa: BLE001
        _log.exception("diagnostics cleanup failed during dispatch_tick; continuing")


def _build_plan_order(*, use_plan: bool) -> dict[str, list[str]]:
    """Build the per-client ticket priority ordering from the persisted plan.

    Returns an empty mapping when ``use_plan`` is False or no plan is found;
    otherwise maps each client to its plan-ordered ticket ids.
    """
    plan_order_by_client: dict[str, list[str]] = {}
    if use_plan:
        plan = load_plan()
        if plan is not None:
            for plan_task in plan.tasks:
                plan_order_by_client.setdefault(plan_task.client, []).append(
                    plan_task.ticket_id,
                )
    return plan_order_by_client


class _ClientTickSnapshot(NamedTuple):
    """A client's per-tick numeric fields for dispatch.tick skip events.

    ``running_count`` / ``client_ceiling`` / ``pending_count`` are plain
    ``int``s naming distinct concepts (running sessions, per-client cap,
    queued tasks) — named fields (vs. a bare positional tuple) prevent a
    transposition mypy can't catch (e.g. swapping running/pending) while
    still unpacking positionally at the call site like a plain tuple.
    """

    running_count: int
    client_ceiling: int
    queue_snapshot: DevQueueStore
    pending_count: int


def _client_tick_snapshot(
    client: ClientConfig,
    *,
    state: CwState,
    config: OrchestratorConfig,
) -> _ClientTickSnapshot:
    """Compute a client's per-tick numeric fields for dispatch.tick skip events.

    Extracted from :func:`dispatch_tick`'s loop body so both preflight gates
    (availability, freshness) can emit a dispatch.tick skip event with all four
    numeric fields populated. Single dev-queue lock acquisition — the returned
    ``queue_snapshot`` is reused by the caller for stale-task and per-lane
    counts.
    """
    running_count = sum(
        1
        for s in state.sessions
        if s.client == client.name
        and s.origin == SessionOrigin.DAEMON
        and s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
    )
    client_ceiling = config.per_client_ceiling.get(client.name, config.default_ceiling)
    with dev_queue_lock():
        queue_snapshot = load_dev_queue()
    pending_count = sum(
        1
        for t in queue_snapshot.tasks
        if t.client == client.name and t.status == QueueItemStatus.PENDING
    )
    return _ClientTickSnapshot(
        running_count=running_count,
        client_ceiling=client_ceiling,
        queue_snapshot=queue_snapshot,
        pending_count=pending_count,
    )


def dispatch_tick(
    config: OrchestratorConfig,
    *,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    warned_stale: set[tuple[str, str]] | None = None,
    warned_fetch_fail: set[str] | None = None,
    warned_collision: set[frozenset[str]] | None = None,
    usage_limited_until: datetime | None = None,
    auto_ff: bool = True,
    client_filter: str | None = None,
) -> DispatchTickResult:
    """Run one dispatch tick.

    For each client that has pending TicketTasks, check how many DAEMON
    sessions are currently ACTIVE or IDLE and compare against the
    per-client cap from *config*.  If below the cap, claim one pending
    task and spawn a Claude session for it.

    Args:
        config: Orchestrator config (per-client caps, tick interval).
        use_plan: If True, respect the persisted DispatchPlan ordering.
        parent: Optional parent session ID. When set, every spawned
            worker is linked to it (``parent_session_id`` +
            bidirectional ``worker_session_ids``) so ``cw orchestrate
            workers`` can list dispatched workers as first-class
            sessions.
        emit: Optional callable for operator-facing stdout lines.
            When None, all human-readable output is suppressed (quiet
            mode).  Typically ``click.echo`` in CLI context.
        warned_stale: Mutable set of ``(client, ticket_id)`` pairs that
            have already received a "main behind origin" warning during
            this dispatcher run.  Prevents repeated spam across ticks.
            Caller owns the set; mutated in-place.
        warned_fetch_fail: Mutable set of client names that have already
            received a fetch-failure WARNING during this dispatcher run.
            Suppresses repeated WARNINGs for persistently unreachable
            remotes.  Caller owns the set; mutated in-place.
        warned_collision: Mutable set of ``frozenset({ticket_id_a,
            ticket_id_b})`` pairs already warned this loop run. Prevents
            duplicate WAVE_COLLISION events for persistent in-flight
            collisions across ticks. Caller owns the set; mutated
            in-place. When None, dedup is skipped (every tick fires).
        usage_limited_until: When set and in the future, all clients are
            skipped with ``skip_reason=USAGE_LIMITED`` and the function
            returns immediately. The back-off window is set by the
            caller (:func:`run_dispatch_loop`) when a
            :class:`~cw.exceptions.UsageLimitError` is detected.
            Single-tick (``--once``) mode does not set back-off.
        auto_ff: When True (default), attempt to fast-forward local main
            automatically before emitting TICKET_NEEDS_SYNC. Only fires
            when ``check_main_ff_safety`` returns ``"behind"``; other
            states (``"ahead"``, ``"diverged"``, ``"detached"``) still
            fall through to the stale-block path. Pass ``False`` to
            restore legacy block-only behavior.
        client_filter: When set, narrow the client loop to this single
            client name. The caller is responsible for validating that the
            name exists before calling; an unknown name silently produces
            an empty tick.

    Returns:
        :class:`DispatchTickResult` with ``spawned`` count and
        ``usage_limit_detected`` flag.
    """
    resolved_native_daemon = native_daemon or get_native_daemon_client()
    any_usage_limit_detected = _reconcile_usage_limited()
    _sweep_expired_diagnostics(config)
    clients = load_effective_clients()
    if client_filter is not None:
        clients = {client_filter: clients[client_filter]}
    state = load_state()
    spawned = 0

    plan_order_by_client = _build_plan_order(use_plan=use_plan)

    # Wave file-collision detection runs before usage-limit gates so that
    # RUNNING tasks are checked even during backoff — they continue running
    # regardless of whether spawning is paused (#784).
    # Why: writes WAVE_COLLISION events to inbox.jsonl for each new collision pair.
    with dev_queue_lock():
        collision_snapshot = load_dev_queue()
    detect_wave_collisions(
        collision_snapshot.tasks,
        warned_collision=warned_collision,
        emit=emit,
    )

    # Usage-limit back-off gate: if the window is still active, skip all clients
    # this tick and emit a dispatch.tick event with skip_reason=USAGE_LIMITED.
    if usage_limited_until is not None and datetime.now(UTC) < usage_limited_until:
        _emit_usage_limit_skip_events(clients, config, state)
        return DispatchTickResult(spawned=0, usage_limit_detected=False)

    # Same-tick race fix: reconcile reverts rate-limited phantom tasks to PENDING
    # on the same tick they're detected. Without this gate the spawn loop runs
    # immediately after reconcile and re-spawns the now-PENDING task, hitting the
    # same limit again. Skip spawning this tick so the caller can set
    # usage_limited_until before the next tick fires (#804).
    if any_usage_limit_detected:
        _emit_usage_limit_skip_events(clients, config, state)
        return DispatchTickResult(spawned=0, usage_limit_detected=True)

    # Tier-1: optionally cap how many clients are eligible per tick.
    # max_parallel_clients=None preserves the original behaviour (all clients).
    dispatched_client_count = 0
    # Fleet-wide availability verdict, memoized across loop iterations (see
    # gate below). ``None`` means "not yet resolved this tick".
    available: bool | None = None
    for client in clients.values():
        if (
            config.max_parallel_clients is not None
            and dispatched_client_count >= config.max_parallel_clients
        ):
            break

        # --- Availability preflight gate (RFC 0011 A5) --- highest
        # precedence. Fleet-wide TTL-cached `gh auth status` probe: resolved
        # at most ONCE per tick, not once per client — the cached verdict is
        # identical for every client in this loop, so a per-client call
        # would re-read the shared sidecar file N times for the same
        # answer. Memoized (not hoisted above the loop) via
        # _resolve_availability_once so it's only called once the loop body
        # actually runs for at least one client — an empty ``clients`` dict
        # or a fully-paused fleet (``max_parallel_clients=0``, which breaks
        # on the first iteration above) never probes or pages, matching the
        # pre-#1157 invariant that this check only fires when there's
        # dispatch work it could gate. Checked before the per-client
        # freshness gate so that during a real GitHub outage no client pays
        # the freshness git-fetch cost for a verdict that gets discarded
        # anyway. Fails open on any resolution error, same posture as
        # _resolve_freshness.
        available = _resolve_availability_once(available)

        # Numeric fields (running, cap, queue snapshot, pending) hoisted above
        # both gates so a dispatch.tick skip event for either gate carries all
        # four. See _client_tick_snapshot.
        running_count, client_ceiling, queue_snapshot, pending_count = (
            _client_tick_snapshot(client, state=state, config=config)
        )
        # Keep legacy cap alias for skip-event and back-off event payloads.
        cap = client_ceiling

        # Fleet-wide availability outage: hold every client PENDING (no claim,
        # no attempts consumed). Composition pin (RFC 0011 A5): on this gated
        # path NEITHER _record_client_freshness_block NOR
        # _reset_client_freshness_blocks runs — the freshness counter stays
        # frozen during an outage, it must not reset.
        if not available:
            _emit_availability_skip(
                client,
                queue_snapshot,
                pending_count=pending_count,
                running_count=running_count,
                cap=cap,
            )
            continue

        # --- Freshness gate --- (only reached when the fleet is available)
        # Check whether the client's local default branch is behind origin
        # before claiming any ticket.  Stale repos cause sessions to exit
        # immediately with local_main_diverged_from_origin, burning a slot.
        # On any error, log and proceed so a transient network issue never
        # blocks the whole loop.  When auto_ff and safely behind, this also
        # fast-forwards local main and clears the stale flag on success.
        stale, freshness_detail = _resolve_freshness(
            client, auto_ff=auto_ff, warned_fetch_fail=warned_fetch_fail
        )

        if stale:
            _emit_stale_skip(
                client,
                queue_snapshot,
                pending_count=pending_count,
                running_count=running_count,
                cap=cap,
                emit=emit,
                warned_stale=warned_stale,
                freshness_detail=freshness_detail,
            )
            _record_client_freshness_block(client.name, freshness_detail, config=config)
            continue

        # Client passed the freshness gate this tick — reset its consecutive
        # freshness-block latch (RFC 0007 §W2). Short-circuits internally
        # when already 0, so a steady-state healthy client never rewrites
        # the override file.
        _reset_client_freshness_blocks(client.name)

        # Why: incremented only past the freshness gate — a stale/skipped
        # client does not consume Tier-1 quota, so max_parallel_clients=N
        # always grants N *dispatchable* clients per tick.
        dispatched_client_count += 1
        priority_ids = plan_order_by_client.get(client.name)
        cap_full = running_count >= client_ceiling

        # Build per-lane running count from tasks→sessions join.
        # Tasks in RUNNING, BLOCKED_ON_USER, or AWAITING_OPERATOR_SIGNOFF with
        # an active session_id count toward their lane's cap (ADR-0006:
        # BLOCKED_ON_USER occupies the slot; #990 extends this to a
        # signoff-parked ticket, which is likewise not eligible for re-dispatch).
        # Reuses the queue_snapshot taken above — nothing between the two
        # points mutates the queue (auto-ff is git-only).
        running_by_lane: dict[str, int] = {}
        for qt in queue_snapshot.tasks:
            if qt.client != client.name:
                continue
            if qt.status not in OCCUPIED_LANE_STATUSES:
                continue
            lane_key = qt.lane
            running_by_lane[lane_key] = running_by_lane.get(lane_key, 0) + 1

        # Resolve effective lanes. For clients with no declared lanes, use the
        # synthesized default lane but override its max_parallel with the client
        # ceiling so backward-compat behaviour is preserved.
        effective_lanes = client.effective_lanes
        if not client.lanes:
            effective_lanes = [
                effective_lanes[0].model_copy(update={"max_parallel": client_ceiling})
            ]

        client_result = _dispatch_client_lanes(
            client,
            effective_lanes,
            queue_snapshot,
            running_count=running_count,
            client_ceiling=client_ceiling,
            cap=cap,
            pending_count=pending_count,
            cap_full=cap_full,
            running_by_lane=running_by_lane,
            priority_ids=priority_ids,
            config=config,
            resolved_native_daemon=resolved_native_daemon,
            parent=parent,
            emit=emit,
        )
        spawned += client_result.spawned
        if client_result.usage_limit_detected:
            any_usage_limit_detected = True

    return DispatchTickResult(
        spawned=spawned, usage_limit_detected=any_usage_limit_detected
    )


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost_usd to task.total_cost_usd, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_stop — normal headless path)
      2. session.last_result.get('cost_usd') (populated by persist_last_result —
         event-replay path where signal_stop did not run)

    When both sources are absent, total_cost_usd is left unchanged.
    Called inside dev_queue_lock so the mutation is covered by the same
    save_dev_queue call that persists the COMPLETED status.
    """
    if session_id is None:
        return
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None:
        return
    cost: float | None = session.cost_usd
    if cost is None and isinstance(session.last_result, dict):
        raw_cost = session.last_result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            cost = float(raw_cost)
    if cost is not None:
        task.total_cost_usd = (task.total_cost_usd or 0.0) + cost


def _extract_scope_tier(last_result: dict[str, object] | None) -> str | None:
    """Pull ``scope.tier`` off a raw sentinel dict, tolerating a missing/non-dict
    ``scope`` key. Shared by ``_persist_carried_context`` and
    ``_resolve_scope_tier`` so the two never drift on how they read the field.
    """
    scope_val = last_result.get("scope") if last_result is not None else None
    return scope_val.get("tier") if isinstance(scope_val, dict) else None


def _persist_carried_context(
    task: TicketTask, last_result: dict[str, object] | None
) -> None:
    """Stamp carried-through context (plan_source, computed scope tier) onto the
    task from a stage-matched sentinel, so a rescue respawn's fresh claim->spawn
    re-materializes it via cw-context.json (#1050). Null/pre-impl values and a
    stray plan_source=PLAN_SOURCE_NONE never clobber an already-set value.
    """
    if not isinstance(last_result, dict):
        return
    plan_source = last_result.get("plan_source")
    if isinstance(plan_source, str) and plan_source not in ("", PLAN_SOURCE_NONE):
        task.plan_source = plan_source
    tier = _extract_scope_tier(last_result)
    if isinstance(tier, str) and tier:
        task.computed_scope_tier = tier


def _resolve_scope_tier(
    last_result: dict[str, object] | None, task: TicketTask
) -> str | None:
    """Resolve the effective scope tier for a scope-gated advance decision.

    Precedence (escalate-only, #314, #696, #926):
      0. If either ``task.scope_hint`` or the sentinel's ``scope.tier`` is
         ``"large"``, the result is ``"large"`` -- an operator hint can only
         ADD the approval gate, never remove it, and a large sentinel tier is
         never de-escalated by a smaller hint.
      1. Otherwise, ``last_result.scope.tier`` -- the plan sentinel's own
         classification.
      2. Otherwise, ``task.scope_hint`` -- operator/queue hint, used when the
         sentinel omits the tier.

    Why: a real PLAN-stage sentinel can legitimately carry ``scope.tier=null``
    (``lines_actual`` is unknown pre-impl), so a raw read blocked small tickets
    that should flow PLAN->IMPL unattended (#663 dogfood). Returns ``None`` when
    neither source supplies a tier -- the caller then blocks conservatively.
    """
    tier = _extract_scope_tier(last_result)
    # Step 0 of the precedence above. Only the exact string "large" escalates;
    # unexpected hint values (e.g. "medium") are treated as not-large.
    if SCOPE_TIER_LARGE in (task.scope_hint, tier):
        return SCOPE_TIER_LARGE
    if isinstance(tier, str):
        return tier
    return task.scope_hint


def resolve_signoff(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> Literal["operator"] | None:
    """Resolve the effective operator-signoff policy for *task* (RFC 0007 Phase 3).

    Precedence (highest to lowest), mirroring ``resolve_reap_policy``
    (reconcile/_shared.py) but with a 3rd tier for the per-ticket override:
      1. ``TicketTask.signoff`` -- per-ticket override (``cw dev-queue add
         --signoff operator``).
      2. ``LaneConfig.signoff`` in the task's client config.
      3. ``OrchestratorConfig.default_signoff`` -- global default; ``"none"``
         resolves to ``None`` (no gate).

    A task whose client is absent from *clients*, or whose lane name is not
    declared in that client's lanes, falls through to the global default --
    keeps behaviour identical to the pre-#990 read for any task that predates
    lane stamping. See GitHub #990.
    """
    if task.signoff is not None:
        return task.signoff
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.signoff is not None:
                return lane_cfg.signoff
    return config.default_signoff if config.default_signoff != "none" else None


def _should_gate_for_signoff(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> bool:
    """True iff *task* requires an explicit operator signoff before advancing.

    Lazily loads ``OrchestratorConfig`` itself -- the single call site for that
    load -- so ``_route_staged_decision``/``apply_staged_decision`` (and
    ``approve_ticket`` in dev_queue.py, its other caller) keep their existing
    signatures unchanged (#990). Mirrors the ad hoc ``load_effective_config()``
    calls already made elsewhere in ``run_dispatch_loop``.
    """
    config = load_effective_config()
    return resolve_signoff(task, clients, config) is not None


def _stage_advance_unchecked(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    *,
    disposition: str | None = None,
    pr_url: str | None = None,
) -> None:
    """Advance task to next pipeline stage, or mark COMPLETED at terminal stage.

    Assert-free: no status precondition. The live consume/reconcile paths reach
    this only through ``apply_staged_decision`` (which asserts RUNNING first);
    the #918 late-sentinel rescue path reaches it through
    ``_route_staged_decision`` for a BLOCKED_ON_USER (idle-parked) task, so the
    RUNNING assert cannot live here. Mirrors ``_advance_task_pointer``'s
    assert-free contract in dev_queue.py.
    """
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        _log.warning(
            "dispatch: advance: client %r not found for task %r -- parking as BLOCKED",
            task.client,
            task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=_UNKNOWN_CLIENT_REASON
        )
        return
    pipeline = client_cfg.pipeline
    stages = pipeline.stages
    if task.stage not in stages:
        _log.warning(
            "dispatch: advance: stage %r not in pipeline for task %r",
            task.stage,
            task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=_INVALID_STAGE_REASON
        )
        return
    if task.stage == stages[-1]:
        transition_task_status(
            task, QueueItemStatus.COMPLETED, disposition=disposition, pr_url=pr_url
        )
    else:
        _advance_task_pointer(task, stages)


# Maps a sentinel's ``AutoDevResult.stage_reached`` (the closed 7-value
# StageReached literal, see cw.auto_dev_result) to the pipeline Stage it
# represents completion of. Used by ``_classify_sentinel_stage_position`` to
# guard against a late/replayed sentinel from a previous leg being routed
# against whatever stage the task's row currently holds (#986 incident, GitHub
# #1019), and to classify a legitimate later-stage self-escalation (#1149).
#
# Stage.HARDEN is deliberately absent: it has no legitimate stage_reached
# counterpart (RFC 0005 A1, dormant stage) -- every one of the 7 canonical
# values below maps to PLAN/IMPL/REVIEW/FINALIZE, never HARDEN, so any
# sentinel arriving against a HARDEN-stage task always mismatches by
# construction.
_STAGE_REACHED_TO_STAGE: dict[str, Stage] = {
    "stage1_pre_flight": Stage.PLAN,
    "stage1_plan": Stage.PLAN,
    "stage2_impl": Stage.IMPL,
    "stage3_review": Stage.REVIEW,
    "stage4a_merge_gate": Stage.FINALIZE,
    "stage4b_pr_create": Stage.FINALIZE,
    "stage5_post_create": Stage.FINALIZE,
}


# Fail fast at import time if this mapping's keys ever drift from the
# canonical StageReached value set (e.g. a new stage added to the Literal in
# auto_dev_result.py without a matching entry here) -- a silent gap here
# degrades to a permanent stage-mismatch refusal with no test signal.
if set(_STAGE_REACHED_TO_STAGE) != _STAGE_REACHED_CANONICAL:
    _drift_msg = (
        "_STAGE_REACHED_TO_STAGE keys drifted from cw.auto_dev_result."
        "_STAGE_REACHED_CANONICAL -- update both together"
    )
    raise AssertionError(_drift_msg)


_StagePosition = Literal["bypass", "earlier", "same", "later", "unresolvable"]


def _classify_sentinel_stage_position(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> tuple[_StagePosition, list[Stage] | None, int | None]:
    """Classify the sentinel's mapped stage relative to ``task.stage`` (#1149).

    Returns ``(position, stages, target_idx)``. ``stages`` and ``target_idx``
    are populated only for the ``"later"`` case (the pipeline stage list and
    the walk's destination index); all other positions return ``(pos, None,
    None)``.

    - ``"bypass"``       -- no ``stage_reached`` to check (e.g. a
      ``BlockedResult``-derived payload). Routing proceeds exactly as before
      #1019.
    - ``"earlier"``      -- the sentinel's stage precedes ``task.stage``: a
      late/replayed sentinel from a previous leg (the #986 incident). Refuse.
    - ``"same"``         -- exact match. Routes normally via the Rule 1-6 table,
      exactly as the pre-#1149 equality guard did.
    - ``"later"``        -- the sentinel's stage follows ``task.stage``: a
      legitimate self-escalation the row has not yet caught up to. Walk forward.
    - ``"unresolvable"`` -- a non-str ``stage_reached``, an unmapped value, an
      unknown client, or a stage absent from the client's pipeline. Fail-closed
      refuse, matching the pre-#1149 equality check's behavior for these cases.

    Position is computed via ``pipeline.stages.index`` (R2) -- never the
    ``Stage`` StrEnum's own ordering, which is alphabetical and unrelated to
    pipeline order. See ``_STAGE_REACHED_TO_STAGE`` for the
    HARDEN-always-mismatches rationale.
    """
    stage_reached = (
        last_result.get("stage_reached") if isinstance(last_result, dict) else None
    )
    if stage_reached is None:
        return "bypass", None, None
    mapped = (
        _STAGE_REACHED_TO_STAGE.get(stage_reached)
        if isinstance(stage_reached, str)
        else None
    )
    if mapped is None:
        return "unresolvable", None, None
    if mapped == task.stage:
        # Exact match routes regardless of client/pipeline resolvability --
        # preserves the pre-#1149 equality guard, which never consulted clients.
        return "same", None, None
    # A non-matching stage needs the pipeline order to decide earlier vs later.
    client_cfg = clients.get(task.client)
    stages = client_cfg.pipeline.stages if client_cfg is not None else None
    if stages is None or task.stage not in stages or mapped not in stages:
        return "unresolvable", None, None
    sentinel_idx = stages.index(mapped)
    if sentinel_idx < stages.index(task.stage):
        return "earlier", None, None
    return "later", stages, sentinel_idx


def _walk_stage_pointer_forward(
    task: TicketTask,
    stages: list[Stage],
    target_idx: int,
    clients: dict[str, ClientConfig],
) -> Literal["proceed", "parked"]:
    """Walk ``task.stage`` forward to ``stages[target_idx]``, one rung at a time.

    Each rung advances through ``_advance_task_pointer`` (the shared
    ``TASK_STAGE_CHANGED`` chokepoint, dev_queue.py), so every real stage move
    emits exactly one event. Before crossing a REVIEW rung, the operator-signoff
    gate is checked -- if it applies, the walk stops at REVIEW and parks the
    task ``AWAITING_OPERATOR_SIGNOFF`` (signoff is the ship checkpoint,
    REVIEW->FINALIZE; RFC 0007 Phase 3, #990).

    ``_advance_task_pointer`` unconditionally clears ``task.session_id`` on
    every hop ("R6: clear session_id on advance"). That is correct for a genuine
    single-hop advance, but a multi-hop walk must not blank the id before the
    landing Rule 1-6 body reads it for its ``SESSION_NEEDS_ATTENTION`` event --
    so the real id is captured once and restored after each hop. The landing
    Rule's own genuine advance (Rule 3) still clears it, exactly as pre-#1149
    single-hop behavior. See GitHub #1149 (plan-review MUST_FIX #1).
    """
    original_session_id = task.session_id
    while stages.index(task.stage) < target_idx:
        if task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
            transition_task_status(
                task,
                QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
                disposition=SIGNOFF_GATE_DISPOSITION,
            )
            return "parked"
        _advance_task_pointer(task, stages)
        task.session_id = original_session_id
    return "proceed"


def _resolve_stage_walk(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> Literal["refuse", "proceed", "parked"]:
    """Decide how a sentinel's stage position routes against ``task.stage`` (#1149).

    Earlier-stage replays and unresolvable positions refuse (fail-closed, the
    #1019/#986 guard, preserved); same-stage and bypass proceed to the ordinary
    Rule 1-6 table (unchanged); a later-stage sentinel walks ``task.stage``
    forward to the sentinel's stage via ``_walk_stage_pointer_forward``, then
    proceeds (or parks at a REVIEW signoff gate). The walk mutates ``task.stage``
    in place as a side effect -- the caller then applies the Rule 1-6 table at
    the now-matching stage.
    """
    position, stages, target_idx = _classify_sentinel_stage_position(
        task, last_result, clients
    )
    if position == "later" and stages is not None and target_idx is not None:
        return _walk_stage_pointer_forward(task, stages, target_idx, clients)
    if position in ("earlier", "unresolvable"):
        return "refuse"
    return "proceed"


def apply_staged_decision(
    task: TicketTask,
    status: str | None,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> bool:
    """Apply the B2 staged advance decision to a RUNNING task.

    Thin RUNNING-asserting wrapper over ``_route_staged_decision`` (the shared
    assert-free routing core). The consume path (_apply_events_to_store) and the
    RUNNING arm of reconcile's emitted-sentinel router (_apply_sentinel_to_task)
    both call this; the #918 late-sentinel rescue path calls
    ``_route_staged_decision`` directly for a parked task. Precondition:
    task.status is RUNNING. Mutates task in place. Returns ``_route_staged_
    decision``'s bool: whether the sentinel was routed (``False`` iff refused
    by the stage-mismatch guard, #1019).
    """
    if task.status != QueueItemStatus.RUNNING:
        msg = f"apply_staged_decision: expected RUNNING, got {task.status!r}"
        raise AssertionError(msg)
    return _route_staged_decision(task, status, last_result, clients)


def _route_scope_gated_approval(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    last_result: dict[str, object] | None,
    disposition: str | None,
    pr_url: str | None,
) -> None:
    """Rule 1 body: scope-gated approval -- small tier auto-advances, large blocks.

    Small tier additionally checks the operator-signoff gate before advancing,
    but ONLY at Stage.REVIEW -- signoff is the ship checkpoint (REVIEW->FINALIZE),
    not a per-stage checkpoint, so a small-tier `plan_pending_approval` at
    Stage.PLAN must advance unattended exactly as it did before #990. Mirrors
    ``_route_stage_success``'s identical REVIEW-scoping. Tier resolution is
    escalate-only -- see ``_resolve_scope_tier`` docstring (#696, #926).
    Extracted from ``_route_staged_decision`` to keep that function under the
    PLR0912 branch ceiling.
    """
    tier = _resolve_scope_tier(last_result, task)
    if tier != SCOPE_TIER_SMALL:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _APPROVAL_GATE_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=disposition
        )
        return
    if task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
        # Why: the operator-signoff gate takes precedence over the small-tier
        # auto-advance -- the ticket parks for an explicit operator approval
        # before continuing the pipeline, rather than advancing unattended
        # (RFC 0007 Phase 3, #990). REVIEW-scoped for the same reason as
        # _route_stage_success: signoff is the ship checkpoint only.
        transition_task_status(
            task,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            disposition=SIGNOFF_GATE_DISPOSITION,
        )
    else:
        _stage_advance_unchecked(task, clients, disposition=disposition, pr_url=pr_url)


def _route_stage_success(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    disposition: str | None,
    pr_url: str | None,
) -> None:
    """Rule 3 body: shipped/stage_complete -- advance or complete.

    Why REVIEW-scoped: STAGE_SUCCESS_STATUSES fires at every pipeline stage as
    the ordinary staged-advance signal (each of HARDEN/PLAN/IMPL/REVIEW's
    "stage_complete", plus terminal "shipped"); gating every one of those
    would pause the ticket at every stage. Signoff is the ship checkpoint
    only -- the REVIEW->FINALIZE transition -- so the gate applies only when
    task.stage is REVIEW. This relies on an unenforced producer contract that
    only REVIEW's advance represents "ready to ship"; dispatch does not
    otherwise verify it (RFC 0007 Phase 3, #990). Extracted from
    ``_route_staged_decision`` to keep that function under the PLR0912
    branch ceiling.
    """
    if task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
        transition_task_status(
            task,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            disposition=SIGNOFF_GATE_DISPOSITION,
        )
    else:
        _stage_advance_unchecked(task, clients, disposition=disposition, pr_url=pr_url)


def _route_staged_decision(
    task: TicketTask,
    status: str | None,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> bool:
    """Shared assert-free core of the B2 staged advance decision table.

    The single advance authority shared by the consume path
    (_apply_events_to_store, via ``apply_staged_decision``) and reconcile's
    emitted-sentinel router (_apply_sentinel_to_task), so staged dispatch
    advances regardless of which path observes the completion first (#698) and
    a late-sentinel rescue lands in exactly the state its live counterpart would
    (#918). No status precondition — callers gate as needed. Mutates in place.

    First classifies the sentinel's ``stage_reached`` against ``task.stage`` by
    pipeline position (``_resolve_stage_walk``, GitHub #1149, extending #1019).
    An *earlier*-stage or unresolvable sentinel (a late/replayed sentinel from a
    previous leg, the #986 incident) is refused: a true no-op -- no status
    transition, no ``save_dev_queue`` by callers that gate on the return value
    -- and ``SENTINEL_STAGE_MISMATCH`` is emitted for observability. A *later*-
    stage sentinel (a legitimate self-escalation the row lags behind) walks
    ``task.stage`` forward one rung at a time to the sentinel's stage, then the
    Rule 1-6 table applies at the now-matching stage; if a REVIEW signoff gate
    intervenes the walk parks the task and returns without applying the table.
    Same-stage and no-``stage_reached`` sentinels route through Rule 1-6 exactly
    as before. Returns ``False`` on refusal, ``True`` for every routed path.
    """
    walk_outcome = _resolve_stage_walk(task, last_result, clients)
    if walk_outcome == "refuse":
        stage_reached = (
            last_result.get("stage_reached") if isinstance(last_result, dict) else None
        )
        record_event(
            OrchestratorEventType.SENTINEL_STAGE_MISMATCH,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "session_id": task.session_id,
                "expected_stage": task.stage,
                "sentinel_stage_reached": stage_reached,
            },
            correlation_id=task.ticket_id,
        )
        return False
    _persist_carried_context(task, last_result)
    if walk_outcome == "parked":
        # A later-stage sentinel that stopped at a REVIEW signoff gate: the task
        # is already parked AWAITING_OPERATOR_SIGNOFF by the walk. Do not apply
        # the Rule 1-6 status table (the sentinel's status was never observed at
        # this stage). Routed, so callers persist the parked state (#1149).
        return True
    disposition = _derive_disposition(status)
    pr_url = _extract_pr_url(last_result)
    if status in SCOPE_GATED_APPROVAL_STATUSES:
        # Rule 1: scope-gated approval; small tier auto-advances, large blocks.
        # Must fire before Rule 2 (SCOPE_GATED ⊂ PAUSED_FOR_USER_INPUT).
        _route_scope_gated_approval(task, clients, last_result, disposition, pr_url)
    elif status in PAUSED_FOR_USER_INPUT_STATUSES:
        # Rule 2: pure pause (v4 statuses: ambiguities_pending_resolution,
        # premises_pending_verification). Scope-gated statuses caught by Rule 1.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _PLAN_PARKED_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=disposition
        )
    elif status in STAGE_SUCCESS_STATUSES:
        # Rule 3: shipped -- advance or complete (REVIEW-scoped signoff gate;
        # see _route_stage_success docstring).
        _route_stage_success(task, clients, disposition, pr_url)
    elif status == "merge_pending":
        # Rule 3b: PR created but awaiting CI/merge gate (#899). Not a failure
        # — preserve pr_url so operator can monitor/merge. Do not re-dispatch.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": "merge_pending",
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            pr_url=pr_url,
        )
    elif status == "no_op":
        # Rule 4: pre-flight already satisfied -- terminal
        # regardless of remaining stages
        transition_task_status(task, QueueItemStatus.COMPLETED, disposition="no_op")
    elif status in STAGE_FAILURE_STATUSES:
        # Rule 5: blocked/merge_gate_blocked/scope_exceeded/forbidden_area
        # Sub-rule 5a: "blocked" at FINALIZE with a regress-eligible blocker
        # reason and attempts below cap → regress to IMPL for self-heal (#770).
        # scope_exceeded/forbidden_area/merge_gate_blocked have no blocker field
        # (validator enforces this) so they always fall through to BLOCKED_ON_USER.
        blocker = last_result.get("blocker") if isinstance(last_result, dict) else None
        blocker_reason = blocker.get("reason") if isinstance(blocker, dict) else None
        if (
            status == "blocked"
            and task.stage == Stage.FINALIZE
            and blocker_reason in FINALIZE_REGRESS_BLOCKER_REASONS
            and task.regress_attempts < FINALIZE_REGRESS_CAP
        ):
            _log.info(
                "dispatch: finalize gate blocked (%r) — regressing %r to IMPL"
                " (regress attempt %d/%d)",
                blocker_reason,
                task.ticket_id,
                task.regress_attempts + 1,
                FINALIZE_REGRESS_CAP,
            )
            _stage_regress(task, Stage.IMPL)
            record_event(
                OrchestratorEventType.TICKET_REQUEUED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "from_stage": Stage.FINALIZE,
                    "to_stage": Stage.IMPL,
                    "reason": "finalize_regress",
                    "blocker_reason": blocker_reason,
                    "regress_attempt": task.regress_attempts,
                },
            )
            return True
        breadcrumbs = str(blocker_reason) if blocker_reason is not None else ""
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": (
                    _AWAITING_OPERATOR_REASON
                    if blocker_reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
                    else status
                ),
                "breadcrumbs": breadcrumbs,
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=disposition
        )
    else:
        # Rule 6: None/not dict/missing status -- conservative fallback
        # Why: unparseable sentinel must never silently advance/complete
        # (B2 correctness requirement). Changes pre-B2 behavior which
        # fell through to COMPLETED.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": "blocked",
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition="abandoned"
        )
    return True


def _apply_events_to_store(
    store: DevQueueStore,
    events: list[OrchestratorEvent],
    clients: dict[str, ClientConfig],
) -> int:
    """Apply SESSION_COMPLETED events to an already-loaded DevQueueStore.

    Caller must hold ``dev_queue_lock``. Saves the store when tasks were
    transitioned; does NOT advance the event cursor — cursor advancement
    is the caller's responsibility after the lock is released.

    Returns the number of tasks transitioned to COMPLETED.
    """
    completed = 0
    for event in events:
        # Crashed events are emitted by reconcile only. For DAEMON
        # sessions reconcile has already reverted the task
        # RUNNING → PENDING; marking the task COMPLETED here would
        # shadow that revert and (worse) match the next freshly-
        # respawned RUNNING task for the same ticket_id, falsely
        # retiring a still-running session. For non-DAEMON crashed
        # sessions reconcile does not touch the queue, so a blanket
        # skip is conservative-safe (no queue task is expected to
        # match anyway). See GitHub issue #97.
        if event.payload.get("crashed"):
            continue
        ticket_id = event.payload.get("ticket_id")
        if not ticket_id:
            # Fallback: recover ticket_id from the session_name for events
            # produced before the reconciler emitted ticket_id explicitly.
            # Drains historical RUNNING tasks whose completion events
            # predate the producer-side fix.
            session_name = event.payload.get("session_name")
            if isinstance(session_name, str):
                ticket_id = ticket_id_for_session(session_name)
        if not ticket_id:
            continue
        event_session_id = event.payload.get("session_id")
        for task in store.tasks:
            if task.ticket_id != ticket_id:
                continue
            # Why: reconcile may have already set this task to BLOCKED_ON_USER
            # (salvaged paused-status session). The task is no longer RUNNING,
            # so this event is harmlessly skipped — overwriting with COMPLETED
            # would shadow BLOCKED_ON_USER, which downstream operators need.
            if task.status != QueueItemStatus.RUNNING:
                continue
            # Disambiguate stale events: when the event carries a
            # session_id and the task has been stamped with one, they
            # must agree. Either side missing a session_id falls back to
            # ticket_id-only matching for backward compatibility with
            # legacy tasks/events that predate the field.
            if (
                isinstance(event_session_id, str)
                and task.session_id is not None
                and task.session_id != event_session_id
            ):
                continue
            state = load_state()
            session = next(
                (s for s in state.sessions if s.id == event_session_id),
                None,
            )
            last_result = (
                session.last_result
                if session is not None and isinstance(session.last_result, dict)
                else None
            )
            status = last_result.get("status") if last_result is not None else None

            # #1019: a stage-mismatch refusal is a true no-op (Pre-flight
            # Resolution #4) -- skip cost accumulation and the completed
            # count so a refused/stale sentinel doesn't mutate task state
            # or trigger save_dev_queue below.
            if apply_staged_decision(task, status, last_result, clients):
                sid = event_session_id if isinstance(event_session_id, str) else None
                _accumulate_task_cost(task, sid)
                completed += 1
            break
    if completed:
        save_dev_queue(store)
    return completed


def consume_completed_sessions() -> int:
    """Process session.completed events from the dispatch inbox.

    Reads new SESSION_COMPLETED events from the inbox since the last cursor
    position for the "dispatch" consumer. For each event that carries a
    ``ticket_id`` in its payload, the corresponding TicketTask (if found in
    RUNNING state) is marked COMPLETED.

    Advances the cursor after processing.

    Returns:
        Number of TicketTasks transitioned to COMPLETED.
    """
    events = read_events(
        consumer=_DISPATCH_CONSUMER,
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    if not events:
        return 0

    # Persist sentinel-block summaries on Sessions BEFORE the advance
    # decision, so _apply_events_to_store reads each just-completed session's
    # last_result (status + scope.tier) instead of a stale/None value. Without
    # this ordering, a freshly-completed stage has last_result=None at decision
    # time → status=None → Rule 6 → BLOCKED_ON_USER, so the staged pipeline
    # never advances (#694). Producer side (worker stdout capture) is gated on
    # the orchestrator P1.A wiring; this consumer is forward-compatible with
    # events that lack a ``stdout`` payload (such an event leaves last_result
    # unset → the conservative-safe BLOCKED_ON_USER default).
    for event in events:
        session_id = event.payload.get("session_id")
        stdout = event.payload.get("stdout")
        if isinstance(session_id, str) and isinstance(stdout, str):
            persist_last_result(session_id, stdout)

    # Gap class (#867, severity=low): persist_last_result writes sessions.json
    # under sessions_lock; the dev-queue mutation below is a separate file.
    # A crash here leaves last_result updated but the task RUNNING.  The
    # un-advanced cursor causes self-healing reprocessing on the next tick.
    with dev_queue_lock():
        store = load_dev_queue()
        clients = load_effective_clients()
        completed = _apply_events_to_store(store, events, clients=clients)
        # Advance cursor inside the dev-queue lock so the cursor never
        # moves past events whose queue mutations haven't been persisted yet.
        advance_cursor(_DISPATCH_CONSUMER, events[-1].id)

    return completed


def persist_last_result(session_id: str, stdout: str) -> bool:
    """Parse *stdout* and write the result onto the matching Session.

    Returns True if a session was updated, False if no match or if parsing
    yielded nothing actionable. Never raises — parser failures surface as
    a synthetic blocker dict on ``Session.last_result`` so post-hoc
    inspection still has something to look at.
    """
    parsed = parse_stdout(stdout)
    with sessions_lock():
        state = load_state()
        target = None
        for session in state.sessions:
            if session.id == session_id:
                target = session
                break
        if target is None:
            _log.warning(
                "persist_last_result: session %s not found in state",
                session_id,
            )
            return False
        if isinstance(parsed, AutoDevResult):
            target.last_result = parsed.model_dump(mode="json")
        else:
            target.last_result = parsed.model_dump(mode="json")
        save_state(state)
    return True


TICK_STALE_SECONDS = 90  # 3x default tick_interval_seconds=30


def run_dispatch_loop(
    *,
    max_parallel: int | None = None,
    once: bool = False,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    auto_ff: bool = True,
    client: str | None = None,
) -> None:
    """Run the dispatch loop, optionally overriding per-client concurrency caps.

    Args:
        max_parallel: If set, override all per-client caps with this value.
        once: If True, run a single tick and return immediately.
        use_plan: If True, load the persisted DispatchPlan and use its
            ordering to claim tasks.  Falls back to enqueue order when no
            plan is found (load_plan returns None).
        parent: Optional orchestrator session ID. Threaded into each
            dispatch tick so spawned workers are linked back to the
            caller's session.
        native_daemon: Optional NativeDaemonClient for testing. Defaults
            to ``get_native_daemon_client()`` at call time. Used for
            spawning dispatched workers.
        emit: Optional callable for operator-facing stdout lines.
            When None, all human-readable output is suppressed (quiet
            mode for cron/scripted use).  Typically ``click.echo`` in
            CLI context.
        auto_ff: Passed through to each ``dispatch_tick`` call.  When
            True (default), stale-but-behind repos are fast-forwarded
            automatically. Pass False to restore legacy block-only
            behavior (``--no-auto-ff`` CLI flag).
        client: When set, scope each tick to this single client's queue.
            Validated at the CLI boundary before this function is called.
    """
    config = load_effective_config()

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()
    # Track fetch-fail-warn deduplication for persistently unreachable remotes.
    warned_fetch_fail: set[str] = set()
    # Track wave-collision pairs already warned; prevents duplicate events
    # for long-running in-flight task pairs across multiple ticks (#784).
    warned_collision: set[frozenset[str]] = set()
    # Back-off window: loaded from the persisted sidecar so a loop restart after
    # a code merge honours an active backoff rather than re-opening the spawn gate
    # immediately (#804).
    usage_limited_until: datetime | None = load_usage_limited_until()

    try:
        while True:
            try:
                config = load_effective_config()
                if max_parallel is not None:
                    clients = load_clients()
                    overridden = dict.fromkeys(clients, max_parallel)
                    config = config.model_copy(
                        update={"per_client_ceiling": overridden}
                    )
            except (yaml.YAMLError, ConfigValidationError):
                _log.warning("dispatch: config reload failed; using last-good config")

            consume_completed_sessions()
            try:
                hydrate_pr_states(config)
            except Exception:  # noqa: BLE001
                # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
                # 1. hydrate_pr_states shells out to ``gh pr view`` — failure
                #    modes include subprocess crash, JSON decode, lock I/O.
                # 2. Logging: _log.exception captures the full traceback.
                # 3. Non-critical: PR-state hydration is best-effort fallback
                #    polling (#929); skipping a pass just delays state refresh.
                # 4. Paired test: tests/test_dispatch.py
                #    TestRunDispatchLoopHydrationHook.
                _log.exception("pr-state hydration failed during tick; continuing")
            try:
                _installed = importlib.metadata.version(_CW_PACKAGE_NAME)
            except importlib.metadata.PackageNotFoundError:
                _installed = "0.0.0+unknown"
            if _installed != _LOADED_VERSION:
                _log.warning(
                    "dispatch: version drift detected (loaded=%s, installed=%s)"
                    " — exiting for reload",
                    _LOADED_VERSION,
                    _installed,
                )
                msg = "version drift detected; exiting for reload"
                raise VersionDriftError(msg)
            result = dispatch_tick(
                config,
                use_plan=use_plan,
                parent=parent,
                native_daemon=resolved_native_daemon,
                emit=emit,
                warned_stale=warned_stale,
                warned_fetch_fail=warned_fetch_fail,
                warned_collision=warned_collision,
                usage_limited_until=usage_limited_until,
                auto_ff=auto_ff,
                client_filter=client,
            )

            if result.usage_limit_detected and not once:
                usage_limited_until = datetime.now(UTC) + timedelta(
                    seconds=config.usage_limit_backoff_seconds
                )
                save_usage_limited_until(usage_limited_until)
                _log.warning(
                    "dispatch: usage limit detected; backing off until %s",
                    usage_limited_until,
                )

            if once:
                return

            time.sleep(config.tick_interval_seconds)
    finally:
        _exc = sys.exc_info()[1]
        _normal = _exc is None or isinstance(_exc, KeyboardInterrupt)
        _payload: dict[str, object] = {
            "normal": _normal,
            "exception_type": None if _normal else type(_exc).__name__,
        }
        with contextlib.suppress(Exception):
            if isinstance(_exc, VersionDriftError):
                _payload["reason"] = "version_drift"
                _payload["loaded_version"] = _LOADED_VERSION
                _payload["installed_version"] = importlib.metadata.version(
                    _CW_PACKAGE_NAME
                )
            record_event(
                OrchestratorEventType.DISPATCH_LOOP_EXITED,
                payload=_payload,
            )
