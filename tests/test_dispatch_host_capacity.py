"""Tests for cw.dispatch.host_capacity — fleet-wide host-capacity admission gate.

GitHub #1444: a single optional ``OrchestratorConfig.host_session_budget``
ceiling on concurrently-running DAEMON sessions across the whole host,
independent of (and folded into) the existing per-client ceiling. See
``src/cw/dispatch/host_capacity.py`` for the amended-R4 ghost-lockout
rationale this module's tests exercise.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import save_state
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.dispatch import _resolve_dispatch_skip_reason, dispatch_tick
from cw.dispatch.host_capacity import resolve_host_capacity
from cw.events import read_events
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests.conftest import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures — mirrors tests/test_dispatch.py's sample_client_config/_make_clients_yaml
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dispatch_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; state isolation is handled by the autouse fixture."""
    return tmp_config_dir


@pytest.fixture
def workspace_dir(make_git_repo: Callable[[str], Path]) -> Path:
    return make_git_repo("workspace/host-capacity-project")


@pytest.fixture
def sample_client_config(workspace_dir: Path, tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        name="test-client",
        workspace_path=workspace_dir,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
    )


def _make_clients_yaml(tmp_path: Path, *clients: ClientConfig) -> None:
    """Write a minimal clients.yaml for the given clients."""
    config_dir = tmp_path / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    clients_file = config_dir / "clients.yaml"
    lines = ["clients:\n"]
    for client in clients:
        lines.append(f"  {client.name}:\n")
        lines.append(f"    workspace_path: {client.workspace_path}\n")
        lines.append(f"    default_branch: {client.default_branch}\n")
        if client.worktree_base is not None:
            lines.append(f"    worktree_base: {client.worktree_base}\n")
    clients_file.write_text("".join(lines))


# ---------------------------------------------------------------------------
# Unit tests — resolve_host_capacity(state, queue, config)
# ---------------------------------------------------------------------------


class TestResolveHostCapacity:
    def test_host_session_budget_none_is_feature_off_invariant(self) -> None:
        """host_session_budget=None passes through as host_budget=None.

        host_running is still computed correctly (parked-task exclusion still
        applies) — it's the downstream fold into available_client_slots that
        the None makes a no-op, not this resolver's counting logic.
        """
        state = CwState(
            sessions=[
                _make_daemon_session(id="s1", status=SessionStatus.ACTIVE),
                _make_daemon_session(id="s2", status=SessionStatus.ACTIVE),
            ]
        )
        queue = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1",
                    session_id="s2",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                )
            ]
        )
        config = OrchestratorConfig(host_session_budget=None)

        host_running, host_budget = resolve_host_capacity(state, queue, config)

        assert host_budget is None
        assert host_running == 1

    def test_counts_daemon_active_and_idle_sessions(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(id="s1", status=SessionStatus.ACTIVE),
                _make_daemon_session(id="s2", status=SessionStatus.IDLE),
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, host_budget = resolve_host_capacity(
            state, DevQueueStore(), config
        )

        assert host_running == 2
        assert host_budget == 5

    def test_excludes_user_origin_sessions(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", origin=SessionOrigin.DAEMON, status=SessionStatus.ACTIVE
                ),
                _make_daemon_session(
                    id="s2", origin=SessionOrigin.USER, status=SessionStatus.ACTIVE
                ),
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, DevQueueStore(), config)

        assert host_running == 1

    def test_excludes_completed_backgrounded_timed_out_sessions(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(id="s1", status=SessionStatus.ACTIVE),
                _make_daemon_session(id="s2", status=SessionStatus.COMPLETED),
                _make_daemon_session(id="s3", status=SessionStatus.BACKGROUNDED),
                _make_daemon_session(id="s4", status=SessionStatus.TIMED_OUT),
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, DevQueueStore(), config)

        assert host_running == 1

    def test_excludes_session_whose_task_is_blocked_on_user(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(id="s1", status=SessionStatus.ACTIVE),
                _make_daemon_session(id="s2", status=SessionStatus.ACTIVE),
            ]
        )
        queue = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1",
                    session_id="s2",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                )
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, queue, config)

        assert host_running == 1

    def test_excludes_session_whose_task_is_awaiting_operator_signoff(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(id="s1", status=SessionStatus.ACTIVE),
                _make_daemon_session(id="s2", status=SessionStatus.ACTIVE),
            ]
        )
        queue = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1",
                    session_id="s2",
                    status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
                )
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, queue, config)

        assert host_running == 1

    def test_counts_active_session_with_no_owning_task(self) -> None:
        """Exclusion only fires on a CONFIRMED parked-task join, never on absence."""
        state = CwState(sessions=[_make_daemon_session(id="s1", status=SessionStatus.ACTIVE)])
        queue = DevQueueStore(tasks=[])
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, queue, config)

        assert host_running == 1

    def test_counts_session_whose_task_is_running(self) -> None:
        """Only the two parked statuses exclude — RUNNING does not."""
        state = CwState(sessions=[_make_daemon_session(id="s1", status=SessionStatus.ACTIVE)])
        queue = DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1",
                    session_id="s1",
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, queue, config)

        assert host_running == 1

    def test_join_is_fleet_wide_across_multiple_clients(self) -> None:
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", client="client-a", status=SessionStatus.ACTIVE
                ),
                _make_daemon_session(
                    id="s2", client="client-b", status=SessionStatus.ACTIVE
                ),
            ]
        )
        config = OrchestratorConfig(host_session_budget=5)

        host_running, _ = resolve_host_capacity(state, DevQueueStore(), config)

        assert host_running == 2


# ---------------------------------------------------------------------------
# Unit tests — _resolve_dispatch_skip_reason precedence (imported, not duplicated)
# ---------------------------------------------------------------------------


class TestResolveDispatchSkipReasonHostCapacity:
    def test_host_capacity_gated_wins_over_cap_full(self) -> None:
        reason = _resolve_dispatch_skip_reason(
            usage_limit_detected=False,
            cap_full=True,
            spawn_error=False,
            lane_cap_blocked=False,
            spawn_backoff_skipped=False,
            lane_circuit_paused=False,
            client_spawned=0,
            host_capacity_gated=True,
        )
        assert reason == DispatchSkipReason.HOST_CAPACITY_GATED

    def test_usage_limited_wins_over_host_capacity_gated(self) -> None:
        reason = _resolve_dispatch_skip_reason(
            usage_limit_detected=True,
            cap_full=False,
            spawn_error=False,
            lane_cap_blocked=False,
            spawn_backoff_skipped=False,
            lane_circuit_paused=False,
            client_spawned=0,
            host_capacity_gated=True,
        )
        assert reason == DispatchSkipReason.USAGE_LIMITED


# ---------------------------------------------------------------------------
# Integration tests — dispatch_tick level
# ---------------------------------------------------------------------------


@pytest.fixture
def host_budget_config() -> OrchestratorConfig:
    """OrchestratorConfig with a generous per-client ceiling but host_session_budget=1."""
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_ceiling={"client-a": 5, "client-b": 5, "test-client": 5},
        host_session_budget=1,
    )


class TestDispatchTickHostCapacity:
    def test_host_budget_gates_second_client_declaration_order(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        host_budget_config: OrchestratorConfig,
    ) -> None:
        """First-declared client spawns; second is host-capacity-gated (R3/R5)."""
        ws_a = make_git_repo("workspace/host-cap-client-a")
        ws_b = make_git_repo("workspace/host-cap-client-b")
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
        _make_clients_yaml(tmp_dispatch_dirs, client_a, client_b)
        add_ticket(TicketTask(ticket_id="A-1", client="client-a"))
        add_ticket(TicketTask(ticket_id="B-1", client="client-b"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(host_budget_config, native_daemon=daemon)

        assert result.spawned == 1

        events = read_events(
            consumer="test-host-budget-declaration-order",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        by_client = {e.payload["client"]: e.payload for e in events}
        assert by_client["client-a"]["skip_reason"] == DispatchSkipReason.NONE
        assert by_client["client-a"]["claimed"] == 1
        assert (
            by_client["client-b"]["skip_reason"] == DispatchSkipReason.HOST_CAPACITY_GATED
        )
        assert by_client["client-b"]["claimed"] == 0

    def test_host_budget_already_oversubscribed_does_not_touch_running(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        host_budget_config: OrchestratorConfig,
    ) -> None:
        """Pre-existing sessions over budget are left alone (R0: no kill, no reject)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        pre_existing = [
            _make_daemon_session(id="pre-1", client="other-client", status=SessionStatus.ACTIVE),
            _make_daemon_session(id="pre-2", client="other-client", status=SessionStatus.ACTIVE),
        ]
        save_state(CwState(sessions=pre_existing))
        add_ticket(TicketTask(ticket_id="CW-1", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(host_budget_config, native_daemon=daemon)

        assert result.spawned == 0
        from cw.config import load_state

        state = load_state()
        session_ids = {s.id for s in state.sessions}
        assert {"pre-1", "pre-2"} <= session_ids
        assert len(state.sessions) == 2

    def test_gated_task_attempts_not_incremented(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        host_budget_config: OrchestratorConfig,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        save_state(
            CwState(
                sessions=[
                    _make_daemon_session(
                        id="pre-1", client="other-client", status=SessionStatus.ACTIVE
                    )
                ]
            )
        )
        add_ticket(TicketTask(ticket_id="CW-2", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(host_budget_config, native_daemon=daemon)

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "CW-2")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0

    def test_ghost_parked_session_does_not_block_free_slot(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
        host_budget_config: OrchestratorConfig,
    ) -> None:
        """A ghost (ACTIVE session, BLOCKED_ON_USER task) does not permanently
        strand the host budget slot it appears to occupy (amended R4)."""
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        save_state(
            CwState(
                sessions=[
                    _make_daemon_session(
                        id="ghost-1",
                        client="other-client",
                        status=SessionStatus.ACTIVE,
                    )
                ]
            )
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GHOST-1",
                        client="other-client",
                        session_id="ghost-1",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                    )
                ]
            )
        )
        add_ticket(TicketTask(ticket_id="CW-3", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(host_budget_config, native_daemon=daemon)

        assert result.spawned == 1

    def test_dispatch_tick_event_carries_host_running_and_host_budget(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_ceiling={"test-client": 5},
            host_session_budget=7,
        )
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        add_ticket(TicketTask(ticket_id="CW-4", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(config, native_daemon=daemon)

        events = read_events(
            consumer="test-host-budget-payload",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["host_budget"] == 7
        assert events[0].payload["host_running"] == 0

    def test_host_capacity_gated_precedence_over_cap_full(
        self,
        tmp_dispatch_dirs: Path,
        sample_client_config: ClientConfig,
    ) -> None:
        """Both cap_full and host_capacity_gated are true; HOST_CAPACITY_GATED wins."""
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_ceiling={"test-client": 1},
            host_session_budget=1,
        )
        _make_clients_yaml(tmp_dispatch_dirs, sample_client_config)
        save_state(
            CwState(
                sessions=[
                    _make_daemon_session(
                        id="pre-1", client="test-client", status=SessionStatus.ACTIVE
                    )
                ]
            )
        )
        add_ticket(TicketTask(ticket_id="CW-5", client="test-client"))

        daemon = FakeNativeDaemonClient()
        dispatch_tick(config, native_daemon=daemon)

        events = read_events(
            consumer="test-host-capacity-precedence",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.HOST_CAPACITY_GATED
