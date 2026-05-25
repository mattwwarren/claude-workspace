"""Pydantic models for session state and client configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"


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


# Schema versions for persisted state. Bump when making a breaking change
# to the on-disk layout; add a migration in `cw.config.migrate_cw_state`
# or `cw.dev_queue.migrate_dev_queue` to handle older versions.
CW_STATE_SCHEMA_VERSION = 3
DEV_QUEUE_SCHEMA_VERSION = 1


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


class OrchestratorEventType(StrEnum):
    """Event types for the orchestrator-level event bus.

    Covers PR lifecycle, ticket queue, and cross-session coordination.
    """

    TICKET_ENQUEUED = "ticket.enqueued"
    SESSION_SPAWNED = "session.spawned"
    SESSION_COMPLETED = "session.completed"
    SESSION_TIMED_OUT = "session.timed_out"
    TICKET_NEEDS_SYNC = "ticket.needs_sync"
    STAGE_ENTERED = "stage.entered"
    STAGE_ERRORED = "stage.errored"
    PR_REGISTERED = "pr.registered"
    PR_CI_FAILED = "pr.ci_failed"
    PR_REVIEW_RECEIVED = "pr.review_received"
    PR_MERGEABLE = "pr.mergeable"
    PR_MERGED = "pr.merged"


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

    def by_client(self, client: str) -> list[TicketTask]:
        return [t for t in self.tasks if t.client == client]


class BackendName(StrEnum):
    """Name of a multiplexer backend cw can drive."""

    CMUX = "cmux"
    TMUX = "tmux"
    FAKE = "fake"


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
    per_client_max_parallel: dict[str, int] = Field(default_factory=dict)
    default_max_parallel: int = 1
    linear_prefix_map: dict[str, str] = Field(default_factory=dict)
    backend: BackendName | None = None

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
    last_handoff_path: Path | None = None
    claude_session_id: str | None = None
    auto_backgrounded: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    idle_at: datetime | None = None
    backgrounded_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_reason: CompletionReason | None = None
    completed_at: datetime | None = None
    parent_session_id: str | None = None
    worker_session_ids: list[str] = Field(default_factory=list)
    # Sentinel-block summary parsed from a headless /auto-dev worker's stdout
    # at completion time. ``None`` for any session that didn't run headless or
    # whose stdout could not be parsed. Stored as a raw dict (rather than the
    # AutoDevResult Pydantic model) so the persisted state file remains
    # readable when the result schema bumps independently of cw's CW_STATE
    # schema. See ``cw.auto_dev_result`` for the parser.
    last_result: dict[str, Any] | None = None


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
    worktree_base: Path | None = None
    auto_purposes: list[SessionPurpose] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_PURPOSES),
    )
    purpose_prompts: dict[str, str] = Field(default_factory=dict)
    auto_background_threshold: int | None = None
    notifications: bool = False
    cmux_workspace: str | None = None

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
            if s.name == identifier or s.id == identifier:
                return s
        return None
