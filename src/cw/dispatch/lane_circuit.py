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
from typing import TYPE_CHECKING, NamedTuple

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    concurrency_override_lock,
)
from cw.dispatch.claim.events import _sanitize_spawn_error_text
from cw.events import record_event
from cw.lane_pause import (
    LanePauseSource,
    LanePauseTrigger,
    breaker_tripped,
    iso_z,
    utc_now_seconds,
    worktree_breaker_threshold,
)
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
# (as opposed to an operator-initiated ``cw lane pause``), and the persisted
# ``LaneConcurrencyOverride.pause_source`` of a trip. See GitHub #875, #2696.
_LANE_PAUSE_SOURCE_CIRCUIT_BREAKER = LanePauseSource.CIRCUIT_BREAKER.value

# How many recorded spawn errors a lane override keeps, oldest dropped (#2696).
_RECENT_SPAWN_ERRORS_KEPT = 5

# Bound on the cool-down's doubling exponent (#2696): far past any useful
# cool-down, it only keeps ``base << exponent`` from growing without limit.
_COOLDOWN_EXPONENT_CAP = 30


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

    Distinguishes a circuit-breaker trip (either breaker counter at its trip
    point, :func:`cw.lane_pause.breaker_tripped`) from a plain operator pause:
    only the former, with pending work still waiting, warrants the
    LANE_CIRCUIT_PAUSED skip_reason. See GitHub #875, #2696.

    ``overrides`` is loaded once per :func:`_dispatch_client_lanes` call (not
    once per paused lane) and passed in by the caller.
    """
    override = overrides.lanes.get(f"{client_name}/{lane_cfg.name}")
    if override is None:
        return False
    if not breaker_tripped(override, threshold=config.lane_circuit_breaker_threshold):
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


class _Trip(NamedTuple):
    """What a breaker trip recorded, for the ``lane.paused`` event payload."""

    consecutive_count: int
    trigger: LanePauseTrigger
    streak: int
    recent_errors: list[str]
    auto_resume_at: datetime | None


def _auto_resume_cooldown_minutes(streak: int, *, base: int, max_minutes: int) -> int:
    """Cool-down before the *streak*-th consecutive trip auto-resumes (#2696).

    ``base`` doubles per consecutive trip, capped at *max_minutes*. The
    exponent is bounded so a flapping lane cannot grow the shift without limit.
    """
    exponent = min(max(streak - 1, 0), _COOLDOWN_EXPONENT_CAP)
    return min(base << exponent, max_minutes)


def _is_operator_held(override: LaneConcurrencyOverride) -> bool:
    """A pause the breaker must not take over: ``operator`` or legacy (no source).

    Decision 5 (#2696): a ``cw lane pause`` that lands while a spawn is in
    flight must stay operator-held even if that spawn's failure reaches the
    threshold -- otherwise the trip would turn the hold into an
    auto-resumable pause. A legacy pause is operator-held too (R1).
    """
    return (
        override.paused is True
        and override.pause_source != _LANE_PAUSE_SOURCE_CIRCUIT_BREAKER
    )


def _trip_updates(
    override: LaneConcurrencyOverride,
    trigger: LanePauseTrigger,
    *,
    config: OrchestratorConfig,
) -> tuple[dict[str, object], int, datetime | None]:
    """The pause fields a trip writes, plus its streak and resume time."""
    paused_at = utc_now_seconds()
    streak = override.circuit_trip_streak + 1
    auto_resume_at = None
    if config.lane_circuit_auto_resume_minutes > 0:
        auto_resume_at = paused_at + timedelta(
            minutes=_auto_resume_cooldown_minutes(
                streak,
                base=config.lane_circuit_auto_resume_minutes,
                max_minutes=config.lane_circuit_auto_resume_max_minutes,
            )
        )
    updates: dict[str, object] = {
        "paused": True,
        "paused_at": paused_at,
        "pause_source": _LANE_PAUSE_SOURCE_CIRCUIT_BREAKER,
        "pause_trigger": trigger.value,
        "circuit_trip_streak": streak,
        "circuit_auto_resume_at": auto_resume_at,
    }
    return updates, streak, auto_resume_at


def _apply_recorded_error(
    override: LaneConcurrencyOverride,
    error_text: str,
    *,
    config: OrchestratorConfig,
    worktree_failure: bool,
) -> tuple[LaneConcurrencyOverride, _Trip | None]:
    """Count one recorded error on *override*; trip at the counter's threshold.

    Pure: no I/O. Every recorded error is appended to ``recent_spawn_errors``
    (worktree failures too, so a worktree trip shows its cause) and clears
    ``circuit_half_open`` -- the one-uncharged-failure-per-window bound holds
    across failure kinds (Decision 2).
    """
    recent = [*override.recent_spawn_errors, error_text][-_RECENT_SPAWN_ERRORS_KEPT:]
    threshold = config.lane_circuit_breaker_threshold
    if worktree_failure:
        field = "consecutive_worktree_errors"
        count = override.consecutive_worktree_errors + 1
        threshold = worktree_breaker_threshold(threshold)
        trigger = LanePauseTrigger.WORKTREE_ERRORS
    else:
        field = "consecutive_spawn_errors"
        count = override.consecutive_spawn_errors + 1
        trigger = LanePauseTrigger.SPAWN_ERRORS
    updates: dict[str, object] = {
        field: count,
        "recent_spawn_errors": recent,
        "circuit_half_open": False,
    }
    if count < threshold or _is_operator_held(override):
        return override.model_copy(update=updates), None
    trip_updates, streak, auto_resume_at = _trip_updates(
        override, trigger, config=config
    )
    updates.update(trip_updates)
    trip = _Trip(count, trigger, streak, recent, auto_resume_at)
    return override.model_copy(update=updates), trip


def _record_lane_spawn_error(
    lane_cfg: LaneConfig,
    client_name: str,
    last_error: str,
    *,
    config: OrchestratorConfig,
    worktree_failure: bool = False,
) -> None:
    """Count one spawn failure on the lane; trip the breaker at its threshold.

    Under the override lock: load → count → save. A ``WorktreeError``
    (*worktree_failure*, R3) counts on ``consecutive_worktree_errors`` and
    trips at 3x ``lane_circuit_breaker_threshold``; anything else counts on
    ``consecutive_spawn_errors`` and trips at the threshold. Either way the
    error is recorded, sanitized, in ``recent_spawn_errors`` (#2696).

    A trip pauses the lane and persists its cause (``paused_at``,
    ``pause_source``, ``pause_trigger``, the trip streak and, when auto-resume
    is on, ``circuit_auto_resume_at``), then emits a circuit-breaker-sourced
    LANE_PAUSED after the lock is released. A trip never takes over an
    operator-held pause (Decision 5). See GitHub #875, #2696.

    The event's ``last_error`` is collapsed to a single line but not redacted,
    as before: some raise sites (e.g. a WorktreeError embedding git's
    multi-line stderr) legitimately contain newlines, but
    ``cli.queues._format_event_line`` renders one event per terminal line
    (#2034). Its ``recent_errors`` is the persisted, redacted list.
    """
    collapsed = " ".join(last_error.split())
    lane_key = f"{client_name}/{lane_cfg.name}"
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.lanes.get(lane_key, LaneConcurrencyOverride())
        updated, trip = _apply_recorded_error(
            override,
            _sanitize_spawn_error_text(last_error),
            config=config,
            worktree_failure=worktree_failure,
        )
        overrides.lanes[lane_key] = updated
        _save_concurrency_overrides(overrides)
    if trip is not None:
        record_event(
            OrchestratorEventType.LANE_PAUSED,
            {
                "client": client_name,
                "lane": lane_cfg.name,
                "source": _LANE_PAUSE_SOURCE_CIRCUIT_BREAKER,
                "consecutive_count": trip.consecutive_count,
                "last_error": collapsed,
                "trigger": trip.trigger.value,
                "trip_streak": trip.streak,
                "recent_errors": trip.recent_errors,
                "auto_resume_at": (
                    iso_z(trip.auto_resume_at) if trip.auto_resume_at else None
                ),
            },
        )


def _has_breaker_progress(override: LaneConcurrencyOverride) -> bool:
    """Whether a spawn success has anything to clear on *override*."""
    return bool(
        override.consecutive_spawn_errors
        or override.consecutive_worktree_errors
        or override.circuit_trip_streak
        or override.recent_spawn_errors
        or override.circuit_half_open
    )


def _reset_lane_spawn_errors(lane_cfg: LaneConfig, client_name: str) -> None:
    """Clear the lane's breaker progress after a successful spawn.

    Zeroes both counters and the trip streak, empties
    ``recent_spawn_errors`` and ends a half-open probe (``circuit_half_open``).
    Short-circuits (no write) when there is nothing to clear or the lane has no
    override, so a steady-state healthy lane never rewrites the override file.
    Never touches ``paused`` or the pause metadata: those are cleared by every
    resume (``cw lane resume`` or auto-resume), and clearing ``pause_source``
    on a still-paused lane would misread it as a legacy pause. See GitHub
    #875, #2696.
    """
    lane_key = f"{client_name}/{lane_cfg.name}"
    with concurrency_override_lock():
        overrides = _load_concurrency_overrides()
        override = overrides.lanes.get(lane_key)
        if override is None or not _has_breaker_progress(override):
            return
        overrides.lanes[lane_key] = override.model_copy(
            update={
                "consecutive_spawn_errors": 0,
                "consecutive_worktree_errors": 0,
                "circuit_trip_streak": 0,
                "recent_spawn_errors": [],
                "circuit_half_open": False,
            }
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
