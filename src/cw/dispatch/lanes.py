"""Per-lane circuit breaker and per-client lane dispatch for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the lane circuit breaker,
the client-freshness-block latch, and the per-client lane walk."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    concurrency_override_lock,
)
from cw.dispatch.host_capacity import HostCapacityContext
from cw.events import record_event
from cw.models import (
    ClientConcurrencyOverride,
    ClientConfig,
    ConcurrencyOverrides,
    DispatchSkipReason,
    LaneConcurrencyOverride,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile import (
    _FRESHNESS_BLOCK_ESCALATED_REASON,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        LaneConfig,
        OrchestratorConfig,
    )
    from cw.native_daemon import NativeDaemonClient
from cw.dispatch.claim import (
    _claim_next_pending,
    _lane_occupants_for_client,
    _spawn_claimed_task,
)
from cw.dispatch.pr_gate import resolve_stale_pr_ticket_ids

_log = logging.getLogger("cw.dispatch")


# ``source`` field on a LANE_PAUSED event emitted by the per-lane circuit breaker
# (as opposed to an operator-initiated ``cw lane pause``). See GitHub issue #875.
_LANE_PAUSE_SOURCE_CIRCUIT_BREAKER = "circuit_breaker"


@dataclass(frozen=True)
class _ClientDispatchResult:
    """Aggregate outcome of dispatching one client's lanes for a tick.

    ``spawned`` — sessions started for the client this tick.
    ``usage_limit_detected`` — True if any lane hit a usage limit.
    """

    spawned: int = 0
    usage_limit_detected: bool = False


def _pending_in_lane(
    lane_cfg: LaneConfig, queue_snapshot: DevQueueStore, client_name: str
) -> int:
    """Count PENDING tasks for *client_name* in *lane_cfg* (#1630).

    Shared by :func:`_check_lane_circuit_paused` (breaker-pause detection)
    and :func:`_maybe_notify_lane_starved` (the starved-lane attention
    signal) so the two never drift onto separately-computed pending counts.
    """
    return sum(
        1
        for t in queue_snapshot.tasks
        if t.client == client_name
        and t.lane == lane_cfg.name
        and t.status == QueueItemStatus.PENDING
    )


def _check_lane_circuit_paused(
    lane_cfg: LaneConfig,
    queue_snapshot: DevQueueStore,
    client_name: str,
    *,
    overrides: ConcurrencyOverrides,
    config: OrchestratorConfig,
) -> bool:
    """Return True when a lane is paused *by the breaker* and has pending work.

    Distinguishes a circuit-breaker trip (consecutive_spawn_errors >= threshold)
    from a plain operator pause: only the former, with pending work still
    waiting, warrants the LANE_CIRCUIT_PAUSED skip_reason. See GitHub #875.

    ``overrides`` is loaded once per :func:`_dispatch_client_lanes` call (not
    once per paused lane) and passed in by the caller.
    """
    override = overrides.lanes.get(f"{client_name}/{lane_cfg.name}")
    if override is None:
        return False
    if override.consecutive_spawn_errors < config.lane_circuit_breaker_threshold:
        return False
    return _pending_in_lane(lane_cfg, queue_snapshot, client_name) > 0


def _maybe_notify_lane_starved(
    client_name: str,
    lane_name: str,
    pending_count: int,
    *,
    config: OrchestratorConfig,
) -> None:
    """Emit a recurring SESSION_NEEDS_ATTENTION for a starved circuit-paused lane.

    No-op when *pending_count* is 0 -- a genuinely idle paused lane must stay
    silent. Otherwise, under ``concurrency_override_lock()``: load the lane's
    override, check ``lane_starved_notify_next_eligible_at`` -- if unset or
    already elapsed, re-stamp it ``config.lane_starved_notify_interval_minutes``
    minutes into the future and save; if still in the future, short-circuit
    (no write, no emit). The event itself is emitted AFTER releasing the lock,
    mirroring :func:`_record_lane_spawn_error`'s lock-then-emit-after-release
    shape. Uses the canonical 9-field SESSION_NEEDS_ATTENTION payload (see
    ``claim.py::_emit_attempt_cap_attention_event``), with
    ``session_id=f"lane:{client_name}/{lane_name}@{now.isoformat()}"``
    standing in for the pre-spawn "no real session exists yet" situation this
    signal fires from. The firing instant is folded into ``session_id``
    (rather than the stable ``f"lane:{client}/{lane}"`` form) because
    ``_terminal_dedup_key`` (``cw.cli.queues``) keys on
    ``(event_type, session_id, paused_status)`` and both of the latter two
    are otherwise constant across every recurrence of the same lane --
    ``cw event tail --dedup-terminal`` (and ``_follow_loop``'s
    process-lifetime ``seen_terminal`` set) would collapse every recurrence
    after the first into a single shown event, silently reproducing this
    ticket's own bug one hop downstream (#1630 send-back, R2a). The debounce
    above already caps firings to one per interval, so a timestamped
    ``session_id`` cannot spam. ``client``/``lane`` remain explicit payload
    fields (R2b) so no consumer has to parse the synthetic id.
    See GitHub #1630.
    """
    if pending_count <= 0:
        return
    lane_key = f"{client_name}/{lane_name}"
    now = datetime.now(UTC)
    should_emit = False
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.lanes.get(lane_key, LaneConcurrencyOverride())
        next_eligible_at = override.lane_starved_notify_next_eligible_at
        if next_eligible_at is None or now >= next_eligible_at:
            should_emit = True
            new_next_eligible_at = now + timedelta(
                minutes=config.lane_starved_notify_interval_minutes
            )
            overrides.lanes[lane_key] = override.model_copy(
                update={"lane_starved_notify_next_eligible_at": new_next_eligible_at}
            )
            _save_concurrency_overrides(overrides)
    if should_emit:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": f"lane:{lane_key}@{now.isoformat()}",
                "session_name": "",
                "client": client_name,
                "ticket_id": None,
                "claude_session_id": None,
                "paused_status": DispatchSkipReason.LANE_CIRCUIT_PAUSED,
                "breadcrumbs": f"pending={pending_count}",
                "crashed": False,
                "lane": lane_name,
            },
            correlation_id=None,
        )


def _record_lane_spawn_error(
    lane_cfg: LaneConfig,
    client_name: str,
    last_error: str,
    *,
    config: OrchestratorConfig,
) -> None:
    """Increment the lane's spawn-error counter; trip the breaker at threshold.

    Under the override lock: load → increment → save.  When the post-increment
    count reaches ``lane_circuit_breaker_threshold`` the lane is paused and a
    circuit-breaker-sourced LANE_PAUSED event is emitted with the post-increment
    count and the (always-string) last error.  See GitHub #875.
    """
    lane_key = f"{client_name}/{lane_cfg.name}"
    tripped = False
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.lanes.get(lane_key, LaneConcurrencyOverride())
        count = override.consecutive_spawn_errors + 1
        updates: dict[str, object] = {"consecutive_spawn_errors": count}
        if count >= config.lane_circuit_breaker_threshold:
            updates["paused"] = True
            tripped = True
        overrides.lanes[lane_key] = override.model_copy(update=updates)
        _save_concurrency_overrides(overrides)
    if tripped:
        record_event(
            OrchestratorEventType.LANE_PAUSED,
            {
                "client": client_name,
                "lane": lane_cfg.name,
                "source": _LANE_PAUSE_SOURCE_CIRCUIT_BREAKER,
                "consecutive_count": count,
                "last_error": last_error,
            },
        )


def _reset_lane_spawn_errors(lane_cfg: LaneConfig, client_name: str) -> None:
    """Reset the lane's spawn-error counter to zero after a successful spawn.

    Short-circuits (no write) when the counter is already zero or the lane has
    no override, so a steady-state healthy lane never rewrites the override
    file.  Never clears ``paused`` — a breaker-paused lane resumes only via
    ``cw lane resume``.  See GitHub #875.
    """
    lane_key = f"{client_name}/{lane_cfg.name}"
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.lanes.get(lane_key)
        if override is None or override.consecutive_spawn_errors == 0:
            return
        overrides.lanes[lane_key] = override.model_copy(
            update={"consecutive_spawn_errors": 0}
        )
        _save_concurrency_overrides(overrides)


def _record_client_freshness_block(
    client_name: str,
    freshness_detail: str | None,
    *,
    config: OrchestratorConfig,
) -> None:
    """Increment the client's freshness-gate-block latch (RFC 0007 §W2).

    Under the override lock: load → increment → save. When the
    post-increment count reaches ``freshness_block_attention_threshold``
    (exact equality — latch semantics, no re-fire while still at/above the
    threshold on subsequent stale ticks) a ``session.needs_attention`` event
    is emitted AFTER releasing the lock, mirroring
    :func:`_record_lane_spawn_error`. No push notification (deliberate — see
    the gh_blocked/finalize_blocked precedent, neither of which pushes).
    """
    tripped = False
    count = 0
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.clients.get(client_name, ClientConcurrencyOverride())
        count = override.consecutive_freshness_blocks + 1
        overrides.clients[client_name] = override.model_copy(
            update={"consecutive_freshness_blocks": count}
        )
        _save_concurrency_overrides(overrides)
        if count == config.freshness_block_attention_threshold:
            tripped = True
    if tripped:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "",
                "session_name": "",
                "client": client_name,
                "ticket_id": None,
                "claude_session_id": None,
                "paused_status": _FRESHNESS_BLOCK_ESCALATED_REASON,
                "breadcrumbs": freshness_detail or "",
                "crashed": False,
            },
            correlation_id=None,
        )


def _reset_client_freshness_blocks(client_name: str) -> None:
    """Reset the client's freshness-gate-block counter to zero (RFC 0007 §W2).

    Short-circuits (no write) when the counter is already zero or the client
    has no override, mirroring :func:`_reset_lane_spawn_errors`.
    """
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.clients.get(client_name)
        if override is None or override.consecutive_freshness_blocks == 0:
            return
        overrides.clients[client_name] = override.model_copy(
            update={"consecutive_freshness_blocks": 0}
        )
        _save_concurrency_overrides(overrides)


def _apply_host_capacity_budget(
    available_client_slots: int, host_slots_remaining: int | None
) -> int:
    """Fold the fleet-wide host-capacity budget into a client's slot count (#1444).

    A no-op when the feature is off (``host_slots_remaining is None``). When
    set, this can only shrink the client's slot budget, never grow it -- R0
    (never reject/shed/kill running work; gate admission only) means excess
    PENDING work is simply left untouched for a later tick, not rejected.
    Extracted from :func:`_dispatch_client_lanes` to keep it within the
    PLR0912/PLR0915 ceilings (CLAUDE.md).
    """
    if host_slots_remaining is None:
        return available_client_slots
    return min(available_client_slots, host_slots_remaining)


_DEFAULT_HOST_CAPACITY = HostCapacityContext()


def _handle_paused_lane(
    lane_cfg: LaneConfig,
    queue_snapshot: DevQueueStore,
    client_name: str,
    *,
    overrides: ConcurrencyOverrides,
    config: OrchestratorConfig,
) -> bool:
    """Circuit-check a paused lane and fire its starved-attention notify (#1630).

    Extracted from :func:`_dispatch_client_lanes` to keep it within the
    PLR0912/PLR0915 ceilings (CLAUDE.md) -- mirrors
    :func:`_apply_host_capacity_budget`'s own extraction for the same reason.
    Returns whether this lane is a breaker-tripped pause with pending work
    (the caller ORs this into its running ``lane_circuit_paused`` flag).
    """
    lane_paused_here = _check_lane_circuit_paused(
        lane_cfg,
        queue_snapshot,
        client_name,
        overrides=overrides,
        config=config,
    )
    if lane_paused_here:
        _maybe_notify_lane_starved(
            client_name,
            lane_cfg.name,
            _pending_in_lane(lane_cfg, queue_snapshot, client_name),
            config=config,
        )
    return lane_paused_here


class _LaneSlotBreakdown(NamedTuple):
    """Per-lane occupancy split feeding the grant math and the tick payload.

    ``blocked``/``signoff`` are the BLOCKED_ON_USER and AWAITING_OPERATOR_SIGNOFF
    subsets of the lane's occupants (both occupy slots per ADR-0006, but must be
    reported separately so an operator can see *why* claimed=0 when pending>0 --
    #588, #990); ``pending`` is the claimable backlog.
    """

    blocked: int
    signoff: int
    pending: int


def _lane_slot_breakdown(
    lane_occupants: list[dict[str, str]],
    queue_snapshot: DevQueueStore,
    *,
    client_name: str,
    lane_name: str,
) -> _LaneSlotBreakdown:
    """Split one lane's occupancy into blocked/signoff/pending counts.

    Extracted from :func:`_dispatch_client_lanes` to keep that function inside
    its PLR statement budget. Derives blocked/signoff from the already-computed
    *lane_occupants* rather than re-scanning the queue; only ``pending`` needs
    its own scan, since PENDING rows are deliberately not lane occupants.
    """
    return _LaneSlotBreakdown(
        blocked=sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.BLOCKED_ON_USER.value
        ),
        signoff=sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF.value
        ),
        pending=sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client_name
            and t.lane == lane_name
            and t.status == QueueItemStatus.PENDING
        ),
    )


def _resolve_stale_pr_ticket_ids_if_gated(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    config: OrchestratorConfig,
    available_client_slots: int,
) -> frozenset[str]:
    """The #1862 pre-dispatch open-PR gate's resolve call, guarded (perf follow-up).

    Extracted from :func:`_dispatch_client_lanes` to keep that function inside
    its PLR branch/statement budget (mirrors the extraction rationale
    ``_lane_slot_breakdown`` above already states).

    Skipped when this client has no capacity to claim anything this tick
    (``available_client_slots <= 0``, mirroring the per-lane loop's own early
    exit) -- probing is pure cost with no possible effect on a tick that
    cannot claim regardless, and skipping it here means a client that is
    already fully saturated never pays the gate's gh-subprocess fan-out. Also
    skipped fleet-wide when the operator has disabled the gate via
    ``OrchestratorConfig.pr_gate_enabled`` (the escape hatch mirroring
    ``ssh_key_gate_enabled``).
    """
    if not config.pr_gate_enabled or available_client_slots <= 0:
        return frozenset()
    return resolve_stale_pr_ticket_ids(client, queue_snapshot)


def _dispatch_client_lanes(
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
    usage_limited_until: datetime | None = None,
    host_capacity: HostCapacityContext = _DEFAULT_HOST_CAPACITY,
) -> _ClientDispatchResult:
    """Claim + spawn across a client's lanes, then emit its dispatch.tick.

    Walks each effective lane within the Tier-1 client slot budget, claiming
    and spawning one task per granted slot.  Breaks out of the lane walk on
    the first usage-limit or spawn-error.  Always records the per-client
    dispatch.tick event (with the resolved skip_reason and per-lane stats).

    *usage_limited_until* is threaded straight through to
    :func:`_claim_next_pending` as defense-in-depth (#1346) — the caller
    (dispatch_tick) already gates the whole tick on this same value.

    *host_capacity* (#1444) bundles the fleet-wide host-capacity budget as of
    this client's turn in the loop (defaults to the feature-off state, when
    ``OrchestratorConfig.host_session_budget`` is unset). ``host_capacity.remaining``
    folds into ``available_client_slots`` as a no-op when ``None``.
    ``host_capacity.gated`` drives the ``HOST_CAPACITY_GATED`` skip_reason.
    ``host_capacity.running``/``host_capacity.budget`` are carried through
    purely for the DISPATCH_TICK event payload (observability, R7) — they do
    not affect admission math here (that's ``host_capacity.remaining``).
    """
    client_spawned = 0
    spawn_error = False
    usage_limit_detected = False
    # True when any lane has pending>0 but grant<=0 due to occupied slots
    # (RUNNING + BLOCKED_ON_USER >= max_parallel). Distinguishes the
    # previously misleading skip_reason=no_pending case (#588).
    lane_cap_blocked = False
    # True when at least one PENDING task was skipped due to active
    # spawn_error backoff (next_eligible_at in the future). See GitHub #868.
    spawn_backoff_skipped = False
    # True when a lane is skipped because its circuit breaker has tripped
    # (consecutive_spawn_errors >= threshold) and pending work waits. See #875.
    lane_circuit_paused = False
    # Lazily loaded at most once per call (only if a paused lane is seen),
    # not once per paused lane -- see _check_lane_circuit_paused. See #875.
    overrides: ConcurrencyOverrides | None = None

    # Tier-1 client slot budget: use the session-based running_count (not
    # the task-based total_running) so pre-existing DAEMON sessions without
    # a corresponding task still occupy slots (backward compat). The per-
    # lane running_by_lane counts govern per-lane grants within this budget.
    available_client_slots = _apply_host_capacity_budget(
        client_ceiling - running_count, host_capacity.remaining
    )
    lane_stats: dict[str, dict[str, int]] = {}
    # Per-lane occupant {ticket_id, status} lists for the dispatch.tick payload
    # (#1243). Computed once here (all lanes), the same OCCUPIED_LANE_STATUSES
    # join _lane_stats_for_client uses -- so blocked_in_lane/signoff_in_lane
    # below derive from it rather than re-scanning the queue.
    occupants_by_lane = _lane_occupants_for_client(client, queue_snapshot)
    # Pre-dispatch open-PR gate (#1862). Resolved once here -- outside every
    # lock and outside the per-lane loop -- because it makes `gh` calls, which
    # _claim_next_pending must never do while holding dev_queue_lock(). The
    # `queue_snapshot` it scans was already loaded lock-free by dispatch_tick
    # (ADR-0005: a stale read is acceptable for read-only callers); a row that
    # changed since then is re-checked by stage under the lock inside
    # _claim_next_pending, so a stale snapshot cannot park a healthy task. See
    # _resolve_stale_pr_ticket_ids_if_gated for the capacity/toggle guard.
    stale_pr_ticket_ids = _resolve_stale_pr_ticket_ids_if_gated(
        client,
        queue_snapshot,
        config=config,
        available_client_slots=available_client_slots,
    )

    for lane_cfg in effective_lanes:
        if available_client_slots <= 0:
            break
        if lane_cfg.paused:
            overrides = overrides or _load_concurrency_overrides()
            # The call is the LEFT operand of `or` (not the reverse): once
            # lane_circuit_paused flips True, `lane_circuit_paused or
            # _handle_paused_lane(...)` would short-circuit and skip the
            # call -- and its _maybe_notify_lane_starved side effect -- for
            # every paused lane after the first tripped one (#1630 -- caught
            # by test_two_starved_lanes_produce_distinguishable_events).
            lane_circuit_paused = (
                _handle_paused_lane(
                    lane_cfg,
                    queue_snapshot,
                    client.name,
                    overrides=overrides,
                    config=config,
                )
                or lane_circuit_paused
            )
            continue
        # running_in_lane = RUNNING + BLOCKED_ON_USER + AWAITING_OPERATOR_SIGNOFF
        # (total occupied slots, OCCUPIED_LANE_STATUSES, #990).
        running_in_lane = running_by_lane.get(lane_cfg.name, 0)
        breakdown = _lane_slot_breakdown(
            occupants_by_lane.get(lane_cfg.name, []),
            queue_snapshot,
            client_name=client.name,
            lane_name=lane_cfg.name,
        )
        grant = min(
            lane_cfg.max_parallel - running_in_lane,
            breakdown.pending,
            available_client_slots,
        )
        # Detect: pending work exists but the lane cap is full of occupied
        # slots (RUNNING + BLOCKED_ON_USER >= max_parallel). Raises the
        # skip_reason to LANE_CAP_BLOCKED instead of the misleading NO_PENDING.
        if grant <= 0 and breakdown.pending > 0:
            lane_cap_blocked = True
        lane_claimed = 0
        for _ in range(max(0, grant)):
            task, backoff_skipped = _claim_next_pending(
                client.name,
                lane=lane_cfg.name,
                client=client,
                config=config,
                priority_ticket_ids=priority_ids,
                usage_limited_until=usage_limited_until,
                stale_pr_ticket_ids=stale_pr_ticket_ids,
            )
            spawn_backoff_skipped |= backoff_skipped
            if task is None:
                break

            outcome = _spawn_claimed_task(
                task,
                client,
                resolved_native_daemon=resolved_native_daemon,
                parent=parent,
                emit=emit,
            )
            if outcome.usage_limit_detected:
                usage_limit_detected = True
            if outcome.spawn_error:
                spawn_error = True
                _record_lane_spawn_error(
                    lane_cfg,
                    client_name=client.name,
                    last_error=outcome.error or "",
                    config=config,
                )
            if outcome.spawned:
                running_count += 1
                client_spawned += 1
                lane_claimed += 1
                available_client_slots -= 1
                _reset_lane_spawn_errors(lane_cfg, client.name)

            if usage_limit_detected or spawn_error:
                break

        lane_stats[lane_cfg.name] = {
            "claimed": lane_claimed,
            "running": running_in_lane - breakdown.blocked - breakdown.signoff,
            "blocked": breakdown.blocked,
            "signoff": breakdown.signoff,
            "pending": breakdown.pending,
        }

        if usage_limit_detected or spawn_error:
            break

    if emit is not None:
        emit(
            f"{client.name}: spawned={client_spawned}"
            f" cap_full={int(cap_full)}"
            f" lane_cap_blocked={int(lane_cap_blocked)}"
        )

    skip_reason = _resolve_dispatch_skip_reason(
        usage_limit_detected=usage_limit_detected,
        cap_full=cap_full,
        spawn_error=spawn_error,
        lane_cap_blocked=lane_cap_blocked,
        spawn_backoff_skipped=spawn_backoff_skipped,
        lane_circuit_paused=lane_circuit_paused,
        client_spawned=client_spawned,
        host_capacity_gated=host_capacity.gated,
    )

    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": client_spawned,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": skip_reason,
            "lanes": lane_stats,
            "lane_occupants": occupants_by_lane,
            "occupied": sum(len(v) for v in occupants_by_lane.values()),
            "host_running": host_capacity.running,
            "host_budget": host_capacity.budget,
        },
    )
    return _ClientDispatchResult(
        spawned=client_spawned, usage_limit_detected=usage_limit_detected
    )


def _resolve_low_precedence_skip_reason(
    *,
    lane_circuit_paused: bool,
    spawn_backoff_skipped: bool,
    client_spawned: int,
) -> DispatchSkipReason:
    """Resolve the low-precedence tail of the skip_reason chain (#875).

    LANE_CIRCUIT_PAUSED > (SPAWN_ERROR_BACKOFF | NO_PENDING when nothing
    spawned this tick) > NONE.  Split out of _resolve_dispatch_skip_reason so
    that function stays within the PLR0911 six-return ceiling.
    """
    if lane_circuit_paused:
        return DispatchSkipReason.LANE_CIRCUIT_PAUSED
    if client_spawned == 0:
        return (
            DispatchSkipReason.SPAWN_ERROR_BACKOFF
            if spawn_backoff_skipped
            else DispatchSkipReason.NO_PENDING
        )
    return DispatchSkipReason.NONE


def _resolve_dispatch_skip_reason(
    *,
    usage_limit_detected: bool,
    cap_full: bool,
    spawn_error: bool,
    lane_cap_blocked: bool,
    spawn_backoff_skipped: bool,
    lane_circuit_paused: bool,
    client_spawned: int,
    host_capacity_gated: bool = False,
) -> DispatchSkipReason:
    """Resolve the dispatch.tick skip_reason via first-match precedence.

    Mirrors the operator resolution order (issue #459, #588, #875, #1444):
    1. freshness_gate — handled by early-continue before this is called
    2. usage_limited — usage limit detected this tick for this client
    3. host_capacity_gated — the fleet-wide host_session_budget is exhausted
       (#1444); ranks above cap_full so an operator can distinguish "the
       whole host is out of budget" from "this client's own cap is full"
    4. cap_full — running_count >= cap before loop entered
    5. lane_cap_blocked — pending>0 but every lane slot is occupied by
       RUNNING or BLOCKED_ON_USER tasks; grant<=0 for all lanes
    6. spawn_error — exception broke the loop (regardless of client_spawned)
    7. lane_circuit_paused — a lane's circuit breaker has tripped and pending
       work is stranded behind it (see _resolve_low_precedence_skip_reason)
    8. spawn_error_backoff — pending tasks exist but all are in spawn_error
       backoff (next_eligible_at in the future); no exception occurred
    9. no_pending — loop exited with zero claims and no spawn error
    10. none — at least one session spawned
    """
    if usage_limit_detected:
        return DispatchSkipReason.USAGE_LIMITED
    # Why: host_capacity_gated wins over cap_full even when a client's OWN
    # ceiling is also full this tick -- a deliberate #1444 planning decision
    # (Adopted Assumption 1), not an oversight. host_capacity_gated is
    # computed unconditionally from the fleet-wide budget, mirroring how
    # cap_full itself is computed unconditionally from the client's own
    # ceiling; neither is gated on the other. Reviewed and passed by both
    # plan-quality stations with this precedence explicit.
    if host_capacity_gated:
        return DispatchSkipReason.HOST_CAPACITY_GATED
    if cap_full:
        return DispatchSkipReason.CAP_FULL
    if lane_cap_blocked:
        return DispatchSkipReason.LANE_CAP_BLOCKED
    if spawn_error:
        return DispatchSkipReason.SPAWN_ERROR
    return _resolve_low_precedence_skip_reason(
        lane_circuit_paused=lane_circuit_paused,
        spawn_backoff_skipped=spawn_backoff_skipped,
        client_spawned=client_spawned,
    )
