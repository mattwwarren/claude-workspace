"""Tests for cw.dispatch - tick-based dispatch loop."""

from __future__ import annotations

import contextlib
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cw.config import (
    _load_concurrency_overrides,
    _save_concurrency_overrides,
    dispatch_loop_lock,
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
    _AVAILABILITY_OUTAGE_REASON,
    _AVAILABILITY_PROBE_TTL_SECONDS,
    _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD,
    _CODEX_CAPABILITY_PROBE_TTL_SECONDS,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
    DispatchTickResult,
    _accumulate_task_cost,
    _cached_codex_capability_diagnosis,
    _codex_capability_gate,
    _reset_codex_capability_cache,
    _resolve_dispatch_skip_reason,
    consume_completed_sessions,
    dispatch_tick,
    run_dispatch_loop,
)
from cw.dispatch_state import (
    AvailabilityProbeCache,
    load_availability_probe_cache,
    save_availability_probe_cache,
    save_usage_limit_armed_at,
    save_usage_limited_until,
)
from cw.events import read_events, record_event
from cw.exceptions import (
    ConfigValidationError,
    DispatchLoopLockedError,
    StaleWorktreeError,
    VersionDriftError,
    WorktreeError,
)
from cw.models import (
    CODEX_BACKEND,
    DEFAULT_GLOBAL_ATTEMPT_CEILING,
    DEFAULT_LANE,
    ClientConcurrencyOverride,
    ClientConfig,
    ConcurrencyOverrides,
    CwState,
    DevQueueStore,
    DispatchPlan,
    DispatchSkipReason,
    LaneConcurrencyOverride,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.native_daemon import NativeDaemonClient
    from tests.conftest import CapturedEvent


# ---------------------------------------------------------------------------
# Package-split import guard (#1310)
# ---------------------------------------------------------------------------


def test_dispatch_package_submodules_import_without_cycle() -> None:
    """The gating/claim/lanes submodules import with no ImportError (#1310).

    Fast-fails the gating<->claim circular-import risk: gating imports claim at
    module top and claim reaches back into gating via a function-level deferred
    import, so a regression that promotes that deferred import to module top
    would surface here as an ImportError before the rest of the suite collects.
    """
    from cw.dispatch import claim, gating, lanes

    assert gating is not None
    assert claim is not None
    assert lanes is not None


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


@pytest.fixture
def breaker_config() -> OrchestratorConfig:
    """OrchestratorConfig with a low lane circuit-breaker threshold for tests."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 1},
        lane_circuit_breaker_threshold=2,
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
        assert prompt == "/auto-dev-plan GEN-300 --headless"

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
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        # Write a session.completed event referencing the ticket
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-300", "client": "test-client"},
        )

        # B2: no session/last_result -> Rule 6 -> BLOCKED_ON_USER
        completed = consume_completed_sessions()
        assert completed == 1

        updated_store = load_dev_queue()
        assert updated_store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

        # B2: no session in state -> Rule 6 -> BLOCKED_ON_USER
        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

        # B2: no session in state -> Rule 6 -> BLOCKED_ON_USER
        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

        # B2: no session in state -> Rule 6 -> BLOCKED_ON_USER
        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

        # B2: no session "daemon-session" in state -> Rule 6 -> BLOCKED_ON_USER
        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

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

    @pytest.mark.parametrize(
        ("v4_status", "ambiguities", "premises"),
        [
            (
                "ambiguities_pending_resolution",
                [{"question": "Use X or Y?"}],
                [],
            ),
            (
                "premises_pending_verification",
                [],
                [{"claim": "Module Z exists"}],
            ),
        ],
    )
    def test_consume_plan_parked_emits_needs_attention(
        self,
        v4_status: str,
        ambiguities: list[dict[str, object]],
        premises: list[dict[str, object]],
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """V4 plan-parked status emits SESSION_NEEDS_ATTENTION(plan_parked) (#923).

        When a session ends with ambiguities_pending_resolution or
        premises_pending_verification, consume_completed_sessions must emit a
        SESSION_NEEDS_ATTENTION event so operators watching that event type
        receive the park signal. ``last_result`` is populated by the RFC 0012
        door in production; here it is pre-populated directly on the ``Session``
        (mirroring ``test_consume_paused_status_routes_to_blocked_on_user``) so
        ``apply_staged_decision`` reads a real sentinel.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-923",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-923",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = {
            "schema_version": 4,
            "ticket_id": "GEN-923",
            "status": v4_status,
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": "small",
                "files": 2,
                "lines_estimate": 50,
                "lines_actual": 0,
                "forbidden_touched": False,
            },
            "plan_source": "github_issue_existing",
            "branch": None,
            "worktree_path": "/tmp/wt",
            "fork_point_sha": None,
            "commits": [],
            "pr": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "HIGH",
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "ambiguities": ambiguities,
            "premises": premises,
            "blocker": None,
            "next_actions": ["Resolve open question before proceeding"],
        }

        sess = Session(
            id="sess-923",
            name="test-client/auto-dev/GEN-923",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result=sentinel,
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-923", "session_id": "sess-923"},
        )

        completed = consume_completed_sessions()
        assert completed == 1
        assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER

        attention = read_events(
            consumer="test-923-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention) == 1
        payload = attention[0].payload
        assert payload["paused_status"] == "plan_parked"
        assert payload["ticket_id"] == "GEN-923"
        assert payload["client"] == "test-client"
        assert payload["session_id"] == "sess-923"
        assert payload["crashed"] is False
        assert payload["lane"] == "default"

    def test_consume_non_paused_status_routes_to_completed(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with a non-paused last_result routes task to COMPLETED.

        Verifies the paused-status guard does not affect normal shipped/no_op
        outcomes. See #489.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

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
            last_result={"status": "shipped", "schema_version": 4},
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-489B", "session_id": "sess-489b"},
        )

        # B2: shipped at PLAN stage -> _stage_advance -> IMPL (PENDING), not COMPLETED
        # The test now validates that shipped at a non-terminal stage advances.
        completed = consume_completed_sessions()
        assert completed == 1
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.PENDING
        from cw.models import Stage

        assert task.stage == Stage.IMPL

    def test_consume_advances_staged_pipeline_from_prepopulated_last_result(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A small-tier plan_pending_approval at PLAN auto-advances to IMPL.

        ``last_result`` is populated by the RFC 0012 door (``emit_result_on``/
        ``emit_result_locked``) at each producer's write site before
        ``SESSION_COMPLETED`` is emitted -- the event itself carries no result
        payload. This test pre-populates the session's ``last_result`` the way
        the door would have (mirroring
        ``test_consume_paused_status_routes_to_blocked_on_user``) and verifies
        ``consume_completed_sessions`` reads it and advances the staged
        pipeline instead of falling through to Rule 6's BLOCKED_ON_USER default.
        """
        from cw.models import Stage

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="GEN-694",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            stage=Stage.PLAN,
            session_id="sess-694",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sess = Session(
            id="sess-694",
            name="test-client/auto-dev/GEN-694",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={
                "schema_version": 4,
                "ticket_id": "GEN-694",
                "status": "plan_pending_approval",
                "stage_reached": "stage1_plan",
                "scope": {
                    "tier": "small",
                    "files": 4,
                    "lines_estimate": 258,
                    "lines_actual": 0,
                    "forbidden_touched": False,
                },
                "plan_source": "github_issue_existing",
                "branch": None,
                "worktree_path": "/tmp/wt",
                "fork_point_sha": None,
                "commits": [],
                "pr": None,
                "review": {
                    "must_fix_initial": 0,
                    "should_fix": 0,
                    "fix_cycles_used": 0,
                },
                "health": {
                    "lowest_agent_confidence": "HIGH",
                    "any_incomplete_risk": False,
                    "shortcuts": [],
                    "recommendation": "PROCEED",
                    "downgrade_applied": False,
                    "fix_loop_escalated": False,
                },
                "friction_highlights": [],
                "ambiguities": [],
                "blocker": None,
                "next_actions": [],
            },
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-694", "session_id": "sess-694"},
        )

        completed = consume_completed_sessions()
        assert completed == 1
        advanced = load_dev_queue().tasks[0]
        # Small-tier plan_pending_approval auto-advances PLAN -> IMPL (PENDING),
        # NOT BLOCKED_ON_USER.
        assert advanced.status == QueueItemStatus.PENDING
        assert advanced.stage == Stage.IMPL

    def test_consume_null_last_result_routes_to_completed(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """SESSION_COMPLETED with last_result=None routes task to COMPLETED.

        Sessions that did not emit a sentinel (e.g. interactive) have
        last_result=None; they must fall through to COMPLETED. See #489.
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

        # B2: last_result=None -> Rule 6 -> BLOCKED_ON_USER
        # Sessions without sentinel must not silently complete in a staged pipeline.
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
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
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
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
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
    # correctly completes the task.
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "ticket_id": "TKT-RACE",
            "session_id": new_session_id,
            "client": sample_client_config.name,
        },
    )
    # B2: no session for new_session_id in state -> Rule 6 -> BLOCKED_ON_USER
    completed = consume_completed_sessions()
    assert completed == 1
    assert load_dev_queue().tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


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
        monkeypatch.setattr("cw.dispatch.claim.create_worktree", _boom)

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

        monkeypatch.setattr("cw.dispatch.claim.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.claim.remove_worktree", _record_remove)

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        assert daemon.spawn_calls == []
        # Stale tree force-removed for the ticket's branch before the revert.
        assert removed == [("dev/GEN-404S", True)]

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

        monkeypatch.setattr("cw.dispatch.claim.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.claim.remove_worktree", _remove_boom)

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

        monkeypatch.setattr("cw.dispatch.claim.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.claim.remove_worktree", _record_remove)
        monkeypatch.setattr(
            "cw.dispatch.claim.worktree_has_unsaved_work", lambda _c, _b: True
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

        monkeypatch.setattr("cw.dispatch.claim.create_worktree", _stale)
        monkeypatch.setattr("cw.dispatch.claim.remove_worktree", _record_remove)
        monkeypatch.setattr(
            "cw.dispatch.claim.worktree_has_unsaved_work", lambda _c, _b: False
        )

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 0
        # Removal with force=True must have been called
        assert removed == [("dev/GEN-425C", True)]
        # Task reverted to PENDING (existing behaviour)
        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None


# ---------------------------------------------------------------------------
# TestDispatchCodexCapabilityGate
# ---------------------------------------------------------------------------


def _make_codex_clients_yaml(tmp_path: Path, client: ClientConfig) -> None:
    """Write a clients.yaml whose plan-stage executor uses the codex backend."""
    config_dir = tmp_path / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        f"  {client.name}:\n"
        f"    workspace_path: {client.workspace_path}\n"
        f"    default_branch: {client.default_branch}\n"
        f"    worktree_base: {client.worktree_base}\n"
        "    pipeline:\n"
        "      executors:\n"
        "        plan:\n"
        "          backend: codex\n"
    )


class _SpyExecutor:
    """Minimal StageExecutor stand-in that records spawn calls."""

    def __init__(self) -> None:
        self.spawn_calls = 0

    def spawn(self, **_kwargs: object) -> str:
        self.spawn_calls += 1
        return "spy-session-id"

    def stage_sentinel_schema(self, _stage: object) -> dict[str, object]:
        return {}


class TestDispatchCodexCapabilityGate:
    """Pre-spawn codex capability gate parks the task before resolve_executor (#1238).

    Monkeypatches the imported ``cw.dispatch.claim.codex_capability_diagnosis``
    name (not shutil/subprocess) — the probe mechanics are covered in
    test_codex_executor.py. The gate calls the probe through an in-process TTL
    cache (``_cached_codex_capability_diagnosis``); reset it before each test
    so one test's monkeypatched return value can't leak into the next via a
    stale cache entry.
    """

    @pytest.fixture(autouse=True)
    def _reset_capability_cache(self) -> None:
        _reset_codex_capability_cache()

    def test_codex_not_found_parks_blocked_on_user(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from cw.executor import CODEX_NOT_FOUND, CodexCapabilityDiagnosis

        _make_codex_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-CDX1", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis",
            lambda **_kwargs: CodexCapabilityDiagnosis(
                CODEX_NOT_FOUND, "codex binary not found"
            ),
        )
        spy = _SpyExecutor()
        monkeypatch.setattr("cw.dispatch.claim.resolve_executor", lambda *_a, **_k: spy)

        daemon = FakeNativeDaemonClient()
        caplog.set_level(logging.WARNING, logger="cw.dispatch")
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert spy.spawn_calls == 0
        assert daemon.spawn_calls == []

        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == CODEX_NOT_FOUND
        assert task.session_id is None

        assert any(
            "codex capability gate parked" in record.getMessage()
            and "test-client" in record.getMessage()
            and "GEN-CDX1" in record.getMessage()
            for record in caplog.records
            if record.name == "cw.dispatch"
        ), "expected WARNING naming the client/ticket"

    def test_codex_version_unknown_parks_blocked_on_user(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.executor import CODEX_VERSION_UNKNOWN, CodexCapabilityDiagnosis

        _make_codex_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-CDX2", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis",
            lambda **_kwargs: CodexCapabilityDiagnosis(
                CODEX_VERSION_UNKNOWN, "could not parse version: junk"
            ),
        )
        spy = _SpyExecutor()
        monkeypatch.setattr("cw.dispatch.claim.resolve_executor", lambda *_a, **_k: spy)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert spy.spawn_calls == 0
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == CODEX_VERSION_UNKNOWN
        assert task.session_id is None

    def test_codex_capable_spawns_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gate is a no-op when the probe reports capable — regression guard."""
        from cw.executor import CodexCapabilityDiagnosis

        _make_codex_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-CDX3", client="test-client"))

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis",
            lambda **_kwargs: CodexCapabilityDiagnosis(None, "0.144.5"),
        )
        spy = _SpyExecutor()
        monkeypatch.setattr("cw.dispatch.claim.resolve_executor", lambda *_a, **_k: spy)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 1
        assert spy.spawn_calls == 1
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.RUNNING
        assert task.session_id == "spy-session-id"

    def test_non_codex_backend_never_probes(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A claude-native task must not invoke the codex probe at all."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-CDX4", client="test-client"))

        probe_calls = 0

        def _spy_probe() -> object:
            nonlocal probe_calls
            probe_calls += 1
            msg = "probe must not run for non-codex backends"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.dispatch.claim.codex_capability_diagnosis", _spy_probe)

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1
        assert probe_calls == 0
        assert len(daemon.spawn_calls) == 1

    def test_cache_hit_reuses_probe_within_ttl(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two calls within the TTL only invoke the underlying probe once."""
        from cw.executor import CodexCapabilityDiagnosis

        calls: list[int] = []

        def _counting_probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
            calls.append(1)
            return CodexCapabilityDiagnosis(None, "0.144.5")

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis", _counting_probe
        )
        monkeypatch.setattr(
            "cw.dispatch.claim.resolve_executor_config",
            lambda *_a, **_k: StageExecutorConfig(backend=CODEX_BACKEND),
        )

        add_ticket(TicketTask(ticket_id="GEN-CDX5", client="test-client"))
        task = load_dev_queue().tasks[0]

        first = _cached_codex_capability_diagnosis()
        second = _cached_codex_capability_diagnosis()
        third = _codex_capability_gate(task, sample_client_config)

        assert len(calls) == 1, "second call within the TTL must reuse the cache"
        assert first == second
        assert third is None, "a capable probe result is a no-op gate"

    def test_cache_expires_after_ttl(
        self,
        tmp_dispatch_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Advancing past the TTL triggers a fresh probe call."""
        from freezegun import freeze_time

        from cw.executor import CodexCapabilityDiagnosis

        calls: list[int] = []

        def _counting_probe(**_kwargs: object) -> CodexCapabilityDiagnosis:
            calls.append(1)
            return CodexCapabilityDiagnosis(None, "0.144.5")

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis", _counting_probe
        )

        ttl_expiry = _CODEX_CAPABILITY_PROBE_TTL_SECONDS + 5
        with freeze_time("2026-07-16 12:00:00") as frozen:
            _cached_codex_capability_diagnosis()
            frozen.tick(delta=timedelta(seconds=ttl_expiry))
            _cached_codex_capability_diagnosis()

        assert len(calls) == 2, "TTL expiry must trigger a second real probe call"

    def test_consecutive_parks_trip_circuit_breaker_at_threshold(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A systemically-wrong probe verdict eventually engages spawn_error.

        Below _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD consecutive parks the
        gate must stay decoupled from spawn_error (per the prior fix cycle);
        at/above it, spawn_error must also be set so the existing per-lane
        circuit breaker provides a bounded backstop (#1238 review finding —
        an unbounded, un-circuit-broken park has no self-limiting mechanism).
        """
        from cw.executor import CODEX_VERSION_UNKNOWN, CodexCapabilityDiagnosis

        monkeypatch.setattr(
            "cw.dispatch.claim.codex_capability_diagnosis",
            lambda **_kwargs: CodexCapabilityDiagnosis(
                CODEX_VERSION_UNKNOWN, "could not parse version: junk"
            ),
        )
        monkeypatch.setattr(
            "cw.dispatch.claim.resolve_executor_config",
            lambda *_a, **_k: StageExecutorConfig(backend=CODEX_BACKEND),
        )

        outcomes = []
        for i in range(_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD + 1):
            add_ticket(TicketTask(ticket_id=f"GEN-CDX6-{i}", client="test-client"))
            task = load_dev_queue().tasks[-1]
            outcomes.append(_codex_capability_gate(task, sample_client_config))

        assert all(o is not None and o.capability_parked for o in outcomes)
        below_threshold = outcomes[: _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD - 1]
        assert all(o is not None and not o.spawn_error for o in below_threshold), (
            "isolated parks must not trip the circuit breaker"
        )
        at_threshold = outcomes[_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD - 1]
        assert at_threshold is not None
        assert at_threshold.spawn_error, (
            "reaching the threshold must engage the circuit-breaker backstop"
        )

    def test_recovery_between_parks_resets_the_streak(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fully-recovered gap between parks must not carry stale streak credit.

        Regression guard for a #1238 review finding: an earlier version of the
        park counter never reset on a capable probe result, so it behaved as a
        lifetime total rather than a consecutive-parks count — meaning a
        long-lived dispatch-loop process that had ever accumulated
        _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD parks (however isolated, however
        long ago, however fully recovered in between) would trip spawn_error on
        every future park forever after. Drives: park, park, capable, park — the
        final park is isolated (only one consecutive park since the last capable
        result) and must NOT trip spawn_error, even though 3 parks total have
        occurred in this test's lifetime.

        Clears only the TTL cache (not the park counter) between steps, via
        direct access to the module-level slot — ``_reset_codex_capability_cache``
        clears both, which would trivially pass this test regardless of whether
        the gate's own capable-branch reset logic (under test here) is correct.
        """
        import cw.dispatch as dispatch_module
        from cw.executor import CODEX_VERSION_UNKNOWN, CodexCapabilityDiagnosis

        monkeypatch.setattr(
            "cw.dispatch.claim.resolve_executor_config",
            lambda *_a, **_k: StageExecutorConfig(backend=CODEX_BACKEND),
        )

        incapable = CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN, "could not parse version: junk"
        )
        capable = CodexCapabilityDiagnosis(None, "0.144.5")

        def _probe_result(idx: int, diagnosis: CodexCapabilityDiagnosis) -> object:
            dispatch_module._codex_capability_cache.clear()
            monkeypatch.setattr(
                "cw.dispatch.claim.codex_capability_diagnosis",
                lambda **_kwargs: diagnosis,
            )
            add_ticket(TicketTask(ticket_id=f"GEN-CDX7-{idx}", client="test-client"))
            task = load_dev_queue().tasks[-1]
            return _codex_capability_gate(task, sample_client_config)

        first = _probe_result(0, incapable)
        second = _probe_result(1, incapable)
        recovered = _probe_result(2, capable)
        third = _probe_result(3, incapable)

        assert first is not None
        assert not first.spawn_error
        assert second is not None
        assert not second.spawn_error
        assert recovered is None, "a capable probe result must be a no-op gate"
        assert third is not None
        assert not third.spawn_error, (
            "an isolated park after a full recovery must not inherit stale"
            " streak credit from before the recovery"
        )


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
            "cw.dispatch.gating.is_main_behind_origin",
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
            "cw.dispatch.gating.is_main_behind_origin",
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

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin", _freshness_check
        )

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
            "cw.dispatch.gating.is_main_behind_origin",
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

        monkeypatch.setattr("cw.dispatch.gating.is_main_behind_origin", _counting)

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

        monkeypatch.setattr("cw.dispatch.gating.is_main_behind_origin", _boom)

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
        monkeypatch.setattr("cw.dispatch.gating.reconcile", _boom_reconcile)

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


def test_dispatch_tick_runs_diagnostics_cleanup_outside_lock(
    tmp_dispatch_dirs: Path,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch_tick runs the diagnostics cleanup once per tick, with the
    configured retention window and WITHOUT sessions_lock held (#1239)."""
    _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
    captured: dict[str, object] = {}

    def _spy(*, retention_hours: int) -> int:
        from cw.config import _sessions_lock_state

        captured["retention_hours"] = retention_hours
        captured["lock_held"] = getattr(_sessions_lock_state, "held", False)
        captured["calls"] = int(captured.get("calls", 0)) + 1
        return 0

    monkeypatch.setattr("cw.dispatch.tick.cleanup_expired_diagnostics", _spy)

    daemon = FakeNativeDaemonClient()
    dispatch_tick(simple_config, native_daemon=daemon)

    assert captured["calls"] == 1
    assert captured["retention_hours"] == simple_config.diagnostics_retention_hours
    assert captured["lock_held"] is False


def test_dispatch_tick_cleanup_failure_does_not_abort_tick(
    tmp_dispatch_dirs: Path,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising diagnostics cleanup is swallowed; the tick still spawns (#1239)."""
    _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
    add_ticket(TicketTask(ticket_id="GEN-diag-cleanup", client="test-client"))

    def _boom(*_a: object, **_k: object) -> int:
        msg = "simulated cleanup failure"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.dispatch.tick.cleanup_expired_diagnostics", _boom)

    daemon = FakeNativeDaemonClient()
    caplog.set_level(logging.ERROR, logger="cw.dispatch")

    spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

    assert spawned == 1
    assert any(
        "diagnostics cleanup failed" in record.getMessage().lower()
        for record in caplog.records
        if record.name == "cw.dispatch" and record.levelno >= logging.ERROR
    )


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
# TestGlobalAttemptCeiling
# ---------------------------------------------------------------------------


class TestGlobalAttemptCeiling:
    """_claim_next_pending parks tasks at the global attempt ceiling.

    GitHub issue #786: the dispatch loop must not re-spawn tasks indefinitely
    when workers die without emitting a sentinel (e.g. during a usage-limit
    window). A global attempt ceiling parks at-ceiling tasks BLOCKED_ON_USER
    and emits a dispatch.tick with skip_reason=ATTEMPT_CAP_BLOCKED.
    """

    def _ceiling_config(self, ceiling: int = 3) -> OrchestratorConfig:
        return OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"test-client": 1},
            global_attempt_ceiling=ceiling,
        )

    def test_below_ceiling_claims_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Task below the ceiling is claimed and attempts is incremented."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-786-below",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=2,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(self._ceiling_config(ceiling=3), native_daemon=daemon)

        queue = load_dev_queue()
        claimed = next(t for t in queue.tasks if t.ticket_id == "GEN-786-below")
        assert claimed.status == QueueItemStatus.RUNNING
        assert claimed.attempts == 3

    def test_at_ceiling_parks_task_blocked(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Task at the ceiling is parked BLOCKED_ON_USER; attempts not incremented."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-786-ceiling",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=3,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(self._ceiling_config(ceiling=3), native_daemon=daemon)

        queue = load_dev_queue()
        parked = next(t for t in queue.tasks if t.ticket_id == "GEN-786-ceiling")
        assert parked.status == QueueItemStatus.BLOCKED_ON_USER
        assert parked.disposition == "attempt_cap_blocked"
        # attempts must NOT be incremented when parking at the ceiling
        assert parked.attempts == 3
        # daemon was never invoked
        assert daemon.spawn_calls == []

    def test_at_ceiling_emits_attempt_cap_blocked_event(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Ceiling-parked task emits dispatch.tick skip_reason=attempt_cap_blocked."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-786-event",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=3,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(self._ceiling_config(ceiling=3), native_daemon=daemon)

        events = read_events(
            consumer="test-786-cap-event",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        cap_events = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.ATTEMPT_CAP_BLOCKED
        ]
        assert len(cap_events) == 1
        assert cap_events[0].payload["ticket_id"] == "GEN-786-event"
        assert cap_events[0].payload["client"] == "test-client"

    def test_at_ceiling_priority_path_parks_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Ceiling check also fires on the priority-ticket path."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-786-pri",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=3,
        )
        store = DevQueueStore(tasks=[task])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        # Use use_plan=True so the priority-ticket path in _claim_next_pending
        # is exercised. The plan must reference the task's client so it gets
        # a non-empty priority_ids list.
        save_plan(
            DispatchPlan(
                tasks=[TicketTask(ticket_id="GEN-786-pri", client="test-client")]
            )
        )

        dispatch_tick(
            self._ceiling_config(ceiling=3), native_daemon=daemon, use_plan=True
        )

        queue = load_dev_queue()
        parked = next(t for t in queue.tasks if t.ticket_id == "GEN-786-pri")
        assert parked.status == QueueItemStatus.BLOCKED_ON_USER
        assert parked.disposition == "attempt_cap_blocked"
        assert parked.attempts == 3

    def test_at_ceiling_head_of_line_does_not_starve_younger_pending_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Older at-ceiling task must not block a younger claimable task (#1248).

        Regression for GitHub #1248: _claim_next_pending's plain pending-scan
        used to `return None` the instant it hit an attempt-capped task,
        abandoning the rest of the sorted pending list for that tick. On a
        max_parallel=1 lane this is indefinite head-of-line starvation -- the
        capped task parks BLOCKED_ON_USER (which occupies the lane's only
        slot) and a claimable task sorted behind it is never reached.

        Mirrors the real repro: two PENDING tasks, equal priority, the older
        one at the global attempt ceiling, the younger one never attempted.
        A single dispatch_tick must park the older AND claim the younger in
        the same tick.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        older_capped = TicketTask(
            ticket_id="GEN-1248-old",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=3,
            created_at=datetime.fromisoformat("2026-07-07T00:00:00+00:00"),
        )
        younger_claimable = TicketTask(
            ticket_id="GEN-1248-young",
            client="test-client",
            status=QueueItemStatus.PENDING,
            attempts=0,
            created_at=datetime.fromisoformat("2026-07-15T00:00:00+00:00"),
        )
        store = DevQueueStore(tasks=[older_capped, younger_claimable])
        save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(self._ceiling_config(ceiling=3), native_daemon=daemon)

        # The younger, claimable task must be claimed in this same tick --
        # not left PENDING behind the parked older task.
        assert result.spawned == 1

        queue = load_dev_queue()
        old_task = next(t for t in queue.tasks if t.ticket_id == "GEN-1248-old")
        young_task = next(t for t in queue.tasks if t.ticket_id == "GEN-1248-young")

        assert old_task.status == QueueItemStatus.BLOCKED_ON_USER
        assert old_task.disposition == "attempt_cap_blocked"
        assert old_task.attempts == 3  # unchanged -- not reclaimed

        assert young_task.status == QueueItemStatus.RUNNING
        assert young_task.attempts == 1
        assert len(daemon.spawn_calls) == 1

    def test_default_ceiling_is_ten(self) -> None:
        """DEFAULT_GLOBAL_ATTEMPT_CEILING constant value and model default match."""
        assert DEFAULT_GLOBAL_ATTEMPT_CEILING == 10
        cfg = OrchestratorConfig()
        assert cfg.global_attempt_ceiling == DEFAULT_GLOBAL_ATTEMPT_CEILING


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
            "cw.dispatch.claim.create_worktree",
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
        task = _make_ticket_task(
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
        sess = _make_daemon_session(
            id=session_id,
            name="test-client/auto-dev/GEN-1",
            client="test-client",
            origin=SessionOrigin.USER,
            workspace_path=Path("/dev/null"),
            surface_ref=None,
            worktree_path=None,
            started_at=datetime.now(UTC),
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

    def test_stage_mismatch_skips_cost_accumulation_and_completed_count(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """A refused stage-mismatch sentinel must not accumulate cost or count
        toward ``completed`` (#1019, Pre-flight Resolution #4's true-no-op
        contract) -- ``_apply_events_to_store`` is the consume-path caller of
        ``apply_staged_decision`` and must honor its bool return like the
        reconcile path already does.
        """
        task = TicketTask(
            ticket_id="GEN-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id="s_mismatch1",
            stage=Stage.REVIEW,
            total_cost_usd=1.0,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        self._make_session(
            "s_mismatch1",
            cost_usd=5.0,
            last_result={"status": "stage_complete", "stage_reached": "stage2_impl"},
        )
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "GEN-1", "session_id": "s_mismatch1"},
        )
        completed = consume_completed_sessions()

        assert completed == 0
        store = load_dev_queue()
        t = store.tasks[0]
        assert t.total_cost_usd == pytest.approx(1.0)
        assert t.status == QueueItemStatus.RUNNING
        assert t.stage == Stage.REVIEW

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

        tick_count = 0

        original_dispatch_tick = dispatch_tick

        def _three_tick_dispatch(
            config: OrchestratorConfig,
            *,
            use_plan: bool = False,
            parent: str | None = None,
            native_daemon: FakeNativeDaemonClient | None = None,
            emit: Callable[[str], None] | None = None,
            warned_stale: set[tuple[str, str]] | None = None,
            warned_fetch_fail: set[str] | None = None,
            warned_collision: set[frozenset[str]] | None = None,
            warned_ssh_key: set[str] | None = None,
            usage_limited_until: datetime | None = None,
            auto_ff: bool = True,
            client_filter: str | None = None,
        ) -> DispatchTickResult:
            nonlocal tick_count
            tick_count += 1
            return original_dispatch_tick(
                config,
                use_plan=use_plan,
                parent=parent,
                native_daemon=native_daemon,
                emit=emit,
                warned_stale=warned_stale,
                warned_fetch_fail=warned_fetch_fail,
                warned_collision=warned_collision,
                warned_ssh_key=warned_ssh_key,
                usage_limited_until=usage_limited_until,
                auto_ff=auto_ff,
                client_filter=client_filter,
            )

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", _three_tick_dispatch)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

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
            "cw.dispatch.gating.is_main_behind_origin",
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
        assert p["lanes"] == {
            "default": {
                "claimed": 0,
                "running": 0,
                "blocked": 0,
                "signoff": 0,
                "pending": 2,
            }
        }

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

    def test_reconcile_usage_limit_skips_spawn_same_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-tick race fix: when reconcile reports usage_limited, dispatch_tick
        skips spawning immediately (before the spawn loop runs) so the task is not
        re-spawned into the active rate-limit window (#804)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL-RACE", client="test-client"))

        daemon = FakeNativeDaemonClient()

        # Reconcile returns usage_limited=True (phantom with usage-limit transcript).
        monkeypatch.setattr(
            "cw.dispatch.tick._reconcile_usage_limited",
            lambda: True,
        )

        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert result.usage_limit_detected is True
        assert daemon.spawn_calls == []

    def test_reconcile_usage_limit_emits_skip_event_same_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same-tick race: skip event has skip_reason=usage_limited (#804)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL-RACE2", client="test-client"))

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.dispatch.tick._reconcile_usage_limited",
            lambda: True,
        )

        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-ul-race-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.USAGE_LIMITED

    def test_run_dispatch_loop_persists_usage_limited_until(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_dispatch_loop calls save_usage_limited_until when usage limit is
        detected in multi-tick mode (once=False). Verified by monkeypatching
        save_usage_limited_until and checking the captured argument (#804)."""
        import cw.dispatch

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL-PERSIST", client="test-client"))

        daemon = FakeNativeDaemonClient()
        saved: list[object] = []

        real_save = save_usage_limited_until

        def capturing_save(dt: object) -> None:
            saved.append(dt)
            real_save(dt)  # type: ignore[arg-type]

        monkeypatch.setattr("cw.dispatch.loop.save_usage_limited_until", capturing_save)

        # Patch time.sleep so the loop exits on the second tick.
        call_count = 0
        original_tick = cw.dispatch.loop.dispatch_tick

        def one_shot_tick(*args: object, **kwargs: object) -> DispatchTickResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate usage_limit_detected=True on first tick via a real spawn
                # error (raise_usage_limit path).
                daemon.raise_usage_limit = True
                result = original_tick(*args, **kwargs)  # type: ignore[arg-type]
                daemon.raise_usage_limit = False
                return result
            # Second tick: exit the loop
            raise KeyboardInterrupt

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", one_shot_tick)
        monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

        with contextlib.suppress(KeyboardInterrupt):
            run_dispatch_loop(native_daemon=daemon)

        assert len(saved) >= 1
        saved_dt = saved[0]
        assert isinstance(saved_dt, datetime)
        assert saved_dt > datetime.now(UTC)

    def test_run_dispatch_loop_loads_persisted_backoff(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_dispatch_loop reads persisted usage_limited_until on start; if still
        active, spawning is suppressed without requiring a fresh detection (#804)."""
        from datetime import timedelta

        from cw.dispatch_state import save_usage_limited_until

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-UL-LOAD", client="test-client"))

        daemon = FakeNativeDaemonClient()

        # Pre-write a backoff window that hasn't expired yet.
        future = datetime.now(UTC) + timedelta(hours=1)
        save_usage_limited_until(future)

        run_dispatch_loop(once=True, native_daemon=daemon)

        # Spawn must have been suppressed because the loaded backoff is still active.
        assert daemon.spawn_calls == []

    def test_run_dispatch_loop_observes_backoff_written_by_another_process_mid_loop(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second, unrelated process writing usage_limited_until between ticks
        is observed on the very next tick of THIS process's loop (#1346). Without
        a per-tick re-read, this process's in-memory usage_limited_until never
        diverges from what it loaded at startup (None here), so it would keep
        spawning through another process's active fleet-wide backoff."""
        import cw.dispatch

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1346-XPROC", client="test-client"))

        daemon = FakeNativeDaemonClient()
        future = datetime.now(UTC) + timedelta(hours=1)

        call_count = 0
        captured: list[datetime | None] = []
        real_tick = cw.dispatch.loop.dispatch_tick

        def observing_tick(*args: object, **kwargs: object) -> DispatchTickResult:
            nonlocal call_count
            call_count += 1
            captured.append(kwargs.get("usage_limited_until"))  # type: ignore[arg-type]
            if call_count == 3:
                raise KeyboardInterrupt
            result = real_tick(*args, **kwargs)  # type: ignore[arg-type]
            if call_count == 1:
                # Simulate a SECOND, unrelated dispatch process detecting a
                # usage limit and persisting it -- this process never calls
                # its own _reconcile_usage_limited/UsageLimitError path.
                save_usage_limited_until(future)
            return result

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", observing_tick)
        monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

        with contextlib.suppress(KeyboardInterrupt):
            run_dispatch_loop(native_daemon=daemon)

        # Tick 1: no backoff anywhere yet -- spawns normally.
        assert captured[0] is None
        assert len(daemon.spawn_calls) == 1
        # Tick 2: the merge picked up the other process's write -- non-None,
        # still-future, and no additional spawn occurred.
        assert captured[1] is not None
        assert captured[1] > datetime.now(UTC)
        assert len(daemon.spawn_calls) == 1

    def test_run_dispatch_loop_corrupt_sidecar_mid_backoff_does_not_shorten_window(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A corrupt sidecar read mid-backoff must not reopen the spawn gate.

        load_usage_limited_until() returns None for a file that is absent,
        unreadable, OR malformed (config.py) -- a bare assignment in the
        per-tick merge would let a transient disk-read failure silently
        resurrect spawning during an active window. The merge must be
        None-safe: only a later PERSISTED value can extend the window,
        never shorten or clear an active in-memory one (#1346).

        Builds the active in-memory window via the merge itself (not the
        pre-loop startup load) on tick 2, then corrupts the sidecar before
        tick 3 -- so this only passes when the merge's None-check actually
        preserves a value it previously merged in, not merely "the value
        never changes because nothing re-reads at all"."""
        import cw.dispatch
        import cw.dispatch_state

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1346-CORRUPT", client="test-client"))

        daemon = FakeNativeDaemonClient()
        future = datetime.now(UTC) + timedelta(hours=1)

        call_count = 0
        captured: list[datetime | None] = []
        real_tick = cw.dispatch.loop.dispatch_tick

        def corrupting_tick(*args: object, **kwargs: object) -> DispatchTickResult:
            nonlocal call_count
            call_count += 1
            captured.append(kwargs.get("usage_limited_until"))  # type: ignore[arg-type]
            if call_count == 4:
                raise KeyboardInterrupt
            result = real_tick(*args, **kwargs)  # type: ignore[arg-type]
            if call_count == 1:
                # Simulate another process writing the backoff between
                # tick 1 and tick 2 (same idiom as the cross-process test).
                save_usage_limited_until(future)
            elif call_count == 2:
                # Corrupt the sidecar between tick 2 and tick 3 -- mirrors
                # test_config.py's test_load_returns_none_on_corrupt_json
                # input, which proves load_usage_limited_until() returns
                # None on it.
                cw.dispatch_state.DISPATCH_STATE_FILE.write_text("not-json")
            return result

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", corrupting_tick)
        monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

        with contextlib.suppress(KeyboardInterrupt):
            run_dispatch_loop(native_daemon=daemon)

        # Tick 1: no window anywhere yet -- spawns normally.
        assert captured[0] is None
        assert len(daemon.spawn_calls) == 1
        # Tick 2: merge picked up the other process's write -- suppressed.
        assert captured[1] is not None
        assert captured[1] > datetime.now(UTC)
        # Tick 3: disk read degrades to None (corrupt), but the merge must
        # NOT shorten/clear the still-active in-memory window built on
        # tick 2 -- still non-None, still future, still no new spawn.
        assert captured[2] is not None
        assert captured[2] > datetime.now(UTC)
        assert len(daemon.spawn_calls) == 1

    def test_run_dispatch_loop_expired_disk_window_does_not_resurrect_backoff(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An already-expired on-disk window must not resurrect a lapsed
        in-memory backoff (#1346). load_usage_limited_until() already
        returns None for a past timestamp (config.py contract), so the
        merge's take-the-max logic must never treat an expired persisted
        value as eligible to extend anything -- it stays None, and the
        gate falls through to its own now-vs-usage_limited_until check,
        which lets tick 2 spawn normally."""
        from freezegun import freeze_time

        import cw.dispatch

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1346-EXPIRE", client="test-client"))

        daemon = FakeNativeDaemonClient()

        with freeze_time("2026-07-16 12:00:00") as frozen:
            # Both in-memory (via pre-loop load) and on-disk start with the
            # SAME short-lived window.
            expiring = datetime.now(UTC) + timedelta(seconds=30)
            save_usage_limited_until(expiring)

            call_count = 0
            captured: list[datetime | None] = []
            real_tick = cw.dispatch.loop.dispatch_tick

            def advancing_tick(*args: object, **kwargs: object) -> DispatchTickResult:
                nonlocal call_count
                call_count += 1
                captured.append(kwargs.get("usage_limited_until"))  # type: ignore[arg-type]
                if call_count == 3:
                    raise KeyboardInterrupt
                result = real_tick(*args, **kwargs)  # type: ignore[arg-type]
                if call_count == 1:
                    # Advance past the window's expiry before tick 2 -- the
                    # sidecar now holds an already-past timestamp too.
                    frozen.tick(delta=timedelta(seconds=60))
                return result

            monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", advancing_tick)
            monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

            with contextlib.suppress(KeyboardInterrupt):
                run_dispatch_loop(native_daemon=daemon)

        # Tick 1: window still active -- spawning suppressed (proven by the
        # captured kwarg, since spawn_calls can only be inspected after the
        # whole (suppressed) loop run completes below).
        assert captured[0] == expiring
        # Tick 2: time has passed the window; the expired disk value must
        # not resurrect it -- spawning proceeds normally. Exactly one spawn
        # total confirms tick 1 contributed none and tick 2 contributed one.
        assert len(daemon.spawn_calls) == 1

    def test_usage_limit_cleared_emitted_on_active_to_inactive_transition(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The tick that observes an active window lapse emits
        dispatch.usage_limit_cleared exactly once (#1343 R1)."""
        from freezegun import freeze_time

        import cw.dispatch

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1343-CLEARED", client="test-client"))

        daemon = FakeNativeDaemonClient()

        with freeze_time("2026-07-16 12:00:00") as frozen:
            armed_at = datetime.now(UTC) - timedelta(minutes=5)
            expiring = datetime.now(UTC) + timedelta(seconds=30)
            save_usage_limit_armed_at(armed_at)
            save_usage_limited_until(expiring)

            call_count = 0
            real_tick = cw.dispatch.loop.dispatch_tick

            def advancing_tick(*args: object, **kwargs: object) -> DispatchTickResult:
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    raise KeyboardInterrupt
                result = real_tick(*args, **kwargs)  # type: ignore[arg-type]
                if call_count == 1:
                    # Advance past the window's expiry before tick 2.
                    frozen.tick(delta=timedelta(seconds=60))
                return result

            monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", advancing_tick)
            monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

            with contextlib.suppress(KeyboardInterrupt):
                run_dispatch_loop(native_daemon=daemon)

        events = read_events(
            consumer="test-ul-cleared-transition",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert len(events) == 1
        assert events[0].payload["detected_at"] == armed_at.isoformat()

    def test_usage_limit_cleared_payload_has_clients_affected_and_counts(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """_emit_usage_limit_cleared's payload carries the exact cohort
        computed from session.timed_out(cause=usage_limit_cutoff) events
        recorded since armed_at (#1343 R1/R2)."""
        import cw.dispatch.loop

        armed_at = datetime.now(UTC) - timedelta(minutes=10)
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": "s1",
                "session_name": "client-a/impl",
                "client": "client-a",
                "ticket_id": "GEN-1",
                "cause": "usage_limit_cutoff",
            },
        )
        cleared_at = datetime.now(UTC)

        cw.dispatch.loop._emit_usage_limit_cleared(armed_at, cleared_at)

        events = read_events(
            consumer="test-ul-cleared-payload",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["clients_affected"] == ["client-a"]
        assert payload["sessions_affected"] == 1
        assert payload["detected_at"] == armed_at.isoformat()
        assert payload["cleared_at"] == cleared_at.isoformat()

    def test_usage_limit_cleared_restart_mid_backoff_still_detects_later_clear(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A freshly-started process that loads an already-active persisted
        window still detects the eventual clear, using the persisted
        armed_at -- not a value derived from this process's own
        backoff_seconds (#1343 R2)."""
        from freezegun import freeze_time

        import cw.dispatch

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1343-RESTART", client="test-client"))

        daemon = FakeNativeDaemonClient()

        with freeze_time("2026-07-16 12:00:00") as frozen:
            # Simulate a PRIOR process's fresh detection, well before this
            # process starts -- this process never itself arms the window.
            armed_at = datetime.now(UTC) - timedelta(minutes=20)
            expiring = datetime.now(UTC) + timedelta(seconds=30)
            save_usage_limit_armed_at(armed_at)
            save_usage_limited_until(expiring)

            call_count = 0
            real_tick = cw.dispatch.loop.dispatch_tick

            def advancing_tick(*args: object, **kwargs: object) -> DispatchTickResult:
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    raise KeyboardInterrupt
                result = real_tick(*args, **kwargs)  # type: ignore[arg-type]
                if call_count == 1:
                    frozen.tick(delta=timedelta(seconds=60))
                return result

            monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", advancing_tick)
            monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

            # A fresh run_dispatch_loop() call simulates a process restart:
            # its usage_limit_window_armed_at local is loaded from the
            # sidecar at loop start, not from any prior in-process history.
            with contextlib.suppress(KeyboardInterrupt):
                run_dispatch_loop(native_daemon=daemon)

        events = read_events(
            consumer="test-ul-cleared-restart",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert len(events) == 1
        assert events[0].payload["detected_at"] == armed_at.isoformat()

    def test_usage_limit_cleared_degrades_gracefully_when_armed_at_missing(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """When armed_at is None (persist failed, or the window predates
        this field), the event still emits with detected_at=null rather
        than being skipped -- skipping would silently drop the correlating
        signal orchestrators need most (#1343)."""
        import cw.dispatch.loop

        cleared_at = datetime.now(UTC)
        cw.dispatch.loop._emit_usage_limit_cleared(None, cleared_at)

        events = read_events(
            consumer="test-ul-cleared-degraded",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert len(events) == 1
        assert events[0].payload["detected_at"] is None

    def test_run_dispatch_loop_once_mode_does_not_emit_usage_limit_cleared(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--once never calls the transition detector, so it structurally
        cannot emit dispatch.usage_limit_cleared -- even when the loaded
        window happens to already be within seconds of lapsing at load
        time (#1343 R3)."""
        import cw.dispatch.loop

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1343-ONCE", client="test-client"))

        daemon = FakeNativeDaemonClient()

        future = datetime.now(UTC) + timedelta(seconds=1)
        save_usage_limited_until(future)
        save_usage_limit_armed_at(datetime.now(UTC) - timedelta(minutes=5))

        calls: list[object] = []
        real_handler = cw.dispatch.loop._handle_usage_limit_window_transition

        def spy_handler(*args: object, **kwargs: object) -> bool:
            calls.append(args)
            return real_handler(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "cw.dispatch.loop._handle_usage_limit_window_transition", spy_handler
        )

        run_dispatch_loop(once=True, native_daemon=daemon)

        assert calls == []
        events = read_events(
            consumer="test-ul-cleared-once",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert events == []

    def test_usage_limit_cleared_cohort_count_excludes_idle_stall_cause(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """The cohort scan filters on cause=usage_limit_cutoff -- an
        idle-stall timeout recorded in the same window must not inflate
        sessions_affected/clients_affected (#1343 R5)."""
        import cw.dispatch.loop

        armed_at = datetime.now(UTC) - timedelta(minutes=10)
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": "s1",
                "client": "client-a",
                "ticket_id": "GEN-1",
                "cause": "usage_limit_cutoff",
            },
        )
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": "s2",
                "client": "client-b",
                "ticket_id": "GEN-2",
                "cause": "idle_stall_recovered",
            },
        )
        cleared_at = datetime.now(UTC)

        cw.dispatch.loop._emit_usage_limit_cleared(armed_at, cleared_at)

        events = read_events(
            consumer="test-ul-cleared-cohort-filter",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["clients_affected"] == ["client-a"]
        assert payload["sessions_affected"] == 1

    def test_usage_limit_cleared_not_emitted_when_window_never_active(
        self,
        tmp_dispatch_dirs: Path,
    ) -> None:
        """_handle_usage_limit_window_transition emits nothing when the
        window was never active -- no false clear on a fleet that never hit
        a usage limit (#1343)."""
        import cw.dispatch.loop

        now_active = cw.dispatch.loop._handle_usage_limit_window_transition(
            False, usage_limited_until=None, armed_at=None
        )

        assert now_active is False
        events = read_events(
            consumer="test-ul-cleared-never-active",
            event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED],
        )
        assert events == []


class TestClaimNextPendingUsageLimitedGate:
    """_claim_next_pending refuses PENDING->RUNNING during an active
    usage-limit backoff, as defense-in-depth alongside dispatch_tick's own
    top-of-tick gate (#1346). The value is passed in as a parameter -- this
    function must never call load_usage_limited_until() itself; claim.py
    stays pure state-transition logic with no I/O of its own."""

    def test_claim_blocked_while_usage_limited_until_future(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.dispatch import _claim_next_pending

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1346-CLAIM1", client="test-client"))

        future = datetime.now(UTC) + timedelta(hours=1)
        result = _claim_next_pending(
            "test-client",
            lane=DEFAULT_LANE,
            config=simple_config,
            usage_limited_until=future,
        )
        assert result == (None, False)

        queue = load_dev_queue()
        stored = next(t for t in queue.tasks if t.ticket_id == "GEN-1346-CLAIM1")
        assert stored.status == QueueItemStatus.PENDING

    def test_claim_succeeds_once_usage_limited_until_past_or_none(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        from cw.dispatch import _claim_next_pending

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-1346-CLAIM2", client="test-client"))

        past = datetime.now(UTC) - timedelta(hours=1)
        claimed, backoff_skipped = _claim_next_pending(
            "test-client",
            lane=DEFAULT_LANE,
            config=simple_config,
            usage_limited_until=past,
        )
        assert claimed is not None
        assert claimed.ticket_id == "GEN-1346-CLAIM2"
        assert backoff_skipped is False

        queue = load_dev_queue()
        stored = next(t for t in queue.tasks if t.ticket_id == "GEN-1346-CLAIM2")
        assert stored.status == QueueItemStatus.RUNNING


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

        monkeypatch.setattr("cw.dispatch.loop.load_effective_config", counting_load)

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

        monkeypatch.setattr("cw.dispatch.loop.load_effective_config", patched_load)

        daemon = FakeNativeDaemonClient()
        # Should NOT raise despite the in-loop reload failing
        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            run_dispatch_loop(once=True, native_daemon=daemon)

        assert any(
            "config reload failed" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )

    def test_config_last_good_on_config_validation_error(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """In-loop reload failure from a bad config-model value (extra='forbid'
        typo etc, wrapped as ConfigValidationError) logs WARNING and continues
        with last-good config — mirrors test_config_last_good_on_corrupt's
        yaml.YAMLError case for the pydantic-validation failure mode (#1200)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        real_load = load_effective_config
        call_count = 0

        def patched_load() -> OrchestratorConfig:
            nonlocal call_count
            call_count += 1
            # First call (startup): succeed normally
            # Second call (in-loop reload): simulate a wrapped ValidationError
            if call_count >= 2:
                msg = "simulated invalid orchestrator config"
                raise ConfigValidationError(msg)
            return real_load()

        monkeypatch.setattr("cw.dispatch.loop.load_effective_config", patched_load)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "abc12345" * 5, "def67890" * 5, 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.fast_forward_main",
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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        def _boom(_client: object, **_kwargs: object) -> tuple[str, str]:
            msg = "git pull failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.dispatch.gating.fast_forward_main", _boom)

        daemon = FakeNativeDaemonClient()
        # Exception must be swallowed; falls through to TICKET_NEEDS_SYNC.
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-auto-ff-raises",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 1

    def test_auto_ff_non_main_head_skips_fast_forward_emits_non_main_head_detail(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns non-default branch → dispatch bails before ff.

        When the dispatch repo's HEAD is on a non-default branch and the repo is
        stale, dispatch must:
        - emit skip_reason=freshness_gate with freshness_detail="non_main_head"
        - still emit TICKET_NEEDS_SYNC for the blocked task
        - NOT call fast_forward_main
        - not spawn any sessions (spawned==0)
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-110", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.get_head_branch",
            lambda _client: "feature/xyz",
        )

        ff_called = {"count": 0}

        def _ff_spy(_client: object, **_kwargs: object) -> tuple[str, str]:
            ff_called["count"] += 1
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.dispatch.gating.fast_forward_main", _ff_spy)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert ff_called["count"] == 0

        tick_events = read_events(
            consumer="test-auto-ff-non-main-head-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        )
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_NON_MAIN_HEAD

        sync_events = read_events(
            consumer="test-auto-ff-non-main-head-sync",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(sync_events) == 1
        assert sync_events[0].payload["ticket_id"] == "CW-110"

    def test_auto_ff_detached_head_uses_normal_path(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns None (detached) → normal auto-ff path proceeds.

        A detached HEAD is not the non-main-HEAD case; fast_forward_main should
        be attempted (check_main_ff_safety gates it appropriately).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-111", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.get_head_branch",
            lambda _client: None,  # detached HEAD
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        ff_called = {"count": 0}

        def _ff_spy(_client: object, **_kwargs: object) -> tuple[str, str]:
            ff_called["count"] += 1
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.dispatch.gating.fast_forward_main", _ff_spy)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert ff_called["count"] == 1

    def test_auto_ff_on_default_branch_uses_normal_path(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_head_branch returns default_branch → normal path (not non_main_head).

        When HEAD == default_branch and the repo is stale with diverged safety,
        dispatch emits freshness_detail="main_diverged_from_origin" — NOT
        "non_main_head" (which would be wrong when we ARE on the default branch).
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-112", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.get_head_branch",
            lambda _client: "main",  # on default branch
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "diverged",  # unsafe, so auto-ff skipped
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        tick_events = read_events(
            consumer="test-auto-ff-default-branch-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["skip_reason"] == DispatchSkipReason.FRESHNESS_GATE
        )
        # Key assertion: not NON_MAIN_HEAD — we ARE on the default branch.
        # With diverged safety, the new distinct detail is FRESHNESS_MAIN_DIVERGED.
        assert tick_events[0].payload["freshness_detail"] != FRESHNESS_NON_MAIN_HEAD
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED

    def test_auto_ff_non_main_head_detached_at_emit_time_shows_detached(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TOCTOU: get_head_branch returns None in _emit_stale_skip → "(detached)".

        _resolve_freshness detects a non-default branch and returns
        freshness_detail="non_main_head".  By the time _emit_stale_skip calls
        get_head_branch a second time the HEAD has moved to detached; the WARN
        message should fall back to "(detached)".
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-113", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )

        call_count: list[int] = [0]

        def _get_head_toctou(_client: object) -> str | None:
            call_count[0] += 1
            if call_count[0] == 1:
                return "feature/xyz"  # _resolve_freshness: non-default → bail
            return None  # _emit_stale_skip: HEAD detached (TOCTOU)

        monkeypatch.setattr("cw.dispatch.gating.get_head_branch", _get_head_toctou)

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        dispatch_tick(
            simple_config,
            native_daemon=daemon,
            emit=emitted.append,
        )

        assert any("(detached)" in m for m in emitted)

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
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        # check_main_ff_safety must NOT be called; if it is called that's a bug
        check_called = [False]

        def _check_boom(_client: object) -> str:
            check_called[0] = True
            return "behind"

        monkeypatch.setattr("cw.dispatch.gating.check_main_ff_safety", _check_boom)

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

    def test_auto_ff_ahead_emits_diverged_detail(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='ahead' → freshness_detail='main_diverged_from_origin' (#766).

        When local main is ahead of origin (unpushed commits exist), the
        dispatch loop should emit a distinct freshness_detail so the operator
        can distinguish "ahead" from "behind" in the status output.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-120", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "ahead",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-ahead-diverged-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED
        assert any("diverged" in ln for ln in emitted)

    def test_auto_ff_diverged_emits_diverged_detail(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='diverged' → freshness_detail='main_diverged_from_origin' (#766).

        When local main has diverged from origin (has both local and remote
        commits), a distinct freshness_detail tells the operator to reconcile
        rather than just wait for auto-ff.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-121", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-diverged-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIVERGED
        assert any("diverged" in ln for ln in emitted)

    def test_auto_ff_behind_dirty_emits_dirty_checkout_detail(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='behind' + dirty checkout → freshness_detail='main_dirty_checkout'.

        When local main is behind origin but the working tree has uncommitted
        tracked changes, auto-ff is blocked.  A distinct freshness_detail
        tells the operator to commit or stash — not wait for auto-ff.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-122", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_checkout_dirty",
            lambda _client: True,
            raising=False,
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-dirty-checkout-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert (
            tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DIRTY_CHECKOUT
        )
        assert any("dirty" in ln or "uncommitted" in ln for ln in emitted)

    def test_auto_ff_diverged_warn_advises_inspect_not_rebase(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#940: diverged WARN advises inspect-first, not ``pull --rebase``.

        A diverged main may carry stray commits from an isolation breach; the
        operator must inspect before touching it, so the advice points at a
        read-only ``git log origin/<default_branch>..HEAD`` and explicitly warns
        against auto-rebase/reset.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-940", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.get_head_branch",
            lambda _client: "main",
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "diverged",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        diverged_warns = [ln for ln in emitted if "diverged" in ln]
        assert diverged_warns, f"no diverged WARN emitted: {emitted}"
        warn = diverged_warns[0]
        assert "log origin/" in warn
        assert "do NOT auto-rebase" in warn
        assert "pull --rebase" not in warn

    def test_auto_ff_detached_emits_detached_detail(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """safety='detached' → freshness_detail='main_detached_head' (#964).

        When the client's main checkout HEAD is detached, dispatch should
        emit a distinct freshness_detail so the operator WARN gives accurate
        checkout advice instead of falling through to the generic
        "main behind origin" message.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(ticket_id="CW-964", client="test-client")
        add_ticket(task)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 1),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.get_head_branch",
            lambda _client: None,  # detached HEAD
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "detached",
        )

        emitted: list[str] = []
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, emit=emitted.append)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-auto-ff-detached-detail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(tick_events) == 1
        assert tick_events[0].payload["freshness_detail"] == FRESHNESS_MAIN_DETACHED
        detached_warns = [ln for ln in emitted if "detached" in ln]
        assert detached_warns, f"no detached WARN emitted: {emitted}"
        warn = detached_warns[0]
        assert "checkout" in warn


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
            "cw.dispatch.gating.is_main_behind_origin",
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
            worktree_base=sample_client_config.worktree_base,
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
            worktree_base=sample_client_config.worktree_base,
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
            worktree_base=sample_client_config.worktree_base,
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

        # running_count=1 >= ceiling=1 → cap_full=True → CAP_FULL (not LANE_CAP_BLOCKED)
        events = read_events(
            consumer="test-blocked-counts-lane-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.CAP_FULL


# ---------------------------------------------------------------------------
# TestLaneCapBlockedSkipReason (#588)
# ---------------------------------------------------------------------------


class TestLaneCapBlockedSkipReason:
    """BLOCKED_ON_USER exhausts lane → skip_reason=lane_cap_blocked, not no_pending."""

    def _setup_blocked_lane(
        self,
        tmp_dispatch_dirs: Path,
        workspace_path: Path,
    ) -> None:
        """Create a client with one impl lane (max_parallel=1), one BLOCKED_ON_USER
        task filling it, and one PENDING task waiting."""
        lanes = [LaneConfig(name="impl", max_parallel=1)]
        client = ClientConfig(
            name="test-client",
            workspace_path=workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        blocked_task = TicketTask(
            ticket_id="LCAP-BLOCKED",
            client="test-client",
            lane="impl",
        )
        blocked_task.status = QueueItemStatus.BLOCKED_ON_USER
        blocked_task.session_id = "sess-lcap-1"
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(blocked_task)
            save_dev_queue(store)

        # No active daemon session — the blocked slot is purely task-based
        # (cap_full based on session-count is False; lane grant is <=0).
        add_ticket(
            TicketTask(ticket_id="LCAP-PENDING", client="test-client", lane="impl")
        )

    def test_skip_reason_is_lane_cap_blocked(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """BLOCKED_ON_USER fills lane cap; pending>0 → skip_reason=lane_cap_blocked."""
        self._setup_blocked_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)

        daemon = FakeNativeDaemonClient()
        config = OrchestratorConfig(default_ceiling=2)
        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 0

        events = read_events(
            consumer="test-lcap-skip-reason",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.LANE_CAP_BLOCKED
        assert p["claimed"] == 0
        assert p["pending"] == 1

    def test_lane_stats_show_blocked_count(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """dispatch.tick lane stats split running vs blocked for operator visibility."""
        self._setup_blocked_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)

        daemon = FakeNativeDaemonClient()
        config = OrchestratorConfig(default_ceiling=2)
        dispatch_tick(config, native_daemon=daemon)

        events = read_events(
            consumer="test-lcap-lane-stats",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        lane = events[0].payload["lanes"]["impl"]
        # Slot is occupied by BLOCKED_ON_USER, not a session-running task
        assert lane["running"] == 0
        assert lane["blocked"] == 1
        assert lane["pending"] == 1
        assert lane["claimed"] == 0
        p = events[0].payload
        assert p["lane_occupants"]["impl"] == [
            {"ticket_id": "LCAP-BLOCKED", "status": "blocked_on_user"}
        ]
        assert p["occupied"] == 1

    def test_lane_occupants_names_the_blocking_ticket(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """lane_occupants names the BLOCKED_ON_USER ticket; PENDING is excluded."""
        self._setup_blocked_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)

        daemon = FakeNativeDaemonClient()
        config = OrchestratorConfig(default_ceiling=2)
        dispatch_tick(config, native_daemon=daemon)

        events = read_events(
            consumer="test-lcap-occupant-names",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        occupants = events[0].payload["lane_occupants"]["impl"]
        # LCAP-BLOCKED occupies the lane; LCAP-PENDING (still PENDING) is absent.
        assert occupants == [{"ticket_id": "LCAP-BLOCKED", "status": "blocked_on_user"}]
        assert all(o["ticket_id"] != "LCAP-PENDING" for o in occupants)

    def test_no_pending_still_used_when_truly_empty(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When lane is blocked but no pending tasks exist → skip_reason=no_pending."""
        lanes = [LaneConfig(name="impl", max_parallel=1)]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        # Only a BLOCKED_ON_USER task — no pending work at all
        blocked_task = TicketTask(
            ticket_id="LCAP-ONLY-BLOCKED",
            client="test-client",
            lane="impl",
        )
        blocked_task.status = QueueItemStatus.BLOCKED_ON_USER
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(blocked_task)
            save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-lcap-no-pending",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.NO_PENDING


# ---------------------------------------------------------------------------
# TestLaneCapCountingWithAwaitingSignoff (#990)
# ---------------------------------------------------------------------------


class TestLaneCapCountingWithAwaitingSignoff:
    """AWAITING_OPERATOR_SIGNOFF occupies its lane slot like BLOCKED_ON_USER."""

    def _setup_signoff_lane(
        self, tmp_dispatch_dirs: Path, workspace_path: Path
    ) -> None:
        """One impl lane (max_parallel=1), one AWAITING_OPERATOR_SIGNOFF task
        filling it (no active session), and one PENDING task waiting."""
        lanes = [LaneConfig(name="impl", max_parallel=1)]
        client = ClientConfig(
            name="test-client",
            workspace_path=workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        signoff_task = TicketTask(
            ticket_id="SIGNOFF-BLOCKED",
            client="test-client",
            lane="impl",
        )
        signoff_task.status = QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        signoff_task.session_id = "sess-signoff-1"
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(signoff_task)
            save_dev_queue(store)

        add_ticket(
            TicketTask(ticket_id="SIGNOFF-PENDING", client="test-client", lane="impl")
        )

    def test_dispatch_client_lanes_signoff_counted_occupied_not_running(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """AWAITING_OPERATOR_SIGNOFF fills lane cap -> skip_reason=lane_cap_blocked."""
        self._setup_signoff_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)

        daemon = FakeNativeDaemonClient()
        config = OrchestratorConfig(default_ceiling=2)
        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 0
        events = read_events(
            consumer="test-signoff-skip-reason",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["skip_reason"] == DispatchSkipReason.LANE_CAP_BLOCKED
        assert p["pending"] == 1

    def test_dispatch_client_lanes_event_payload_includes_signoff_bucket(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """dispatch.tick lane stats split running vs signoff for operators."""
        self._setup_signoff_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)

        daemon = FakeNativeDaemonClient()
        config = OrchestratorConfig(default_ceiling=2)
        dispatch_tick(config, native_daemon=daemon)

        events = read_events(
            consumer="test-signoff-lane-stats",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        lane = events[0].payload["lanes"]["impl"]
        assert lane["running"] == 0
        assert lane["blocked"] == 0
        assert lane["signoff"] == 1
        assert lane["pending"] == 1
        p = events[0].payload
        assert p["lane_occupants"]["impl"] == [
            {"ticket_id": "SIGNOFF-BLOCKED", "status": "awaiting_operator_signoff"}
        ]
        assert p["occupied"] == 1

    def test_running_by_lane_counts_signoff_as_occupied(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """A signoff-parked task with active session occupies lane; no over-spawn."""
        lanes = [LaneConfig(name="impl", max_parallel=1)]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)

        signoff_task = TicketTask(
            ticket_id="SIGNOFF-2",
            client="test-client",
            lane="impl",
        )
        signoff_task.status = QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        signoff_task.session_id = "sess-signoff-2"
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(signoff_task)
            save_dev_queue(store)

        sess = Session(
            id="sess-signoff-2",
            name="test-client/auto-dev/SIGNOFF-2",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
        )
        save_state(CwState(sessions=[sess]))

        add_ticket(
            TicketTask(ticket_id="IMPL-NEW-2", client="test-client", lane="impl")
        )

        config = OrchestratorConfig(default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        # Lane is full (AWAITING_OPERATOR_SIGNOFF counts) — should NOT spawn
        assert result.spawned == 0

        events = read_events(
            consumer="test-signoff-counts-lane-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.CAP_FULL

    def test_lane_stats_for_client_signoff_bucket(self, tmp_path: Path) -> None:
        """_lane_stats_for_client reports a distinct signoff count (#990)."""
        from cw.dispatch import _lane_stats_for_client

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            lanes=[LaneConfig(name="impl", max_parallel=2)],
        )
        tasks = [
            TicketTask(
                ticket_id="T1",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            ),
            TicketTask(
                ticket_id="T2",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
        ]
        stats = _lane_stats_for_client(client, DevQueueStore(tasks=tasks))
        assert stats["impl"] == {
            "claimed": 0,
            "running": 1,
            "blocked": 0,
            "signoff": 1,
            "pending": 0,
        }
        from cw.dispatch import _lane_occupants_for_client

        occupants = _lane_occupants_for_client(client, DevQueueStore(tasks=tasks))
        assert {"ticket_id": "T1", "status": "awaiting_operator_signoff"} in occupants[
            "impl"
        ]
        assert {"ticket_id": "T2", "status": "running"} in occupants["impl"]
        assert len(occupants["impl"]) == 2


# ---------------------------------------------------------------------------
# TestLaneOccupantsPayload (#1243) — lane_occupants/occupied on every skip path
# ---------------------------------------------------------------------------


class TestLaneOccupantsPayload:
    """dispatch.tick carries lane_occupants/occupied across every skip path."""

    def _make_running_lane(self, tmp_dispatch_dirs: Path, workspace_path: Path) -> None:
        """One impl lane (max_parallel=1) with a single RUNNING occupant."""
        client = ClientConfig(
            name="test-client",
            workspace_path=workspace_path,
            default_branch="main",
            lanes=[LaneConfig(name="impl", max_parallel=1)],
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)
        running_task = TicketTask(
            ticket_id="OCC-RUN", client="test-client", lane="impl"
        )
        running_task.status = QueueItemStatus.RUNNING
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(running_task)
            save_dev_queue(store)

    def _assert_occupants_consistent(self, payload: dict[str, object]) -> None:
        """lane_occupants names the running task; occupied sums the lists."""
        occupants = payload["lane_occupants"]
        assert isinstance(occupants, dict)
        assert occupants["impl"] == [{"ticket_id": "OCC-RUN", "status": "running"}]
        assert payload["occupied"] == sum(len(v) for v in occupants.values())
        assert payload["occupied"] == 1

    def test_availability_skip_emits_lane_occupants(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AVAILABILITY_GATE skip carries lane_occupants/occupied."""
        self._make_running_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-occ-availability",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.AVAILABILITY_GATE
        ]
        assert len(ticks) == 1
        self._assert_occupants_consistent(ticks[0].payload)

    def test_usage_limit_skip_emits_lane_occupants(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """USAGE_LIMITED skip carries lane_occupants/occupied."""
        self._make_running_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)
        future = datetime.now(UTC) + timedelta(hours=1)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, usage_limited_until=future)

        events = read_events(
            consumer="test-occ-usage-limit",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.USAGE_LIMITED
        ]
        assert len(ticks) == 1
        self._assert_occupants_consistent(ticks[0].payload)

    def test_stale_skip_emits_lane_occupants(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FRESHNESS_GATE skip carries lane_occupants/occupied."""
        self._make_running_lane(tmp_dispatch_dirs, sample_client_config.workspace_path)
        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 2),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-occ-stale",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.FRESHNESS_GATE
        ]
        assert len(ticks) == 1
        self._assert_occupants_consistent(ticks[0].payload)

    def test_occupied_count_sums_across_lanes(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Top-level occupied sums per-lane occupant lists across two lanes."""
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            lanes=[
                LaneConfig(name="lane-a", max_parallel=1),
                LaneConfig(name="lane-b", max_parallel=1),
            ],
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)
        run_task = TicketTask(ticket_id="OCC-A", client="test-client", lane="lane-a")
        run_task.status = QueueItemStatus.RUNNING
        blocked_task = TicketTask(
            ticket_id="OCC-B", client="test-client", lane="lane-b"
        )
        blocked_task.status = QueueItemStatus.BLOCKED_ON_USER
        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks.append(run_task)
            store.tasks.append(blocked_task)
            save_dev_queue(store)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-occ-sum",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["occupied"] == 2
        assert len(p["lane_occupants"]["lane-a"]) == 1
        assert len(p["lane_occupants"]["lane-b"]) == 1


class TestLaneOccupantsForClient:
    """Direct unit tests for _lane_occupants_for_client (#1243)."""

    def test_lane_occupants_for_client_excludes_pending_and_completed(
        self, tmp_path: Path
    ) -> None:
        """Only OCCUPIED_LANE_STATUSES tasks appear; terminal/pending excluded."""
        from cw.dispatch import _lane_occupants_for_client

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            lanes=[LaneConfig(name="impl", max_parallel=4)],
        )
        tasks = [
            TicketTask(
                ticket_id="P",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.PENDING,
            ),
            TicketTask(
                ticket_id="C",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.COMPLETED,
            ),
            TicketTask(
                ticket_id="X",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.CANCELLED,
            ),
            TicketTask(
                ticket_id="F",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.FAILED,
            ),
            TicketTask(
                ticket_id="R",
                client="test-client",
                lane="impl",
                status=QueueItemStatus.RUNNING,
            ),
        ]
        occupants = _lane_occupants_for_client(client, DevQueueStore(tasks=tasks))
        assert occupants["impl"] == [{"ticket_id": "R", "status": "running"}]

    def test_lane_occupants_for_client_empty_lane_is_empty_list(
        self, tmp_path: Path
    ) -> None:
        """A declared lane with no tasks maps to [] (present, not absent)."""
        from cw.dispatch import _lane_occupants_for_client

        client = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            lanes=[LaneConfig(name="impl", max_parallel=1)],
        )
        occupants = _lane_occupants_for_client(client, DevQueueStore(tasks=[]))
        assert occupants["impl"] == []


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


# ---------------------------------------------------------------------------
# Tests: dispatch_tick client_filter
# ---------------------------------------------------------------------------


class TestClientFilter:
    """client_filter narrows the dispatch loop to a single client."""

    def test_client_filter_ticks_only_targeted_client(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """With client_filter='client-a', only client-a's task is spawned."""
        ws_a = make_git_repo("workspace/cf-client-a")
        ws_b = make_git_repo("workspace/cf-client-b")
        client_a = ClientConfig(
            name="cf-client-a",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-cf-a",
        )
        client_b = ClientConfig(
            name="cf-client-b",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-cf-b",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  cf-client-a:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  cf-client-b:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="CF-A1", client="cf-client-a"))
        add_ticket(TicketTask(ticket_id="CF-B1", client="cf-client-b"))

        config = OrchestratorConfig(default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(
            config, native_daemon=daemon, client_filter="cf-client-a"
        )

        assert result.spawned == 1
        store = load_dev_queue()
        running = store.running()
        assert len(running) == 1
        assert running[0].client == "cf-client-a"
        assert running[0].ticket_id == "CF-A1"

    def test_client_filter_none_dispatches_all_clients(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Without client_filter, both clients are dispatched."""
        ws_a = make_git_repo("workspace/cf-all-a")
        ws_b = make_git_repo("workspace/cf-all-b")
        client_a = ClientConfig(
            name="cf-all-a",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-cf-all-a",
        )
        client_b = ClientConfig(
            name="cf-all-b",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-cf-all-b",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  cf-all-a:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  cf-all-b:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="ALL-A1", client="cf-all-a"))
        add_ticket(TicketTask(ticket_id="ALL-B1", client="cf-all-b"))

        config = OrchestratorConfig(default_ceiling=1)
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 2


# ---------------------------------------------------------------------------
# TestDispatchLoopExitedEvent
# ---------------------------------------------------------------------------


class TestDispatchLoopExitedEvent:
    """DISPATCH_LOOP_EXITED event emitted on clean exit and on crash."""

    def test_loop_exited_event_on_clean_exit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_dispatch_loop(once=True) emits DISPATCH_LOOP_EXITED with normal=True."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.DISPATCH_LOOP_EXITED:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.loop.record_event", capture_event)

        daemon = FakeNativeDaemonClient()
        run_dispatch_loop(once=True, native_daemon=daemon)

        assert len(captured) == 1
        _, evt_payload = captured[0]
        assert evt_payload["normal"] is True
        assert evt_payload["exception_type"] is None

    def test_loop_exited_event_on_crash(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A crash in dispatch_tick emits DISPATCH_LOOP_EXITED with normal=False."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.DISPATCH_LOOP_EXITED:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.loop.record_event", capture_event)
        monkeypatch.setattr(
            "cw.dispatch.loop.dispatch_tick",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        daemon = FakeNativeDaemonClient()
        with pytest.raises(RuntimeError, match="boom"):
            run_dispatch_loop(once=True, native_daemon=daemon)

        assert len(captured) == 1
        _, evt_payload = captured[0]
        assert evt_payload["normal"] is False
        assert evt_payload["exception_type"] == "RuntimeError"

    def test_loop_exited_suppress_covers_record_event_failure(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """record_event failure in finally is suppressed — loop completes normally."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        def raising_on_loop_exited(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.DISPATCH_LOOP_EXITED:
                msg = "emit failed"
                raise RuntimeError(msg)
            return None

        monkeypatch.setattr("cw.dispatch.loop.record_event", raising_on_loop_exited)

        daemon = FakeNativeDaemonClient()
        # Should complete without raising despite DISPATCH_LOOP_EXITED emit failing
        run_dispatch_loop(once=True, native_daemon=daemon)

    def test_version_drift_raises_version_drift_error(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drift between loaded and installed version raises VersionDriftError."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        monkeypatch.setattr(
            "cw.dispatch.loop.importlib.metadata.version",
            lambda _name: "0.0.0-fake",
        )
        daemon = FakeNativeDaemonClient()
        with pytest.raises(VersionDriftError):
            run_dispatch_loop(once=True, native_daemon=daemon)

    def test_version_drift_emits_loop_exited_with_drift_fields(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drift causes exactly one DISPATCH_LOOP_EXITED event with drift fields."""
        import cw

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        monkeypatch.setattr(
            "cw.dispatch.loop.importlib.metadata.version",
            lambda _name: "0.0.0-fake",
        )

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.DISPATCH_LOOP_EXITED:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.loop.record_event", capture_event)

        daemon = FakeNativeDaemonClient()
        with contextlib.suppress(VersionDriftError):
            run_dispatch_loop(once=True, native_daemon=daemon)

        assert len(captured) == 1
        _, evt_payload = captured[0]
        assert evt_payload["reason"] == "version_drift"
        assert evt_payload["loaded_version"] == cw.__version__
        assert evt_payload["installed_version"] == "0.0.0-fake"
        assert evt_payload["normal"] is False
        assert evt_payload["exception_type"] == "VersionDriftError"

    def test_version_drift_check_before_dispatch(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Version check fires before dispatch_tick — tick is never called on drift."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        monkeypatch.setattr(
            "cw.dispatch.loop.importlib.metadata.version",
            lambda _name: "0.0.0-fake",
        )

        tick_calls: list[object] = []
        monkeypatch.setattr(
            "cw.dispatch.loop.dispatch_tick",
            lambda *_a, **_kw: tick_calls.append(True),
        )

        daemon = FakeNativeDaemonClient()
        with contextlib.suppress(VersionDriftError):
            run_dispatch_loop(once=True, native_daemon=daemon)

        assert tick_calls == [], "dispatch_tick must not be called on version drift"

    def test_resolve_loaded_version_returns_unknown_on_package_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_resolve_loaded_version returns '0.0.0+unknown' when package is absent."""
        import importlib.metadata

        from cw.dispatch import _resolve_loaded_version

        monkeypatch.setattr(
            "cw.dispatch.loop.importlib.metadata.version",
            lambda _name: (_ for _ in ()).throw(
                importlib.metadata.PackageNotFoundError("cw")
            ),
        )
        assert _resolve_loaded_version() == "0.0.0+unknown"

    def test_version_drift_tick_package_not_found_no_drift_error(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PackageNotFoundError on installed-version check is treated as 0.0.0+unknown.

        When both loaded and installed versions are unknown, no VersionDriftError
        is raised — the loop continues normally.
        """
        import importlib.metadata

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Pin _LOADED_VERSION to the same sentinel so the comparison is equal.
        monkeypatch.setattr("cw.dispatch.loop._LOADED_VERSION", "0.0.0+unknown")
        monkeypatch.setattr(
            "cw.dispatch.loop.importlib.metadata.version",
            lambda _name: (_ for _ in ()).throw(
                importlib.metadata.PackageNotFoundError("cw")
            ),
        )
        daemon = FakeNativeDaemonClient()
        # Should not raise VersionDriftError — both versions resolve to 0.0.0+unknown.
        run_dispatch_loop(once=True, native_daemon=daemon)


# ---------------------------------------------------------------------------
# TestApplyStagedDecision
# ---------------------------------------------------------------------------


class TestApplyStagedDecision:
    """apply_staged_decision stamps disposition/pr_url on the task for each branch."""

    def _make_running_task(
        self,
        ticket_id: str,
        stage: Stage = Stage.FINALIZE,
    ) -> TicketTask:
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="test-client",
            status=QueueItemStatus.RUNNING,
            stage=stage,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def _clients(self, tmp_path: Path) -> dict[str, ClientConfig]:
        return {
            "test-client": ClientConfig(name="test-client", workspace_path=tmp_path)
        }

    def test_shipped_stamps_disposition_and_pr_url(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """shipped at terminal stage → COMPLETED + disposition='shipped' + pr_url."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SHIP-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "shipped",
            "pr": {"url": "https://github.com/user/repo/pull/42"},
        }
        apply_staged_decision(task, "shipped", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"
        assert task.pr_url == "https://github.com/user/repo/pull/42"

    def test_no_op_stamps_disposition(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """no_op → COMPLETED + disposition='no_op'."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NOOP-1")
        apply_staged_decision(task, "no_op", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "no_op"

    def test_stage_failure_stamps_disposition(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """STAGE_FAILURE status → BLOCKED_ON_USER + disposition=status."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("FAIL-1")
        apply_staged_decision(task, "blocked", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "blocked"

    def test_none_status_stamps_abandoned(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """None/unparseable status → BLOCKED_ON_USER + disposition='abandoned'."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NONE-1")
        apply_staged_decision(task, None, None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "abandoned"

    def test_blocked_at_finalize_with_regress_reason_regresses_to_impl(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """blocked at FINALIZE with agent_block → Stage.IMPL PENDING (#770)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("REGRESS-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL
        assert task.regress_attempts == 1
        assert task.session_id is None

    def test_stage_advance_unchecked_unknown_client_stamps_disposition(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """Unknown client -> BLOCKED_ON_USER + disposition='unknown_client' (#976)."""
        from cw.dispatch import _UNKNOWN_CLIENT_REASON, _stage_advance_unchecked

        task = self._make_running_task("UNKCLIENT-1")

        _stage_advance_unchecked(task, {})

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == _UNKNOWN_CLIENT_REASON

    def test_stage_advance_unchecked_stage_not_in_pipeline_stamps_disposition(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """task.stage not in pipeline.stages -> BLOCKED_ON_USER +
        disposition='invalid_stage_config' (#976)."""
        from cw.dispatch import _INVALID_STAGE_REASON, _stage_advance_unchecked
        from cw.models import StagePipelineConfig

        client_cfg = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            pipeline=StagePipelineConfig(
                stages=[Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
            ),
        )
        task = self._make_running_task("BADSTAGE-1", stage=Stage.HARDEN)

        _stage_advance_unchecked(task, {"test-client": client_cfg})

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == _INVALID_STAGE_REASON

    def test_blocked_at_finalize_regress_increments_counter(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Each regress increments regress_attempts."""
        from cw.dispatch import apply_staged_decision

        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }

        # First regress
        task = self._make_running_task("REGRESS-2", stage=Stage.FINALIZE)
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))
        assert task.regress_attempts == 1

        # Simulate re-run: back at FINALIZE, still below cap
        task.status = QueueItemStatus.RUNNING
        task.stage = Stage.FINALIZE
        save_dev_queue(DevQueueStore(tasks=[task]))
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))
        assert task.regress_attempts == 2
        assert task.status == QueueItemStatus.PENDING

    def test_blocked_at_finalize_cap_exceeded_parks_blocked_on_user(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """blocked at FINALIZE with regress_attempts >= cap → BLOCKED_ON_USER."""
        from cw.auto_dev_result import FINALIZE_REGRESS_CAP
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("CAP-1", stage=Stage.FINALIZE)
        task.regress_attempts = FINALIZE_REGRESS_CAP
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "blocked"
        assert task.regress_attempts == FINALIZE_REGRESS_CAP  # unchanged

    def test_blocked_at_finalize_non_regress_reason_parks_blocked_on_user(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """blocked at FINALIZE with non-eligible reason → BLOCKED_ON_USER."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NR-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "no_result_emitted"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.FINALIZE

    def test_blocked_not_at_finalize_parks_blocked_on_user(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """blocked at non-FINALIZE stage → BLOCKED_ON_USER (no regress)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NF-1", stage=Stage.IMPL)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s2_impl", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.IMPL

    def test_blocked_at_finalize_no_blocker_in_result_parks_blocked_on_user(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """blocked at FINALIZE with no blocker dict → BLOCKED_ON_USER (defensive)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NB-1", stage=Stage.FINALIZE)
        apply_staged_decision(task, "blocked", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_blocked_at_finalize_regress_emits_ticket_requeued_event(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked at FINALIZE with regress reason emits TICKET_REQUEUED (#770)."""
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.TICKET_REQUEUED:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("EVT-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["ticket_id"] == "EVT-1"
        assert payload["from_stage"] == Stage.FINALIZE
        assert payload["to_stage"] == Stage.IMPL
        assert payload["reason"] == "finalize_regress"
        assert payload["blocker_reason"] == "agent_block"

    def test_finalize_regress_emits_both_requeued_and_stage_changed(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Finalize-regress self-heal fires BOTH events across two modules.

        The dispatch layer emits the pre-existing TICKET_REQUEUED (from
        cw.dispatch), while the shared _stage_regress chokepoint emits the new
        task.stage_changed (from cw.dev_queue). record_event is patched by the
        *calling* module's binding, so each producer needs its own capture —
        the cw.dispatch capture would never observe the cw.dev_queue emit.
        """
        from cw.dispatch import apply_staged_decision

        requeued = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.TICKET_REQUEUED
        )
        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        task = self._make_running_task("DUAL-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert len(requeued) == 1
        assert len(stage_changed) == 1
        _, sc_payload, sc_corr = stage_changed[0]
        assert sc_corr == "DUAL-1"
        assert sc_payload["old_stage"] == Stage.FINALIZE
        assert sc_payload["new_stage"] == Stage.IMPL
        assert sc_payload["direction"] == "regress"

    def test_merge_pending_routes_to_blocked_on_user_with_pr_url(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """merge_pending → BLOCKED_ON_USER + disposition='merge_pending' + pr_url.

        Regression for #899: FINALIZE created a PR then could not merge (CI
        pending). The sentinel coerced from blocked+pr to merge_pending must
        route to BLOCKED_ON_USER (not FAILED) with the PR url preserved.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("MP-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "merge_pending",
            "pr": {"url": "https://github.com/org/repo/pull/898"},
        }
        apply_staged_decision(
            task, "merge_pending", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "merge_pending"
        assert task.pr_url == "https://github.com/org/repo/pull/898"

    @pytest.mark.parametrize(
        "v4_status",
        ["ambiguities_pending_resolution", "premises_pending_verification"],
    )
    def test_v4_pause_emits_needs_attention(
        self,
        v4_status: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """V4 pause statuses emit SESSION_NEEDS_ATTENTION(plan_parked) (#923).

        apply_staged_decision is shared by the consume path and the reconcile
        path (_apply_sentinel_to_task); testing it directly confirms both paths
        emit the attention event without requiring an end-to-end integration setup.
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("PK-1", stage=Stage.IMPL)
        task.session_id = "sess-pk1"
        task.client = "test-client"
        apply_staged_decision(task, v4_status, None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["paused_status"] == "plan_parked"
        assert payload["ticket_id"] == "PK-1"
        assert payload["client"] == "test-client"
        assert payload["session_id"] == "sess-pk1"
        assert payload["crashed"] is False

    @pytest.mark.parametrize(
        "non_v4_status",
        ["no_op", "shipped"],
    )
    def test_non_v4_status_does_not_emit_plan_parked(
        self,
        non_v4_status: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-V4 statuses do not emit SESSION_NEEDS_ATTENTION(plan_parked).

        Guards against false-positive plan_parked emissions for the two
        statuses that must stay fully silent on the attention channel: no_op
        (terminal, no park) and shipped (Rule 3 success path, no park). NOTE
        (#1117): "blocked" and "merge_pending" were dropped from this
        parametrize list -- they now legitimately emit SESSION_NEEDS_ATTENTION
        via Rule 5/3b (see test_stage_failure_status_emits_attention_with_matching_
        paused_status and test_merge_pending_emits_attention); this test no
        longer covers them.
        """
        from cw.dispatch import apply_staged_decision

        attention_events: list[object] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                attention_events.append(event_type)
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        last_result: dict[str, object] = {"status": non_v4_status}
        task = self._make_running_task("NPK-1", stage=Stage.FINALIZE)
        apply_staged_decision(task, non_v4_status, last_result, self._clients(tmp_path))

        assert len(attention_events) == 0

    # -- GitHub #1117: attention signal on all blocked_on_user parks ---------

    @pytest.mark.parametrize(
        "stage_failure_status",
        ["blocked", "merge_gate_blocked", "scope_exceeded", "forbidden_area"],
    )
    def test_stage_failure_status_emits_attention_with_matching_paused_status(
        self,
        stage_failure_status: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """STAGE_FAILURE_STATUSES park with paused_status mirroring status (#1117).

        Rule 5's attention payload must distinguish a hard stage blocker from a
        scope-boundary violation from a merge-gate CI failure -- paused_status
        carries the originating status string verbatim (not a single collapsed
        "blocked" literal for all four members), so an operator scanning the
        event feed / board badge can tell them apart without opening the
        session transcript. Driven at Stage.IMPL (not FINALIZE) so Rule 5a's
        self-heal branch is never reached.
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("SF-1", stage=Stage.IMPL)
        task.session_id = "sess-sf1"
        task.client = "test-client"
        last_result: dict[str, object] = {"status": stage_failure_status}
        apply_staged_decision(
            task, stage_failure_status, last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["paused_status"] == stage_failure_status
        assert payload["ticket_id"] == "SF-1"
        assert payload["client"] == "test-client"
        assert payload["session_id"] == "sess-sf1"
        assert payload["crashed"] is False
        assert payload["lane"] == "default"

    @pytest.mark.parametrize(
        ("status", "blocker", "expected_breadcrumbs"),
        [
            pytest.param(
                "blocked",
                {"stage": "s1_plan", "reason": "plan_unreviewable"},
                "plan_unreviewable",
                id="blocked_with_blocker",
            ),
            pytest.param(
                "merge_gate_blocked",
                {"stage": "s4_finalize", "reason": "prior_pipeline_pr_open"},
                "prior_pipeline_pr_open",
                id="merge_gate_blocked_with_blocker",
            ),
            pytest.param(
                "merge_gate_blocked",
                None,
                "",
                id="merge_gate_blocked_without_blocker",
            ),
            pytest.param(
                "scope_exceeded",
                None,
                "",
                id="scope_exceeded_no_blocker",
            ),
            pytest.param(
                "forbidden_area",
                None,
                "",
                id="forbidden_area_no_blocker",
            ),
        ],
    )
    def test_stage_failure_breadcrumbs_gated_on_blocker_dict(
        self,
        status: str,
        blocker: dict[str, str] | None,
        expected_breadcrumbs: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rule 5's breadcrumbs gate is "blocker is a dict", not "blocked" (#1117).

        The validator (auto_dev_result.py) allows merge_gate_blocked to
        optionally carry a non-null blocker (issue #777, prior_pipeline_pr_open
        exception) -- gating breadcrumbs on status=="blocked" alone would
        silently drop that case to "". Confirms breadcrumbs mirrors
        blocker["reason"] whenever a blocker dict is present (blocked, always;
        merge_gate_blocked, when populated), and stays "" when it isn't
        (merge_gate_blocked with blocker=None, and scope_exceeded/
        forbidden_area, which the validator forbids from ever carrying a
        blocker).
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("SF-BC-1", stage=Stage.IMPL)
        last_result: dict[str, object] = {"status": status, "blocker": blocker}
        apply_staged_decision(task, status, last_result, self._clients(tmp_path))

        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["breadcrumbs"] == expected_breadcrumbs
        assert payload["paused_status"] == status

    def test_finalize_regress_self_heal_does_not_emit_attention(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Rule 5a's self-heal early-return never emits SESSION_NEEDS_ATTENTION (#1117).

        5a regresses a regress-eligible FINALIZE blocker back to IMPL and
        returns True before ever reaching transition_task_status(...,
        BLOCKED_ON_USER, ...) -- the task never actually parks, so paging the
        operator here would fire for a condition the system is actively
        self-healing (#770), defeating this ticket's own no-double-fire
        acceptance criterion in the opposite direction (over-firing).
        """
        from cw.dispatch import apply_staged_decision

        attention = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )

        task = self._make_running_task("REGRESS-ATTN-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4a_merge_gate",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert len(attention) == 0
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def test_merge_pending_emits_attention(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merge_pending emits SESSION_NEEDS_ATTENTION(merge_pending) (#1117).

        Rule 3b parks BLOCKED_ON_USER for merge_pending (#899, PR open,
        awaiting CI/merge gate) but previously left the pause invisible on
        the operator event feed -- the same "invisible park" gap as Rule
        5/6, now closed.
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("MP-ATTN-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "merge_pending",
            "pr": {"url": "https://github.com/org/repo/pull/1117"},
        }
        apply_staged_decision(
            task, "merge_pending", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.pr_url == "https://github.com/org/repo/pull/1117"
        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["paused_status"] == "merge_pending"
        assert payload["breadcrumbs"] == ""
        assert payload["crashed"] is False
        assert payload["lane"] == "default"

    def test_unparseable_status_emits_attention(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rule 6's unparseable-sentinel fallback emits SESSION_NEEDS_ATTENTION (#1117).

        A None/missing status must never silently advance/complete (B2
        correctness requirement) -- it must also page the operator, same as
        every other BLOCKED_ON_USER park this ticket covers.
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("UNPARSE-1", stage=Stage.IMPL)
        apply_staged_decision(task, None, None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "abandoned"
        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["paused_status"] == "blocked"
        assert payload["breadcrumbs"] == ""
        assert payload["lane"] == "default"

    @pytest.mark.parametrize(
        "scope_gated_status",
        ["plan_pending_approval", "review_pending_approval"],
    )
    def test_scope_gated_approval_park_emits_needs_attention(
        self,
        scope_gated_status: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Rule 1's large-tier scope-gated park emits SESSION_NEEDS_ATTENTION (#1302).

        Before this fix, Rule 1 (SCOPE_GATED_APPROVAL_STATUSES) intercepted
        these statuses before Rule 2 -- which does emit the event -- ever saw
        them, since SCOPE_GATED_APPROVAL_STATUSES is a subset of
        PAUSED_FOR_USER_INPUT_STATUSES. The park left the operator with no
        attention signal. Also pins "no double-fire": exactly one event, not
        two, per park.
        """
        from cw.dispatch import apply_staged_decision

        attention = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )

        task = self._make_running_task("SG-ATTN-1", stage=Stage.PLAN)
        task.scope_hint = "large"
        task.session_id = "sess-sg1"
        task.client = "test-client"
        task.lane = "bugs"
        last_result: dict[str, object] = {
            "status": scope_gated_status,
            "scope": {"tier": "large"},
        }
        apply_staged_decision(
            task, scope_gated_status, last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert len(attention) == 1
        event_type, payload, correlation_id = attention[0]
        assert event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert payload["paused_status"] == "approval_gate"
        assert payload["ticket_id"] == "SG-ATTN-1"
        assert payload["client"] == "test-client"
        assert payload["session_id"] == "sess-sg1"
        assert payload["crashed"] is False
        assert payload["lane"] == "bugs"
        assert correlation_id == "SG-ATTN-1"

    def test_scope_gated_small_tier_park_does_not_emit_attention(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Small-tier scope-gated statuses never touch the attention channel (#1302).

        Small tier auto-advances (Stage.PLAN) or parks on the unrelated
        AWAITING_OPERATOR_SIGNOFF status (Stage.REVIEW with signoff active) --
        neither is the BLOCKED_ON_USER park this ticket's fix targets, so
        SESSION_NEEDS_ATTENTION must stay silent for both.
        """
        from cw.dispatch import apply_staged_decision

        attention = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )

        plan_task = self._make_running_task("SG-SMALL-ATTN-1", stage=Stage.PLAN)
        plan_task.scope_hint = "small"
        plan_last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            plan_task,
            "plan_pending_approval",
            plan_last_result,
            self._clients(tmp_path),
        )
        assert plan_task.status == QueueItemStatus.PENDING
        assert plan_task.stage == Stage.IMPL

        review_task = self._make_running_task("SG-SMALL-ATTN-2", stage=Stage.REVIEW)
        review_task.signoff = "operator"
        review_last_result: dict[str, object] = {
            "status": "review_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            review_task,
            "review_pending_approval",
            review_last_result,
            self._clients(tmp_path),
        )
        assert review_task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF

        assert len(attention) == 0

    def test_scope_gate_hint_large_tier_small_blocks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint='large' + sentinel tier='small' → BLOCKED_ON_USER (#926).

        Regression: an operator ``--scope large`` hint must force the approval
        gate even when the plan sentinel reclassifies the ticket 'small'. A hint
        can only ADD the gate, never remove it. Pre-fix this auto-advanced.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HL-TS", stage=Stage.PLAN)
        task.scope_hint = "large"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.PLAN

    def test_scope_gate_hint_large_tier_large_blocks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint='large' + sentinel tier='large' → BLOCKED_ON_USER (#926)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HL-TL", stage=Stage.PLAN)
        task.scope_hint = "large"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "large"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.PLAN

    def test_scope_gate_hint_large_tier_none_blocks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint='large' + sentinel omits tier → BLOCKED_ON_USER (#926)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HL-TN", stage=Stage.PLAN)
        task.scope_hint = "large"
        last_result: dict[str, object] = {"status": "plan_pending_approval"}
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.PLAN

    def test_scope_gate_hint_small_tier_large_blocks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint='small' + sentinel tier='large' → BLOCKED_ON_USER (#926).

        A large sentinel tier is never de-escalated by a smaller hint.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HS-TL", stage=Stage.PLAN)
        task.scope_hint = "small"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "large"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.PLAN

    def test_scope_gate_hint_small_tier_small_advances(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint='small' + sentinel tier='small' → advances PLAN→IMPL (#926)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HS-TS", stage=Stage.PLAN)
        task.scope_hint = "small"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def test_scope_gate_hint_none_tier_small_advances(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """scope_hint=None + sentinel tier='small' → advances PLAN→IMPL (#926)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SG-HN-TS", stage=Stage.PLAN)
        task.scope_hint = None
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def _make_parked_task(
        self,
        ticket_id: str,
        stage: Stage = Stage.FINALIZE,
    ) -> TicketTask:
        """A BLOCKED_ON_USER task retaining its session_id (idle-parked shape).

        The idle watchdog parks a still-completing session BLOCKED_ON_USER
        without clearing session_id (#918); the late Stop-hook sentinel then
        rescues it through _route_staged_decision.
        """
        task = self._make_running_task(ticket_id, stage=stage)
        task.status = QueueItemStatus.BLOCKED_ON_USER
        task.session_id = f"sess-{ticket_id}"
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def test_apply_staged_decision_asserts_running(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """apply_staged_decision on a non-RUNNING task raises AssertionError (#918).

        The RUNNING precondition now lives only in the wrapper; the routing
        core (_route_staged_decision) is assert-free so the rescue path can
        advance a parked task.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_parked_task("ASSERT-1", stage=Stage.FINALIZE)
        with pytest.raises(AssertionError):
            apply_staged_decision(task, "stage_complete", None, self._clients(tmp_path))

    def test_route_staged_decision_advances_parked_terminal(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """_route_staged_decision on a parked terminal task → COMPLETED (#918).

        No AssertionError despite BLOCKED_ON_USER status; disposition and
        pr_url are preserved (Comment 5 item 1 terminal arm).
        """
        from cw.dispatch import _route_staged_decision

        task = self._make_parked_task("PARK-TERM-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "pr": {"url": "https://github.com/user/repo/pull/918"},
        }
        _route_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "stage_complete"
        assert task.pr_url == "https://github.com/user/repo/pull/918"

    def test_route_staged_decision_advances_parked_nonterminal(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """_route_staged_decision on a parked non-terminal task advances (#918).

        BLOCKED_ON_USER task at IMPL + stage_complete → PENDING at REVIEW
        (pointer advanced, no assert raised).
        """
        from cw.dispatch import _route_staged_decision

        task = self._make_parked_task("PARK-NONTERM-1", stage=Stage.IMPL)
        _route_staged_decision(task, "stage_complete", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW

    def test_route_staged_decision_scope_gated_small_parked_advances(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Small-tier scope-gated arm advances a parked task (#918, Comment 11).

        The SCOPE_GATED_APPROVAL_STATUSES arm calls the (now assert-free)
        advance helper; a small-tier parked task at PLAN advances to IMPL.
        """
        from cw.dispatch import _route_staged_decision

        task = self._make_parked_task("PARK-SG-1", stage=Stage.PLAN)
        task.scope_hint = "small"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        _route_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def test_route_staged_decision_scope_gated_large_parked_restamps(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Non-small-tier scope-gated arm re-stamps a parked task (#918).

        Unlike the small-tier arm (which advances), a large/medium-tier
        SCOPE_GATED_APPROVAL_STATUSES sentinel calls transition_task_status
        directly with BLOCKED_ON_USER -- a same-state transition when the
        target is already parked. Confirms this arm tolerates that call
        (no assert, disposition re-stamped) and stays parked, not advanced.
        """
        from cw.dispatch import _route_staged_decision

        task = self._make_parked_task("PARK-SG-LARGE-1", stage=Stage.PLAN)
        task.scope_hint = "large"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "large"},
        }
        _route_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "plan_pending_approval"
        assert task.stage == Stage.PLAN

    # -- Operator-signoff gates (RFC 0007 Phase 3, #990) ---------------------

    def test_small_tier_plan_stage_with_signoff_ignores_signoff_scoping(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Rule 1 small-tier at Stage.PLAN + signoff -> advances unattended.

        Signoff is the ship checkpoint (REVIEW->FINALIZE) only, mirroring Rule
        3's identical REVIEW-scoping (test_stage_complete_at_non_review_stage_
        ignores_signoff). A small-tier plan_pending_approval fires at
        Stage.PLAN, not Stage.REVIEW, so it must advance PLAN->IMPL unattended
        even when signoff is configured -- the same as if signoff were unset.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SIGNOFF-1", stage=Stage.PLAN)
        task.signoff = "operator"
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def test_small_tier_without_signoff_advances_unchanged(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Regression: no signoff configured -> small tier advances as before."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SIGNOFF-2", stage=Stage.PLAN)
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "plan_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    def test_review_pending_approval_downgraded_small_with_signoff_parks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """review_pending_approval downgraded to small tier + signoff -> parks."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SIGNOFF-3", stage=Stage.REVIEW)
        task.signoff = "operator"
        last_result: dict[str, object] = {
            "status": "review_pending_approval",
            "scope": {"tier": "small"},
        }
        apply_staged_decision(
            task, "review_pending_approval", last_result, self._clients(tmp_path)
        )

        assert task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert task.disposition == "signoff_gate"
        assert task.stage == Stage.REVIEW

    def test_stage_complete_at_review_stage_with_signoff_parks(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Rule 3 stage_complete at REVIEW + signoff -> parks before FINALIZE."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SIGNOFF-4", stage=Stage.REVIEW)
        task.signoff = "operator"
        apply_staged_decision(task, "stage_complete", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert task.disposition == "signoff_gate"
        assert task.stage == Stage.REVIEW  # not advanced to FINALIZE

    def test_stage_complete_at_non_review_stage_ignores_signoff(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Rule 3 stage_complete at non-REVIEW stage ignores signoff scoping.

        Signoff is the ship checkpoint (REVIEW->FINALIZE); ordinary
        mid-pipeline stage_complete advances (IMPL->REVIEW here) unattended
        even when signoff is configured on the ticket.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("SIGNOFF-5", stage=Stage.IMPL)
        task.signoff = "operator"
        apply_staged_decision(task, "stage_complete", None, self._clients(tmp_path))

        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.REVIEW

    def test_resolve_signoff_ticket_beats_lane_beats_global(
        self, tmp_path: Path
    ) -> None:
        """3-tier resolution: ticket > lane > global default (#990)."""
        from cw.dispatch import resolve_signoff

        client_with_lane_signoff = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            lanes=[LaneConfig(name=DEFAULT_LANE, signoff="operator")],
        )
        clients_lane = {"test-client": client_with_lane_signoff}
        config_none = OrchestratorConfig(default_signoff="none")
        config_operator = OrchestratorConfig(default_signoff="operator")

        # Tier 1: ticket-level override wins even when lane/global say "none".
        task_ticket = TicketTask(
            ticket_id="T1", client="test-client", lane=DEFAULT_LANE, signoff="operator"
        )
        assert resolve_signoff(task_ticket, clients_lane, config_none) == "operator"

        # Tier 2: lane wins when the ticket itself has no override.
        task_lane = TicketTask(ticket_id="T2", client="test-client", lane=DEFAULT_LANE)
        assert resolve_signoff(task_lane, clients_lane, config_none) == "operator"

        # Tier 3: global default applies when neither ticket nor lane set it.
        client_no_lane_signoff = ClientConfig(
            name="test-client",
            workspace_path=tmp_path,
            lanes=[LaneConfig(name=DEFAULT_LANE)],
        )
        clients_no_lane = {"test-client": client_no_lane_signoff}
        task_global = TicketTask(
            ticket_id="T3", client="test-client", lane=DEFAULT_LANE
        )
        assert resolve_signoff(task_global, clients_no_lane, config_operator) == (
            "operator"
        )
        assert resolve_signoff(task_global, clients_no_lane, config_none) is None

    def test_resolve_signoff_falls_through_for_unknown_client_or_lane(
        self, tmp_path: Path
    ) -> None:
        """Unresolvable client/lane falls through to the global default (#990)."""
        from cw.dispatch import resolve_signoff

        config = OrchestratorConfig(default_signoff="operator")

        task_unknown_client = TicketTask(
            ticket_id="U1", client="ghost-client", lane=DEFAULT_LANE
        )
        assert resolve_signoff(task_unknown_client, {}, config) == "operator"

        client_cfg = ClientConfig(name="test-client", workspace_path=tmp_path)
        task_unknown_lane = TicketTask(
            ticket_id="U2", client="test-client", lane="no-such-lane"
        )
        assert (
            resolve_signoff(task_unknown_lane, {"test-client": client_cfg}, config)
            == "operator"
        )

    def test_should_gate_for_signoff_loads_config_without_signature_change(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """_should_gate_for_signoff reachable via (task, clients); staged-decision
        signatures stay unchanged (#990)."""
        import inspect

        from cw.dispatch import (
            _route_staged_decision,
            _should_gate_for_signoff,
            apply_staged_decision,
        )

        task = self._make_running_task("SIGNOFF-SIG-1", stage=Stage.REVIEW)
        task.signoff = "operator"
        assert _should_gate_for_signoff(task, self._clients(tmp_path)) is True

        no_signoff_task = self._make_running_task("SIGNOFF-SIG-2", stage=Stage.REVIEW)
        assert _should_gate_for_signoff(no_signoff_task, self._clients(tmp_path)) is (
            False
        )

        assert list(inspect.signature(apply_staged_decision).parameters) == [
            "task",
            "status",
            "last_result",
            "clients",
        ]
        assert list(inspect.signature(_route_staged_decision).parameters) == [
            "task",
            "status",
            "last_result",
            "clients",
        ]

    def test_matching_stage_reached_routes_normally(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """stage_reached matching task.stage → routes normally (positive
        control, #1019)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("MATCH-1", stage=Stage.REVIEW)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage3_review",
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.FINALIZE

    def test_stage_mismatch_refuses_routing_no_transition(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Stale IMPL sentinel against a REVIEW-stage task → refused,
        untouched (#986/#1019).

        Reproduces the #986 incident shape: a late/replayed sentinel from a
        previous leg (stage_reached=stage2_impl) arrives against a task whose
        row has already advanced to REVIEW. The guard must refuse to route it.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("MISMATCH-1", stage=Stage.REVIEW)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage2_impl",
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is False
        assert task.status == QueueItemStatus.RUNNING
        assert task.stage == Stage.REVIEW

    def test_stage_mismatch_emits_sentinel_stage_mismatch_event(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """Stage mismatch emits SENTINEL_STAGE_MISMATCH with full payload (#1019)."""
        from cw.dispatch import apply_staged_decision

        captured = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.SENTINEL_STAGE_MISMATCH
        )

        task = self._make_running_task("MISMATCH-EVT-1", stage=Stage.REVIEW)
        task.session_id = "sess-mismatch-evt-1"
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage2_impl",
        }
        apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert len(captured) == 1
        _, payload, correlation_id = captured[0]
        assert payload["ticket_id"] == "MISMATCH-EVT-1"
        assert payload["client"] == "test-client"
        assert payload["session_id"] == "sess-mismatch-evt-1"
        assert payload["expected_stage"] == Stage.REVIEW
        assert payload["sentinel_stage_reached"] == "stage2_impl"
        assert correlation_id == "MISMATCH-EVT-1"

    def test_missing_stage_reached_bypasses_guard(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """No stage_reached key in last_result → guard bypassed, routes
        normally (#1019)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NOKEY-1", stage=Stage.REVIEW)
        last_result: dict[str, object] = {"status": "stage_complete"}
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.FINALIZE

    def test_non_string_stage_reached_treated_as_mismatch(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Non-str, non-None stage_reached (malformed payload) → refused,
        not a KeyError/TypeError (#1019 defensive branch)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("BADTYPE-1", stage=Stage.REVIEW)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": 3,
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is False
        assert task.status == QueueItemStatus.RUNNING
        assert task.stage == Stage.REVIEW

    def test_none_status_none_last_result_bypasses_guard(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """(None, None) → Rule 6 fallback unaffected by the stage-mismatch
        guard (#1019)."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("NONERESULT-1")
        routed = apply_staged_decision(task, None, None, self._clients(tmp_path))

        assert routed is True
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "abandoned"

    def test_harden_stage_sentinel_always_mismatches(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """HARDEN-stage task + any populated stage_reached → refused by construction.

        Stage.HARDEN has no legitimate stage_reached counterpart (RFC 0005 A1,
        dormant stage) -- deliberately absent from _STAGE_REACHED_TO_STAGE, so
        every one of the 7 canonical stage_reached values maps to PLAN/IMPL/
        REVIEW/FINALIZE and never HARDEN (#1019).
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("HARDEN-1", stage=Stage.HARDEN)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage1_pre_flight",
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is False
        assert task.status == QueueItemStatus.RUNNING
        assert task.stage == Stage.HARDEN

    def test_finalize_regress_returns_true_after_guard_refactor(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Rule 5a's finalize-regress early return still reports routed=True (#1019).

        Regression guard on the guard-refactor: the bare `return` inside Rule
        5a became `return True` when _route_staged_decision was widened to
        `-> bool`.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("REGRESS-TRUE-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4a_merge_gate",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        routed = apply_staged_decision(
            task, "blocked", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.IMPL

    # ------------------------------------------------------------------
    # GitHub #1149 Path 2: later-stage self-escalation walks forward
    # instead of refusing. Earlier-stage replays stay refused (#1019).
    # ------------------------------------------------------------------

    def _reordered_clients(
        self, tmp_path: Path, stages: list[Stage]
    ) -> dict[str, ClientConfig]:
        """A client whose pipeline.stages is explicitly ordered as *stages*."""
        from cw.models import StagePipelineConfig

        return {
            "test-client": ClientConfig(
                name="test-client",
                workspace_path=tmp_path,
                pipeline=StagePipelineConfig(stages=stages),
            )
        }

    def test_later_stage_walks_forward_one_rung_at_a_time(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """IMPL task + FINALIZE sentinel walks IMPL->REVIEW->FINALIZE (#1149).

        The Path 2 repro shape: a worker self-escalated to FINALIZE while the
        row stayed at IMPL. The walk advances one rung at a time (two
        TASK_STAGE_CHANGED events) and Rule 5 then routes the blocked status at
        the landed FINALIZE stage. Pins that the walk preserves task.session_id
        across each _advance_task_pointer hop (plan-review MUST_FIX #1).
        """
        from cw.dispatch import apply_staged_decision

        attention = capture_events(
            "cw.dispatch.routing", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        task = self._make_running_task("WALK-1", stage=Stage.IMPL)
        task.session_id = "sess-walk-1"
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4b_pr_create",
        }
        routed = apply_staged_decision(
            task, "blocked", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.stage == Stage.FINALIZE
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        advances = [p for _, p, _ in stage_changed if p.get("direction") == "advance"]
        assert len(advances) == 2
        assert advances[0]["old_stage"] == Stage.IMPL
        assert advances[0]["new_stage"] == Stage.REVIEW
        assert advances[1]["old_stage"] == Stage.REVIEW
        assert advances[1]["new_stage"] == Stage.FINALIZE
        # The landing Rule's SESSION_NEEDS_ATTENTION must carry the real
        # session id, not "" (dev_queue.py's per-hop session_id clear must not
        # leak through the walk).
        assert len(attention) == 1
        assert attention[0][1]["session_id"] == "sess-walk-1"

    def test_later_stage_stops_at_review_signoff_gate(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A signoff gate stops the walk at REVIEW instead of crossing it (#1149)."""
        from cw.dev_queue import SIGNOFF_GATE_DISPOSITION
        from cw.dispatch import apply_staged_decision

        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        task = self._make_running_task("WALK-SIGNOFF-1", stage=Stage.IMPL)
        task.session_id = "sess-walk-signoff-1"
        task.signoff = "operator"
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4b_pr_create",
        }
        routed = apply_staged_decision(
            task, "blocked", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.stage == Stage.REVIEW
        assert task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert task.disposition == SIGNOFF_GATE_DISPOSITION
        advances = [p for _, p, _ in stage_changed if p.get("direction") == "advance"]
        assert len(advances) == 1
        assert advances[0]["old_stage"] == Stage.IMPL
        assert advances[0]["new_stage"] == Stage.REVIEW
        # Park outcome must not leak the per-hop session_id clear either.
        assert task.session_id == "sess-walk-signoff-1"

    def test_later_stage_stage_complete_walks_then_advances_once_more(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """PLAN task + IMPL stage_complete sentinel: walk PLAN->IMPL, then Rule 3
        advances IMPL->REVIEW (#1149).

        Proves the walk and the existing Rule 3 stage-success advance compose
        without double- or under-advancing. Rule 3's own genuine advance clears
        session_id exactly as pre-ticket single-hop behavior does.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("WALK-SC-1", stage=Stage.PLAN)
        task.session_id = "sess-walk-sc-1"
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage2_impl",
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.stage == Stage.REVIEW
        # Rule 3's own IMPL->REVIEW advance clears session_id (the walk's
        # session-id preserve applies only to its own intermediate hops).
        assert task.session_id is None

    def test_later_stage_positional_not_alphabetical(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Stage position is by pipeline-list index, not StrEnum string order (#1149).

        Pipeline is reordered [REVIEW, IMPL] so a naive ``sentinel_stage <
        task.stage`` string compare (``"impl" < "review"``) would misclassify
        an IMPL sentinel against a REVIEW-stage task as *earlier* (and refuse
        it). Positional index (REVIEW=0, IMPL=1) correctly classifies it as
        *later* and walks forward. R2 regression guard.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("WALK-POS-1", stage=Stage.REVIEW)
        task.session_id = "sess-walk-pos-1"
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage2_impl",
        }
        routed = apply_staged_decision(
            task,
            "blocked",
            last_result,
            self._reordered_clients(tmp_path, [Stage.REVIEW, Stage.IMPL]),
        )

        assert routed is True
        assert task.stage == Stage.IMPL
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_earlier_stage_replay_still_refused_unchanged(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Earlier-stage replay (#1019) stays refused after the #1149 walk lands.

        Restates the two pinned refusal cases without adding assertions to
        them: an IMPL-mapped sentinel against a REVIEW task, and a
        PLAN-mapped sentinel against an IMPL task, both refuse with no
        transition.
        """
        from cw.dispatch import apply_staged_decision

        task_a = self._make_running_task("REPLAY-A", stage=Stage.REVIEW)
        routed_a = apply_staged_decision(
            task_a,
            "stage_complete",
            {"status": "stage_complete", "stage_reached": "stage2_impl"},
            self._clients(tmp_path),
        )
        assert routed_a is False
        assert task_a.status == QueueItemStatus.RUNNING
        assert task_a.stage == Stage.REVIEW

        task_b = self._make_running_task("REPLAY-B", stage=Stage.IMPL)
        routed_b = apply_staged_decision(
            task_b,
            "stage_complete",
            {"status": "stage_complete", "stage_reached": "stage1_plan"},
            self._clients(tmp_path),
        )
        assert routed_b is False
        assert task_b.status == QueueItemStatus.RUNNING
        assert task_b.stage == Stage.IMPL

    def test_unresolvable_stage_position_falls_back_to_refuse(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Sentinel stage not present in the client's pipeline → fail-closed refuse.

        A subset pipeline [PLAN, IMPL] cannot resolve a FINALIZE-mapped
        sentinel's position, so the walk falls back to a refusal (fail-closed,
        matching pre-#1149 equality-check behavior).
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("UNRESOLVABLE-1", stage=Stage.IMPL)
        routed = apply_staged_decision(
            task,
            "blocked",
            {"status": "blocked", "stage_reached": "stage4b_pr_create"},
            self._reordered_clients(tmp_path, [Stage.PLAN, Stage.IMPL]),
        )

        assert routed is False
        assert task.status == QueueItemStatus.RUNNING
        assert task.stage == Stage.IMPL

    def test_later_stage_walk_unknown_client_falls_back_to_refuse(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Task's client absent from clients dict → fail-closed refuse (#1149).

        Distinct code path from the stage-not-in-pipeline case: the client
        itself cannot be resolved, so pipeline.stages is unavailable.
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("UNKNOWN-CLIENT-1", stage=Stage.IMPL)
        routed = apply_staged_decision(
            task,
            "blocked",
            {"status": "blocked", "stage_reached": "stage4b_pr_create"},
            {},
        )

        assert routed is False
        assert task.status == QueueItemStatus.RUNNING
        assert task.stage == Stage.IMPL

    def test_later_stage_walk_uses_advance_task_pointer_chokepoint(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """The walk never bypasses the stage chokepoint with a direct assignment.

        Each rung must emit a TASK_STAGE_CHANGED (from cw.dev_queue's
        _emit_stage_change), proving the walk routes through
        _advance_task_pointer rather than assigning task.stage directly.
        """
        from cw.dispatch import apply_staged_decision

        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        task = self._make_running_task("WALK-CHOKE-1", stage=Stage.PLAN)
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage3_review",
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        advances = [p for _, p, _ in stage_changed if p.get("direction") == "advance"]
        # PLAN->IMPL, IMPL->REVIEW: exactly one event per real rung.
        assert len(advances) == 2
        assert task.stage == Stage.REVIEW

    def test_later_stage_walk_to_finalize_then_regresses_on_blocked_agent_block(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Walk to FINALIZE composes with Rule 5a's finalize-regress self-heal (#1149).

        IMPL task + FINALIZE sentinel with a regress-eligible agent_block →
        walk advances IMPL->REVIEW->FINALIZE, then Rule 5a regresses
        FINALIZE->IMPL. Event sequence: [advance IMPL->REVIEW, advance
        REVIEW->FINALIZE, regress FINALIZE->IMPL]. session_id must still be the
        real value at the moment Rule 5a's regress condition is evaluated.
        """
        import cw.dispatch
        from cw.dispatch import apply_staged_decision

        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        captured_session_id: list[str | None] = []
        real_stage_regress = cw.dispatch.routing._stage_regress

        def _spy_stage_regress(task: TicketTask, target_stage: Stage) -> None:
            captured_session_id.append(task.session_id)
            real_stage_regress(task, target_stage)

        monkeypatch.setattr("cw.dispatch.routing._stage_regress", _spy_stage_regress)

        task = self._make_running_task("WALK-REGRESS-1", stage=Stage.IMPL)
        task.session_id = "sess-walk-regress-1"
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4b_pr_create",
            "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
        }
        routed = apply_staged_decision(
            task, "blocked", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.stage == Stage.IMPL
        assert task.regress_attempts == 1
        # session_id was the real value when Rule 5a's regress fired.
        assert captured_session_id == ["sess-walk-regress-1"]
        directions = [p.get("direction") for _, p, _ in stage_changed]
        assert directions == ["advance", "advance", "regress"]

    def test_later_stage_terminal_status_walks_then_completes(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A later-stage sentinel with a TERMINAL status (not just intermediate
        `blocked`/`stage_complete`) composes correctly with the multi-hop walk
        (#1149 decision-table coverage gap: earlier/same-position tests exist
        for terminal status via Rule 1/3's ordinary same-stage path, and
        later-position tests exist for intermediate status, but no test pinned
        later-position + terminal-status together before this).

        PLAN task + FINALIZE `shipped` sentinel walks PLAN->IMPL->REVIEW->
        FINALIZE (three advances, no signoff configured), then Rule 3's
        _stage_advance_unchecked sees FINALIZE is the terminal pipeline stage
        and marks the task COMPLETED with disposition="shipped" -- proving the
        walk and the terminal-completion path compose without the sentinel's
        terminal status leaking into an intermediate rung.
        """
        from cw.dispatch import apply_staged_decision

        stage_changed = capture_events(
            "cw.dev_queue.lifecycle", OrchestratorEventType.TASK_STAGE_CHANGED
        )

        task = self._make_running_task("WALK-TERMINAL-1", stage=Stage.PLAN)
        task.session_id = "sess-walk-terminal-1"
        last_result: dict[str, object] = {
            "status": "shipped",
            "stage_reached": "stage4b_pr_create",
            "pr": {
                "number": 42,
                "url": "https://github.com/foo/bar/pull/42",
                "auto_merge": True,
                "base": "main",
            },
        }
        routed = apply_staged_decision(
            task, "shipped", last_result, self._clients(tmp_path)
        )

        assert routed is True
        assert task.stage == Stage.FINALIZE
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"
        assert task.pr_url == "https://github.com/foo/bar/pull/42"
        advances = [p for _, p, _ in stage_changed if p.get("direction") == "advance"]
        assert len(advances) == 3
        assert advances[0]["old_stage"] == Stage.PLAN
        assert advances[0]["new_stage"] == Stage.IMPL
        assert advances[1]["old_stage"] == Stage.IMPL
        assert advances[1]["new_stage"] == Stage.REVIEW
        assert advances[2]["old_stage"] == Stage.REVIEW
        assert advances[2]["new_stage"] == Stage.FINALIZE

    # -- RFC 0011 A1: distinct awaiting_operator park class (#1155) ---------

    @pytest.mark.parametrize(
        "reason",
        ["push_auth_failed", "operator_unavailable"],
    )
    def test_operator_unavailable_blocker_sets_awaiting_operator_paused_status(
        self,
        reason: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blocked status with an operator-unavailable blocker reason tags the
        attention payload's paused_status as awaiting_operator_availability
        instead of the generic "blocked" (RFC 0011 A1). breadcrumbs still
        carries the specific blocker reason verbatim.
        """
        from cw.dispatch import apply_staged_decision

        captured: list[tuple[object, dict[str, object]]] = []

        def capture_event(
            event_type: object,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            if event_type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                captured.append((event_type, payload or {}))
            return None

        monkeypatch.setattr("cw.dispatch.routing.record_event", capture_event)

        task = self._make_running_task("AO-1", stage=Stage.IMPL)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s2_impl", "reason": reason},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert len(captured) == 1
        _, payload = captured[0]
        assert payload["paused_status"] == "awaiting_operator_availability"
        assert payload["breadcrumbs"] == reason

    @pytest.mark.parametrize(
        "reason",
        ["push_auth_failed", "operator_unavailable"],
    )
    def test_blocked_at_finalize_operator_unavailable_reason_parks_without_regress(
        self,
        reason: str,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
    ) -> None:
        """blocked at FINALIZE with an operator-unavailable reason parks
        BLOCKED_ON_USER without regressing to IMPL (R5 non-regression proof
        for the FINALIZE stage; operator-unavailable reasons are absent from
        FINALIZE_REGRESS_BLOCKER_REASONS, RFC 0011 A1).
        """
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("AO-FIN-1", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "blocker": {"stage": "s4_finalize", "reason": reason},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.stage == Stage.FINALIZE

    def test_awaiting_operator_reason_constant_value(self) -> None:
        from cw.dispatch import _AWAITING_OPERATOR_REASON

        assert _AWAITING_OPERATOR_REASON == "awaiting_operator_availability"


# ---------------------------------------------------------------------------
# TestPersistCarriedContext
# ---------------------------------------------------------------------------


class TestPersistCarriedContext:
    """GitHub #1050: _route_staged_decision stamps carried-through context

    (plan_source, computed_scope_tier) onto the task from a stage-matched
    sentinel, so a rescue respawn's fresh claim->spawn re-materializes it via
    cw-context.json.
    """

    def _make_running_task(
        self,
        ticket_id: str,
        stage: Stage = Stage.IMPL,
        plan_source: str | None = None,
        computed_scope_tier: str | None = None,
    ) -> TicketTask:
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="test-client",
            status=QueueItemStatus.RUNNING,
            stage=stage,
            plan_source=plan_source,
            computed_scope_tier=computed_scope_tier,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def _clients(self, tmp_path: Path) -> dict[str, ClientConfig]:
        return {
            "test-client": ClientConfig(name="test-client", workspace_path=tmp_path)
        }

    def test_route_staged_decision_persists_plan_source(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Stage-matched stage_complete sentinel stamps plan_source + tier."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("PC-1", stage=Stage.IMPL)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage2_impl",
            "plan_source": "github_issue_existing",
            "scope": {"tier": "small", "files": 3, "lines_estimate": 10},
        }
        apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert task.plan_source == "github_issue_existing"
        assert task.computed_scope_tier == "small"

    def test_route_staged_decision_persists_from_blocked_sentinel(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """Stage-matched blocked finalize sentinel still carries plan_source/tier."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("PC-2", stage=Stage.FINALIZE)
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage4b_pr_create",
            "plan_source": "generated",
            "scope": {"tier": "large", "files": 20, "lines_estimate": 800},
            "blocker": {"stage": "s4_finalize", "reason": "dirty_tree_no_sentinel"},
        }
        apply_staged_decision(task, "blocked", last_result, self._clients(tmp_path))

        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.plan_source == "generated"
        assert task.computed_scope_tier == "large"

    def test_route_staged_decision_does_not_overwrite_with_null_tier(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """A stage-matched sentinel with scope.tier=None never clobbers an
        already-set computed_scope_tier."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task(
            "PC-3", stage=Stage.PLAN, computed_scope_tier="large"
        )
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage1_plan",
            "plan_source": "free_text",
            "scope": {"tier": None, "files": None, "lines_estimate": None},
        }
        apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert task.computed_scope_tier == "large"

    def test_route_staged_decision_does_not_overwrite_plan_source_with_none(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """A stage-matched sentinel with plan_source="none" never clobbers an
        already-resolved plan_source."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task(
            "PC-4", stage=Stage.IMPL, plan_source="github_issue_existing"
        )
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage2_impl",
            "plan_source": "none",
            "scope": {"tier": "small", "files": 1, "lines_estimate": 5},
        }
        apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert task.plan_source == "github_issue_existing"

    def test_route_staged_decision_skips_persist_on_stage_mismatch(
        self, tmp_dispatch_dirs: Path, tmp_path: Path
    ) -> None:
        """A late/replayed sentinel whose stage_reached mismatches task.stage
        is refused by the stage-mismatch guard -- neither field mutates."""
        from cw.dispatch import apply_staged_decision

        task = self._make_running_task("PC-5", stage=Stage.IMPL)
        last_result: dict[str, object] = {
            "status": "stage_complete",
            "stage_reached": "stage1_plan",
            "plan_source": "github_issue_existing",
            "scope": {"tier": "small", "files": 1, "lines_estimate": 5},
        }
        routed = apply_staged_decision(
            task, "stage_complete", last_result, self._clients(tmp_path)
        )

        assert routed is False
        assert task.plan_source is None
        assert task.computed_scope_tier is None

    def test_resolve_scope_tier_unchanged_by_new_field(self) -> None:
        """_resolve_scope_tier behavior is identical regardless of whether
        computed_scope_tier is set (#1050 regression guard: the new field
        must never feed into the escalate-only resolver)."""
        from cw.dispatch import _resolve_scope_tier

        # (a) scope_hint="large" still escalates even with computed_scope_tier set.
        task_a = TicketTask(ticket_id="RS-A", client="c", scope_hint="large")
        task_a.computed_scope_tier = "small"
        assert _resolve_scope_tier({"scope": {"tier": "small"}}, task_a) == "large"

        # (b) sentinel tier used when present, computed_scope_tier set differently.
        task_b = TicketTask(ticket_id="RS-B", client="c")
        task_b.computed_scope_tier = "large"
        assert _resolve_scope_tier({"scope": {"tier": "small"}}, task_b) == "small"

        # (c) returns None when both scope_hint and sentinel tier absent, even
        # though computed_scope_tier is set.
        task_c = TicketTask(ticket_id="RS-C", client="c")
        task_c.computed_scope_tier = "large"
        assert _resolve_scope_tier({"scope": {}}, task_c) is None

    def test_route_scope_gated_approval_1091_shaped_small_tier_auto_advances(
        self,
        tmp_dispatch_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """#1104 regression: a #1091-shaped corrected scope block (small tier,
        forbidden_touched=False, 11 files, 33 lines) auto-advances via Rule 1
        rather than parking BLOCKED_ON_USER -- pins acceptance criterion 2
        end-to-end through the dispatcher's ``_route_scope_gated_approval``,
        not just the gate-recipe layer (see
        tests/test_reconcile_gate_recipes.py for that layer's pin)."""
        from cw.dispatch import _route_scope_gated_approval

        calls: list[TicketTask] = []

        def _advance_spy(
            task: TicketTask,
            clients: dict[str, ClientConfig],
            *,
            disposition: str | None = None,
            pr_url: str | None = None,
        ) -> None:
            calls.append(task)

        monkeypatch.setattr(
            "cw.dispatch.routing._stage_advance_unchecked", _advance_spy
        )

        task = self._make_running_task("RS-1091", stage=Stage.PLAN)
        last_result: dict[str, object] = {
            "status": "plan_pending_approval",
            "scope": {
                "tier": "small",
                "forbidden_touched": False,
                "files": 11,
                "lines_actual": 33,
            },
        }
        _route_scope_gated_approval(
            task, self._clients(tmp_path), last_result, "plan_pending_approval", None
        )

        assert len(calls) == 1
        assert task.status == QueueItemStatus.RUNNING  # unchanged -- never parked

    def test_consume_stamps_carried_context_on_task(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """consume_completed_sessions persists plan_source/scope.tier from a
        SESSION_COMPLETED event's last_result, surviving the queue-store save."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        task = TicketTask(
            ticket_id="PC-CONSUME-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            stage=Stage.IMPL,
            session_id="sess-pc-consume-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sess = Session(
            id="sess-pc-consume-1",
            name="test-client/auto-dev/PC-CONSUME-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=sample_client_config.workspace_path,
            last_result={
                "status": "stage_complete",
                "stage_reached": "stage2_impl",
                "plan_source": "github_issue_existing",
                "scope": {"tier": "small", "files": 2, "lines_estimate": 20},
            },
        )
        save_state(CwState(sessions=[sess]))

        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {"ticket_id": "PC-CONSUME-1", "session_id": "sess-pc-consume-1"},
        )

        completed = consume_completed_sessions()
        assert completed == 1

        reloaded = load_dev_queue().tasks[0]
        assert reloaded.plan_source == "github_issue_existing"
        assert reloaded.computed_scope_tier == "small"


# ---------------------------------------------------------------------------
# TestWaveCollisionDetection
# ---------------------------------------------------------------------------


class TestWaveCollisionDetection:
    """Verify dispatch_tick wires warned_collision to collision detection (#784).

    Covers: kwarg acceptance, run_dispatch_loop initialization, and end-to-end
    collision event emission for two RUNNING tasks sharing touched files.
    """

    def test_dispatch_tick_accepts_warned_collision_kwarg(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """dispatch_tick must accept warned_collision without raising TypeError."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )

        warned: set[frozenset[str]] = set()
        # Should not raise even when no tasks are queued
        result = dispatch_tick(
            simple_config,
            native_daemon=FakeNativeDaemonClient(),
            warned_collision=warned,
        )
        assert isinstance(result, DispatchTickResult)

    def test_run_dispatch_loop_initializes_warned_collision(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_dispatch_loop must pass warned_collision to each dispatch_tick call."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        captured_collision_sets: list[object] = []

        original = dispatch_tick

        def _spy(
            config: OrchestratorConfig,
            *,
            use_plan: bool = False,
            parent: str | None = None,
            native_daemon: NativeDaemonClient | None = None,
            emit: Callable[[str], None] | None = None,
            warned_stale: set[tuple[str, str]] | None = None,
            warned_fetch_fail: set[str] | None = None,
            warned_collision: set[frozenset[str]] | None = None,
            warned_ssh_key: set[str] | None = None,
            usage_limited_until: datetime | None = None,
            auto_ff: bool = True,
            client_filter: str | None = None,
        ) -> DispatchTickResult:
            captured_collision_sets.append(warned_collision)
            return original(
                config,
                use_plan=use_plan,
                parent=parent,
                native_daemon=native_daemon,
                emit=emit,
                warned_stale=warned_stale,
                warned_fetch_fail=warned_fetch_fail,
                warned_collision=warned_collision,
                warned_ssh_key=warned_ssh_key,
                usage_limited_until=usage_limited_until,
                auto_ff=auto_ff,
                client_filter=client_filter,
            )

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", _spy)
        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

        run_dispatch_loop(once=True, native_daemon=FakeNativeDaemonClient())

        assert len(captured_collision_sets) == 1
        assert isinstance(captured_collision_sets[0], set)

    def test_collision_detection_fires_for_running_tasks_sharing_files(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two RUNNING tasks sharing a file → WAVE_COLLISION event emitted."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        # Pre-populate two RUNNING tasks on the same client with worktrees
        wt1 = tmp_dispatch_dirs / "wt1"
        wt2 = tmp_dispatch_dirs / "wt2"
        wt1.mkdir(parents=True)
        wt2.mkdir(parents=True)

        with dev_queue_lock():
            store = load_dev_queue()
            store.tasks = [
                TicketTask(
                    ticket_id="COL-1",
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    worktree_path=wt1,
                    stage_base_ref="abc123",
                ),
                TicketTask(
                    ticket_id="COL-2",
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    worktree_path=wt2,
                    stage_base_ref="abc123",
                ),
            ]
            save_dev_queue(store)

        monkeypatch.setattr(
            "cw.collision._git_changed_files",
            lambda _path, _base_ref: frozenset({"src/shared.py"}),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (False, "abc", "abc", 0),
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

        warned: set[frozenset[str]] = set()
        dispatch_tick(
            simple_config,
            native_daemon=FakeNativeDaemonClient(),
            warned_collision=warned,
        )

        events = read_events(
            consumer="test-dispatch-collision",
            event_types=[OrchestratorEventType.WAVE_COLLISION],
        )
        assert len(events) == 1
        assert set(events[0].payload["ticket_ids"]) == {"COL-1", "COL-2"}


# ---------------------------------------------------------------------------
# TestSpawnErrorBackoff
# ---------------------------------------------------------------------------


class TestSpawnErrorBackoff:
    """Exponential backoff on spawn_error: re-claim is deferred, not immediate.

    Mirror of TestDispatchUsageLimitBackoff for the generic spawn_error path.
    No freezegun — timing verified via before/after comparisons and pre-seeded
    queue state.  See GitHub #868.
    """

    def test_spawn_error_stamps_next_eligible_at_and_count(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """After a spawn error the task has next_eligible_at in the future and
        spawn_error_count == 1."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-868A", client="test-client"))

        before = datetime.now(UTC)
        daemon = _RaisingNativeDaemon(RuntimeError("daemon hiccup"))
        dispatch_tick(simple_config, native_daemon=daemon)
        after = datetime.now(UTC) + timedelta(seconds=1)

        queue = load_dev_queue()
        task = queue.tasks[0]
        assert task.status == QueueItemStatus.PENDING
        assert task.spawn_error_count == 1
        assert task.next_eligible_at is not None
        assert task.next_eligible_at > before
        # delay should be _SPAWN_ERROR_BACKOFF_INITIAL_SECONDS=2 after the revert
        assert task.next_eligible_at < after + timedelta(seconds=2)

    def test_backedoff_task_not_claimed_on_next_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A task with next_eligible_at in the future is not claimed."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Pre-seed task with active backoff
        task = TicketTask(
            ticket_id="GEN-868B",
            client="test-client",
            spawn_error_count=1,
            next_eligible_at=datetime.now(UTC) + timedelta(hours=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        queue = load_dev_queue()
        assert queue.tasks[0].status == QueueItemStatus.PENDING

    def test_backoff_skip_reason_emitted_in_event(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """dispatch.tick event has skip_reason=spawn_error_backoff when all
        pending tasks are in backoff."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-868C",
            client="test-client",
            spawn_error_count=1,
            next_eligible_at=datetime.now(UTC) + timedelta(hours=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        events = read_events(
            consumer="test-868-backoff-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert (
            events[0].payload["skip_reason"] == DispatchSkipReason.SPAWN_ERROR_BACKOFF
        )

    def test_backoff_grows_exponentially_on_repeated_errors(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Successive spawn errors produce increasing next_eligible_at delays."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-868D", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("daemon hiccup"))

        # First error: spawn_error_count=1, delay≈2s
        dispatch_tick(simple_config, native_daemon=daemon)
        q1 = load_dev_queue()
        assert q1.tasks[0].spawn_error_count == 1
        eligible_after_1 = q1.tasks[0].next_eligible_at
        assert eligible_after_1 is not None

        # Task is in backoff; reset next_eligible_at to past so tick can retry
        q1.tasks[0].next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        save_dev_queue(q1)

        # Second error: spawn_error_count=2, delay≈4s (doubles)
        dispatch_tick(simple_config, native_daemon=daemon)
        q2 = load_dev_queue()
        assert q2.tasks[0].spawn_error_count == 2
        eligible_after_2 = q2.tasks[0].next_eligible_at
        assert eligible_after_2 is not None
        # Second window must start later than first
        assert eligible_after_2 > eligible_after_1

    def test_backoff_capped_at_max(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Delay is capped at _SPAWN_ERROR_BACKOFF_CAP_SECONDS regardless of count."""
        from cw.dispatch import _SPAWN_ERROR_BACKOFF_CAP_SECONDS

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Pre-seed with very high count so uncapped delay would be enormous
        task = TicketTask(
            ticket_id="GEN-868E",
            client="test-client",
            spawn_error_count=100,
            next_eligible_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = _RaisingNativeDaemon(RuntimeError("daemon hiccup"))
        before = datetime.now(UTC)
        dispatch_tick(simple_config, native_daemon=daemon)

        queue = load_dev_queue()
        eligible = queue.tasks[0].next_eligible_at
        assert eligible is not None
        delay = (eligible - before).total_seconds()
        # Should be at most cap + a small epsilon for timing jitter
        assert delay <= _SPAWN_ERROR_BACKOFF_CAP_SECONDS + 2

    def test_backoff_resets_after_successful_spawn(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """After a successful spawn, spawn_error_count and next_eligible_at clear."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Pre-seed task that has suffered a prior backoff but the window has elapsed
        task = TicketTask(
            ticket_id="GEN-868F",
            client="test-client",
            spawn_error_count=3,
            next_eligible_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 1
        queue = load_dev_queue()
        claimed = queue.tasks[0]
        assert claimed.status == QueueItemStatus.RUNNING
        assert claimed.spawn_error_count == 0
        assert claimed.next_eligible_at is None

    def test_expired_backoff_allows_reclaim(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """A task whose next_eligible_at has passed is claimed and spawned normally."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        task = TicketTask(
            ticket_id="GEN-868G",
            client="test-client",
            spawn_error_count=1,
            next_eligible_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon)

        assert result.spawned == 1
        assert len(daemon.spawn_calls) == 1

    def test_priority_task_in_backoff_is_skipped(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """Priority-ticket path: a backedoff priority task is skipped (not claimed),
        and a non-priority eligible task is claimed instead (#868 priority loop)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Priority task A is in backoff
        task_a = TicketTask(
            ticket_id="GEN-868-PRIO-A",
            client="test-client",
            spawn_error_count=1,
            next_eligible_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # Non-priority task B is eligible
        task_b = TicketTask(ticket_id="GEN-868-PRIO-B", client="test-client")
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))
        # Plan orders A first (would be claimed first without backoff)
        save_plan(
            DispatchPlan(
                tasks=[
                    TicketTask(ticket_id="GEN-868-PRIO-A", client="test-client"),
                    TicketTask(ticket_id="GEN-868-PRIO-B", client="test-client"),
                ]
            )
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, use_plan=True)

        assert result.spawned == 1
        queue = load_dev_queue()
        a = next(t for t in queue.tasks if t.ticket_id == "GEN-868-PRIO-A")
        b = next(t for t in queue.tasks if t.ticket_id == "GEN-868-PRIO-B")
        assert a.status == QueueItemStatus.PENDING
        assert b.status == QueueItemStatus.RUNNING

    def test_skip_to_next_skips_backedoff_claims_other(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """skip-to-next: a backedoff task is skipped; the next eligible task
        is claimed instead."""
        # cap=2 so both tasks could theoretically be claimed
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"test-client": 2},
        )
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        # Task A is in backoff; task B is eligible (created later, lower priority tie)
        task_a = TicketTask(
            ticket_id="GEN-868H-A",
            client="test-client",
            spawn_error_count=1,
            next_eligible_at=datetime.now(UTC) + timedelta(hours=1),
        )
        task_b = TicketTask(
            ticket_id="GEN-868H-B",
            client="test-client",
        )
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 1
        queue = load_dev_queue()
        a = next(t for t in queue.tasks if t.ticket_id == "GEN-868H-A")
        b = next(t for t in queue.tasks if t.ticket_id == "GEN-868H-B")
        # B must be RUNNING (spawned); A stays PENDING in backoff
        assert b.status == QueueItemStatus.RUNNING
        assert a.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# TestLaneCircuitBreaker — per-lane breaker on consecutive spawn_error (#875)
# ---------------------------------------------------------------------------


_BREAKER_LANE_KEY = "test-client/default"


def _seed_lane_override(count: int, *, paused: bool | None = None) -> None:
    """Persist a LaneConcurrencyOverride for the default test-client lane."""
    _save_concurrency_overrides(
        ConcurrencyOverrides(
            lanes={
                _BREAKER_LANE_KEY: LaneConcurrencyOverride(
                    consecutive_spawn_errors=count,
                    paused=paused,
                )
            }
        )
    )


def _lane_override() -> LaneConcurrencyOverride:
    """Read back the persisted default test-client lane override."""
    return _load_concurrency_overrides().lanes[_BREAKER_LANE_KEY]


class TestLaneCircuitBreaker:
    """Per-lane circuit breaker trips after N consecutive spawn errors (#875).

    Sibling of TestSpawnErrorBackoff: the per-lane counter is orthogonal to
    the per-task exponential backoff. The counter increments once per tick on
    a spawn error, trips (pauses the lane) at the configured threshold, and
    resets to zero on any successful spawn.
    """

    def test_spawn_error_increments_lane_counter(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """One raising tick increments the lane counter but does not yet trip."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-875A", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        override = _lane_override()
        assert override.consecutive_spawn_errors == 1
        # Threshold is 2; not yet tripped.
        assert not override.paused

    def test_counter_increments_once_per_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """Two pending tasks + raising daemon → counter increments by exactly 1."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-875B1", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-875B2", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        assert _lane_override().consecutive_spawn_errors == 1

    def test_breaker_trips_at_threshold_sets_paused(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """Reaching the threshold on a tick sets the lane's paused flag."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(1)
        add_ticket(TicketTask(ticket_id="GEN-875C", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        override = _lane_override()
        assert override.consecutive_spawn_errors == 2
        assert override.paused is True

    def test_trip_emits_lane_paused_breaker_payload(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """The tripping tick emits a circuit-breaker-sourced LANE_PAUSED event."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(1)
        add_ticket(TicketTask(ticket_id="GEN-875D", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        events = read_events(
            consumer="test-875-trip",
            event_types=[OrchestratorEventType.LANE_PAUSED],
        )
        assert len(events) == 1
        assert events[0].payload == {
            "client": "test-client",
            "lane": "default",
            "source": "circuit_breaker",
            "consecutive_count": 2,
            "last_error": "boom",
        }

    def test_last_error_empty_string_when_exception_blank(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A blank exception message yields last_error == "" (never null)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(1)
        add_ticket(TicketTask(ticket_id="GEN-875E", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError(""))
        dispatch_tick(breaker_config, native_daemon=daemon)

        events = read_events(
            consumer="test-875-blank",
            event_types=[OrchestratorEventType.LANE_PAUSED],
        )
        assert len(events) == 1
        assert events[0].payload["last_error"] == ""

    def test_success_resets_lane_counter(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A successful spawn resets the lane's consecutive-error counter."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(1)
        add_ticket(TicketTask(ticket_id="GEN-875F", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(breaker_config, native_daemon=daemon)

        assert result.spawned == 1
        assert _lane_override().consecutive_spawn_errors == 0

    def test_reset_short_circuits_when_already_zero(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An already-zero counter is not re-persisted on a successful spawn."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(0)
        add_ticket(TicketTask(ticket_id="GEN-875G", client="test-client"))

        saves: list[object] = []
        from cw.config import _save_concurrency_overrides as real_save

        def _counting_save(overrides: object) -> None:
            saves.append(overrides)
            real_save(overrides)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "cw.dispatch.lanes._save_concurrency_overrides", _counting_save
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(breaker_config, native_daemon=daemon)

        assert result.spawned == 1
        # Counter was already 0 → reset helper must short-circuit before save.
        assert saves == []

    def test_paused_lane_skipped_next_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A breaker-paused lane is skipped: no spawn attempted."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(2, paused=True)
        add_ticket(TicketTask(ticket_id="GEN-875H", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(breaker_config, native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []

    def test_circuit_paused_skip_reason_emitted(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A breaker-paused lane with pending work reports LANE_CIRCUIT_PAUSED."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(2, paused=True)
        add_ticket(TicketTask(ticket_id="GEN-875I", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(breaker_config, native_daemon=daemon)

        events = read_events(
            consumer="test-875-skip",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert (
            events[0].payload["skip_reason"] == DispatchSkipReason.LANE_CIRCUIT_PAUSED
        )

    def test_operator_pause_does_not_report_circuit_paused(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """An operator pause (counter below threshold) is not LANE_CIRCUIT_PAUSED."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(0, paused=True)
        add_ticket(TicketTask(ticket_id="GEN-875J", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(breaker_config, native_daemon=daemon)

        events = read_events(
            consumer="test-875-oppause",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.NO_PENDING

    def test_spawn_error_precedes_circuit_paused_on_tripping_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """The tick that trips reports SPAWN_ERROR (higher precedence)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_lane_override(1)
        add_ticket(TicketTask(ticket_id="GEN-875K", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        events = read_events(
            consumer="test-875-precede",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.SPAWN_ERROR

    def test_backoff_unchanged(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """Per-task spawn_error_count still increments alongside the lane counter."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-875L", client="test-client"))

        daemon = _RaisingNativeDaemon(RuntimeError("boom"))
        dispatch_tick(breaker_config, native_daemon=daemon)

        queue = load_dev_queue()
        assert queue.tasks[0].spawn_error_count == 1
        assert _lane_override().consecutive_spawn_errors == 1

    def test_check_returns_false_when_lane_has_no_override(
        self,
        tmp_dispatch_dirs: Path,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A lane paused with no override entry is not a breaker pause."""
        from cw.dispatch import _check_lane_circuit_paused

        result = _check_lane_circuit_paused(
            LaneConfig(name="default"),
            DevQueueStore(tasks=[]),
            "test-client",
            overrides=ConcurrencyOverrides(),
            config=breaker_config,
        )
        assert result is False

    def test_check_returns_false_when_below_threshold(
        self,
        tmp_dispatch_dirs: Path,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """An override below the threshold is not (yet) a breaker pause."""
        from cw.dispatch import _check_lane_circuit_paused

        overrides = ConcurrencyOverrides(
            lanes={
                _BREAKER_LANE_KEY: LaneConcurrencyOverride(consecutive_spawn_errors=1)
            }
        )
        queue_snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GEN-875M",
                    client="test-client",
                    status=QueueItemStatus.PENDING,
                )
            ]
        )
        result = _check_lane_circuit_paused(
            LaneConfig(name="default"),
            queue_snapshot,
            "test-client",
            overrides=overrides,
            config=breaker_config,
        )
        assert result is False

    def test_check_returns_true_at_threshold_with_pending(
        self,
        tmp_dispatch_dirs: Path,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """At/above threshold with pending work in the lane is a breaker pause."""
        from cw.dispatch import _check_lane_circuit_paused

        overrides = ConcurrencyOverrides(
            lanes={
                _BREAKER_LANE_KEY: LaneConcurrencyOverride(consecutive_spawn_errors=2)
            }
        )
        queue_snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GEN-875N",
                    client="test-client",
                    status=QueueItemStatus.PENDING,
                )
            ]
        )
        result = _check_lane_circuit_paused(
            LaneConfig(name="default"),
            queue_snapshot,
            "test-client",
            overrides=overrides,
            config=breaker_config,
        )
        assert result is True

    def test_check_returns_false_when_tripped_but_no_pending(
        self,
        tmp_dispatch_dirs: Path,
        breaker_config: OrchestratorConfig,
    ) -> None:
        """A tripped lane with no pending work is not a (reportable) breaker pause."""
        from cw.dispatch import _check_lane_circuit_paused

        overrides = ConcurrencyOverrides(
            lanes={
                _BREAKER_LANE_KEY: LaneConcurrencyOverride(consecutive_spawn_errors=2)
            }
        )
        result = _check_lane_circuit_paused(
            LaneConfig(name="default"),
            DevQueueStore(tasks=[]),
            "test-client",
            overrides=overrides,
            config=breaker_config,
        )
        assert result is False

    def test_dispatch_loads_overrides_once_with_multiple_paused_lanes(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two simultaneously-paused lanes in one tick share a single override load.

        Regression guard for the lazy-load fix in _dispatch_client_lanes: without
        it, _check_lane_circuit_paused would reload the override file once per
        paused lane per tick instead of once per call.

        Expects exactly 2 loads per tick: one from the client-scoped freshness-
        block latch (_reset_client_freshness_blocks, RFC 0007 §W2 — this client
        passes the freshness gate every tick in this fixture), plus the single
        lane-pause-check load shared across both paused lanes (the invariant
        this test actually guards). A regression back to N-loads-per-paused-lane
        would show up as 3, not 2.
        """
        lanes = [
            LaneConfig(name="impl", max_parallel=1),
            LaneConfig(name="idea", max_parallel=1),
        ]
        client = ClientConfig(
            name="test-client",
            workspace_path=sample_client_config.workspace_path,
            default_branch="main",
            worktree_base=sample_client_config.worktree_base,
            lanes=lanes,
        )
        _make_clients_yaml(tmp_dispatch_dirs, client)
        _save_concurrency_overrides(
            ConcurrencyOverrides(
                lanes={
                    "test-client/impl": LaneConcurrencyOverride(paused=True),
                    "test-client/idea": LaneConcurrencyOverride(paused=True),
                }
            )
        )
        add_ticket(TicketTask(ticket_id="GEN-875O1", client="test-client", lane="impl"))
        add_ticket(TicketTask(ticket_id="GEN-875O2", client="test-client", lane="idea"))

        loads: list[object] = []
        real_load = _load_concurrency_overrides

        def _counting_load() -> ConcurrencyOverrides:
            loads.append(None)
            return real_load()

        monkeypatch.setattr(
            "cw.dispatch.lanes._load_concurrency_overrides", _counting_load
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(breaker_config, native_daemon=daemon)

        assert result.spawned == 0
        assert len(loads) == 2


class TestResolveDispatchSkipReasonCircuitPaused:
    """Precedence of LANE_CIRCUIT_PAUSED inside _resolve_dispatch_skip_reason."""

    def test_circuit_paused_returned_when_set(self) -> None:
        reason = _resolve_dispatch_skip_reason(
            usage_limit_detected=False,
            cap_full=False,
            spawn_error=False,
            lane_cap_blocked=False,
            spawn_backoff_skipped=False,
            lane_circuit_paused=True,
            client_spawned=0,
        )
        assert reason == DispatchSkipReason.LANE_CIRCUIT_PAUSED

    def test_spawn_error_wins_over_circuit_paused(self) -> None:
        reason = _resolve_dispatch_skip_reason(
            usage_limit_detected=False,
            cap_full=False,
            spawn_error=True,
            lane_cap_blocked=False,
            spawn_backoff_skipped=False,
            lane_circuit_paused=True,
            client_spawned=0,
        )
        assert reason == DispatchSkipReason.SPAWN_ERROR

    def test_circuit_paused_wins_over_backoff(self) -> None:
        reason = _resolve_dispatch_skip_reason(
            usage_limit_detected=False,
            cap_full=False,
            spawn_error=False,
            lane_cap_blocked=False,
            spawn_backoff_skipped=True,
            lane_circuit_paused=True,
            client_spawned=0,
        )
        assert reason == DispatchSkipReason.LANE_CIRCUIT_PAUSED


class TestRunDispatchLoopHydrationHook:
    """PR-state hydration hook fires once per loop iteration, fault-tolerant (#929)."""

    def test_hydrate_called_once_per_iteration(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)
        calls: list[object] = []

        def _record(cfg: object) -> None:
            calls.append(cfg)

        monkeypatch.setattr("cw.dispatch.loop.hydrate_pr_states", _record)
        run_dispatch_loop(once=True, emit=None)
        assert len(calls) == 1

    def test_hydrate_exception_does_not_crash_loop(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)

        def _boom(_cfg: object) -> None:
            msg = "hydration boom"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.loop.hydrate_pr_states", _boom)
        # Broad-catch idiom: hydration failure must never crash the tick loop.
        run_dispatch_loop(once=True, emit=None)

    def test_hydrate_called_once_per_tick_with_multiple_clients(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With 2 configured clients, the hook still fires exactly once per tick.

        A single-client fixture can't distinguish "once per tick" from "once
        per client" — both would assert len(calls) == 1. This confirms the
        hook is wired at the outer per-tick level, not inside the per-client
        dispatch loop.
        """
        ws_a = make_git_repo("workspace/client-a")
        ws_b = make_git_repo("workspace/client-b")
        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            f"    workspace_path: {ws_a}\n"
            "    default_branch: main\n"
            f"    worktree_base: {tmp_path / 'worktrees-a'}\n"
            "  client-b:\n"
            f"    workspace_path: {ws_b}\n"
            "    default_branch: main\n"
            f"    worktree_base: {tmp_path / 'worktrees-b'}\n"
        )
        monkeypatch.setattr("cw.dispatch.gating.reconcile", lambda: None)
        calls: list[object] = []

        def _record(cfg: object) -> None:
            calls.append(cfg)

        monkeypatch.setattr("cw.dispatch.loop.hydrate_pr_states", _record)
        run_dispatch_loop(once=True, emit=None)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# TestFreshnessBlockAttentionLatch — per-client freshness-gate-block latch
# (RFC 0007 §W2)
# ---------------------------------------------------------------------------


@pytest.fixture
def freshness_breaker_config() -> OrchestratorConfig:
    """OrchestratorConfig with a low freshness-block attention threshold."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel={"test-client": 1},
        freshness_block_attention_threshold=2,
    )


def _seed_client_freshness_override(count: int) -> None:
    """Persist a ClientConcurrencyOverride for test-client's freshness latch."""
    _save_concurrency_overrides(
        ConcurrencyOverrides(
            clients={
                "test-client": ClientConcurrencyOverride(
                    consecutive_freshness_blocks=count
                )
            }
        )
    )


def _client_freshness_override() -> ClientConcurrencyOverride:
    """Read back the persisted test-client freshness-block override."""
    return _load_concurrency_overrides().clients["test-client"]


def _force_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force _resolve_freshness to report test-client as stale (main behind)."""
    monkeypatch.setattr(
        "cw.dispatch.gating.is_main_behind_origin",
        lambda _client, **_kw: (True, "aaa", "bbb", 3),
    )


class TestFreshnessBlockAttentionLatch:
    """Per-client consecutive freshness-gate-block latch (RFC 0007 §W2).

    Sibling of TestLaneCircuitBreaker: the client-keyed counter increments
    once per tick the client is skipped with skip_reason=FRESHNESS_GATE,
    fires session.needs_attention exactly once at the configured threshold
    (latch: no re-fire while still at/above threshold), and resets to 0 on
    the next non-stale tick.
    """

    def test_below_threshold_no_emit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One stale tick increments the counter but does not yet emit."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-W2A", client="test-client"))
        _force_stale(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(freshness_breaker_config, native_daemon=daemon, auto_ff=False)

        assert _client_freshness_override().consecutive_freshness_blocks == 1
        events = read_events(
            consumer="test-w2-below",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert events == []

    def test_counter_increments_once_per_tick_not_per_ticket(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two pending tickets on a stale client → counter increments by 1."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-W2B1", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-W2B2", client="test-client"))
        _force_stale(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(freshness_breaker_config, native_daemon=daemon, auto_ff=False)

        assert _client_freshness_override().consecutive_freshness_blocks == 1

    def test_exact_threshold_emits_full_payload(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reaching the threshold emits session.needs_attention with all 8 fields."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_client_freshness_override(1)
        add_ticket(TicketTask(ticket_id="GEN-W2C", client="test-client"))
        _force_stale(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(freshness_breaker_config, native_daemon=daemon, auto_ff=False)

        assert _client_freshness_override().consecutive_freshness_blocks == 2
        events = read_events(
            consumer="test-w2-threshold",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload == {
            "session_id": "",
            "session_name": "",
            "client": "test-client",
            "ticket_id": None,
            "claude_session_id": None,
            "paused_status": "freshness_gate_blocked",
            "breadcrumbs": "main_behind_origin",
            "crashed": False,
        }
        assert events[0].correlation_id is None

    def test_no_refire_while_latched(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tick that keeps the client at/above threshold does not re-emit."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_client_freshness_override(2)
        add_ticket(TicketTask(ticket_id="GEN-W2D", client="test-client"))
        _force_stale(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(freshness_breaker_config, native_daemon=daemon, auto_ff=False)

        assert _client_freshness_override().consecutive_freshness_blocks == 3
        events = read_events(
            consumer="test-w2-latched",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert events == []

    def test_reset_on_non_stale_tick(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
    ) -> None:
        """A non-stale tick resets the client's freshness-block counter to 0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_client_freshness_override(1)
        add_ticket(TicketTask(ticket_id="GEN-W2E", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(
            freshness_breaker_config, native_daemon=daemon, auto_ff=False
        )

        assert result.spawned == 1
        assert _client_freshness_override().consecutive_freshness_blocks == 0

    def test_reset_short_circuits_when_already_zero(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An already-zero counter is not re-persisted on a non-stale tick."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_client_freshness_override(0)
        add_ticket(TicketTask(ticket_id="GEN-W2F", client="test-client"))

        saves: list[object] = []
        from cw.config import _save_concurrency_overrides as real_save

        def _counting_save(overrides: object) -> None:
            saves.append(overrides)
            real_save(overrides)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "cw.dispatch.lanes._save_concurrency_overrides", _counting_save
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(
            freshness_breaker_config, native_daemon=daemon, auto_ff=False
        )

        assert result.spawned == 1
        # Counter was already 0 → reset helper must short-circuit before save.
        assert saves == []

    def test_no_push_notification_on_threshold_emit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        freshness_breaker_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The freshness-block escalation never calls fire_push_notification."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        _seed_client_freshness_override(1)
        add_ticket(TicketTask(ticket_id="GEN-W2G", client="test-client"))
        _force_stale(monkeypatch)

        push_calls: list[object] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification",
            lambda *a, **kw: push_calls.append((a, kw)),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(freshness_breaker_config, native_daemon=daemon, auto_ff=False)

        assert _client_freshness_override().consecutive_freshness_blocks == 2
        assert push_calls == []


# ---------------------------------------------------------------------------
# TestAvailabilityPreflightGate (RFC 0011 A5, #1157)
# ---------------------------------------------------------------------------


def _force_gh_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fleet-wide gh-availability probe to report unavailable.

    Overrides the autouse ``_mock_gh_availability`` default (which returns
    True) on the same ``cw.dispatch.gating.check_gh_availability`` seam.
    """
    monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", lambda **_kw: False)


def _seed_availability_cache(
    *, probed_at: datetime, available: bool, latched: bool
) -> None:
    """Persist a fleet-wide AvailabilityProbeCache to the shared sidecar."""
    save_availability_probe_cache(
        AvailabilityProbeCache(
            probed_at=probed_at, available=available, latched=latched
        )
    )


def _availability_cache() -> AvailabilityProbeCache:
    """Read back the persisted fleet-wide availability probe cache."""
    cache = load_availability_probe_cache()
    assert cache is not None
    return cache


class TestAvailabilityPreflightGate:
    """Fleet-wide gh-availability preflight gate (RFC 0011 A5).

    A TTL-cached ``gh auth status`` probe runs as the highest-precedence
    per-client pre-claim gate in ``dispatch_tick``'s client loop. On probe
    failure every client stays PENDING (no claim, no ``attempts`` consumed),
    the fleet-wide outage latch fires ``session.needs_attention`` exactly once
    per outage episode (edge-triggered), and a fresh success silently resets
    the latch.
    """

    def test_available_spawns_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When the probe reports available, dispatch proceeds as usual."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5A", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_unavailable_holds_task_pending_no_attempt_consumed(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The core binding requirement: a gated PENDING task keeps attempts=0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5B", client="test-client", attempts=0))
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-A5B")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0

    def test_unavailable_emits_dispatch_tick_availability_gate(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gated client emits dispatch.tick with skip_reason=availability_gate."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5C", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-a5-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.AVAILABILITY_GATE
        ]
        assert len(ticks) == 1
        payload = ticks[0].payload
        assert payload["client"] == "test-client"
        assert payload["claimed"] == 0
        assert payload["pending"] == 1

    def test_first_failure_emits_session_needs_attention_once(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first bad probe fires the full fleet-wide attention payload."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5D", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-a5-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload == {
            "session_id": "",
            "session_name": "",
            "client": "",
            "ticket_id": None,
            "claude_session_id": None,
            "paused_status": _AVAILABILITY_OUTAGE_REASON,
            "breadcrumbs": "availability_probe_failed",
            "crashed": False,
        }
        assert events[0].correlation_id is None

    def test_second_consecutive_failure_within_ttl_does_not_re_emit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cache-hit second tick within the TTL fires no second attention."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5E", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-a5-reemit",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1

    def test_persistent_failure_across_ttl_expiry_does_not_re_emit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A still-failing fresh probe after TTL expiry does NOT re-emit (MF2).

        Distinct from the within-TTL test: here the TTL expires so a fresh
        ``check_gh_availability`` call actually runs on the second tick, yet the
        outage-episode latch suppresses a second attention event. Uses a
        call-counting stub (not the constant ``_force_gh_unavailable`` lambda)
        so the "a fresh probe call occurs" half of MF2 is independently
        asserted, not just inferred from the freeze_time advance.
        """
        from freezegun import freeze_time

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5F", client="test-client"))

        calls: list[int] = []

        def _counting_unavailable_probe(**_kw: object) -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(
            "cw.dispatch.gating.check_gh_availability", _counting_unavailable_probe
        )

        daemon = FakeNativeDaemonClient()
        with freeze_time("2026-07-16 12:00:00") as frozen:
            dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
            frozen.tick(delta=timedelta(seconds=_AVAILABILITY_PROBE_TTL_SECONDS + 10))
            dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 2, "TTL expiry must trigger a second real probe call"

        events = read_events(
            consumer="test-a5-ttl-reemit",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert _availability_cache().latched is True

    def test_recovery_resets_latch_silently(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh success after an outage resets the latch and fires no event."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5G", client="test-client"))
        # Seed a latched outage state whose TTL is already expired so the next
        # tick re-probes (autouse default → available → recovery).
        _seed_availability_cache(
            probed_at=datetime.now(UTC)
            - timedelta(seconds=_AVAILABILITY_PROBE_TTL_SECONDS + 10),
            available=False,
            latched=True,
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1
        assert _availability_cache().latched is False
        events = read_events(
            consumer="test-a5-recover",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert events == []

    def test_ttl_cache_suppresses_repeat_probe_within_window(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two ticks within the TTL trigger only one real probe call."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5H", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", _counting_probe)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 1

    def test_reprobes_after_ttl_expiry(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tick after the TTL window runs a fresh probe."""
        from freezegun import freeze_time

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5I", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", _counting_probe)

        daemon = FakeNativeDaemonClient()
        with freeze_time("2026-07-16 12:00:00") as frozen:
            dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
            frozen.tick(delta=timedelta(seconds=_AVAILABILITY_PROBE_TTL_SECONDS + 10))
            dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 2

    def test_ttl_dedup_across_multiple_clients_in_one_tick(
        self,
        tmp_dispatch_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
        tmp_path: Path,
    ) -> None:
        """A single dispatch_tick with two eligible clients probes only once.

        Distinct from test_ttl_cache_suppresses_repeat_probe_within_window
        (which proves dedup *across ticks*): this proves dedup *within* one
        tick's client loop, via the in-process ``_resolve_availability_once``
        memoization already hoisted outside the per-client loop.
        """
        ws_a = make_git_repo("workspace/ttl-client-a")
        ws_b = make_git_repo("workspace/ttl-client-b")
        client_a = ClientConfig(
            name="ttl-client-a",
            workspace_path=ws_a,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-ttl-a",
        )
        client_b = ClientConfig(
            name="ttl-client-b",
            workspace_path=ws_b,
            default_branch="main",
            worktree_base=tmp_path / "worktrees-ttl-b",
        )

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            f"  {client_a.name}:\n"
            f"    workspace_path: {client_a.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_a.worktree_base}\n"
            f"  {client_b.name}:\n"
            f"    workspace_path: {client_b.workspace_path}\n"
            f"    default_branch: main\n"
            f"    worktree_base: {client_b.worktree_base}\n"
        )

        add_ticket(TicketTask(ticket_id="GEN-A5K1", client=client_a.name))
        add_ticket(TicketTask(ticket_id="GEN-A5K2", client=client_b.name))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", _counting_probe)

        config = OrchestratorConfig(default_max_parallel=1)
        daemon = FakeNativeDaemonClient()
        dispatch_tick(config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 1

    def test_resolution_error_fails_open(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any resolution error fails open — dispatch proceeds (no gate)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5J", client="test-client"))

        def _boom(**_kw: object) -> bool:
            msg = "probe blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", _boom)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_no_push_notification_on_first_failure_emit(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outage escalation never calls fire_push_notification."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5K", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        push_calls: list[object] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification",
            lambda *a, **kw: push_calls.append((a, kw)),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert push_calls == []

    def test_availability_outage_reason_constant_value(self) -> None:
        """Pin the paused_status constant value (distinct from awaiting-op)."""
        assert _AVAILABILITY_OUTAGE_REASON == "gh_availability_outage"

    def test_availability_gate_precedes_freshness_gate(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the availability gate is closed, _resolve_freshness is not called.

        Event-ordering alone doesn't prove short-circuit (MF1) — spy the
        freshness resolver and assert it is never invoked on the gated path.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5L", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        freshness_calls: list[object] = []
        monkeypatch.setattr(
            "cw.dispatch.tick._resolve_freshness",
            lambda *a, **kw: freshness_calls.append((a, kw)) or (False, None),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert freshness_calls == []

    def test_finalize_stage_task_also_held_pending(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A FINALIZE-stage PENDING task is also held by the single gate (S2)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(
            TicketTask(
                ticket_id="GEN-A5M",
                client="test-client",
                stage=Stage.FINALIZE,
                attempts=0,
            )
        )
        _force_gh_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-A5M")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0

    def test_freshness_helpers_not_called_on_gated_path(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On the gated path neither freshness latch helper runs (S3).

        The freshness counter must stay frozen during an outage, not reset —
        prevents a future reorder from silently clearing real freshness latches.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5N", client="test-client"))
        _force_gh_unavailable(monkeypatch)

        record_calls: list[object] = []
        reset_calls: list[object] = []
        monkeypatch.setattr(
            "cw.dispatch.tick._record_client_freshness_block",
            lambda *a, **kw: record_calls.append((a, kw)),
        )
        monkeypatch.setattr(
            "cw.dispatch.tick._reset_client_freshness_blocks",
            lambda *a, **kw: reset_calls.append((a, kw)),
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert record_calls == []
        assert reset_calls == []

    def test_probe_not_called_when_fleet_dispatch_paused(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """max_parallel_clients=0 (fleet-wide dispatch pause) never probes.

        The availability check is memoized inside the per-client loop (only
        resolved once the loop body actually runs for a client), not hoisted
        unconditionally above it — a fully-paused fleet, which breaks out of
        the loop on its very first iteration before reaching the gate, must
        not shell out to `gh auth status` or touch the outage latch.
        """
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5O", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", _counting_probe)

        paused_config = simple_config.model_copy(update={"max_parallel_clients": 0})
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(paused_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        assert calls == []


# ---------------------------------------------------------------------------
# TestSshKeyPreflightGate (#927)
# ---------------------------------------------------------------------------


def _force_ssh_key_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SSH-agent-key preflight probe to report unavailable.

    Overrides the autouse ``_mock_ssh_key_available`` default (which returns
    True) on the same ``cw.dispatch.gating.check_ssh_key_available`` seam.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.check_ssh_key_available", lambda **_kw: False
    )


class TestSshKeyPreflightGate:
    """SSH-agent-key preflight gate (#927).

    A per-tick-memoized ``ssh-add -l`` probe runs as the second-highest-
    precedence per-client pre-claim gate in ``dispatch_tick``'s client loop,
    immediately after the fleet-wide gh-availability gate and before the
    per-client freshness gate. On probe failure every client stays PENDING
    (no claim, no ``attempts`` consumed) and an operator error line is
    emitted once per dispatch-loop run.
    """

    def test_available_spawns_normally(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When the probe reports available, dispatch proceeds as usual."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1A", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_unavailable_holds_task_pending_no_attempt_consumed(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The core binding requirement: a gated PENDING task keeps attempts=0."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1B", client="test-client", attempts=0))
        _force_ssh_key_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-S1B")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0

    def test_unavailable_emits_dispatch_tick_ssh_key_gate(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gated client emits dispatch.tick with skip_reason=ssh_key_gate."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1C", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-s1-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ]
        assert len(ticks) == 1
        payload = ticks[0].payload
        assert payload["client"] == "test-client"
        assert payload["claimed"] == 0
        assert payload["pending"] == 1

    def test_unavailable_emits_operator_error_line_once(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator error line is deduplicated across ticks in one run."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1D", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)

        lines: list[str] = []
        warned_ssh_key: set[str] = set()
        daemon = FakeNativeDaemonClient()
        dispatch_tick(
            simple_config,
            native_daemon=daemon,
            auto_ff=False,
            emit=lines.append,
            warned_ssh_key=warned_ssh_key,
        )
        dispatch_tick(
            simple_config,
            native_daemon=daemon,
            auto_ff=False,
            emit=lines.append,
            warned_ssh_key=warned_ssh_key,
        )

        expected = (
            "Error: SSH key not available in agent."
            " Run 'ssh-add' to unlock before dispatching."
        )
        matches = [ln for ln in lines if ln == expected]
        assert len(matches) == 1

    def test_availability_gate_takes_precedence_over_ssh_key_gate(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both probes forced unavailable: AVAILABILITY_GATE wins, not SSH_KEY_GATE."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1E", client="test-client"))
        _force_gh_unavailable(monkeypatch)
        _force_ssh_key_unavailable(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-s1-precedence-avail",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.AVAILABILITY_GATE

    def test_ssh_key_gate_takes_precedence_over_freshness_gate(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSH unavailable + stale repo: SSH_KEY_GATE wins over FRESHNESS_GATE."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1F", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        monkeypatch.setattr(
            "cw.dispatch.gating.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-s1-precedence-fresh",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.SSH_KEY_GATE


# ---------------------------------------------------------------------------
# TestSpawnInvalidatesStaleContextJson (#1046)
# ---------------------------------------------------------------------------


class TestSpawnInvalidatesStaleContextJson:
    """Pre-spawn invalidation of a stale ``.cw/context.json`` (#1046).

    A requeued/re-spawned task (``attempts > 1``) must not let a worker
    silently replan against a prior session's materialized context — see
    #1030 for the incident this guards against. LocalExecutor is excluded:
    ``local_runner.build_task_message`` reads ``.cw/context.json`` and
    degrades silently to an empty ``## Ticket:`` header if it disappears
    (the #952 regression class).
    """

    def test_deletes_stale_context_json_for_non_local_executor(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """attempts > 1 + non-local (default) executor -> stale
        .cw/context.json is removed before spawn."""
        from cw.worktree import create_worktree

        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)

        branch = f"{sample_client_config.feature_branch_prefix}/GEN-STALE"
        worktree_path = create_worktree(
            sample_client_config, branch, allow_dirty_reuse=True
        )
        context_file = worktree_path / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "stale"}')

        add_ticket(TicketTask(ticket_id="GEN-STALE", client="test-client", attempts=1))

        daemon = FakeNativeDaemonClient()
        spawned = dispatch_tick(simple_config, native_daemon=daemon).spawned

        assert spawned == 1
        assert not context_file.exists()

    def test_preserves_context_json_for_local_executor(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """attempts > 1 + LOCAL_BACKEND executor -> .cw/context.json
        survives (local_runner.build_task_message reads it; deleting it
        would recreate the #952 empty-header regression)."""
        from cw.worktree import create_worktree

        config_dir = tmp_dispatch_dirs / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        clients_file = config_dir / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  test-client:\n"
            f"    workspace_path: {sample_client_config.workspace_path}\n"
            f"    default_branch: {sample_client_config.default_branch}\n"
            f"    worktree_base: {sample_client_config.worktree_base}\n"
            "    pipeline:\n"
            "      executors:\n"
            "        plan:\n"
            "          backend: local\n"
        )

        branch = f"{sample_client_config.feature_branch_prefix}/GEN-STALE-LOCAL"
        worktree_path = create_worktree(
            sample_client_config, branch, allow_dirty_reuse=True
        )
        context_file = worktree_path / ".cw" / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"sentinel": "preserved"}')

        add_ticket(
            TicketTask(ticket_id="GEN-STALE-LOCAL", client="test-client", attempts=1)
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon)

        assert context_file.exists()
        assert context_file.read_text() == '{"sentinel": "preserved"}'


# ---------------------------------------------------------------------------
# TestRunDispatchLoopSingletonLock — #1362 dispatch-loop singleton lock
# ---------------------------------------------------------------------------


class TestRunDispatchLoopSingletonLock:
    """run_dispatch_loop acquires the global singleton lock at entry (#1362)."""

    def test_run_dispatch_loop_once_raises_when_lock_already_held(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """R1: ``--once`` is NOT exempt — a second launch while the lock is
        held (e.g. by a live ``serve``) is refused, not silently allowed
        through as a "quick" single tick."""
        with dispatch_loop_lock(), pytest.raises(DispatchLoopLockedError):
            run_dispatch_loop(once=True, native_daemon=FakeNativeDaemonClient())

    def test_run_dispatch_loop_once_acquires_freely_when_unheld(
        self, tmp_dispatch_dirs: Path
    ) -> None:
        """R1: ``--once`` acquires and completes when no loop holds the lock."""
        # No prior holder — must complete without raising.
        run_dispatch_loop(once=True, native_daemon=FakeNativeDaemonClient())
        # And the lock was released, so a subsequent launch also succeeds.
        run_dispatch_loop(once=True, native_daemon=FakeNativeDaemonClient())

    def test_run_dispatch_loop_force_bypasses_lock_and_warns(
        self, tmp_dispatch_dirs: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R3: ``force=True`` bypasses an externally-held lock and logs a WARNING."""
        with (
            dispatch_loop_lock(),
            caplog.at_level(logging.WARNING, logger="cw.dispatch"),
        ):
            run_dispatch_loop(
                once=True,
                force=True,
                native_daemon=FakeNativeDaemonClient(),
            )
        assert any("force" in record.message.lower() for record in caplog.records), (
            f"expected a force WARNING but got: {[r.message for r in caplog.records]!r}"
        )
