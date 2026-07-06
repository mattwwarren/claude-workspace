"""Shared test fixtures for cw test suite."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.config import save_state
from cw.models import (
    ClientConfig,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient

if TYPE_CHECKING:
    from collections.abc import Callable


def _seed_daemon_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    session_id: str = "test1234",
    client: str = "test-client",
    name: str | None = None,
    surface_ref: str | None = "fake-pane-99",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """Create and save a daemon session in state."""
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    sess = Session(
        id=session_id,
        name=name or f"{client}/auto-dev/GEN-42",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=status,
        workspace_path=workspace,
        surface_ref=surface_ref,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    return sess


def _write_idle_transcript(
    home: Path,
    worktree: Path,
    filename: str = "fake-short-id-sess.jsonl",
) -> Path:
    """Write a minimal transcript .jsonl under the project dir for *worktree*.

    Default filename starts with ``fake-short-id`` so that
    ``_locate_session_transcript``'s surface_ref-prefix glob finds it when the
    session has ``surface_ref="fake-short-id"`` (the default in
    ``_mk_headless_daemon_session``).
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    record = '{"type": "assistant", "message": {"role": "assistant", "content": []}}\n'
    path.write_text(record)
    return path


def _make_daemon_session(
    *, claude_session_id: str | None = None, surface_ref: str = "live-ref"
) -> Session:
    return Session(
        id="sess-1",
        name="client-a/auto-dev/T-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref=surface_ref,
        claude_session_id=claude_session_id,
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every cw state/config path to ``tmp_path``.

    Autouse so no test can accidentally touch ``~/.local/share/cw`` or
    ``~/.config/cw``. Consumers read paths via ``cw.config`` accessor
    functions, so patching the module-level constants here reaches every
    caller — individual test files should not need to patch module-local
    bindings. Attribute names must match exactly; any drift fails loudly
    rather than being swallowed.
    """
    config_dir = tmp_path / ".config" / "cw"
    state_dir = tmp_path / ".local" / "share" / "cw"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", config_dir / "clients.yaml")
    monkeypatch.setattr("cw.config.STATE_FILE", state_dir / "sessions.json")
    monkeypatch.setattr("cw.config.QUEUES_DIR", state_dir / "queues")
    monkeypatch.setattr("cw.config.EVENTS_DIR", state_dir / "events")
    monkeypatch.setattr("cw.config.HISTORY_DIR", state_dir / "history")
    monkeypatch.setattr("cw.config.PR_WATCHER_DIR", state_dir / "pr_watcher")
    monkeypatch.setattr("cw.config.REVIEW_MONITOR_DIR", tmp_path / "review-monitor")
    monkeypatch.setattr(
        "cw.config.ORCHESTRATOR_CONFIG_DIR", tmp_path / ".claude-workspace"
    )
    monkeypatch.setattr(
        "cw.config.ORCHESTRATOR_CONFIG_FILE",
        tmp_path / ".claude-workspace" / "orchestrator.yaml",
    )
    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", state_dir / "dev_queue.json")
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", state_dir / ".dev_queue.lock")
    monkeypatch.setattr("cw.config.DEV_PLAN_FILE", state_dir / "dev_plan.json")
    monkeypatch.setattr("cw.config.DEV_PLAN_LOCK", state_dir / ".dev_plan.lock")
    monkeypatch.setattr("cw.config.DEV_PLAN_OUTPUT_DIR", state_dir / "plan_output")
    monkeypatch.setattr("cw.config.SESSIONS_LOCK", state_dir / ".sessions.lock")
    monkeypatch.setattr("cw.config.CLIENTS_LOCK", config_dir / ".clients.yaml.lock")
    monkeypatch.setattr(
        "cw.config.DISPATCH_STATE_FILE", state_dir / "dispatch_state.json"
    )
    monkeypatch.setattr(
        "cw.config.CONCURRENCY_OVERRIDE_FILE",
        state_dir / "concurrency_overrides.json",
    )
    monkeypatch.setattr(
        "cw.config.CONCURRENCY_OVERRIDE_LOCK",
        state_dir / ".concurrency_overrides.lock",
    )

    # Redirect the native-daemon roster path so tests don't read the
    # user's real ~/.claude/daemon/roster.json. RealNativeDaemonClient
    # tolerates a missing file (returns empty set), so this isolates the
    # native side of reconcile for any test that doesn't explicitly
    # inject a fake daemon client.
    monkeypatch.setattr(
        "cw.native_daemon._ROSTER_PATH",
        tmp_path / ".claude" / "daemon" / "roster.json",
    )

    # Stub _claude_agents_json so tests don't invoke the real ``claude``
    # binary. Tests that want specific liveness behaviour override this with
    # their own monkeypatch.setattr call; pytest patches stack and the
    # test-level patch wins.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        list,
    )

    return tmp_path


@pytest.fixture(autouse=True)
def _mock_push_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop tests from firing real desktop notifications.

    ``cw.notify.fire_push_notification`` spawns a daemon thread that shells
    out to ``notify-send`` and ``peon.sh``. On a machine with a window manager
    that means every reconcile attention-path under test floods the desktop
    with real notifications (and can wedge the WM). Every production call site
    (``reconcile.idle``/``tasks``/``salvage``) reaches the helper through the
    re-export at ``cw.reconcile._deps.fire_push_notification``, so patching that
    one seam autouse guarantees no test fires for real — even ones that forget
    to mock it themselves.

    Tests that assert on the call (``test_reconcile.py``) re-patch the same name
    inside the test; pytest patches stack and the test-level patch wins.
    ``test_notify.py`` exercises the real helper via ``cw.notify`` directly and
    is unaffected. Attribute name must match exactly; drift fails loudly.
    """
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification",
        MagicMock(name="fire_push_notification"),
    )


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
def mock_native_daemon() -> FakeNativeDaemonClient:
    """A FakeNativeDaemonClient for testing daemon-origin spawn and reconcile."""
    return FakeNativeDaemonClient()


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[[str], Path]:
    """Factory fixture to create git repos in tmp_path.

    Initialises with a single empty commit on ``main`` so callers that
    invoke ``git worktree add`` (notably dispatch / pr_responder tests)
    have a real commit to branch from. Sets per-repo user.name/email so
    the commit succeeds without a global git config (CI runners often
    lack one).
    """

    def _make(name: str) -> Path:
        repo = tmp_path / name
        repo.mkdir(parents=True, exist_ok=True)
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def _git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                check=True,
                env=clean_env,
            )

        _git("init", "-b", "main")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "cw test")
        _git("commit", "--allow-empty", "-m", "initial")
        return repo

    return _make
