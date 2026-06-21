"""Pydantic models for session state and client configuration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"
    ORCHESTRATE = "orchestrate"


# Purposes a worker session can be dispatched/created with. ORCHESTRATE is
# excluded: an ORCHESTRATE session is created only via `cw orchestrate start`
# (#595 / Phase 4b), never selected as a worker --purpose.
WORKER_PURPOSES: tuple[SessionPurpose, ...] = (
    SessionPurpose.IMPL,
    SessionPurpose.IDEA,
    SessionPurpose.DEBT,
)


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"
    # Headless daemon session exceeded wall-clock budget without emitting a
    # sentinel. Terminal-ish but retry-eligible: reconciler reverts the
    # owning TicketTask to PENDING so the dispatch loop can retry.
    # See GitHub issue #176 Layer 1.
    TIMED_OUT = "timed_out"


class CompletionReason(StrEnum):
    USER = "user"
    HANDOFF = "handoff"
    CRASHED = "crashed"
    NORMAL = "normal"
    TIMED_OUT = "timed_out"


class SessionOrigin(StrEnum):
    USER = "user"
    DAEMON = "daemon"


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_ON_USER = "blocked_on_user"


class ReapReason(StrEnum):
    """Reason taxonomy for queue.session_reaped bus events.

    Each value maps to exactly one reconcile disposition path. See the
    reap-site decision table in GitHub issue #380.
    """

    PHANTOM_SURFACE = "phantom_surface"
    IDLE_STALL = "idle_stall"
    USAGE_LIMIT_CUTOFF = "usage_limit_cutoff"
    RETRY_CAP_PARKED = "retry_cap_parked"
    STALLED_RETRY_CAP_PARKED = "stalled_retry_cap_parked"
    WALL_CLOCK_BUDGET = "wall_clock_budget"
    COMPLETED_BACKSTOP = "completed_backstop"
    SALVAGE_COMPLETED = "salvage_completed"
    SALVAGE_PARKED = "salvage_parked"


class Stage(StrEnum):
    """RFC 0005 pipeline stage. Dormant in A1 — no dispatch wiring yet."""

    HARDEN = "harden"
    PLAN = "plan"
    IMPL = "impl"
    REVIEW = "review"
    FINALIZE = "finalize"


# Schema versions for persisted state. Bump when making a breaking change
# to the on-disk layout; add a migration in `cw.config.migrate_cw_state`
# or `cw.dev_queue.migrate_dev_queue` to handle older versions.
# v6: added Session.idle_observation_count (GitHub #545).
# v7: added Session.reap_reason (GitHub #380).
# v8: added Session.reap_proposed_at (GitHub #555).
# v9: added Session.lane (GitHub #594).
# v10: added Session.stage (GitHub #612).
CW_STATE_SCHEMA_VERSION = 10
# v3: added TicketTask.lane (GitHub #557).
# v4: added TicketTask.stage + stage_base_ref (GitHub #612).
DEV_QUEUE_SCHEMA_VERSION = 4
DEFAULT_LANE: str = "default"
DEFAULT_STAGE: Stage = Stage.PLAN


class TaskSpec(BaseModel):
    """Machine-parseable task specification for agent-to-agent handoffs."""

    description: str
    purpose: SessionPurpose
    prompt: str
    context_files: list[str] = Field(default_factory=list)
    success_criteria: str | None = None
    source_session: str | None = None
    priority: int = 0
    target_session: str | None = None


class QueueItem(BaseModel):
    """A queued work item for delegation or daemon processing."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    client: str
    task: TaskSpec
    status: QueueItemStatus = QueueItemStatus.PENDING
    assigned_session_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: str | None = None


class QueueStore(BaseModel):
    """Persisted queue state for a client."""

    items: list[QueueItem] = Field(default_factory=list)

    def pending(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == QueueItemStatus.PENDING]

    def running(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == QueueItemStatus.RUNNING]

    def by_purpose(self, purpose: str) -> list[QueueItem]:
        return [i for i in self.items if i.task.purpose == purpose]

    def by_status(self, status: QueueItemStatus) -> list[QueueItem]:
        return [i for i in self.items if i.status == status]

    def find_item(self, item_id: str) -> QueueItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None


class LaneConcurrencyOverride(BaseModel):
    """Per-lane overrides from the concurrency override store."""

    max_parallel: int | None = None
    paused: bool | None = None


class ClientConcurrencyOverride(BaseModel):
    """Per-client ceiling override from the concurrency override store."""

    ceiling: int | None = None


class ConcurrencyOverrides(BaseModel):
    """Runtime concurrency overrides persisted outside orchestrator.yaml.

    Written by ``cw config concurrency set`` and ``cw lane pause/resume``.
    Merged with the declared config by ``load_effective_config()``.
    NOT added to schema.REGISTRY — test_schema.py must stay unchanged.
    """

    max_parallel_clients: int | None = None
    clients: dict[str, ClientConcurrencyOverride] = Field(default_factory=dict)
    lanes: dict[str, LaneConcurrencyOverride] = Field(default_factory=dict)


class OrchestratorEventType(StrEnum):
    """Event types for the orchestrator-level event bus.

    Covers PR lifecycle, ticket queue, and cross-session coordination.
    """

    TICKET_ENQUEUED = "ticket.enqueued"
    SESSION_SPAWNED = "session.spawned"
    SESSION_COMPLETED = "session.completed"
    SESSION_TIMED_OUT = "session.timed_out"
    SESSION_NEEDS_ATTENTION = "session.needs_attention"
    TICKET_NEEDS_SYNC = "ticket.needs_sync"
    STAGE_ENTERED = "stage.entered"
    STAGE_ERRORED = "stage.errored"
    PR_REGISTERED = "pr.registered"
    PR_CI_FAILED = "pr.ci_failed"
    PR_REVIEW_RECEIVED = "pr.review_received"
    PR_MERGEABLE = "pr.mergeable"
    PR_MERGED = "pr.merged"
    DISPATCH_TICK = "dispatch.tick"
    DISPATCH_LOOP_EXITED = "dispatch.loop_exited"
    SESSION_PHANTOM_REVERTED = "session.phantom_reverted"
    SESSION_SALVAGE_SKIPPED = "session.salvage_skipped"
    SESSION_REAP_PROPOSED = "session.reap_proposed"
    SESSION_REAP_AUTHORIZED = "session.reap_authorized"
    SESSION_SPAWN_UNREGISTERED = "session.spawn_unregistered"
    LANE_CREATED = "lane.created"
    LANE_PAUSED = "lane.paused"
    LANE_RESUMED = "lane.resumed"
    TICKET_MOVED = "ticket.moved"
    TICKET_APPROVED = "ticket.approved"
    TICKET_REQUEUED = "ticket.requeued"
    TICKET_UNBLOCKED = "ticket.unblocked"
    SESSION_STAGE_TIMED_OUT_RETRIED = "session.stage_timed_out_retried"


class DispatchSkipReason(StrEnum):
    """First-match skip_reason values emitted in dispatch.tick events.

    Precedence (highest first):
    FRESHNESS_GATE > USAGE_LIMITED > CAP_FULL > LANE_CAP_BLOCKED
    > SPAWN_ERROR > NO_PENDING > NONE.
    """

    FRESHNESS_GATE = "freshness_gate"
    USAGE_LIMITED = "usage_limited"
    CAP_FULL = "cap_full"
    LANE_CAP_BLOCKED = "lane_cap_blocked"
    SPAWN_ERROR = "spawn_error"
    NO_PENDING = "no_pending"
    NONE = "none"


class OrchestratorEvent(BaseModel):
    """A single event on the orchestrator event bus."""

    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    type: OrchestratorEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consumed_at: datetime | None = None


class TicketTask(BaseModel):
    """A ticket queued for dispatch to a Claude session."""

    ticket_id: str
    client: str
    priority: int = 0
    worktree_path: Path | None = None
    linear_url: str | None = None
    scope_hint: str | None = None
    status: QueueItemStatus = QueueItemStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Stamped by dispatch_tick after spawn_create_impl returns; cleared on
    # reconcile revert. Used by consume_completed_sessions to disambiguate
    # SESSION_COMPLETED events from old (crashed) sessions that share a
    # ticket_id with a freshly-respawned task. None for legacy tasks
    # persisted before this field existed — consumer falls back to
    # ticket_id-only matching in that case.
    session_id: str | None = None
    # Incremented each time the task is claimed by _claim_next_pending. Used to
    # apply a hard cap on validation_failed retries (see issue #251).
    attempts: int = 0
    # Per-ticket wall-clock budget override (seconds). When set, takes precedence
    # over the per-tier default in OrchestratorConfig.headless_timeout_by_tier and
    # the global HEADLESS_TIMEOUT_SECONDS fallback. Set via ``cw dev-queue add
    # --timeout <s>``. None means "use tier or global default". See issue #265.
    headless_timeout_override: int | None = None
    # Per-ticket idle-watchdog budget override (seconds). When set, takes precedence
    # over the per-tier default in OrchestratorConfig.idle_watchdog_by_tier and
    # the global IDLE_WATCHDOG_SECONDS fallback. None means "use tier or global
    # default". See GitHub issue #326.
    idle_watchdog_override: int | None = None
    # Cumulative USD cost across all auto-dev attempts for this ticket.
    # Populated by _accumulate_task_cost in consume_completed_sessions.
    # None when no cost data has been recorded yet. See GitHub issue #124.
    total_cost_usd: float | None = None
    # The source of the implementation plan for this ticket. Carried into
    # cw-context.json so workers can emit the correct plan_source in their
    # AutoDevResult sentinel without rediscovering it at runtime (#314).
    # Always None today; populated by a future dev-queue plan command.
    plan_source: str | None = None
    # Lane this ticket is assigned to. Defaults to DEFAULT_LANE; set by
    # orchestrate/dispatch in Phase 2 (#558).
    lane: str = DEFAULT_LANE
    # RFC 0005 A1 — dormant; no dispatch wiring yet (GitHub #612).
    stage: Stage = DEFAULT_STAGE
    stage_base_ref: str | None = None


class DispatchPlan(BaseModel):
    """Ordered list of tickets to dispatch, with optional grouping hints."""

    tasks: list[TicketTask] = Field(default_factory=list)
    grouping_hints: dict[str, str] = Field(default_factory=dict)


class DevQueueStore(BaseModel):
    """Persisted dev-queue state holding TicketTasks."""

    schema_version: int = DEV_QUEUE_SCHEMA_VERSION
    tasks: list[TicketTask] = Field(default_factory=list)

    def pending(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.PENDING]

    def running(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.RUNNING]

    def completed(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.COMPLETED]

    def cancelled(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.CANCELLED]

    def by_client(self, client: str) -> list[TicketTask]:
        return [t for t in self.tasks if t.client == client]


class ReapPolicy(StrEnum):
    """Policy controlling whether the reconciler destroys a stalled session.

    Under ``SIGNAL_ONLY`` (default): route the owning task to BLOCKED_ON_USER,
    leave session/worktree/daemon surface intact.  Requires operator action to clear.
    Under ``AUTO``: self-healing — stop daemon, revert task to PENDING, clean worktree.
    """

    SIGNAL_ONLY = "signal_only"
    AUTO = "auto"


class StageExecutorConfig(BaseModel):
    """Executor configuration for a single pipeline stage (RFC 0005 A1, dormant)."""

    backend: str = "claude-native"
    model: str | None = None


class StagePipelineConfig(BaseModel):
    """Per-client (or per-lane) pipeline configuration (RFC 0005 A1, dormant)."""

    stages: list[Stage] = Field(
        default_factory=lambda: [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
    )
    executors: dict[Stage, StageExecutorConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stages_unique(self) -> StagePipelineConfig:
        if len(self.stages) != len(set(self.stages)):
            msg = "pipeline stages must be unique"
            raise ValueError(msg)
        return self


class LaneConfig(BaseModel):
    """Configuration for a named dispatch lane.

    Lanes provide a scheduling boundary for TicketTasks.
    Phase 1 (data model only): no dispatch wiring yet — see #558.
    """

    name: str
    max_parallel: int = 1
    priority: int = 0
    paused: bool = False
    description: str = ""
    reap_policy: ReapPolicy | None = None
    pipeline: StagePipelineConfig | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v:
            msg = "lane name must be non-empty"
            raise ValueError(msg)
        return v


_USAGE_LIMIT_BACKOFF_SECONDS = 3600


class OrchestratorConfig(BaseModel):
    """Parsed contents of orchestrator.yaml.

    ``default_max_parallel`` is the cap applied to any client missing from
    ``per_client_max_parallel``. The legacy yaml layout placed this value
    under ``per_client_max_parallel.default``, but that key was treated as
    a literal client name and silently ignored (see GitHub issue #145).
    A model validator migrates any stray ``default`` key into the new
    top-level field so old configs keep working with a one-time warning.
    """

    tick_interval_seconds: int = 30
    usage_limit_backoff_seconds: int = _USAGE_LIMIT_BACKOFF_SECONDS
    per_client_max_parallel: dict[str, int] = Field(default_factory=dict)
    default_max_parallel: int = 1
    linear_prefix_map: dict[str, str] = Field(default_factory=dict)
    # Per-tier wall-clock budgets (seconds) for headless DAEMON sessions.
    # Keyed by scope.tier from the auto-dev sentinel; unknown tiers fall back
    # to HEADLESS_TIMEOUT_SECONDS. See GitHub issue #265.
    headless_timeout_by_tier: dict[str, int] = Field(
        default_factory=lambda: {"small": 1800, "large": 5400}
    )
    # Per-tier idle-watchdog budgets (seconds). Keyed by TicketTask.scope_hint;
    # unknown tiers fall back to IDLE_WATCHDOG_SECONDS (900s). Large-tier
    # sessions can legitimately stall longer on slow tests/mypy before emitting
    # any sentinel. See GitHub issues #326, #340.
    idle_watchdog_by_tier: dict[str, int] = Field(
        default_factory=lambda: {"large": 1800}
    )
    # Global idle-watchdog budget (seconds) applied when a session has no
    # per-ticket override and no resolvable per-tier budget (e.g. it stalled
    # before Stage 1 set a scope_hint). ``None`` falls back to the
    # IDLE_WATCHDOG_SECONDS constant (900s). Raise this so the watchdog does
    # not reap workers still mid-plan/mid-review — 15 min is too short for a
    # full plan+review pass. See the 2026-05-30 fanout-cascade RCA.
    idle_watchdog_seconds: int | None = None
    # Per-tier cap on idle-stall auto-retries before a headless worker is
    # parked BLOCKED_ON_USER for the operator. Keyed by TicketTask.scope_hint;
    # unknown tiers fall back to DEFAULT_IDLE_RETRY_CAP. See GitHub issue #384.
    idle_retry_cap_by_tier: dict[str, int] = Field(default_factory=dict)
    # Per-tier cap on wall-clock-budget (stalled stage) retries before a
    # headless worker is parked BLOCKED_ON_USER instead of re-queued to PENDING.
    # Keyed by TicketTask.scope_hint; unknown tiers fall back to
    # DEFAULT_STALLED_RETRY_CAP. See GitHub issue #756.
    stalled_retry_cap_by_tier: dict[str, int] = Field(default_factory=dict)
    # Number of consecutive failed idle-watchdog observations required before a
    # session is dispositioned (reaped/parked/git-salvaged). 1 reproduces the
    # pre-#545 single-observation behavior. See GitHub #545.
    idle_confirm_observations: int = 2
    # Gating policy for destructive reap actions (stop daemon, revert task to
    # PENDING, remove worktree). Default ``signal_only`` routes stalled/phantom
    # sessions to BLOCKED_ON_USER for operator review; ``auto`` restores the
    # pre-#554 self-healing behavior. See ADR-0006 invariant 4 and GitHub #554.
    reap_policy: ReapPolicy = ReapPolicy.SIGNAL_ONLY
    # Elapsed seconds before reconcile attempts to route an emitted-but-unrouted
    # sentinel (signal_stop never fired). Shorter than the idle watchdog budget
    # because an emitted sentinel is positive evidence the worker completed.
    # See GitHub #578.
    sentinel_unrouted_check_seconds: int = 300
    # RFC 0004 Phase 2 — two-knob scheduler (#558)
    # Tier-1: limit how many clients are eligible per tick.
    # None = no limit (today's behavior preserved).
    max_parallel_clients: int | None = None
    # Tier-2: per-client ceiling across all lanes. Takes precedence over the
    # legacy per_client_max_parallel / default_max_parallel fields; those are
    # migrated on load via _migrate_legacy_ceiling_fields and kept as deprecated
    # aliases for one release.
    per_client_ceiling: dict[str, int] = Field(default_factory=dict)
    default_ceiling: int = 1

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_ceiling_fields(cls, data: object) -> object:
        """Lift legacy per_client_max_parallel / default_max_parallel into new fields.

        The new per_client_ceiling / default_ceiling fields take precedence when
        both are present. Legacy fields are kept as deprecated aliases and still
        populate OrchestratorConfig for one release — callers using the legacy
        field name directly will see the same value via the new field.
        """
        if not isinstance(data, dict):
            return data
        has_new_ceiling = "per_client_ceiling" in data or "default_ceiling" in data
        legacy_per_client = data.get("per_client_max_parallel")
        legacy_default = data.get("default_max_parallel")
        if not has_new_ceiling:
            if isinstance(legacy_per_client, dict) and legacy_per_client:
                data.setdefault("per_client_ceiling", dict(legacy_per_client))
                logging.getLogger(__name__).warning(
                    "OrchestratorConfig: per_client_max_parallel is deprecated; "
                    "use per_client_ceiling instead"
                )
            if isinstance(legacy_default, int):
                data.setdefault("default_ceiling", legacy_default)
                logging.getLogger(__name__).warning(
                    "OrchestratorConfig: default_max_parallel is deprecated; "
                    "use default_ceiling instead"
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _coerce_reap_policy(cls, data: object) -> object:
        """Coerce invalid/absent reap_policy to signal_only (fail-safe, ADR-0006)."""
        if not isinstance(data, dict):
            return data
        val = data.get("reap_policy")
        if not isinstance(val, str) or val not in {p.value for p in ReapPolicy}:
            data["reap_policy"] = ReapPolicy.SIGNAL_ONLY
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_default_key(cls, data: object) -> object:
        """Lift a stray ``per_client_max_parallel.default`` into the top field.

        Only fires when the caller has not already set ``default_max_parallel``
        explicitly — explicit configuration wins. The legacy key is removed
        from the per-client dict so it doesn't shadow real client names.
        """
        if not isinstance(data, dict):
            return data
        per_client = data.get("per_client_max_parallel")
        if not isinstance(per_client, dict):
            return data
        legacy = per_client.pop("default", None)
        if legacy is None:
            return data
        if "default_max_parallel" not in data:
            data["default_max_parallel"] = legacy
        return data


class HookRule(BaseModel):
    """A user-defined shell command to run when a lifecycle event fires."""

    event_type: str
    command: str
    description: str = ""


class EventHookRegistry(BaseModel):
    """Persisted event hook rules for a client."""

    rules: list[HookRule] = Field(default_factory=list)


class Session(BaseModel):
    """A tracked Claude Code session."""

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


DEFAULT_AUTO_PURPOSES: list[SessionPurpose] = [
    SessionPurpose.IDEA,
    SessionPurpose.IMPL,
    SessionPurpose.DEBT,
]


class ClientConfig(BaseModel):
    """Configuration for a client workspace.

    Two modes:
    - **Legacy**: ``workspace_path`` points to an existing clone.
    - **Worktree**: ``repo_path`` + ``branch`` are set.  ``workspace_path``
      is auto-set to ``repo_path`` as a sentinel; the real worktree path is
      resolved at session start time.
    """

    name: str
    # Typed as Path but defaults to None; the model validator below guarantees
    # it is always set after construction (from either the user or repo_path).
    workspace_path: Path = Field(default=None)  # type: ignore[assignment]
    repo_path: Path | None = None
    branch: str | None = None
    default_branch: str = "main"
    # Prefix for the per-ticket feature branch the staged pipeline provisions
    # and the auto-dev skills push to: ``<feature_branch_prefix>/<ticket_id>``
    # (e.g. ``dev/662``). Single source of truth shared by cw's worktree
    # provisioning (dispatch) and the skill's branch_pattern, so cw and the
    # worker agree on one branch — no mid-pipeline rename that would trip the
    # worktree-reuse guard (#712). Distinct from the session-name prefix
    # ``auto-dev/`` (AUTO_DEV_LABEL_PREFIX), which stays for reconcile's
    # ticket-from-session-name parsing.
    feature_branch_prefix: str = "dev"
    worktree_base: Path | None = None
    auto_purposes: list[SessionPurpose] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_PURPOSES),
    )
    purpose_prompts: dict[str, str] = Field(default_factory=dict)
    # When set, ``cw`` passes ``--model <worker_model>`` to ``claude --bg``
    # for DAEMON-origin spawns (auto-dev workers, including resume re-spawns
    # in :func:`cw.session.resume_session`). Opaque string — no validation;
    # user is responsible for matching Anthropic's published model ids.
    # Default ``None`` inherits the user's logged-in default model.
    # See issue #248.
    worker_model: str | None = None
    auto_background_threshold: int | None = None
    notifications: bool = False
    lanes: list[LaneConfig] = Field(default_factory=list)
    # RFC 0005 A1 — dormant pipeline config; no dispatch wiring yet (#612).
    pipeline: StagePipelineConfig = Field(default_factory=StagePipelineConfig)

    @property
    def effective_lanes(self) -> list[LaneConfig]:
        """Return declared lanes; synthesize a default lane when none are declared."""
        if self.lanes:
            return list(self.lanes)
        return [LaneConfig(name=DEFAULT_LANE)]

    @model_validator(mode="after")
    def _validate_path_config(self) -> ClientConfig:
        has_workspace = self.workspace_path is not None
        has_repo = self.repo_path is not None and self.branch is not None

        if not has_workspace and not has_repo:
            msg = "Either workspace_path or both repo_path + branch must be set"
            raise ValueError(msg)

        if self.repo_path is not None and not has_workspace:
            # Sentinel: real path resolved at start time via create_worktree
            self.workspace_path = self.repo_path

        return self

    @property
    def is_worktree_client(self) -> bool:
        """True when this client uses repo_path + branch (worktree mode)."""
        return self.repo_path is not None and self.branch is not None


class CwState(BaseModel):
    """Persisted state across all sessions."""

    schema_version: int = CW_STATE_SCHEMA_VERSION
    sessions: list[Session] = Field(default_factory=list)

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.ACTIVE]

    def backgrounded_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.BACKGROUNDED]

    def idled_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.IDLE]

    def find_session(self, client: str, purpose: str) -> Session | None:
        """Find the most recent session for a client+purpose combo."""
        matches = [
            s
            for s in self.sessions
            if s.client == client
            and s.purpose == purpose
            and s.status != SessionStatus.COMPLETED
        ]
        if not matches:
            return None
        return max(matches, key=lambda s: s.started_at)

    def find_by_name_or_id(self, identifier: str) -> Session | None:
        """Find a session by name (client/purpose) or ID."""
        for s in reversed(self.sessions):
            if identifier in (s.name, s.id):
                return s
        return None
