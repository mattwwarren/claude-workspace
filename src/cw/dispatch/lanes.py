"""Per-lane circuit breaker and per-client lane dispatch for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the lane circuit breaker,
the client-freshness-block latch, and the per-client lane walk."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    concurrency_override_lock,
)
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
    pending_in_lane = sum(
        1
        for t in queue_snapshot.tasks
        if t.client == client_name
        and t.lane == lane_cfg.name
        and t.status == QueueItemStatus.PENDING
    )
    return pending_in_lane > 0


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
) -> _ClientDispatchResult:
    """Claim + spawn across a client's lanes, then emit its dispatch.tick.

    Walks each effective lane within the Tier-1 client slot budget, claiming
    and spawning one task per granted slot.  Breaks out of the lane walk on
    the first usage-limit or spawn-error.  Always records the per-client
    dispatch.tick event (with the resolved skip_reason and per-lane stats).
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
    available_client_slots = client_ceiling - running_count
    lane_stats: dict[str, dict[str, int]] = {}
    # Per-lane occupant {ticket_id, status} lists for the dispatch.tick payload
    # (#1243). Computed once here (all lanes), the same OCCUPIED_LANE_STATUSES
    # join _lane_stats_for_client uses -- so blocked_in_lane/signoff_in_lane
    # below derive from it rather than re-scanning the queue.
    occupants_by_lane = _lane_occupants_for_client(client, queue_snapshot)

    for lane_cfg in effective_lanes:
        if available_client_slots <= 0:
            break
        if lane_cfg.paused:
            overrides = overrides or _load_concurrency_overrides()
            lane_circuit_paused = lane_circuit_paused or _check_lane_circuit_paused(
                lane_cfg,
                queue_snapshot,
                client.name,
                overrides=overrides,
                config=config,
            )
            continue
        # running_in_lane = RUNNING + BLOCKED_ON_USER + AWAITING_OPERATOR_SIGNOFF
        # (total occupied slots, OCCUPIED_LANE_STATUSES, #990).
        running_in_lane = running_by_lane.get(lane_cfg.name, 0)
        # blocked_in_lane = BLOCKED_ON_USER only (for per-lane breakdown),
        # derived from occupants_by_lane rather than re-scanning the queue.
        blocked_in_lane = sum(
            1
            for o in occupants_by_lane.get(lane_cfg.name, [])
            if o["status"] == QueueItemStatus.BLOCKED_ON_USER.value
        )
        # signoff_in_lane = AWAITING_OPERATOR_SIGNOFF only (for per-lane
        # breakdown). Must be subtracted out of running_in_lane below, else a
        # signoff-parked ticket is misreported as "running" (#990).
        signoff_in_lane = sum(
            1
            for o in occupants_by_lane.get(lane_cfg.name, [])
            if o["status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF.value
        )
        pending_in_lane = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        grant = min(
            lane_cfg.max_parallel - running_in_lane,
            pending_in_lane,
            available_client_slots,
        )
        # Detect: pending work exists but the lane cap is full of occupied
        # slots (RUNNING + BLOCKED_ON_USER >= max_parallel). Raises the
        # skip_reason to LANE_CAP_BLOCKED instead of the misleading NO_PENDING.
        if grant <= 0 and pending_in_lane > 0:
            lane_cap_blocked = True
        lane_claimed = 0
        for _ in range(max(0, grant)):
            task, backoff_skipped = _claim_next_pending(
                client.name,
                lane=lane_cfg.name,
                config=config,
                priority_ticket_ids=priority_ids,
            )
            spawn_backoff_skipped |= backoff_skipped
            if task is None:
                break

            outcome = _spawn_claimed_task(
                task,
                client,
                config=config,
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
            "running": running_in_lane - blocked_in_lane - signoff_in_lane,
            "blocked": blocked_in_lane,
            "signoff": signoff_in_lane,
            "pending": pending_in_lane,
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
) -> DispatchSkipReason:
    """Resolve the dispatch.tick skip_reason via first-match precedence.

    Mirrors the operator resolution order (issue #459, #588, #875):
    1. freshness_gate — handled by early-continue before this is called
    2. usage_limited — usage limit detected this tick for this client
    3. cap_full — running_count >= cap before loop entered
    4. lane_cap_blocked — pending>0 but every lane slot is occupied by
       RUNNING or BLOCKED_ON_USER tasks; grant<=0 for all lanes
    5. spawn_error — exception broke the loop (regardless of client_spawned)
    6. lane_circuit_paused — a lane's circuit breaker has tripped and pending
       work is stranded behind it (see _resolve_low_precedence_skip_reason)
    7. spawn_error_backoff — pending tasks exist but all are in spawn_error
       backoff (next_eligible_at in the future); no exception occurred
    8. no_pending — loop exited with zero claims and no spawn error
    9. none — at least one session spawned
    """
    if usage_limit_detected:
        return DispatchSkipReason.USAGE_LIMITED
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
