"""Tests for cw.dispatch - tick-based dispatch loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.dispatch import consume_completed_sessions, dispatch_tick
from cw.events import record_event
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dispatch_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect all config/state/events/dev-queue paths to tmp_path."""
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
    dev_queue_file = state_dir / "dev_queue.json"
    dev_queue_lock = state_dir / ".dev_queue.lock"

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", clients_file)
    monkeypatch.setattr("cw.config.STATE_FILE", state_file)
    monkeypatch.setattr("cw.config.HISTORY_DIR", history_dir)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", dev_queue_lock)

    # Patch module-level imported references
    monkeypatch.setattr("cw.events.EVENTS_DIR", events_dir)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_LOCK", dev_queue_lock)

    return tmp_path


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Return a workspace directory for a fake client."""
    ws = tmp_path / "workspace" / "test-project"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
def sample_client_config(workspace_dir: Path) -> ClientConfig:
    """A ClientConfig for use with dispatch tests."""
    return ClientConfig(
        name="test-client",
        workspace_path=workspace_dir,
        default_branch="main",
    )


@pytest.fixture
def simple_config() -> OrchestratorConfig:
    """OrchestratorConfig with cap=1 for test-client."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 1},
    )


@pytest.fixture
def cap2_config() -> OrchestratorConfig:
    """OrchestratorConfig with cap=2 for test-client."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 2},
    )


def _make_clients_yaml(tmp_path: Path, client: ClientConfig) -> None:
    """Write a minimal clients.yaml for the given client."""
    config_dir = tmp_path / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    clients_file = config_dir / "clients.yaml"
    clients_file.write_text(
        f"clients:\n"
        f"  {client.name}:\n"
        f"    workspace_path: {client.workspace_path}\n"
        f"    default_branch: {client.default_branch}\n"
    )


# ---------------------------------------------------------------------------
# TestDispatchTickSpawnsSession
# ---------------------------------------------------------------------------


class TestDispatchTickSpawnsSession:
    def test_dispatch_tick_spawns_session(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Enqueue one task, run tick, confirm session created and task is RUNNING."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-100",
            client="test-client",
        )
        add_ticket(task)

        adapter = FakeCmuxAdapter()
        spawned = dispatch_tick(simple_config, adapter=adapter)

        assert spawned == 1

        # Task should now be RUNNING
        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "GEN-100"
        assert running[0].status == QueueItemStatus.RUNNING

        # Session should have been created
        state = load_state()
        assert len(state.sessions) == 1
        sess = state.sessions[0]
        assert sess.origin == SessionOrigin.DAEMON
        assert "GEN-100" in sess.name

        # Adapter should have been called
        assert len(adapter.calls["spawn"]) == 1


# ---------------------------------------------------------------------------
# TestPerClientCapRespected
# ---------------------------------------------------------------------------


class TestPerClientCapRespected:
    def test_per_client_cap_respected(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """Enqueue 3 tasks, cap=2, tick spawns exactly 2."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        for i in range(3):
            add_ticket(TicketTask(ticket_id=f"GEN-{i}", client="test-client"))

        adapter = FakeCmuxAdapter()
        spawned = dispatch_tick(cap2_config, adapter=adapter)

        assert spawned == 2
        assert len(adapter.calls["spawn"]) == 2

        store = load_dev_queue()
        assert len(store.running()) == 2
        assert len(store.pending()) == 1


# ---------------------------------------------------------------------------
# TestNoDoubleDispatch
# ---------------------------------------------------------------------------


class TestNoDoubleDispatch:
    def test_no_double_dispatch(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Two ticks with cap=1: second tick skips (task already RUNNING)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        add_ticket(TicketTask(ticket_id="GEN-200", client="test-client"))

        adapter = FakeCmuxAdapter()

        # First tick claims the task
        spawned1 = dispatch_tick(simple_config, adapter=adapter)
        assert spawned1 == 1

        # Second tick: running_count=1 >= cap=1, should skip
        spawned2 = dispatch_tick(simple_config, adapter=adapter)
        assert spawned2 == 0

        # Only one spawn call total
        assert len(adapter.calls["spawn"]) == 1

        store = load_dev_queue()
        assert len(store.running()) == 1


# ---------------------------------------------------------------------------
# TestConsumeCompletesTasks
# ---------------------------------------------------------------------------


class TestConsumeCompletesTasks:
    def test_consume_completes_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Write a session.completed event with ticket_id; task becomes COMPLETED."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Put a RUNNING task in the queue
        task = TicketTask(
            ticket_id="GEN-300",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        # Write a session.completed event referencing the ticket
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-300", "client": "test-client"},
        )

        completed = consume_completed_sessions()
        assert completed == 1

        updated_store = load_dev_queue()
        assert updated_store.tasks[0].status == QueueItemStatus.COMPLETED

    def test_consume_ignores_events_without_ticket_id(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """session.completed events without ticket_id do not affect the queue."""
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"session_id": "some-session", "client": "other-client"},
        )

        completed = consume_completed_sessions()
        assert completed == 0

    def test_consume_advances_cursor(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """Cursor advances after consuming so events aren't re-processed."""
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-400", "client": "test-client"},
        )

        # First consume: returns 0 because no matching task in queue
        consume_completed_sessions()

        # Second consume: cursor advanced, no new events
        completed2 = consume_completed_sessions()
        assert completed2 == 0


# ---------------------------------------------------------------------------
# TestDispatchTickReturnsCount
# ---------------------------------------------------------------------------


class TestDispatchTickReturnsCount:
    def test_dispatch_tick_returns_count(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """Enqueue 2 tasks with cap=2; tick returns 2."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        add_ticket(TicketTask(ticket_id="GEN-500", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-501", client="test-client"))

        adapter = FakeCmuxAdapter()
        count = dispatch_tick(cap2_config, adapter=adapter)

        assert count == 2

    def test_dispatch_tick_returns_zero_when_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Empty queue returns 0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        adapter = FakeCmuxAdapter()
        count = dispatch_tick(simple_config, adapter=adapter)

        assert count == 0

    def test_running_sessions_count_toward_cap(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Pre-existing DAEMON ACTIVE sessions count toward the per-client cap."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Put an already-running session in state
        existing_session = Session(
            name="test-client/auto-dev/GEN-999",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
        )
        state = CwState(sessions=[existing_session])
        save_state(state)

        # Enqueue a task
        add_ticket(TicketTask(ticket_id="GEN-600", client="test-client"))

        adapter = FakeCmuxAdapter()
        # cap=1, running=1 => should not spawn
        count = dispatch_tick(simple_config, adapter=adapter)

        assert count == 0
        assert len(adapter.calls["spawn"]) == 0
