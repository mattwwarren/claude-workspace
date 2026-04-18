"""Shared test fixtures for cw test suite."""

from __future__ import annotations

import contextlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config module paths to tmp_path."""
    config_dir = tmp_path / ".config" / "cw"
    state_dir = tmp_path / ".local" / "share" / "cw"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    clients_file = config_dir / "clients.yaml"
    state_file = state_dir / "sessions.json"

    history_dir = state_dir / "history"
    history_dir.mkdir(parents=True)
    queues_dir = state_dir / "queues"
    queues_dir.mkdir(parents=True)
    events_dir = state_dir / "events"
    events_dir.mkdir(parents=True)

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", clients_file)
    monkeypatch.setattr("cw.config.STATE_FILE", state_file)
    monkeypatch.setattr("cw.config.HISTORY_DIR", history_dir)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    # Also patch module-level imported references
    monkeypatch.setattr("cw.history.HISTORY_DIR", history_dir)
    with contextlib.suppress(AttributeError):
        monkeypatch.setattr("cw.events.EVENTS_DIR", events_dir)
    with contextlib.suppress(AttributeError):
        monkeypatch.setattr("cw.pr_responder.STATE_DIR", state_dir)

    return tmp_path


@pytest.fixture
def tmp_state_dir(tmp_config_dir: Path) -> Path:
    """Return the state directory within tmp_config_dir."""
    return tmp_config_dir / ".local" / "share" / "cw"


@pytest.fixture
def sample_client(tmp_path: Path) -> ClientConfig:
    """A ClientConfig pointing at tmp_path."""
    workspace = tmp_path / "workspace" / "test-project"
    workspace.mkdir(parents=True)
    return ClientConfig(
        name="test-client",
        workspace_path=workspace,
        default_branch="main",
    )


@pytest.fixture
def sample_session(sample_client: ClientConfig) -> Session:
    """A Session with known values."""
    return Session(
        id="abcd1234",
        name="test-client/impl",
        client="test-client",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=sample_client.workspace_path,
        surface_ref="impl",
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_state(sample_client: ClientConfig) -> CwState:
    """A CwState with a mix of active/backgrounded/completed sessions."""
    return CwState(
        sessions=[
            Session(
                id="sess0001",
                name="test-client/impl",
                client="test-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="sess0002",
                name="test-client/idea",
                client="test-client",
                purpose=SessionPurpose.IDEA,
                status=SessionStatus.BACKGROUNDED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 9, 0, 0, tzinfo=UTC),
                backgrounded_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
                last_handoff_path=(
                    sample_client.workspace_path / ".handoffs" / "session-abc.md"
                ),
            ),
            Session(
                id="sess0003",
                name="other-client/impl",
                client="other-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.COMPLETED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 14, 8, 0, 0, tzinfo=UTC),
            ),
        ]
    )


@pytest.fixture
def mock_cmux_adapter() -> FakeCmuxAdapter:
    """A FakeCmuxAdapter for testing session operations."""
    return FakeCmuxAdapter()


@pytest.fixture
def sample_handoff_file(tmp_path: Path) -> Path:
    """Create a .handoffs/session-*.md with valid resumption prompt."""
    handoffs_dir = tmp_path / "workspace" / "test-project" / ".handoffs"
    handoffs_dir.mkdir(parents=True)
    handoff = handoffs_dir / "session-test123.md"
    handoff.write_text(
        "# Session Handoff\n\n"
        "## Summary\n\n"
        "Did some work on the feature.\n\n"
        "## Resumption Prompt\n\n"
        "Use this to resume:\n\n"
        "```\n"
        "Continue working on the auth feature. The login endpoint is done,\n"
        "but the signup endpoint still needs validation.\n"
        "```\n"
    )
    return handoff


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[[str], Path]:
    """Factory fixture to create git repos in tmp_path."""

    def _make(name: str) -> Path:
        repo = tmp_path / name
        repo.mkdir(parents=True, exist_ok=True)
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(
            ["git", "init", str(repo)],
            capture_output=True,
            check=True,
            env=clean_env,
        )
        return repo

    return _make
