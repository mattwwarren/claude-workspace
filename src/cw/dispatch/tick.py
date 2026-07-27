"""One dispatch tick: preflight gates, per-client snapshot, and lane dispatch.

Part of the ``cw.dispatch`` package split (#1311, part 2): the ``dispatch_tick``
orchestration and its per-tick helpers, carved out of ``_legacy`` so the tick
path and the event loop live in separate submodules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.collision import detect_wave_collisions
from cw.config import (
    load_effective_clients,
    load_state,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    load_plan,
)
from cw.executor_diagnostics import cleanup_expired_diagnostics
from cw.models import (
    OCCUPIED_LANE_STATUSES,
    ClientConfig,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.dispatch.lanes import _ClientDispatchResult
    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        LaneConfig,
        OrchestratorConfig,
    )
    from cw.native_daemon import NativeDaemonClient
from cw.dispatch.gating import (
    _emit_availability_skip,
    _emit_ssh_key_bypass,
    _emit_ssh_key_skip,
    _emit_stale_skip,
    _emit_usage_limit_skip_events,
    _reconcile_usage_limited,
    _resolve_availability_once,
    _resolve_freshness,
    _resolve_ssh_key_once,
)
from cw.dispatch.host_capacity import HostCapacityContext, resolve_host_capacity
from cw.dispatch.lanes import (
    _dispatch_client_lanes,
    _record_client_freshness_block,
    _reset_client_freshness_blocks,
)

_log = logging.getLogger("cw.dispatch")


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
    except Exception:  # noqa: BLE001 — best-effort retention sweep must never abort the tick; see docstring
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


def _resolve_host_budget_snapshot(
    state: CwState, queue: DevQueueStore, config: OrchestratorConfig
) -> HostCapacityContext:
    """Resolve this tick's host-capacity numbers, once, before the client loop (#1444).

    ``.remaining`` is ``None`` (feature off, no-op fold downstream) when
    ``config.host_session_budget`` is unset; otherwise ``budget - running``,
    floor unclamped (a negative value simply means every client is
    host-capacity-gated this tick). Extracted from :func:`dispatch_tick` to
    keep it within the PLR0912/PLR0915 ceilings (CLAUDE.md).
    """
    host_running, host_budget = resolve_host_capacity(state, queue, config)
    host_slots_remaining = None if host_budget is None else host_budget - host_running
    return HostCapacityContext(
        running=host_running, budget=host_budget, remaining=host_slots_remaining
    )


def _dispatch_client_with_host_budget(
    client: ClientConfig,
    effective_lanes: list[LaneConfig],
    queue_snapshot: DevQueueStore,
    *,
    running_count: int,
    client_ceiling: int,
    cap: int,
    pending_count: int,
    cap_full: bool,
    running_by_lane: dict[str, int],
    priority_ids: list[str] | None,
    config: OrchestratorConfig,
    resolved_native_daemon: NativeDaemonClient,
    parent: str | None,
    emit: Callable[[str], None] | None,
    usage_limited_until: datetime | None,
    host_capacity: HostCapacityContext,
) -> tuple[_ClientDispatchResult, HostCapacityContext]:
    """Wrap :func:`_dispatch_client_lanes` with the host-capacity gate (#1444).

    Delegates to :func:`_dispatch_client_lanes` with the given *host_capacity*
    snapshot, then returns its own remaining budget decremented by this
    client's spawn count -- extracted to keep :func:`dispatch_tick`'s client
    loop within the PLR0912/PLR0915 ceilings (CLAUDE.md).
    """
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
        usage_limited_until=usage_limited_until,
        host_capacity=host_capacity,
    )
    return client_result, host_capacity.decremented(client_result.spawned)


def _resolve_client_lane_context(
    client: ClientConfig, queue_snapshot: DevQueueStore, client_ceiling: int
) -> tuple[dict[str, int], list[LaneConfig]]:
    """Build a client's per-lane running count and effective lane list for this tick.

    Returns ``(running_by_lane, effective_lanes)``. Tasks in RUNNING,
    BLOCKED_ON_USER, or AWAITING_OPERATOR_SIGNOFF with an active session_id
    count toward their lane's cap (ADR-0006: BLOCKED_ON_USER occupies the
    slot; #990 extends this to a signoff-parked ticket, which is likewise not
    eligible for re-dispatch). Reuses the *queue_snapshot* the caller already
    holds — nothing between the two points mutates the queue (auto-ff is
    git-only). For clients with no declared lanes, the synthesized default
    lane's ``max_parallel`` is overridden with *client_ceiling* so
    backward-compat behaviour is preserved. Extracted from
    :func:`dispatch_tick` to keep it within the PLR0912/PLR0915 ceilings
    (CLAUDE.md).
    """
    running_by_lane: dict[str, int] = {}
    for qt in queue_snapshot.tasks:
        if qt.client != client.name:
            continue
        if qt.status not in OCCUPIED_LANE_STATUSES:
            continue
        lane_key = qt.lane
        running_by_lane[lane_key] = running_by_lane.get(lane_key, 0) + 1

    effective_lanes = client.effective_lanes
    if not client.lanes:
        effective_lanes = [
            effective_lanes[0].model_copy(update={"max_parallel": client_ceiling})
        ]
    return running_by_lane, effective_lanes


class _PreflightGateResult(NamedTuple):
    """Resolved verdicts from :func:`_run_preflight_gates`.

    ``available`` and ``gated`` are both plain ``bool``s naming distinct
    concepts (gh-availability verdict, whether to skip this client) — named
    fields (vs. a bare positional tuple) prevent a transposition mypy can't
    catch (e.g. swapping ``gated`` and ``available``) while still unpacking
    positionally at the call site like a plain tuple, same rationale as
    :class:`_ClientTickSnapshot`.
    """

    available: bool
    ssh_key_available: bool | None
    gated: bool


def _run_preflight_gates(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    available: bool | None,
    ssh_key_available: bool | None,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_ssh_key: set[str] | None,
    ssh_key_gate_enabled: bool = True,
) -> _PreflightGateResult:
    """Resolve + apply the availability and SSH-key preflight gates, in order.

    Combines both independent per-client pre-claim gates (fleet-wide
    gh-availability, then SSH-agent-key (#927)) behind a single caller-side
    branch: :func:`dispatch_tick`'s client loop keeps one `if gated: continue`
    for both instead of one per gate, keeping ``dispatch_tick`` under the
    PLR0912/PLR0915 ceilings (CLAUDE.md) as this second gate is added.

    Returns the resolved (memoized) verdicts to thread back into the caller's
    loop-scoped variables, and whether the client should be skipped this tick
    (its own dispatch.tick skip event has already been emitted when
    ``gated`` is True). ``ssh_key_available`` is returned unresolved
    (possibly still ``None``) when gated on the availability check first,
    since the SSH-key probe short-circuits and is never reached in that
    case.

    ``ssh_key_gate_enabled`` (GitHub #1437) is the operator escape hatch: the
    probe always runs unconditionally (needed either way to compute the
    resolved verdict threaded back to the caller, and to populate the bypass
    event's ``probe_result``), but when the probe reports unavailable and
    this is False, the would-be skip is suppressed -- a bypass event is
    recorded instead of the skip, and the client proceeds (``gated=False``).
    Default True reproduces pre-#1437 behavior exactly.
    """
    resolved_available = _resolve_availability_once(available)
    if not resolved_available:
        _emit_availability_skip(
            client,
            queue_snapshot,
            pending_count=pending_count,
            running_count=running_count,
            cap=cap,
        )
        return _PreflightGateResult(resolved_available, ssh_key_available, True)

    resolved_ssh_key_available = _resolve_ssh_key_once(ssh_key_available)
    if not resolved_ssh_key_available:
        if not ssh_key_gate_enabled:
            _emit_ssh_key_bypass(
                client,
                probe_result=resolved_ssh_key_available,
                gate_enabled=ssh_key_gate_enabled,
            )
            return _PreflightGateResult(
                resolved_available, resolved_ssh_key_available, False
            )
        _emit_ssh_key_skip(
            client,
            queue_snapshot,
            pending_count=pending_count,
            running_count=running_count,
            cap=cap,
            emit=emit,
            warned_ssh_key=warned_ssh_key,
        )
        return _PreflightGateResult(
            resolved_available, resolved_ssh_key_available, True
        )

    return _PreflightGateResult(resolved_available, resolved_ssh_key_available, False)


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
    warned_ssh_key: set[str] | None = None,
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
        warned_ssh_key: Mutable set used to deduplicate the SSH-key-gate
            operator error line across ticks within this dispatcher run
            (fleet-wide, keyed on a single sentinel -- see
            ``_SSH_KEY_WARN_SENTINEL``). Caller owns the set; mutated
            in-place.
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
    # SSH-agent-key preflight verdict (#927), memoized the same way as
    # ``available`` -- see the SSH-key gate below.
    ssh_key_available: bool | None = None

    # Host-capacity admission budget (#1444): resolved once per tick,
    # client-filter-independent, against the fleet-wide state/queue snapshots
    # already loaded above (``collision_snapshot`` is an unfiltered
    # DevQueueStore load taken before this loop -- reused here rather than a
    # second dev-queue load). host_capacity.remaining is decremented by each
    # client's spawn count as the loop proceeds so a later-declared client
    # sees the budget already consumed by an earlier-declared one this same
    # tick (R3/R5: fleet-wide, declaration order preserved).
    host_capacity = _resolve_host_budget_snapshot(state, collision_snapshot, config)
    for client in clients.values():
        if (
            config.max_parallel_clients is not None
            and dispatched_client_count >= config.max_parallel_clients
        ):
            break

        # Numeric fields (running, cap, queue snapshot, pending) hoisted above
        # both preflight gates so a dispatch.tick skip event for any of them
        # carries all four. See _client_tick_snapshot.
        running_count, client_ceiling, queue_snapshot, pending_count = (
            _client_tick_snapshot(client, state=state, config=config)
        )
        # Keep legacy cap alias for skip-event and back-off event payloads.
        cap = client_ceiling

        # --- Preflight gates --- highest precedence, run in order:
        # (1) fleet-wide gh-availability (RFC 0011 A5): a TTL-cached `gh auth
        # status` probe, resolved at most ONCE per tick, not once per client
        # — the cached verdict is identical for every client in this loop,
        # so a per-client call would re-read the shared sidecar file N times
        # for the same answer. (2) SSH-agent-key (#927): a session spawned
        # without an unlocked SSH key cannot push, so this holds every
        # client PENDING rather than burn a slot on a guaranteed failure.
        # Both are memoized (not hoisted above the loop) so they're only
        # resolved once the loop body actually runs for at least one client
        # — an empty ``clients`` dict or a fully-paused fleet
        # (``max_parallel_clients=0``, which breaks on the first iteration
        # above) never probes or pages, matching the pre-#1157 invariant
        # that these checks only fire when there's dispatch work they could
        # gate. Checked before the per-client freshness gate so that during
        # a real outage no client pays the freshness git-fetch cost for a
        # verdict that gets discarded anyway. The availability gate fails
        # open on any resolution error (same posture as _resolve_freshness);
        # the SSH-key gate fails CLOSED on any probe error (missing
        # ssh-add binary, OSError, timeout -- see check_ssh_key_available),
        # holding the fleet PENDING rather than risking a guaranteed-failing
        # spawn. Combined into one helper (see _run_preflight_gates) so this
        # loop keeps a single branch for both gates (PLR0912/PLR0915).
        #
        # Fleet-wide availability outage: hold every client PENDING (no
        # claim, no attempts consumed). Composition pin (RFC 0011 A5): on
        # this gated path NEITHER _record_client_freshness_block NOR
        # _reset_client_freshness_blocks runs — the freshness counter stays
        # frozen during an outage, it must not reset. Same posture applies
        # to the SSH-key gate.
        available, ssh_key_available, gated = _run_preflight_gates(
            client,
            queue_snapshot,
            available=available,
            ssh_key_available=ssh_key_available,
            pending_count=pending_count,
            running_count=running_count,
            cap=cap,
            emit=emit,
            warned_ssh_key=warned_ssh_key,
            ssh_key_gate_enabled=config.ssh_key_gate_enabled,
        )
        if gated:
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

        running_by_lane, effective_lanes = _resolve_client_lane_context(
            client, queue_snapshot, client_ceiling
        )

        client_result, host_capacity = _dispatch_client_with_host_budget(
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
            usage_limited_until=usage_limited_until,
            host_capacity=host_capacity,
        )
        spawned += client_result.spawned
        if client_result.usage_limit_detected:
            any_usage_limit_detected = True

    return DispatchTickResult(
        spawned=spawned, usage_limit_detected=any_usage_limit_detected
    )
