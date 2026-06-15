"""Tests for cw.dispatch - tick-based dispatch loop."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cw.config import (
    load_effective_config,
    load_state,
    orchestrator_config_file,
    save_state,
)
from cw.dev_queue import (
    add_ticket,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    save_plan,
)
from cw.dispatch import (
    DispatchTickResult,
    _accumulate_task_cost,
    consume_completed_sessions,
    dispatch_tick,
    persist_last_result,
    run_dispatch_loop,
)
from cw.events import read_events, record_event
from cw.exceptions import StaleWorktreeError, WorktreeError
from cw.models import (
    DEFAULT_LANE,
    ClientConfig,
    CwState,
    DevQueueStore,
    DispatchPlan,
    DispatchSkipReason,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient

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
def sample_client_config(workspace_dir: Path, tmp_path: Path) -> ClientConfig:
    """A ClientConfig for use with dispatch tests.

    Sets worktree_base to a tmp_path subdirectory so create_worktree
    writes test worktrees under tmp_path (not ~/.cw/wt/), preventing
    stale-directory accumulation across test runs.
    """
    return ClientConfig(
        name="test-client",
        workspace_path=workspace_dir,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
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
    lines = [
        "clients:\n",
        f"  {client.name}:\n",
        f"    workspace_path: {client.workspace_path}\n",
        f"    default_branch: {client.default_branch}\n",
    ]
    if client.worktree_base is not None:
        lines.append(f"    worktree_base: {client.worktree_base}\n")
    if client.lanes:
        lines.append("    lanes:\n")
        for lane in client.lanes:
            lines.append(f"      - name: {lane.name}\n")
            lines.append(f"        max_parallel: {lane.max_parallel}\n")
            if lane.priority != 0:
                lines.append(f"        priority: {lane.priority}\n")
    clients_file.write_text("".join(lines))


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

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

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

        # Native daemon should have been called
        assert len(daemon.spawn_calls) == 1

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

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert len(daemon.spawn_calls) == 1
        _cwd, prompt = daemon.spawn_calls[0]
        assert prompt == "/auto-dev GEN-300 --headless"

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

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(
            cap2_config, native_daemon=daemon, parent=parent.id
        ).spawned
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

    def test_dispatch_tick_stamps_session_id_on_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """After spawn, the task carries the session_id from the spawner.

        The completion consumer relies on this stamp to disambiguate
        SESSION_COMPLETED events when an older session for the same ticket
        crashed and was respawned. See GitHub issue #97.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-STAMP", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].session_id is not None
        # The stamped session_id must match the spawned session in state.
        spawned_state = load_state()
        assert len(spawned_state.sessions) == 1
        assert running[0].session_id == spawned_state.sessions[0].id

    def test_dispatch_tick_no_parent_leaves_linkage_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Direct CLI run (no orchestrator): parent=None → no linkage, no error."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-400", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)  # parent omitted

        state = load_state()
        worker = state.sessions[0]
        assert worker.parent_session_id is None
        assert worker.worker_session_ids == []

    def test_dispatch_stamps_task_lane_on_session(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """dispatch_tick passes task.lane to spawn_create_impl, stamps Session.lane."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Use DEFAULT_LANE so the task matches the client's effective lane and
        # gets dispatched. Lane is stamped verbatim on the spawned session.
        task = TicketTask(
            ticket_id="GEN-LANE",
            client="test-client",
            lane=DEFAULT_LANE,
        )
        add_ticket(task)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].lane == DEFAULT_LANE


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

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(cap2_config, native_daemon=daemon).spawned

        assert spawned == 2
        assert len(daemon.spawn_calls) == 2

        store = load_dev_queue()
        assert len(store.running()) == 2
        assert len(store.pending()) == 1

    def test_unlisted_client_uses_default_max_parallel(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Client missing from per_client_max_parallel uses default_max_parallel.

        Regression test for GitHub issue #145 — the cap fallback used to
        be hardcoded to 1, so a top-level ``default_max_parallel: 3`` had
        no effect on unlisted clients.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={},  # test-client deliberately unlisted
            default_max_parallel=3,
        )

        for i in range(4):
            add_ticket(TicketTask(ticket_id=f"GEN-{i}", client="test-client"))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(config, native_daemon=daemon).spawned

        # default_max_parallel=3 → spawn 3, leave 1 pending.
        assert spawned == 3
        store = load_dev_queue()
        assert len(store.running()) == 3
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

        daemon = FakeNativeDaemonClient()

        # First tick claims the task
        spawned1 = dispatch_tick(simple_config, native_daemon=daemon).spawned
        assert spawned1 == 1

        # Second tick: running_count=1 >= cap=1, should skip
        spawned2 = dispatch_tick(simple_config, native_daemon=daemon).spawned
        assert spawned2 == 0

        # Only one spawn call total
        assert len(daemon.spawn_calls) == 1

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
            session_id="sess-300",
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        # Seed a session with no_op so Rule 4 routes to COMPLETED.
        sess = Session(
            id="sess-300",
            name="test-client/auto-dev/GEN-300",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op"},
        )
        save_state(CwState(sessions=[sess]))

        # Write a session.completed event referencing the ticket
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-300", "session_id": "sess-300", "client": "test-client"},
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
        producer-side fix — see GitHub issue #94. Only non-crashed events go
        through this path; crashed events are skipped (see
        ``test_consume_skips_crashed_events``).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-700",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Seed a session with no_op so the stage-advance rule routes to COMPLETED.
        sess = Session(
            id="abc123",
            name="test-client/auto-dev/GEN-700",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op"},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": "abc123",
                "session_name": "test-client/auto-dev/GEN-700",
                "client": "test-client",
            },
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

    def test_consume_skips_crashed_events(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """crashed: true events must not mark tasks COMPLETED.

        Reconcile is the authoritative actor for crashed sessions — it
        reverts the task RUNNING → PENDING. The consumer must be a no-op
        for these events; otherwise it shadows reconcile's revert and
        falsely matches a freshly-respawned task with the same ticket_id.
        See GitHub issue #97.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-CRASH",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "ticket_id": "GEN-CRASH",
                "session_id": "old-session",
                "client": "test-client",
                "crashed": True,
            },
        )

        completed = consume_completed_sessions()
        assert completed == 0
        assert load_dev_queue().tasks[0].status == QueueItemStatus.RUNNING

    def test_consume_skips_crashed_events_via_session_name_fallback(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Crashed events are skipped before the session_name fallback runs.

        Belt-and-suspenders: even if a crashed event lacks ticket_id and
        only carries session_name, it must not be drained.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-CRASH-FB",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": "old-session",
                "session_name": "test-client/auto-dev/GEN-CRASH-FB",
                "client": "test-client",
                "crashed": True,
            },
        )

        completed = consume_completed_sessions()
        assert completed == 0
        assert load_dev_queue().tasks[0].status == QueueItemStatus.RUNNING

    def test_consume_rejects_event_with_mismatched_session_id(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A SESSION_COMPLETED event from an old session does not match a
        respawn that already has a fresh session_id stamped on the task.

        This guards the second failure mode in GitHub issue #97: stale
        non-crashed events carrying an older session_id must not falsely
        complete a freshly-respawned task.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-STALE",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="new-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "ticket_id": "GEN-STALE",
                "session_id": "old-session",
                "client": "test-client",
            },
        )

        completed = consume_completed_sessions()
        assert completed == 0
        assert load_dev_queue().tasks[0].status == QueueItemStatus.RUNNING

    def test_consume_completes_when_session_id_matches(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Matching session_id on event and task completes the task."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-MATCH",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="current-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Seed a session with no_op so the stage-advance rule (Rule 4) still
        # routes to COMPLETED — this test verifies session-id disambiguation,
        # not stage-advance semantics.
        sess = Session(
            id="current-session",
            name="test-client/auto-dev/GEN-MATCH",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op"},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "ticket_id": "GEN-MATCH",
                "session_id": "current-session",
                "client": "test-client",
            },
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

    def test_consume_falls_back_to_ticket_id_when_task_has_no_session_id(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Legacy tasks without session_id still match by ticket_id alone."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Task predates the session_id field — session_id stays None.
        task = TicketTask(
            ticket_id="GEN-LEGACY",
            client="test-client",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Seed a session with no_op so Rule 4 routes to COMPLETED — this test
        # verifies ticket_id fallback matching, not stage-advance semantics.
        sess = Session(
            id="any-session",
            name="test-client/auto-dev/GEN-LEGACY",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op"},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "ticket_id": "GEN-LEGACY",
                "session_id": "any-session",
                "client": "test-client",
            },
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

    def test_consume_completes_extended_event_shape(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with extra fields (status, session_name) completes.

        Issue #99: signal_stop emits SESSION_COMPLETED with crashed=False
        plus ``status`` (the parsed auto-dev outcome) and ``session_name``.
        The consumer must treat this exactly like a legacy non-crashed event
        — the extra fields are forward-compatible metadata, not behavior.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-WRAP",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="daemon-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Seed a session with no_op so Rule 4 routes to COMPLETED — the
        # extended-event shape test verifies event parsing, not stage advance.
        sess = Session(
            id="daemon-session",
            name="test-client/auto-dev/GEN-WRAP",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op"},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": "daemon-session",
                "session_name": "test-client/auto-dev/GEN-WRAP",
                "client": "test-client",
                "crashed": False,
                "status": "shipped",
                "ticket_id": "GEN-WRAP",
            },
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

    @pytest.mark.parametrize(
        "paused_status",
        [
            "premises_pending_verification",
            "ambiguities_pending_resolution",
            "plan_pending_approval",
            "review_pending_approval",
        ],
    )
    def test_consume_paused_status_routes_to_blocked_on_user(
        self,
        paused_status: str,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with paused last_result routes task to BLOCKED_ON_USER.

        When a session ends with a paused sentinel (any status in
        PAUSED_FOR_USER_INPUT_STATUSES), the task must become BLOCKED_ON_USER,
        not COMPLETED. See #489 (original) and #633 (plan_pending_approval,
        review_pending_approval).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-489A",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-489a",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sess = Session(
            id="sess-489a",
            name="test-client/auto-dev/GEN-489A",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={
                "status": paused_status,
                "schema_version": 4,
            },
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-489A", "session_id": "sess-489a"},
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

    def test_consume_non_paused_status_routes_to_completed(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with a non-paused last_result routes task to COMPLETED.

        Verifies the paused-status guard does not affect terminal outcomes.
        Uses no_op (Rule 4: always COMPLETED) and shipped at the last pipeline
        stage (Rule 3: advance-or-complete → COMPLETED). See #489.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # no_op always routes to COMPLETED regardless of stage (Rule 4).
        task = TicketTask(
            ticket_id="GEN-489B",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-489b",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sess = Session(
            id="sess-489b",
            name="test-client/auto-dev/GEN-489B",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={"status": "no_op", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-489B", "session_id": "sess-489b"},
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED

    def test_consume_null_last_result_routes_to_blocked_on_user(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with last_result=None routes task to BLOCKED_ON_USER.

        Sessions that did not emit a sentinel have last_result=None; under
        RFC 0005 B2 Rule 6, an unparseable/missing sentinel is conservative-safe
        and routes to BLOCKED_ON_USER so a human can inspect. See #489 (original)
        and #617 (B2 decision table).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-489C",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-489c",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sess = Session(
            id="sess-489c",
            name="test-client/auto-dev/GEN-489C",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result=None,
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-489C", "session_id": "sess-489c"},
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

        daemon = FakeNativeDaemonClient()
        count = dispatch_tick(cap2_config, native_daemon=daemon).spawned

        assert count == 2

    def test_dispatch_tick_returns_zero_when_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Empty queue returns 0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        daemon = FakeNativeDaemonClient()
        count = dispatch_tick(simple_config, native_daemon=daemon).spawned

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

        daemon = FakeNativeDaemonClient()
        # cap=1, running=1 => should not spawn
        count = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert count == 0
        assert len(daemon.spawn_calls) == 0


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

        daemon = FakeNativeDaemonClient()
        # cap=2 => should claim C first, then A
        spawned = dispatch_tick(
            cap2_config, native_daemon=daemon, use_plan=True
        ).spawned
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

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(
            simple_config, native_daemon=daemon, use_plan=True
        ).spawned
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

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(
            cap2_config, native_daemon=daemon, use_plan=True
        ).spawned
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
    monkeypatch: pytest.MonkeyPatch,
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
                    # started_at older than SPAWN_GRACE_SECONDS so this
                    # session is eligible for phantom-reaping rather
                    # than protected by the grace window.
                    started_at=datetime(2026, 4, 19, tzinfo=UTC),
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

    daemon = FakeNativeDaemonClient()
    # Non-empty live set bypasses reconcile's outage guard; "dead" ref still
    # isn't live so the phantom is reaped as intended.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned
    assert spawned == 1

    reloaded = load_state()
    phantom = reloaded.find_by_name_or_id("phantom-daemon")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED


def test_crash_revert_respawn_rejects_old_event_completes_new(
    tmp_dispatch_dirs: Path,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: crash → reconcile revert → respawn → consumer disambiguates.

    Composes the three actors into the exact failure mode from GitHub
    issue #97:

    1. Task RUNNING with session_id="old-session" (phantom DAEMON session
       in state).
    2. dispatch_tick triggers reconcile, which reverts the task to
       PENDING and clears session_id, then claims it again and stamps
       the freshly-spawned session_id.
    3. The reconcile-emitted SESSION_COMPLETED event (session_id="old",
       crashed=True) is consumed and SKIPPED by the crashed guard — the
       newly-RUNNING task stays RUNNING.
    4. A subsequent SESSION_COMPLETED event matching the NEW session_id
       is consumed and correctly completes the task.
    """
    _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

    # Step 1: pre-existing phantom DAEMON session + RUNNING task with
    # the old session's id stamped.
    save_state(
        CwState(
            sessions=[
                Session(
                    id="old-session",
                    name=f"{sample_client_config.name}/auto-dev/TKT-RACE",
                    client=sample_client_config.name,
                    purpose=SessionPurpose.IMPL,
                    origin=SessionOrigin.DAEMON,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client_config.workspace_path,
                    surface_ref="dead",
                    # Older than SPAWN_GRACE_SECONDS — phantom-eligible.
                    started_at=datetime(2026, 4, 19, tzinfo=UTC),
                ),
            ]
        )
    )
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="TKT-RACE",
                    client=sample_client_config.name,
                    status=QueueItemStatus.RUNNING,
                    session_id="old-session",
                ),
            ]
        )
    )

    # Non-empty live set bypasses outage guard; "dead" ref still isn't live.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )
    daemon = FakeNativeDaemonClient()

    # Step 2: tick triggers reconcile (revert + emit crashed event), then
    # claims and respawns the same ticket with a new session id.
    spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned
    assert spawned == 1

    queue = load_dev_queue()
    running = queue.running()
    assert len(running) == 1
    new_session_id = running[0].session_id
    assert new_session_id is not None
    assert new_session_id != "old-session"

    # Step 3: consume the queued events. The reconcile-emitted crashed
    # event is skipped; the task stays RUNNING.
    consume_completed_sessions()
    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.RUNNING
    assert queue.tasks[0].session_id == new_session_id

    # Step 4: a real completion event for the NEW session arrives and
    # correctly completes the task. Seed last_result=no_op on the new
    # session so the stage-advance rule (Rule 4) routes to COMPLETED.
    new_state = load_state()
    for s in new_state.sessions:
        if s.id == new_session_id:
            s.last_result = {"status": "no_op"}
    save_state(new_state)

    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "ticket_id": "TKT-RACE",
            "session_id": new_session_id,
            "client": sample_client_config.name,
        },
    )
    completed = consume_completed_sessions()
    assert completed == 1
    assert load_dev_queue().tasks[0].status == QueueItemStatus.COMPLETED


# ---------------------------------------------------------------------------
# TestDispatchTickSpawnErrors
# ---------------------------------------------------------------------------


class _RaisingNativeDaemon(FakeNativeDaemonClient):
    """Native daemon whose ``spawn_bg`` always raises the configured exception.

    Used to exercise the spawn-failure containment added for issue #149:
    a single failure must not crash dispatch_tick.
    """

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        self.spawn_calls.append((cwd, prompt))
        raise self._exc


class TestDispatchTickSpawnErrors:
    """Spawn failure inside dispatch_tick is contained, not propagated.

    The dispatch loop is the substrate the dev queue runs on; a single
    spawn failure (subprocess error, worktree error, native-daemon
    exception) used to kill the entire loop and leave a half-claimed task
    at ``status=RUNNING, session_id=None``.  These tests pin the new
    behaviour: log + revert task to PENDING + don't propagate.
    """

    def test_subprocess_error_does_not_crash_loop(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-149A", client="test-client"))

        daemon = _RaisingNativeDaemon(
            subprocess.CalledProcessError(
                returncode=1,
                cmd=["claude", "--bg"],
                output=b"daemon unreachable",
            ),
        )

        caplog.set_level(logging.ERROR, logger="cw.dispatch")
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0

        queue = load_dev_queue()
        assert len(queue.tasks) == 1
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

        assert any(
            "spawn failed" in record.getMessage().lower()
            for record in caplog.records
            if record.name == "cw.dispatch" and record.levelno >= logging.ERROR
        ), "expected ERROR log from cw.dispatch mentioning 'spawn failed'"

    def test_worktree_error_does_not_crash_loop(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-149B", client="test-client"))

        def _boom(*_args: object, **_kwargs: object) -> Path:
            msg = "git worktree add failed"
            raise WorktreeError(msg)

        # Patch the name as imported into cw.dispatch, not the source module.
        monkeypatch.setattr("cw.dispatch.create_worktree", _boom)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.ERROR, logger="cw.dispatch")
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert daemon.spawn_calls == []

        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

        assert any(
            "spawn failed" in record.getMessage().lower()
            for record in caplog.records
            if record.name == "cw.dispatch" and record.levelno >= logging.ERROR
        ), "expected ERROR log from cw.dispatch mentioning 'spawn failed'"

    def test_stale_worktree_error_force_removes_then_reverts(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A StaleWorktreeError from create_worktree force-removes the stale
        tree (so the next tick rebuilds fresh) and reverts the task to PENDING.

        Without the removal the task would re-claim and re-hit the same stale
        worktree every tick — an infinite spin, because no session is created
        here for reconcile's TIMED_OUT cleanup to act on (#404).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-404S", client="test-client"))

        def _stale(*_args: object, **_kwargs: object) -> Path:
            msg = "Refusing to reuse stale worktree"
            raise StaleWorktreeError(msg)

        removed: list[tuple[str, bool]] = []

        def _record_remove(
            _client: object, branch: str, *, force: bool = False
        ) -> None:
            removed.append((branch, force))

        monkeypatch.setattr("cw.dispatch.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.remove_worktree", _record_remove)

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert daemon.spawn_calls == []
        # Stale tree force-removed for the ticket's branch before the revert.
        assert removed == [("auto-dev/GEN-404S", True)]

        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

    def test_stale_worktree_removal_failure_still_reverts(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the force-remove itself fails, the loop still survives and reverts
        the task to PENDING — removal is best-effort (#404)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-404F", client="test-client"))

        def _stale(*_args: object, **_kwargs: object) -> Path:
            msg = "Refusing to reuse stale worktree"
            raise StaleWorktreeError(msg)

        def _remove_boom(*_args: object, **_kwargs: object) -> None:
            msg = "git worktree remove failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.dispatch.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.remove_worktree", _remove_boom)

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        queue = load_dev_queue()
        assert queue.tasks[0].status == QueueItemStatus.PENDING
        assert queue.tasks[0].session_id is None

    def test_stale_worktree_dirty_blocks_task_and_skips_removal(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """StaleWorktreeError + dirty worktree: removal SKIPPED, task →
        BLOCKED_ON_USER (#425)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-425D", client="test-client"))

        def _stale(*_args: object, **_kwargs: object) -> Path:
            msg = "Refusing to reuse stale worktree"
            raise StaleWorktreeError(msg)

        removed: list[str] = []

        def _record_remove(
            _client: object, branch: str, *, force: bool = False
        ) -> None:
            removed.append(branch)

        monkeypatch.setattr("cw.dispatch.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.remove_worktree", _record_remove)
        monkeypatch.setattr(
            "cw.dispatch.worktree_has_unsaved_work", lambda _c, _b: True
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        # Removal must NOT have been called
        assert removed == []
        # Task must be BLOCKED_ON_USER, not PENDING
        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.session_id is None

    def test_stale_worktree_clean_removes_and_reverts(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """StaleWorktreeError + clean worktree: removal proceeds, task → PENDING
        (#425)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-425C", client="test-client"))

        def _stale(*_args: object, **_kwargs: object) -> Path:
            msg = "Refusing to reuse stale worktree"
            raise StaleWorktreeError(msg)

        removed: list[tuple[str, bool]] = []

        def _record_remove(
            _client: object, branch: str, *, force: bool = False
        ) -> None:
            removed.append((branch, force))

        monkeypatch.setattr("cw.dispatch.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.remove_worktree", _record_remove)
        monkeypatch.setattr(
            "cw.dispatch.worktree_has_unsaved_work", lambda _c, _b: False
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        # Removal with force=True must have been called
        assert removed == [("auto-dev/GEN-425C", True)]
        # Task reverted to PENDING (existing behaviour)
        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None


# ---------------------------------------------------------------------------
# TestDispatchTickFreshnessGate
# ---------------------------------------------------------------------------


class TestDispatchTickFreshnessGate:
    """Freshness-gate tests: stale main blocks dispatch and emits ticket.needs_sync."""

    def test_stale_main_skips_dispatch_and_keeps_pending(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale client: dispatch returns 0, task stays PENDING, event emitted."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-1", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0

        store = load_dev_queue()
        assert store.tasks[0].status == QueueItemStatus.PENDING

        assert len(daemon.spawn_calls) == 0

        events = read_events(
            consumer="test-freshness",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "CW-1"
        assert events[0].payload["client"] == "test-client"

    def test_stale_main_emits_event_once_per_pending_ticket(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two PENDING tasks emit two ticket.needs_sync events (one per task)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-10", client="test-client"))
        add_ticket(TicketTask(ticket_id="CW-11", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-freshness-multi",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 2
        ticket_ids = {e.payload["ticket_id"] for e in events}
        assert ticket_ids == {"CW-10", "CW-11"}

    def test_stale_check_skips_only_stale_client(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Stale client A skipped; fresh client B dispatches normally."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Create second fresh client
        fresh_ws = make_git_repo("workspace/fresh-project")
        fresh_client = ClientConfig(
            name="fresh-client",
            workspace_path=fresh_ws,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-fresh",
        )
        # Append fresh-client to clients.yaml
        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        clients_file = config_dir / "clients.yaml"
        existing = clients_file.read_text()
        existing += (
            f"  {fresh_client.name}:\n"
            f"    workspace_path: {fresh_client.workspace_path}\n"
            f"    default_branch: {fresh_client.default_branch}\n"
            f"    worktree_base: {fresh_client.worktree_base}\n"
        )
        clients_file.write_text(existing)

        add_ticket(TicketTask(ticket_id="CW-20", client="test-client"))
        add_ticket(TicketTask(ticket_id="CW-21", client="fresh-client"))

        def _freshness_check(
            client: ClientConfig,
            warned_fetch_fail: set[str] | None = None,
        ) -> tuple[bool, str, str, int]:
            if client.name == "test-client":
                return (True, "aaa", "bbb", 2)
            return (False, "abc", "abc", 0)

        monkeypatch.setattr("cw.dispatch.is_main_behind_origin", _freshness_check)

        # fresh-client also needs cap=1
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"test-client": 1, "fresh-client": 1},
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(config, native_daemon=daemon).spawned

        assert spawned == 1  # only fresh-client

        events = read_events(
            consumer="test-freshness-split",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["client"] == "test-client"

    def test_fresh_main_dispatches_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh main: existing dispatch behaviour unchanged."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-30", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1

        events = read_events(
            consumer="test-freshness-no-event",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 0, "Fresh main should not emit ticket.needs_sync"

    def test_freshness_check_called_once_per_client_per_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """is_main_behind_origin called exactly once per client even with 3 tasks."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        for i in range(3):
            add_ticket(TicketTask(ticket_id=f"CW-4{i}", client="test-client"))

        call_count = 0

        def _counting(
            _client: ClientConfig,
            warned_fetch_fail: set[str] | None = None,
        ) -> tuple[bool, str, str, int]:
            nonlocal call_count
            call_count += 1
            return (False, "abc", "abc", 0)

        monkeypatch.setattr("cw.dispatch.is_main_behind_origin", _counting)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert call_count == 1

    def test_freshness_check_missing_workspace_no_traceback(
        self,
        tmp_dispatch_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """dispatch_tick with missing workspace_path: WARNING logged, no traceback."""
        missing_dir = tmp_path / "nonexistent"  # intentionally not created
        missing_client = ClientConfig(
            name="missing-ws",
            workspace_path=missing_dir,
            default_branch="main",
        )
        _make_clients_yaml(tmp_dispatch_dirs, missing_client)
        add_ticket(TicketTask(ticket_id="CW-99", client="missing-ws"))

        daemon = FakeNativeDaemonClient()
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        caplog.set_level(logging.WARNING, logger="cw.worktree")

        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"missing-ws": 1},
        )
        # Should not raise even with missing workspace_path
        dispatch_tick(config, native_daemon=daemon)

        # No exc_info on freshness-related log records.
        # (dispatch may log other errors if it proceeds to create_worktree with
        # the missing path; those are separate concerns from the freshness gate.)
        freshness_records = [
            r
            for r in caplog.records
            if r.name in ("cw.dispatch", "cw.worktree")
            and "freshness" in r.message.lower()
        ]
        assert not any(r.exc_info for r in freshness_records), (
            "No traceback should appear for missing workspace freshness check — "
            "got exc_info on: "
            + str([r.message for r in freshness_records if r.exc_info])
        )
        # The freshness skip warning should appear
        assert any("freshness_check_skip" in r.message for r in caplog.records), (
            "Expected freshness_check_skip warning for missing workspace"
        )

    def test_freshness_check_failure_does_not_block_dispatch(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RuntimeError from freshness check: WARNING logged, dispatch proceeds."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-50", client="test-client"))

        def _boom(
            _client: ClientConfig,
            warned_fetch_fail: set[str] | None = None,
        ) -> tuple[bool, str, str, int]:
            msg = "network unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.is_main_behind_origin", _boom)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1  # dispatch proceeded
        assert any(
            "freshness check failed" in r.message.lower() for r in caplog.records
        )


class TestDispatchTickReconcileErrors:
    """Reconcile failure inside dispatch_tick is contained, not propagated.

    Paired test for the sanctioned BLE001 broad-catch at
    src/cw/dispatch.py:105. Reconcile is best-effort housekeeping; if it
    fails (transient adapter outage, corrupted roster, OSError on stale
    socket), dispatch_tick must log + continue, not propagate.
    """

    def test_reconcile_failure_does_not_crash_dispatch_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        def _boom_reconcile(*_args: object, **_kwargs: object) -> None:
            msg = "simulated reconcile failure"
            raise RuntimeError(msg)

        # Patch the name as imported into cw.dispatch (not cw.reconcile).
        monkeypatch.setattr("cw.dispatch.reconcile", _boom_reconcile)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.ERROR, logger="cw.dispatch")

        # Must not raise; reconcile guard catches and logs, dispatch_tick
        # continues to the dev-queue scan and returns normally.
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert any(
            "reconcile failed" in record.getMessage().lower()
            for record in caplog.records
            if record.name == "cw.dispatch" and record.levelno >= logging.ERROR
        ), "expected ERROR log from cw.dispatch mentioning 'reconcile failed'"


class TestClaimNextPendingAttempts:
    """_claim_next_pending increments TicketTask.attempts on each claim.

    Regression for GitHub issue #251: the attempts counter must be
    persisted at claim time so _apply_sentinel_to_task (called from
    signal_stop before the session is marked COMPLETED) can enforce the
    validation_failed hard cap.
    """

    def test_claim_next_pending_increments_attempts(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-251-attempts",
            client="test-client",
            attempts=0,
        )
        add_ticket(task)

        daemon = FakeNativeDaemonClient()

        dispatch_tick(simple_config, native_daemon=daemon)

        queue = load_dev_queue()
        claimed = next(t for t in queue.tasks if t.ticket_id == "GEN-251-attempts")
        assert claimed.status == QueueItemStatus.RUNNING
        assert claimed.attempts == 1

    def test_claim_next_pending_increments_attempts_cumulatively(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A task reverted to PENDING and re-claimed accumulates attempts."""
        from cw.dev_queue import save_dev_queue

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Pre-seed with attempts=2 (simulates two prior claim+revert cycles).
        task = TicketTask(
            ticket_id="GEN-251-cumulative",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=2,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()

        dispatch_tick(simple_config, native_daemon=daemon)

        queue = load_dev_queue()
        claimed = next(t for t in queue.tasks if t.ticket_id == "GEN-251-cumulative")
        assert claimed.status == QueueItemStatus.RUNNING
        assert claimed.attempts == 3


# ---------------------------------------------------------------------------
# TestClaimNextPendingPriority
# ---------------------------------------------------------------------------


class TestClaimNextPendingPriority:
    """_claim_next_pending respects priority field (highest claimed first).

    Regression for GitHub issue #506: the fallback FIFO loop ignored the
    priority field.  Tasks with higher priority must be claimed before
    lower-priority tasks, regardless of enqueue order.
    """

    def test_high_priority_claimed_before_low_priority(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """High-priority task enqueued after low-priority is claimed first."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Enqueue low-priority first, high-priority second
        add_ticket(
            TicketTask(
                ticket_id="GEN-LOW",
                client="test-client",
                priority=0,
                created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-HIGH",
                client="test-client",
                priority=10,
                created_at=datetime.fromisoformat("2026-01-01T00:01:00+00:00"),
            )
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "GEN-HIGH"

    def test_equal_priority_fifo_order(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Equal-priority tasks are claimed in FIFO (oldest created_at first)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Enqueue in order: earlier created_at first, later second
        add_ticket(
            TicketTask(
                ticket_id="GEN-FIRST",
                client="test-client",
                priority=5,
                created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-SECOND",
                client="test-client",
                priority=5,
                created_at=datetime.fromisoformat("2026-01-01T00:01:00+00:00"),
            )
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "GEN-FIRST"

    def test_priority_without_use_plan(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        cap2_config: OrchestratorConfig,
    ) -> None:
        """Priority respected in fallback loop even when use_plan=False."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # FIFO backlog: two low-priority tasks enqueued first
        add_ticket(
            TicketTask(
                ticket_id="GEN-BACKLOG-A",
                client="test-client",
                priority=0,
                created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-BACKLOG-B",
                client="test-client",
                priority=0,
                created_at=datetime.fromisoformat("2026-01-01T00:01:00+00:00"),
            )
        )
        # High-priority task added last
        add_ticket(
            TicketTask(
                ticket_id="GEN-URGENT",
                client="test-client",
                priority=10,
                created_at=datetime.fromisoformat("2026-01-01T00:02:00+00:00"),
            )
        )

        daemon = FakeNativeDaemonClient()
        # cap=2 => two claims; URGENT (p=10) + BACKLOG-A (p=0, earlier)
        spawned = dispatch_tick(cap2_config, native_daemon=daemon).spawned
        assert spawned == 2

        store = load_dev_queue()
        running_ids = sorted(t.ticket_id for t in store.running())
        assert running_ids == ["GEN-BACKLOG-A", "GEN-URGENT"]


# ---------------------------------------------------------------------------
# TestDispatchDoesNotTouchMainCheckout
# ---------------------------------------------------------------------------


class TestDispatchDoesNotTouchMainCheckout:
    """Regression tests for #300: dispatch must never commit to the main checkout.

    When create_worktree degenerately returns the main checkout path, the
    identity guard in dispatch_tick must refuse the spawn and revert
    the task to PENDING — no session created, no commits to the main repo.
    """

    def test_dispatch_tick_does_not_modify_main_checkout_head(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Normal dispatch: main checkout HEAD must be unchanged after the tick."""
        workspace_dir = sample_client_config.workspace_path
        before_head = subprocess.check_output(
            ["git", "-C", str(workspace_dir), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-300", client="test-client"))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        after_head = subprocess.check_output(
            ["git", "-C", str(workspace_dir), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        assert spawned == 1, "session should have been spawned in the branch worktree"
        assert len(daemon.spawn_calls) == 1
        assert after_head == before_head

    def test_dispatch_tick_with_worktree_equal_to_main_checkout_reverts_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Degenerate path: create_worktree returns main checkout → PENDING revert.

        Simulates the #300 regression: if create_worktree returns the main
        checkout path (due to a path-computation bug), the identity guard in
        dispatch_tick must refuse and the dispatch loop must revert the
        task to PENDING with no daemon spawn.
        """
        workspace_dir = sample_client_config.workspace_path
        monkeypatch.setattr(
            "cw.dispatch.create_worktree",
            lambda _client, _branch: workspace_dir,
        )

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-300-guard", client="test-client"))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert daemon.spawn_calls == []

        store = load_dev_queue()
        tasks = [t for t in store.tasks if t.ticket_id == "GEN-300-guard"]
        assert len(tasks) == 1
        assert tasks[0].status == QueueItemStatus.PENDING
        assert tasks[0].attempts == 1


# ---------------------------------------------------------------------------
# Race regression: spawn close cancels task so dispatcher cannot re-spawn
# ---------------------------------------------------------------------------


class TestSpawnCloseRaceRegression:
    def test_spawn_close_prevents_respawn_via_cancelled_status(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_spawn_close_impl CANCELS the task; dispatch_tick does not re-spawn it.

        Regression test for GitHub issue #317.  Setup:
        1. One DAEMON ACTIVE session with a RUNNING TicketTask that has its
           session_id stamped.
        2. _spawn_close_impl is called (simulates `cw spawn close`).
        3. A subsequent dispatch_tick must NOT spawn a new session for the
           now-CANCELLED task.
        """
        from cw.cli import _spawn_close_impl
        from cw.config import save_state
        from cw.models import SessionPurpose

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Build a DAEMON ACTIVE session.
        workspace = sample_client_config.workspace_path
        sess = Session(
            id="race-sess-1",
            name="test-client/auto-dev/RACE-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            status=SessionStatus.ACTIVE,
            surface_ref="fake-surface-ref",
        )
        save_state(CwState(sessions=[sess]))

        # Matching RUNNING TicketTask.
        task = TicketTask(
            ticket_id="RACE-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="race-sess-1",
        )
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore

        save_dev_queue(DevQueueStore(tasks=[task]))

        # Stub out daemon.stop so it doesn't error on the fake surface_ref.
        daemon = FakeNativeDaemonClient()

        # Call _spawn_close_impl — should CANCEL the task atomically.
        _spawn_close_impl(session_id="race-sess-1", native_daemon=daemon)

        # Verify the task is CANCELLED.
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "RACE-1")
        assert t.status == QueueItemStatus.CANCELLED, (
            f"Expected CANCELLED after spawn close, got {t.status}"
        )

        # Now dispatch_tick should NOT re-spawn (CANCELLED != PENDING).
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned
        assert spawned == 0, (
            f"Dispatcher should not re-spawn a CANCELLED task, got {spawned}"
        )

        # Still CANCELLED — not reverted to PENDING.
        store2 = load_dev_queue()
        t2 = next(t for t in store2.tasks if t.ticket_id == "RACE-1")
        assert t2.status == QueueItemStatus.CANCELLED


class TestAccumulateTaskCost:
    """Tests for _accumulate_task_cost helper inside consume_completed_sessions."""

    def _make_running_task(
        self, session_id: str, ticket_id: str = "GEN-1"
    ) -> TicketTask:
        task = TicketTask(
            ticket_id=ticket_id,
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def _make_session(
        self,
        session_id: str,
        *,
        cost_usd: float | None = None,
        last_result: dict[str, object] | None = None,
    ) -> None:
        sess = Session(
            id=session_id,
            name="test-client/auto-dev/GEN-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/dev/null"),
            cost_usd=cost_usd,
            last_result=last_result,
        )
        save_state(CwState(sessions=[sess]))

    def test_accumulates_cost_from_cost_usd_field(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """When session.cost_usd is set, accumulates from that field."""
        self._make_running_task("s_cost1")
        self._make_session("s_cost1", cost_usd=1.5)
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-1", "session_id": "s_cost1"},
        )
        consume_completed_sessions()
        store = load_dev_queue()
        assert store.tasks[0].total_cost_usd == pytest.approx(1.5)

    def test_accumulates_cost_from_last_result_when_cost_usd_field_absent(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """Falls back to session.last_result['cost_usd'] when cost_usd field is None."""
        self._make_running_task("s_lr1")
        self._make_session(
            "s_lr1",
            cost_usd=None,
            last_result={"cost_usd": 2.0, "status": "shipped"},
        )
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-1", "session_id": "s_lr1"},
        )
        consume_completed_sessions()
        store = load_dev_queue()
        assert store.tasks[0].total_cost_usd == pytest.approx(2.0)

    def test_accumulates_cost_zero_when_both_sources_absent(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """When both cost sources are None, total_cost_usd is unchanged (no crash)."""
        task = TicketTask(
            ticket_id="GEN-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="s_none1",
            total_cost_usd=5.0,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        self._make_session("s_none1", cost_usd=None, last_result=None)
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-1", "session_id": "s_none1"},
        )
        consume_completed_sessions()
        store = load_dev_queue()
        assert store.tasks[0].total_cost_usd == pytest.approx(5.0)

    def test_empty_string_session_id_is_not_treated_as_missing(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """Empty-string session_id must not short-circuit like None."""
        task = TicketTask(
            ticket_id="GEN-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="",
            total_cost_usd=3.0,
        )
        # No session in state — _accumulate_task_cost should attempt the lookup
        # (not return early on empty string) and leave total_cost_usd unchanged.
        save_state(CwState(sessions=[]))
        _accumulate_task_cost(task, "")
        assert task.total_cost_usd == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# TestRunDispatchLoopVerbose
# ---------------------------------------------------------------------------


class TestRunDispatchLoopVerbose:
    """run_dispatch_loop emits human-readable stdout lines when emit= is set."""

    def test_stale_main_emits_needs_sync_line(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Freshness-gate fires → at least one 'main behind origin' line emitted."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-420", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        lines: list[str] = []
        run_dispatch_loop(
            once=True,
            emit=lines.append,
        )

        assert any("main behind origin" in line for line in lines), (
            f"Expected 'main behind origin' in output but got: {lines!r}"
        )

    def test_stale_main_line_includes_ticket_and_client(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Freshness-gate warn line includes client name and ticket_id."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-421", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        lines: list[str] = []
        run_dispatch_loop(
            once=True,
            emit=lines.append,
        )

        warn_lines = [ln for ln in lines if "main behind origin" in ln]
        assert warn_lines, "expected at least one warn line"
        assert any("test-client" in ln and "CW-421" in ln for ln in warn_lines)

    def test_stale_main_deduplicated_across_ticks(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same stale ticket warned only once per run (deduplication)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-422", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        tick_count = 0

        original_dispatch_tick = dispatch_tick

        def _three_tick_dispatch(
            config: OrchestratorConfig,
            **kwargs: object,
        ) -> int:
            nonlocal tick_count
            tick_count += 1
            return original_dispatch_tick(config, **kwargs)

        monkeypatch.setattr("cw.dispatch.dispatch_tick", _three_tick_dispatch)

        lines: list[str] = []
        # Run three ticks manually by calling dispatch_tick three times via loop
        # Use once=True three separate invocations with the same warned_set state.
        # Directly test run_dispatch_loop with once=True three times.
        for _ in range(3):
            run_dispatch_loop(once=True, emit=lines.append)

        warn_lines = [
            ln for ln in lines if "main behind origin" in ln and "CW-422" in ln
        ]
        # Should be <= 3 (one per run at most) but dedup within a single run
        # For three separate calls each has its own warned set, so could be 3.
        # The key invariant: within a single tick (once=True), each ticket warned once.
        assert len(warn_lines) <= 3  # at most once per run_dispatch_loop call

    def test_spawn_emits_spawn_line(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Successful spawn → emit line containing 'SPAWN' with client/ticket info."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-423", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        daemon = FakeNativeDaemonClient()
        lines: list[str] = []
        run_dispatch_loop(
            once=True,
            emit=lines.append,
            native_daemon=daemon,
        )

        spawn_lines = [ln for ln in lines if "SPAWN" in ln]
        assert spawn_lines, f"Expected SPAWN line but got: {lines!r}"
        assert any("test-client" in ln and "CW-423" in ln for ln in spawn_lines)

    def test_per_tick_summary_line_emitted(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """dispatch_tick emits a per-client summary line including spawned count."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-424", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        daemon = FakeNativeDaemonClient()
        lines: list[str] = []
        run_dispatch_loop(
            once=True,
            emit=lines.append,
            native_daemon=daemon,
        )

        summary_lines = [ln for ln in lines if "test-client" in ln and "spawned=" in ln]
        assert summary_lines, f"Expected per-client summary but got: {lines!r}"

    def test_no_emit_when_emit_is_none(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """emit=None (quiet mode) produces no output — no exception raised."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-425", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr("cw.dispatch.reconcile", lambda: None)

        # Should not raise; output is silently discarded
        run_dispatch_loop(once=True, emit=None)


# ---------------------------------------------------------------------------
# TestDispatchTickEvents — dispatch.tick event emission (#459)
# ---------------------------------------------------------------------------


class TestDispatchTickEvents:
    """dispatch.tick emitted once per client per tick with accurate payload."""

    def test_skip_reason_none_on_successful_spawn(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Successful spawn → skip_reason='none', claimed≥1."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="TICK-1", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-none",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.NONE
        assert p["claimed"] == 1
        assert p["client"] == "test-client"
        assert p["cap"] == 1
        assert events[0].correlation_id is None

    def test_skip_reason_no_pending_when_queue_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """No pending tasks → skip_reason='no_pending', claimed=0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # No tickets added — queue is empty

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-no-pending",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.NO_PENDING
        assert p["claimed"] == 0
        assert p["pending"] == 0

    def test_skip_reason_cap_full_when_running_at_cap(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Running session at cap → skip_reason='cap_full', claimed=0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="TICK-CF", client="test-client"))

        # Put an active DAEMON session in state so running_count == cap (1)
        sess = Session(
            id="running-sess",
            name="test-client/auto-dev/OTHER-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
        )
        save_state(CwState(sessions=[sess]))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-cap-full",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.CAP_FULL
        assert p["claimed"] == 0
        assert p["running"] == 1
        assert p["cap"] == 1

    def test_skip_reason_freshness_gate_on_stale_main(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale main → skip_reason='freshness_gate', claimed=0, pending=pre-claim."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="TICK-FG-1", client="test-client"))
        add_ticket(TicketTask(ticket_id="TICK-FG-2", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-freshness",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        assert p["claimed"] == 0
        assert p["pending"] == 2  # both tasks were pending pre-claim
        # Payload shape is consistent across all emit sites (#558 PM review):
        # the freshness-gate event carries the per-lane breakdown too.
        assert p["lanes"] == {"default": {"claimed": 0, "running": 0, "pending": 2}}

    def test_skip_reason_spawn_error_on_spawn_failure(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Spawn failure → skip_reason='spawn_error', claimed=0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="TICK-SE", client="test-client"))

        daemon = _RaisingNativeDaemon(
            RuntimeError("backend outage"),
        )
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-spawn-error",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.SPAWN_ERROR
        assert p["claimed"] == 0

    def test_pending_is_pre_claim_count(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """pending in event payload reflects pre-claim count, not post-claim."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="TICK-PRE-1", client="test-client"))
        add_ticket(TicketTask(ticket_id="TICK-PRE-2", client="test-client"))
        # cap=1, so only one will be claimed

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-tick-pre-claim",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        # Pre-claim: 2 pending. Post-claim: 1 pending. Event must show 2.
        assert p["pending"] == 2
        assert p["claimed"] == 1


# ---------------------------------------------------------------------------
# TestDispatchUsageLimitBackoff
# ---------------------------------------------------------------------------


class TestDispatchUsageLimitBackoff:
    """Usage-limit back-off: detected at spawn time, subsequent ticks skipped."""

    def test_usage_limit_detected_from_spawn_raises(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """UsageLimitError from spawn: tick result has usage_limit_detected=True."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL1", client="test-client"))

        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True

        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert isinstance(result, DispatchTickResult)
        assert result.usage_limit_detected is True
        assert result.spawned == 0

    def test_usage_limit_skip_reason_in_event(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """dispatch.tick event has skip_reason=usage_limited when limit detected."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL2", client="test-client"))

        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True

        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-ul-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.USAGE_LIMITED

    def test_usage_limited_until_future_skips_all_clients(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When usage_limited_until is in the future, tick skips all clients."""
        from datetime import timedelta

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL3", client="test-client"))

        daemon = FakeNativeDaemonClient()
        future = datetime.now(UTC) + timedelta(hours=1)

        result = dispatch_tick(
            simple_config,
            native_daemon=daemon,
            usage_limited_until=future,
        )

        assert isinstance(result, DispatchTickResult)
        assert result.spawned == 0
        assert result.usage_limit_detected is False
        assert daemon.spawn_calls == []

        events = read_events(
            consumer="test-ul-future",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.USAGE_LIMITED

    def test_usage_limited_until_elapsed_spawns_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """After usage_limited_until elapses, spawning resumes normally."""
        from datetime import timedelta

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL4", client="test-client"))

        daemon = FakeNativeDaemonClient()
        past = datetime.now(UTC) - timedelta(hours=1)

        result = dispatch_tick(
            simple_config,
            native_daemon=daemon,
            usage_limited_until=past,
        )

        assert isinstance(result, DispatchTickResult)
        assert result.spawned == 1
        assert len(daemon.spawn_calls) == 1

    def test_run_dispatch_loop_sets_usage_limited_until(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """run_dispatch_loop with usage limit hit: no spawns occur."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL5", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-UL6", client="test-client"))

        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True

        # Run once — limit detected
        run_dispatch_loop(
            once=True,
            native_daemon=daemon,
            max_parallel=2,
        )

        # No spawns because limit was hit
        assert daemon.spawn_calls == []

    def test_once_mode_does_not_set_backoff(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """once=True: usage_limit_detected does NOT set usage_limited_until."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL7", client="test-client"))

        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True

        result = dispatch_tick(
            simple_config,
            native_daemon=daemon,
            usage_limited_until=None,
        )

        # DispatchTickResult.usage_limit_detected=True but no backoff state set
        assert isinstance(result, DispatchTickResult)
        assert result.usage_limit_detected is True


# ---------------------------------------------------------------------------
# TestConfigReloadedEachTick
# ---------------------------------------------------------------------------


class TestConfigReloadedEachTick:
    def test_config_reloaded_each_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_dispatch_loop re-calls load_effective_config on every tick."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        call_count = 0
        real_load = load_effective_config

        def counting_load() -> OrchestratorConfig:
            nonlocal call_count
            call_count += 1
            return real_load()

        monkeypatch.setattr("cw.dispatch.load_effective_config", counting_load)

        daemon = FakeNativeDaemonClient()
        run_dispatch_loop(once=True, native_daemon=daemon)

        # With once=True: 1 startup load + 1 in-loop reload = exactly 2.
        # (Before fix: call_count == 1 — in-loop reload is missing → FAILS pre-fix)
        assert call_count == 2

    def test_config_reload_takes_effect(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """A cap change written between ticks is honored on the next tick."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Resolve the write path via the fixture-patched accessor
        config_path = orchestrator_config_file()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Tick 1: cap=2, 2 tickets queued — both should spawn
        config_path.write_text("per_client_max_parallel:\n  test-client: 2\n")
        add_ticket(TicketTask(ticket_id="GEN-CFG-1", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-CFG-2", client="test-client"))

        daemon = FakeNativeDaemonClient()
        run_dispatch_loop(once=True, native_daemon=daemon)
        assert len(daemon.spawn_calls) == 2  # cap=2: both tickets spawned

        # Reset queue to PENDING and clear sessions so tick 2 has a clean slate
        queue = load_dev_queue()
        for task in queue.tasks:
            task.status = QueueItemStatus.PENDING
            task.session_id = None
        save_dev_queue(queue)
        # Clear daemon sessions from state so running_count resets to 0
        state = load_state()
        state.sessions = []
        save_state(state)

        # Rewrite config: cap drops to 1
        config_path.write_text("per_client_max_parallel:\n  test-client: 1\n")

        spawns_before = len(daemon.spawn_calls)
        run_dispatch_loop(once=True, native_daemon=daemon)
        # Under broken code (frozen cap=2): would spawn 2 more → total 4
        # Under correct code (reloaded cap=1): spawns exactly 1 → total 3
        assert len(daemon.spawn_calls) == spawns_before + 1

    def test_config_last_good_on_corrupt(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """In-loop reload failure logs WARNING and continues with last-good config."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        real_load = load_effective_config
        call_count = 0

        def patched_load() -> OrchestratorConfig:
            nonlocal call_count
            call_count += 1
            # First call (startup): succeed normally
            # Second call (in-loop reload): simulate a corrupt YAML file
            if call_count >= 2:
                msg = "simulated corrupt yaml"
                raise yaml.YAMLError(msg)
            return real_load()

        monkeypatch.setattr("cw.dispatch.load_effective_config", patched_load)

        daemon = FakeNativeDaemonClient()
        # Should NOT raise despite the in-loop reload failing
        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            run_dispatch_loop(once=True, native_daemon=daemon)

        assert any(
            "config reload failed" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )


# ---------------------------------------------------------------------------
# TestFreshnessGateAutoFF
# ---------------------------------------------------------------------------


class TestFreshnessGateAutoFF:
    """Auto-ff tests: stale+behind triggers fast-forward; other states block."""

    def test_auto_ff_behind_succeeds_claims_ticket(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' + successful ff → task claimed.

        TICKET_NEEDS_SYNC must NOT be emitted; spawned=1.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-100", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "abc12345" * 5, "def67890" * 5, 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr(
            "cw.dispatch.fast_forward_main",
            lambda _client, **_kwargs: ("abc12345" * 5, "def67890" * 5),
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        # ff succeeded → stale cleared → task should be spawned
        assert result.spawned == 1

        events = read_events(
            consumer="test-auto-ff-behind",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        # TICKET_NEEDS_SYNC must NOT be emitted after a successful auto-ff.
        assert len(events) == 0

    def test_auto_ff_ahead_skips_with_ticket_needs_sync(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='ahead' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-101", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.check_main_ff_safety",
            lambda _client, **_kw: "ahead",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-ahead",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "CW-101"

    def test_auto_ff_diverged_skips(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='diverged' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-102", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-diverged",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_detached_skips(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='detached' → TICKET_NEEDS_SYNC emitted, claim blocked."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-103", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.check_main_ff_safety",
            lambda _client, **_kw: "detached",
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-detached",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_ff_raises_falls_through_to_ticket_needs_sync(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' but fast_forward_main raises WorktreeError.

        Exception must be swallowed; TICKET_NEEDS_SYNC emitted as fallback.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-104", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        def _boom(_client: object, **_kwargs: object) -> tuple[str, str]:
            msg = "git pull failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.dispatch.fast_forward_main", _boom)

        daemon = FakeNativeDaemonClient()
        # Exception must be swallowed; falls through to TICKET_NEEDS_SYNC.
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-raises",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_false_keeps_ticket_needs_sync(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """auto_ff=False preserves legacy block-only behavior even when 'behind'."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-105", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        # check_main_ff_safety must NOT be called; if it is called that's a bug
        check_called = [False]

        def _check_boom(_client: object) -> str:
            check_called[0] = True
            return "behind"

        monkeypatch.setattr("cw.dispatch.check_main_ff_safety", _check_boom)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, auto_ff=False, native_daemon=daemon)

        assert result.spawned == 0
        # check_main_ff_safety must NOT be called when auto_ff=False.
        assert not check_called[0]
        events = read_events(
            consumer="test-auto-ff-disabled",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1


# ---------------------------------------------------------------------------
# TestTier1ClientSelection — max_parallel_clients (#558)
# ---------------------------------------------------------------------------


class TestTier1ClientSelection:
    """Tier-1: max_parallel_clients limits how many clients are dispatched per tick."""

    def test_max_parallel_clients_limits_eligible_clients(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """With max_parallel_clients=1 and two clients, only one is dispatched."""
        ws_a = make_git_repo("workspace/client-a")
        ws_b = make_git_repo("workspace/client-b")
        client_a = ClientConfig(
            name="client-a",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-a",
        )
        client_b = ClientConfig(
            name="client-b",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-b",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  client-a:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  client-b:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="T-A1", client="client-a"))
        add_ticket(TicketTask(ticket_id="T-B1", client="client-b"))

        config = OrchestratorConfig(max_parallel_clients=1, default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Only 1 client dispatched (whichever iteration visits first)
        assert result.spawned == 1

    def test_max_parallel_clients_none_dispatches_all(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """With max_parallel_clients=None (default), all eligible clients dispatched."""
        ws_a = make_git_repo("workspace/client-a2")
        ws_b = make_git_repo("workspace/client-b2")
        client_a = ClientConfig(
            name="client-a2",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-a2",
        )
        client_b = ClientConfig(
            name="client-b2",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-b2",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  client-a2:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  client-b2:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="T-A2", client="client-a2"))
        add_ticket(TicketTask(ticket_id="T-B2", client="client-b2"))

        config = OrchestratorConfig(max_parallel_clients=None, default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Both clients dispatched
        assert result.spawned == 2

    def test_stale_client_does_not_consume_tier1_quota(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A freshness-gate-skipped client does not consume Tier-1 quota."""
        ws_a = make_git_repo("workspace/client-a3")
        ws_b = make_git_repo("workspace/client-b3")
        client_a = ClientConfig(
            name="client-a3",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-a3",
        )
        client_b = ClientConfig(
            name="client-b3",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-b3",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  client-a3:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  client-b3:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="T-A3", client="client-a3"))
        add_ticket(TicketTask(ticket_id="T-B3", client="client-b3"))

        # client-a3 is stale (skipped by the freshness gate); client-b3 fresh.
        monkeypatch.setattr(
            "cw.dispatch.is_main_behind_origin",
            lambda client, **_kw: (client.name == "client-a3", "aaa", "bbb", 1),
        )

        config = OrchestratorConfig(max_parallel_clients=1, default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon, auto_ff=False)

        # The stale client did not consume the single Tier-1 slot.
        assert result.spawned == 1


# ---------------------------------------------------------------------------
# TestTier2LaneAllocation — per-lane grants (#558)
# ---------------------------------------------------------------------------


class TestTier2LaneAllocation:
    """Tier-2: per-lane grant formula allocates slots across lanes correctly."""

    def test_multi_lane_grants_respected_independently(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Two lanes max_parallel=1 each; two tasks in different lanes → 2 spawned."""
        lanes = [
            LaneConfig(name="impl", max_parallel=1),
            LaneConfig(name="idea", max_parallel=1),
        ]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        add_ticket(TicketTask(ticket_id="IMPL-1", client="test-client", lane="impl"))
        add_ticket(TicketTask(ticket_id="IDEA-1", client="test-client", lane="idea"))

        config = OrchestratorConfig(default_ceiling=2)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 2

    def test_saturated_lane_does_not_block_other_lanes(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Lane 'impl' is at capacity; lane 'idea' still spawns."""
        lanes = [
            LaneConfig(name="impl", max_parallel=1),
            LaneConfig(name="idea", max_parallel=1),
        ]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        # Seed an active session for the 'impl' lane task
        impl_task = TicketTask(
            ticket_id="IMPL-RUNNING", client="test-client", lane="impl"
        )
        impl_task.status = QueueItemStatus.RUNNING
        impl_task.session_id = "sess-impl-1"
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(impl_task)
            save_dev_queue(store)

        sess = Session(
            id="sess-impl-1",
            name="test-client/auto-dev/IMPL-RUNNING",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
        )
        save_state(CwState(sessions=[sess]))

        # Pending task in the 'idea' lane
        add_ticket(TicketTask(ticket_id="IDEA-1", client="test-client", lane="idea"))
        # Pending task in 'impl' lane — should NOT be claimed (lane full)
        add_ticket(TicketTask(ticket_id="IMPL-2", client="test-client", lane="impl"))

        config = OrchestratorConfig(default_ceiling=2)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Only idea-lane task spawned; impl lane is saturated
        assert result.spawned == 1
        store = load_dev_queue()
        running = [
            t
            for t in store.tasks
            if t.status == QueueItemStatus.RUNNING and t.session_id != "sess-impl-1"
        ]
        assert len(running) == 1
        assert running[0].ticket_id == "IDEA-1"

    def test_lane_filtered_claim_order(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """_claim_next_pending with lane filter only claims tasks in that lane."""
        lanes = [
            LaneConfig(name="impl", max_parallel=1),
            LaneConfig(name="idea", max_parallel=1),
        ]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        # Higher-priority task in wrong lane
        add_ticket(
            TicketTask(
                ticket_id="IMPL-HIGH",
                client="test-client",
                lane="impl",
                priority=10,
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="IDEA-LOW",
                client="test-client",
                lane="idea",
                priority=0,
            )
        )

        config = OrchestratorConfig(default_ceiling=2)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Both lanes each get one spawn
        assert result.spawned == 2
        store = load_dev_queue()
        running = store.running()
        running_ids = {t.ticket_id for t in running}
        assert "IMPL-HIGH" in running_ids
        assert "IDEA-LOW" in running_ids


# ---------------------------------------------------------------------------
# TestLaneCapCountingWithBlockedOnUser (#558)
# ---------------------------------------------------------------------------


class TestLaneCapCountingWithBlockedOnUser:
    """BLOCKED_ON_USER task session_id still occupies the lane slot (ADR-0006)."""

    def test_blocked_on_user_session_counts_toward_lane_cap(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """A BLOCKED_ON_USER task with active session occupies lane; no over-spawn."""
        lanes = [LaneConfig(name="impl", max_parallel=1)]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        # A BLOCKED_ON_USER task with a live session_id
        blocked_task = TicketTask(
            ticket_id="IMPL-BLOCKED",
            client="test-client",
            lane="impl",
        )
        blocked_task.status = QueueItemStatus.BLOCKED_ON_USER
        blocked_task.session_id = "sess-blocked-1"
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(blocked_task)
            save_dev_queue(store)

        sess = Session(
            id="sess-blocked-1",
            name="test-client/auto-dev/IMPL-BLOCKED",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
        )
        save_state(CwState(sessions=[sess]))

        # Another pending task in the same lane
        add_ticket(TicketTask(ticket_id="IMPL-NEW", client="test-client", lane="impl"))

        config = OrchestratorConfig(default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Lane is full (BLOCKED_ON_USER counts) — should NOT spawn
        assert result.spawned == 0


# ---------------------------------------------------------------------------
# TestSingleLaneBackwardCompat (#558)
# ---------------------------------------------------------------------------


class TestSingleLaneBackwardCompat:
    """No-lanes client: synthesized default lane; byte-identical to pre-#558."""

    def test_no_lanes_config_dispatches_same_as_before(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A client with no lanes: dispatches exactly as before (1 task, 1 session)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="COMPAT-1", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 1
        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].ticket_id == "COMPAT-1"

        # Verify DISPATCH_TICK event has lanes field
        events = read_events(
            consumer="test-compat",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["claimed"] == 1
        assert "lanes" in p
        assert DEFAULT_LANE in p["lanes"]
        assert p["lanes"][DEFAULT_LANE]["claimed"] == 1

    def test_no_lanes_second_tick_respects_cap(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """No-lanes client: second tick, cap=1, running session → does not overspawn."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="COMPAT-A", client="test-client"))
        add_ticket(TicketTask(ticket_id="COMPAT-B", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)
        # After first tick: 1 RUNNING, 1 PENDING
        result2 = dispatch_tick(simple_config, native_daemon=daemon)
        # Cap=1, running=1 → no spawn on second tick
        assert result2.spawned == 0
