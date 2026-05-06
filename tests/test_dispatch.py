"""Tests for cw.dispatch - tick-based dispatch loop."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue, save_plan
from cw.dispatch import (
    consume_completed_sessions,
    dispatch_tick,
    persist_last_result,
)
from cw.events import record_event
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    DispatchPlan,
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
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dispatch_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; state isolation is handled by the autouse fixture."""
    return tmp_config_dir


@pytest.fixture
def workspace_dir(make_git_repo: Callable[[str], Path]) -> Path:
    """Return a real git repo to host the fake client.

    dispatch_tick now calls ``create_worktree`` on this dir, so it must
    be a real git repo with at least one commit.
    """
    return make_git_repo("workspace/test-project")


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

    def test_dispatch_tick_appends_headless_flag(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """dispatch_tick spawns auto-dev with the --headless flag.

        Workers spawned via cw dev-queue must run headless so their
        machine-readable AUTO_DEV_RESULT block is parseable by the
        orchestrator.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-300", client="test-client"))

        adapter = FakeCmuxAdapter()
        dispatch_tick(simple_config, adapter=adapter)

        assert len(adapter.calls["spawn"]) == 1
        _workspace, command, _surface = adapter.calls["spawn"][0]
        assert "/auto-dev GEN-300 --headless" in command

    def test_dispatch_tick_links_workers_to_parent(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """dispatch_tick with parent= writes bidirectional linkage on every worker."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Seed an orchestrator session as the parent.
        parent_workspace = tmp_dispatch_dirs / "workspace" / "orch"
        parent_workspace.mkdir(parents=True)
        parent = Session(
            name="orch/impl",
            client="orch",
            purpose=SessionPurpose.IMPL,
            workspace_path=parent_workspace,
        )
        state = load_state()
        state.sessions.append(parent)
        save_state(state)

        for i in range(2):
            add_ticket(TicketTask(ticket_id=f"GEN-{i}", client="test-client"))

        adapter = FakeCmuxAdapter()
        spawned = dispatch_tick(cap2_config, adapter=adapter, parent=parent.id)
        assert spawned == 2

        state = load_state()
        workers = [s for s in state.sessions if s.origin == SessionOrigin.DAEMON]
        assert len(workers) == 2
        for w in workers:
            assert w.parent_session_id == parent.id

        refreshed_parent = state.find_by_name_or_id(parent.id)
        assert refreshed_parent is not None
        worker_ids = {w.id for w in workers}
        assert worker_ids.issubset(set(refreshed_parent.worker_session_ids))

    def test_dispatch_tick_no_parent_leaves_linkage_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Direct CLI run (no orchestrator): parent=None → no linkage, no error."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-400", client="test-client"))

        adapter = FakeCmuxAdapter()
        dispatch_tick(simple_config, adapter=adapter)  # parent omitted

        state = load_state()
        worker = state.sessions[0]
        assert worker.parent_session_id is None
        assert worker.worker_session_ids == []


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

    def test_consume_recovers_ticket_id_from_session_name(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Events without explicit ticket_id are recovered from session_name.

        Drains historical RUNNING tasks whose completion events predate the
        producer-side fix — see GitHub issue #94.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-700",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": "abc123",
                "session_name": "test-client/auto-dev/GEN-700",
                "client": "test-client",
                "crashed": True,
            },
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

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


class TestPersistLastResult:
    @staticmethod
    def _seed_session(session_id: str = "sess0001") -> Session:
        s = Session(
            id=session_id,
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/tmp/wt"),
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
        )
        save_state(CwState(sessions=[s]))
        return s

    def test_persists_parsed_result(self, tmp_dispatch_dirs: Path) -> None:
        self._seed_session("sess0001")
        stdout = (
            "narrative\n"
            "<<<AUTO_DEV_RESULT\n"
            '{"schema_version": 1, "ticket_id": "GEN-1", "status": "shipped",'
            ' "stage_reached": "stage5_post_create",'
            ' "scope": {"tier": "small", "files": 1, "lines_estimate": 5,'
            ' "lines_actual": 5, "forbidden_touched": false},'
            ' "plan_source": "linear_existing", "branch": "dev/gen-1",'
            ' "worktree_path": "/tmp/wt", "fork_point_sha": "abc",'
            ' "commits": ["c1"],'
            ' "pr": {"number": 1, "url": "https://example.com",'
            ' "auto_merge": true, "base": "main"},'
            ' "review": {"must_fix_initial": 0, "should_fix": 0,'
            ' "fix_cycles_used": 0},'
            ' "health": {"lowest_agent_confidence": "HIGH",'
            ' "any_incomplete_risk": false, "shortcuts": [],'
            ' "recommendation": "PROCEED", "downgrade_applied": false,'
            ' "fix_loop_escalated": false},'
            ' "friction_highlights": [], "blocker": null,'
            ' "next_actions": ["wait_for_ci"]}\n'
            "AUTO_DEV_RESULT>>>\n"
        )
        assert persist_last_result("sess0001", stdout) is True
        state = load_state()
        assert state.sessions[0].last_result is not None
        assert state.sessions[0].last_result["status"] == "shipped"

    def test_persists_blocked_for_missing_sentinel(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        self._seed_session("sess0002")
        assert persist_last_result("sess0002", "no sentinel here\n") is True
        state = load_state()
        assert state.sessions[0].last_result is not None
        assert state.sessions[0].last_result["status"] == "blocked"
        assert state.sessions[0].last_result["blocker"]["reason"] == "no_result_emitted"

    def test_returns_false_when_session_missing(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        save_state(CwState(sessions=[]))
        assert persist_last_result("nope0000", "anything") is False


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


# ---------------------------------------------------------------------------
# TestDispatchTickWithPlan
# ---------------------------------------------------------------------------


class TestDispatchTickWithPlan:
    """use_plan=True respects DispatchPlan ordering, falls back gracefully."""

    def test_use_plan_reorders_pending_claims(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """When use_plan=True, dispatch claims tickets in plan order, not enqueue."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Enqueue in order A, B, C
        add_ticket(TicketTask(ticket_id="GEN-A", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-B", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-C", client="test-client"))

        # Plan reorders to C, A, B
        save_plan(
            DispatchPlan(
                tasks=[
                    TicketTask(ticket_id="GEN-C", client="test-client"),
                    TicketTask(ticket_id="GEN-A", client="test-client"),
                    TicketTask(ticket_id="GEN-B", client="test-client"),
                ]
            )
        )

        adapter = FakeCmuxAdapter()
        # cap=2 => should claim C first, then A
        spawned = dispatch_tick(cap2_config, adapter=adapter, use_plan=True)
        assert spawned == 2

        store = load_dev_queue()
        running_ids = sorted(t.ticket_id for t in store.running())
        assert running_ids == ["GEN-A", "GEN-C"]

    def test_use_plan_missing_falls_back_to_enqueue_order(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """No persisted plan: dispatch falls back to enqueue order, no crash."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        add_ticket(TicketTask(ticket_id="GEN-FIRST", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-SECOND", client="test-client"))

        adapter = FakeCmuxAdapter()
        spawned = dispatch_tick(simple_config, adapter=adapter, use_plan=True)
        assert spawned == 1

        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "GEN-FIRST"

    def test_use_plan_drains_unplanned_after_planned(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """Planned tickets first, then unplanned still get dispatched."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        add_ticket(TicketTask(ticket_id="GEN-A", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-B", client="test-client"))

        # Plan only mentions B
        save_plan(
            DispatchPlan(tasks=[TicketTask(ticket_id="GEN-B", client="test-client")])
        )

        adapter = FakeCmuxAdapter()
        spawned = dispatch_tick(cap2_config, adapter=adapter, use_plan=True)
        # cap=2: claims B first (per plan), then A (fallback)
        assert spawned == 2

        store = load_dev_queue()
        assert len(store.running()) == 2


# ---------------------------------------------------------------------------
# TestDispatchTickReconcilePhantoms
# ---------------------------------------------------------------------------


def test_dispatch_tick_reconciles_phantoms_before_counting(
    tmp_dispatch_dirs: Path,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
) -> None:
    """Phantom DAEMON sessions do not block new dispatch."""
    _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

    # Cap = 1, one ACTIVE phantom DAEMON session for the same client,
    # and one PENDING ticket. Without reconciliation, running_count == 1
    # would equal the cap and dispatch would spawn nothing.
    save_state(
        CwState(
            sessions=[
                Session(
                    id="phantom-daemon",
                    name=f"{sample_client_config.name}/auto-dev/TKT-OLD",
                    client=sample_client_config.name,
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client_config.workspace_path,
                    surface_ref="dead",
                ),
            ]
        )
    )
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="TKT-OLD",
                    client=sample_client_config.name,
                    status=QueueItemStatus.RUNNING,
                ),
                TicketTask(
                    ticket_id="TKT-NEW",
                    client=sample_client_config.name,
                    status=QueueItemStatus.PENDING,
                ),
            ]
        )
    )

    adapter = FakeCmuxAdapter()
    # Non-empty live set bypasses reconcile's outage guard; "dead" ref still
    # isn't live so the phantom is reaped as intended.
    adapter.spawn("decoy-ws", "echo")

    spawned = dispatch_tick(simple_config, adapter=adapter)
    assert spawned == 1

    reloaded = load_state()
    phantom = reloaded.find_by_name_or_id("phantom-daemon")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED
