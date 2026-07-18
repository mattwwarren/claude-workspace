"""Session models: LocalLivenessHandle, Session.

Depends only on ``cw.models.enums``. See ``cw.models.__init__`` for DAG order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cw.models.enums import (
    CompletionReason,
    LivenessBucket,
    ReapReason,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
)


class LocalLivenessHandle(BaseModel):
    """Process-liveness handle for a LocalExecutor aider subprocess (RFC 0005 F3).

    Binds a PID to its process creation-time (nanoseconds, epoch-relative —
    ``psutil.Process(pid).create_time()``, see GitHub #921) captured at spawn.
    The start-time pin lets harvest detection reject a recycled PID: a dead
    aider PID reassigned to an unrelated process re-reads a different
    start-time, so the session is treated as dead (harvested) rather than
    falsely observed alive. Frozen — an immutable snapshot. See GitHub #888.
    """

    model_config = ConfigDict(frozen=True)

    pid: int
    start_time_ns: int


class Session(BaseModel):
    """A tracked Claude Code session."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    name: str  # Human-readable: "client-a/impl"
    client: str
    purpose: SessionPurpose
    status: SessionStatus = SessionStatus.ACTIVE
    origin: SessionOrigin = SessionOrigin.USER
    workspace_path: Path
    worktree_path: Path | None = None
    branch: str | None = None
    surface_ref: str | None = None
    claude_session_id: str | None = None
    auto_backgrounded: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    idle_at: datetime | None = None
    # Consecutive idle-watchdog observations where this session failed liveness
    # checks; reset on recovery; session is dispositioned (reaped/parked/
    # git-salvaged) only when it reaches OrchestratorConfig.idle_confirm_observations.
    # See GitHub #545.
    idle_observation_count: int = 0
    # Consecutive salvage-skip count for the per-session attention latch
    # (closes #974). Incremented each time this session is skipped via
    # ProposedAction.SKIP_PARKED (SESSION_SALVAGE_SKIPPED); reset to 0 on
    # recovery (any non-SKIP_PARKED detect-phase disposition). Same
    # reset-on-recovery latch shape as idle_observation_count above.
    consecutive_salvage_skips: int = 0
    backgrounded_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_reason: CompletionReason | None = None
    completed_at: datetime | None = None
    # Reason written at each reap site so the queue-events bus server can
    # include it in queue.session_reaped notifications. Finer-grained than
    # CompletionReason — see ReapReason and GitHub #380. None for sessions
    # not reaped by reconcile (e.g. user-backgrounded or /session-done'd).
    reap_reason: ReapReason | None = None
    # Stamped in-place (under sessions_lock, NOT via mutate_state — self-deadlock
    # risk per ADR-0006 invariant 2) when SESSION_REAP_PROPOSED is emitted for
    # this session. Dedup guard: _emit_reap_proposed skips sessions already
    # stamped. See GitHub #555.
    reap_proposed_at: datetime | None = None
    # Dispatch lane this session was spawned into. Stamped by spawn_create_impl
    # when called from the dispatch loop (GitHub #594). None for sessions
    # spawned outside the queue (interactive, plan, cli). Stored for
    # observability; occupancy counting remains task-join based (ADR-0006).
    lane: str | None = None
    parent_session_id: str | None = None
    worker_session_ids: list[str] = Field(default_factory=list)
    # Sentinel-block summary parsed from a headless /auto-dev worker's stdout
    # at completion time. ``None`` for any session that didn't run headless or
    # whose stdout could not be parsed. Stored as a raw dict (rather than the
    # AutoDevResult Pydantic model) so the persisted state file remains
    # readable when the result schema bumps independently of cw's CW_STATE
    # schema. See ``cw.auto_dev_result`` for the parser.
    last_result: dict[str, Any] | None = None
    # Total USD cost for this session's auto-dev run. Populated by
    # signal_stop from AutoDevResult.cost_usd. None when cost data
    # was not emitted by the producer. See GitHub issue #124.
    cost_usd: float | None = None
    # Per-model cost breakdown for this session. Populated via the SDK
    # orchestrator path (post-#116). None when not available.
    cost_breakdown: dict[str, float] | None = None
    # RFC 0005 A1 — dormant; tracks which pipeline stage spawned this session.
    # None for sessions not spawned by the staged pipeline (GitHub #612).
    stage: Stage | None = None
    # RFC 0005 F3 — process-liveness handle for a fire-and-forget LocalExecutor
    # aider subprocess. Set when LocalExecutor.spawn() launches aider and leaves
    # the session ACTIVE; reconcile/local harvest reads it to detect the dead
    # process and synthesize the git-based completion. None for every non-LOCAL
    # session (surface_ref-backed sessions use daemon-roster liveness). See #888.
    local_liveness: LocalLivenessHandle | None = None
    # RFC 0008 W2 — latched transcript-staleness bucket, edge-triggered by
    # cw.reconcile.liveness on each crossing (no per-observation counter, unlike
    # idle_observation_count above). Session.stage is NOT used to resolve the
    # per-stage floor; the owning TicketTask.stage is (see
    # cw.reconcile.liveness._detect_liveness_candidates). See GitHub #1001.
    liveness_bucket: LivenessBucket = LivenessBucket.LIVE
