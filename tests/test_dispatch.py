"""Tests for cw.dispatch - tick-based dispatch loop."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue, save_plan
from cw.dispatch import (
    consume_completed_sessions,
    dispatch_tick,
    persist_last_result,
)
from cw.events import read_events, record_event
from cw.exceptions import WorktreeError
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
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
        spawned = dispatch_tick(cap2_config, native_daemon=daemon, parent=parent.id)
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
        spawned = dispatch_tick(cap2_config, native_daemon=daemon)

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
        spawned = dispatch_tick(config, native_daemon=daemon)

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
        spawned1 = dispatch_tick(simple_config, native_daemon=daemon)
        assert spawned1 == 1

        # Second tick: running_count=1 >= cap=1, should skip
        spawned2 = dispatch_tick(simple_config, native_daemon=daemon)
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

    def test_consume_completes_wrapper_emitted_event_shape(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """The full event payload emitted by wrapper.signal_completed completes.

        Issue #99: the wrapper emits SESSION_COMPLETED with crashed=False
        plus ``status`` (the parsed auto-dev outcome) and ``session_name``.
        The consumer must treat this exactly like a legacy non-crashed event
        — the new fields are forward-compatible metadata, not behavior.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-WRAP",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="wrapper-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": "wrapper-session",
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
        count = dispatch_tick(cap2_config, native_daemon=daemon)

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
        count = dispatch_tick(simple_config, native_daemon=daemon)

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
        count = dispatch_tick(simple_config, native_daemon=daemon)

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
        spawned = dispatch_tick(cap2_config, native_daemon=daemon, use_plan=True)
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
        spawned = dispatch_tick(simple_config, native_daemon=daemon, use_plan=True)
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
        spawned = dispatch_tick(cap2_config, native_daemon=daemon, use_plan=True)
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

    spawned = dispatch_tick(simple_config, native_daemon=daemon)
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
    daemon = FakeNativeDaemonClient()

    # Step 2: tick triggers reconcile (revert + emit crashed event), then
    # claims and respawns the same ticket with a new session id.
    spawned = dispatch_tick(simple_config, native_daemon=daemon)
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
    # correctly completes the task.
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
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
            lambda _client: (True, "aaa", "bbb", 3),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
            lambda _client: (True, "aaa", "bbb", 1),
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

        def _freshness_check(client: ClientConfig) -> tuple[bool, str, str, int]:
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
        spawned = dispatch_tick(config, native_daemon=daemon)

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
            lambda _client: (False, "abc", "abc", 0),
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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

        def _counting(_client: ClientConfig) -> tuple[bool, str, str, int]:
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

        def _boom(_client: ClientConfig) -> tuple[bool, str, str, int]:
            msg = "network unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.is_main_behind_origin", _boom)

        daemon = FakeNativeDaemonClient()

        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
        import subprocess

        workspace_dir = sample_client_config.workspace_path
        before_head = subprocess.check_output(
            ["git", "-C", str(workspace_dir), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-300", client="test-client"))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

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
        spawned = dispatch_tick(simple_config, native_daemon=daemon)

        assert spawned == 0
        assert daemon.spawn_calls == []

        store = load_dev_queue()
        tasks = [t for t in store.tasks if t.ticket_id == "GEN-300-guard"]
        assert len(tasks) == 1
        assert tasks[0].status == QueueItemStatus.PENDING
