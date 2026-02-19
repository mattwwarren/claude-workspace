"""Pydantic models for session state and client configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SessionPurpose(StrEnum):
    IMPL = "impl"
    REVIEW = "review"
    DEBT = "debt"
    EXPLORE = "explore"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"


class Session(BaseModel):
    """A tracked Claude Code session."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    name: str  # Human-readable: "client-a/impl"
    client: str
    purpose: SessionPurpose
    status: SessionStatus = SessionStatus.ACTIVE
    workspace_path: Path
    worktree_path: Path | None = None
    branch: str | None = None
    zellij_pane: str | None = None
    zellij_tab: str | None = None
    claude_session_id: UUID = Field(default_factory=uuid4)
    last_handoff_path: Path | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backgrounded_at: datetime | None = None
    resumed_at: datetime | None = None


_DEFAULT_AUTO_PURPOSES: list[SessionPurpose] = [
    SessionPurpose.IMPL,
    SessionPurpose.REVIEW,
    SessionPurpose.DEBT,
]


class ClientConfig(BaseModel):
    """Configuration for a client workspace."""

    name: str
    workspace_path: Path
    default_branch: str = "main"
    worktree_base: Path | None = None
    auto_purposes: list[SessionPurpose] = Field(
        default_factory=lambda: list(_DEFAULT_AUTO_PURPOSES),
    )
    purpose_prompts: dict[str, str] = Field(default_factory=dict)


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
