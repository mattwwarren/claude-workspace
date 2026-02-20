"""Shared test fixtures for cw test suite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus


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

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", clients_file)
    monkeypatch.setattr("cw.config.STATE_FILE", state_file)
    monkeypatch.setattr("cw.config.HISTORY_DIR", history_dir)
    # Also patch history module's imported reference
    monkeypatch.setattr("cw.history.HISTORY_DIR", history_dir)

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
        zellij_pane="impl",
        zellij_tab="test-client",
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
                name="test-client/review",
                client="test-client",
                purpose=SessionPurpose.REVIEW,
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
def mock_zellij(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, list[tuple[object, ...]]]:
    """Patch all cw.zellij functions used by session.py, returning call tracker."""
    calls: dict[str, list[tuple[object, ...]]] = {
        "is_installed": [],
        "in_zellij_session": [],
        "resolve_session_target": [],
        "session_exists": [],
        "generate_layout": [],
        "create_and_attach": [],
        "attach_session": [],
        "go_to_tab": [],
        "focus_pane": [],
        "write_to_pane": [],
        "check_pane_health": [],
        "new_pane": [],
        "new_tab": [],
    }

    def _is_installed() -> bool:
        calls["is_installed"].append(())
        return True

    def _in_zellij() -> bool:
        calls["in_zellij_session"].append(())
        return False

    def _resolve_session_target(default: str) -> str | None:
        calls["resolve_session_target"].append((default,))
        # Mock is "outside zellij" by default, so return the default
        return default

    def _session_exists(name: str) -> bool:
        calls["session_exists"].append((name,))
        return False

    def _generate_layout(c: object, **kwargs: object) -> Path:
        calls["generate_layout"].append((c, kwargs))
        return Path(tmp_path / "layout.kdl")

    def _create_and_attach(s: str, lp: object) -> None:
        calls["create_and_attach"].append((s, lp))

    def _attach(s: str) -> None:
        calls["attach_session"].append((s,))

    def _go_to_tab(t: str, session: str | None = None) -> None:
        calls["go_to_tab"].append((t, session))

    def _focus_pane(p: str, session: str | None = None) -> None:
        calls["focus_pane"].append((p, session))

    def _write_to_pane(t: str, session: str | None = None) -> None:
        calls["write_to_pane"].append((t, session))

    def _check_pane_health(
        session: str | None = None, tab_name: str | None = None,
    ) -> dict[str, bool]:
        calls["check_pane_health"].append((session, tab_name))
        return {}

    def _new_pane(
        command: str, **kwargs: object,
    ) -> None:
        calls["new_pane"].append((command, kwargs))

    def _new_tab(
        client: object, **kwargs: object,
    ) -> None:
        calls["new_tab"].append((client, kwargs))

    monkeypatch.setattr("cw.zellij.new_pane", _new_pane)
    monkeypatch.setattr("cw.zellij.new_tab", _new_tab)
    monkeypatch.setattr("cw.zellij.is_installed", _is_installed)
    monkeypatch.setattr(
        "cw.zellij.in_zellij_session", _in_zellij
    )
    monkeypatch.setattr(
        "cw.zellij.resolve_session_target", _resolve_session_target
    )
    monkeypatch.setattr(
        "cw.zellij.session_exists", _session_exists
    )
    monkeypatch.setattr(
        "cw.zellij.generate_layout", _generate_layout
    )
    monkeypatch.setattr(
        "cw.zellij.create_and_attach", _create_and_attach
    )
    monkeypatch.setattr(
        "cw.zellij.attach_session", _attach
    )
    monkeypatch.setattr("cw.zellij.go_to_tab", _go_to_tab)
    monkeypatch.setattr(
        "cw.zellij.focus_pane", _focus_pane
    )
    monkeypatch.setattr(
        "cw.zellij.write_to_pane", _write_to_pane
    )
    monkeypatch.setattr(
        "cw.zellij.check_pane_health", _check_pane_health
    )

    return calls


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
