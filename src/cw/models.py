"""Pydantic models for session state and client configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"
    EXPLORE = "explore"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"


class CompletionReason(StrEnum):
    USER = "user"
    HANDOFF = "handoff"
    CRASHED = "crashed"


class HandoffReason(StrEnum):
    """Known reasons for abnormal session endings via /handoff."""

    CONTEXT = "context"
    DEBUG_FORK = "debug-fork"
    SCOPE = "scope"


class SessionOrigin(StrEnum):
    USER = "user"
    DELEGATE = "delegate"
    DAEMON = "daemon"


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskSpec(BaseModel):
    """Machine-parseable task specification for agent-to-agent handoffs."""

    description: str
    purpose: SessionPurpose
    prompt: str
    context_files: list[str] = Field(default_factory=list)
    success_criteria: str | None = None
    source_session: str | None = None


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
    zellij_pane: str | None = None
    zellij_tab: str | None = None
    claude_session_id: UUID = Field(default_factory=uuid4)
    last_handoff_path: Path | None = None
    auto_backgrounded: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backgrounded_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_reason: CompletionReason | None = None
    completed_at: datetime | None = None


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

    sessions: list[Session] = Field(default_factory=list)

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.ACTIVE]

    def backgrounded_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.BACKGROUNDED]

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

    def client_sessions(self, client: str) -> list[Session]:
        """All sessions for a client, regardless of status."""
        return [s for s in self.sessions if s.client == client]

    def active_for_client(self, client: str) -> list[Session]:
        """Active and backgrounded sessions for a client."""
        return [
            s
            for s in self.sessions
            if s.client == client
            and s.status in (SessionStatus.ACTIVE, SessionStatus.BACKGROUNDED)
        ]

    def sibling_sessions(self, session: Session) -> list[Session]:
        """Non-completed sessions for the same client, excluding the given session."""
        return [
            s
            for s in self.sessions
            if s.client == session.client
            and s.id != session.id
            and s.status != SessionStatus.COMPLETED
        ]
