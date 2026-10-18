"""Per-lane circuit breaker for the dispatch loop (#875, #1630).

Part of the ``cw.dispatch`` package split: extracted from
:mod:`cw.dispatch.lanes` (#2696) so that module stays under the ~1000-line
ceiling. This module owns the breaker concern -- counting spawn errors per
lane, tripping the pause, detecting a breaker pause with stranded work, and
the recurring starved-lane page. :mod:`cw.dispatch.lanes` keeps the per-client
lane walk that drives it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    concurrency_override_lock,
)
from cw.events import record_event
from cw.models import (
    ConcurrencyOverrides,
    DispatchSkipReason,
    LaneConcurrencyOverride,
    OrchestratorEventType,
    QueueItemStatus,
)

if TYPE_CHECKING:
    from cw.models import DevQueueStore, LaneConfig, OrchestratorConfig


# ``source`` field on a LANE_PAUSED event emitted by the per-lane circuit breaker
# (as opposed to an operator-initiated ``cw lane pause``). See GitHub issue #875.
_LANE_PAUSE_SOURCE_CIRCUIT_BREAKER = "circuit_breaker"


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

    ``last_error`` is collapsed to a single line before use: some raise sites
    (e.g. a WorktreeError embedding git's multi-line stderr) legitimately
    contain newlines, but ``cli.queues._format_event_line`` renders one event
    per terminal line — an embedded newline would break that contract (#2034).
    """
    last_error = " ".join(last_error.split())
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
