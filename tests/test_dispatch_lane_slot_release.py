"""Settled parked rows free their lane slot through ``dispatch_tick`` (GitHub #3774).

Cross-surface integration cases for the settled-worker carve-out of
``occupies_lane_slot`` (R4): a BLOCKED_ON_USER row counts toward its lane's cap
only while it has a live worker or an unsettled execution claim. The predicate
matrix lives in ``tests/test_models.py``; this module drives a whole tick, so
admission, the client ceiling, the #2077 worktree screen and the skip-event
payloads are all exercised together. It also pins, by AST scan, that every
production occupancy call passes the tick's session statuses (ARCHITECTURE
§8.4: patch the predicate, not one call site).

Follows the ``tests/test_dispatch_host_capacity.py`` precedent of a focused
dispatch module instead of growing ``tests/test_dispatch.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, requeue_ticket
from cw.dispatch import dispatch_tick
from cw.events import read_events
from cw.models import (
    ClientConfig,
    CwState,
    DispatchSkipReason,
    LaneConfig,
    LaunchedFixWorker,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._dispatch_gating_helpers import _force_gh_unavailable
from tests._shared.git_repos import occupy_worktree
from tests._ticket_view_helpers import bound_session_for, seed_queue

if TYPE_CHECKING:
    from cw.models import Session

_CLIENT = "test-client"
_LANE = "impl"
_RUN = "SLOT-RUN"
_PARK = "SLOT-PARK"
_NEW = "SLOT-NEW"
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "cw"
_PINNED_CALLS = frozenset({"occupies_lane_slot", "_lane_occupants_for_client"})


def _lane_client(workspace_dir: Path, tmp_path: Path) -> ClientConfig:
    """``test-client`` with one ``impl`` lane of ``max_parallel=2``."""
    client = ClientConfig(
        name=_CLIENT,
        workspace_path=workspace_dir,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        lanes=[LaneConfig(name=_LANE, max_parallel=2)],
    )
    write_clients_yaml(client)
    return client


def _config(ceiling: int = 5) -> OrchestratorConfig:
    return OrchestratorConfig(
        tick_interval_seconds=30, per_client_ceiling={_CLIENT: ceiling}
    )


def _row(ticket_id: str, status: QueueItemStatus, **overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": _CLIENT,
        "lane": _LANE,
        "status": status,
    }
    kwargs.update(overrides)
    return TicketTask.model_validate(kwargs)


def _park(**overrides: object) -> TicketTask:
    """A ``plan_scope_drift`` park at IMPL bound to ``sess-park``."""
    kwargs: dict[str, object] = {
        "stage": Stage.IMPL,
        "blocked_reason": "plan_scope_drift",
        "session_id": "sess-park",
    }
    kwargs.update(overrides)
    return _row(_PARK, QueueItemStatus.BLOCKED_ON_USER, **kwargs)


def _seed_lane(
    park_status: SessionStatus, *, park: TicketTask | None = None
) -> TicketTask:
    """One RUNNING row, one parked row (session at *park_status*), one PENDING row.

    The RUNNING row names no session, so it holds its slot by status alone
    and adds nothing to the client ceiling.
    """
    parked = park if park is not None else _park()
    sessions: list[Session] = [bound_session_for(parked, status=park_status)]
    seed_queue(
        [
            _row(_RUN, QueueItemStatus.RUNNING),
            parked,
            _row(_NEW, QueueItemStatus.PENDING),
        ],
        sessions,
    )
    return parked


def _ticks(consumer: str) -> list[dict[str, object]]:
    events = read_events(
        consumer=consumer, event_types=[OrchestratorEventType.DISPATCH_TICK]
    )
    return [e.payload for e in events if e.payload.get("client") == _CLIENT]


def _status(ticket_id: str) -> QueueItemStatus:
    (task,) = [t for t in load_dev_queue().tasks if t.ticket_id == ticket_id]
    return task.status


def _occupant_ids(payload: dict[str, object]) -> list[str]:
    occupants = payload["lane_occupants"]
    assert isinstance(occupants, dict)
    return sorted(o["ticket_id"] for o in occupants[_LANE])


class TestSettledParkFreesTheSlot:
    def test_completed_worker_park_lets_pending_row_spawn(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        _seed_lane(SessionStatus.COMPLETED)
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 1
        assert len(daemon.spawn_calls) == 1
        assert _status(_NEW) == QueueItemStatus.RUNNING
        assert _status(_PARK) == QueueItemStatus.BLOCKED_ON_USER
        (payload,) = _ticks("test-3774-freed")
        assert payload["skip_reason"] != DispatchSkipReason.LANE_CAP_BLOCKED
        lanes = payload["lanes"]
        assert isinstance(lanes, dict)
        assert lanes[_LANE]["running"] >= 0
        assert _PARK not in _occupant_ids(payload)

    def test_live_worker_park_keeps_the_lane_full(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        _seed_lane(SessionStatus.ACTIVE)
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        assert _status(_NEW) == QueueItemStatus.PENDING
        (payload,) = _ticks("test-3774-held")
        assert payload["skip_reason"] == DispatchSkipReason.LANE_CAP_BLOCKED
        assert _occupant_ids(payload) == sorted([_PARK, _RUN])

    def test_launched_fix_worker_is_an_unsettled_claim(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        _lane_client(sample_client_config.workspace_path, tmp_path)
        park = _park(
            fix_dispatch_launched_worker=LaunchedFixWorker(
                surface_ref="fix-ref", launched_at=datetime(2026, 11, 19, tzinfo=UTC)
            )
        )
        _seed_lane(SessionStatus.COMPLETED, park=park)
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        assert _status(_NEW) == QueueItemStatus.PENDING


class TestFreedSlotKeepsOtherGuards:
    def test_client_ceiling_still_binds(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        """A free lane slot never lifts the client ceiling (one live session, cap 1)."""
        _lane_client(sample_client_config.workspace_path, tmp_path)
        _seed_lane(SessionStatus.COMPLETED)
        state = load_state()
        live = bound_session_for(
            _row("OTHER", QueueItemStatus.COMPLETED, session_id="sess-live"),
            status=SessionStatus.ACTIVE,
        )
        save_state(CwState(sessions=[*state.sessions, live]))
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(ceiling=1), native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        assert _status(_NEW) == QueueItemStatus.PENDING
        (payload,) = _ticks("test-3774-ceiling")
        assert payload["skip_reason"] != DispatchSkipReason.LANE_CAP_BLOCKED
        assert _PARK not in _occupant_ids(payload)

    def test_host_budget_still_binds(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        _seed_lane(SessionStatus.COMPLETED)
        state = load_state()
        other = bound_session_for(
            _row("OTHER", QueueItemStatus.COMPLETED, session_id="sess-host"),
            client="other-client",
            status=SessionStatus.ACTIVE,
        )
        save_state(CwState(sessions=[*state.sessions, other]))
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_ceiling={_CLIENT: 5},
            host_session_budget=1,
        )
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(config, native_daemon=daemon)

        assert result.spawned == 0
        assert _status(_NEW) == QueueItemStatus.PENDING
        (payload,) = _ticks("test-3774-host")
        assert payload["skip_reason"] == DispatchSkipReason.HOST_CAPACITY_GATED

    def test_duplicate_worker_guard_screens_the_pending_sibling(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        """A live roster worker on the ticket's worktree still blocks a sibling row.

        The parked row's own session is COMPLETED, so it frees its slot; the
        #2077 occupancy screen, not the lane cap, keeps the PENDING sibling for
        the same ticket from spawning into the occupied tree.
        """
        from cw.worktree import create_worktree

        client = _lane_client(sample_client_config.workspace_path, tmp_path)
        park = _park()
        sibling = _row(_PARK, QueueItemStatus.PENDING, stage=Stage.IMPL)
        seed_queue(
            [park, sibling], [bound_session_for(park, status=SessionStatus.COMPLETED)]
        )
        daemon = FakeNativeDaemonClient()
        branch = f"{client.feature_branch_prefix}/{_PARK}"
        worktree = create_worktree(client, branch, allow_dirty_reuse=True)
        occupy_worktree(client, worktree, "roster", daemon=daemon)

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        statuses = sorted(t.status.value for t in load_dev_queue().tasks)
        assert statuses == sorted(
            [QueueItemStatus.BLOCKED_ON_USER.value, QueueItemStatus.PENDING.value]
        )
        skips = [p["skip_reason"] for p in _ticks("test-3774-dup")]
        assert DispatchSkipReason.WORKTREE_OCCUPIED in skips


class TestReleaseCompetesNormally:
    """R4: release goes through ordinary admission, no bypass."""

    def test_requeued_park_waits_behind_a_full_lane(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        live_a = _row(_RUN, QueueItemStatus.RUNNING)
        live_b = _row("SLOT-RUN-2", QueueItemStatus.RUNNING)
        park = _park(session_id=None, blocked_reason="impl_failed")
        seed_queue([live_a, live_b, park])

        requeue_ticket(_PARK, _CLIENT)
        assert _status(_PARK) == QueueItemStatus.PENDING
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 0
        assert _status(_PARK) == QueueItemStatus.PENDING
        (payload,) = _ticks("test-3774-requeue-full")
        assert payload["skip_reason"] == DispatchSkipReason.LANE_CAP_BLOCKED

    def test_requeued_park_spawns_into_a_free_slot(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        park = _park(session_id=None, blocked_reason="impl_failed")
        seed_queue([_row(_RUN, QueueItemStatus.RUNNING), park])

        requeue_ticket(_PARK, _CLIENT)
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 1
        assert _status(_PARK) == QueueItemStatus.RUNNING

    def test_approved_scope_drift_park_spawns_into_a_free_slot(
        self,
        sample_client_config: ClientConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.dev_queue import approve_scope_drift_ticket

        _lane_client(sample_client_config.workspace_path, tmp_path)
        park = _park(session_id=None, stage_base_ref="base0000")
        seed_queue([_row(_RUN, QueueItemStatus.RUNNING), park])
        monkeypatch.setattr(
            "cw.dev_queue.scope_drift.branch_head_sha_on_origin",
            lambda _branch, **_kw: ("a" * 40, True),
        )
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: "test-operator"
        )

        approve_scope_drift_ticket(_PARK, _CLIENT, ["src/extra.py"])
        assert _status(_PARK) == QueueItemStatus.PENDING
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 1
        assert _status(_PARK) == QueueItemStatus.RUNNING

    def test_freed_park_is_never_released_by_the_tick(
        self, sample_client_config: ClientConfig, tmp_path: Path
    ) -> None:
        """Freeing the slot is not a release: the gate row stays parked."""
        _lane_client(sample_client_config.workspace_path, tmp_path)
        seed_queue([_park(session_id=None)])
        daemon = FakeNativeDaemonClient()

        result = dispatch_tick(_config(), native_daemon=daemon)

        assert result.spawned == 0
        assert _status(_PARK) == QueueItemStatus.BLOCKED_ON_USER


class TestSkipEventConsistency:
    def test_availability_skip_reports_the_claiming_ticks_occupants(
        self,
        sample_client_config: ClientConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _lane_client(sample_client_config.workspace_path, tmp_path)
        _seed_lane(SessionStatus.COMPLETED)
        _force_gh_unavailable(monkeypatch)
        daemon = FakeNativeDaemonClient()

        dispatch_tick(_config(), native_daemon=daemon, auto_ff=False)

        (payload,) = _ticks("test-3774-availability")
        assert payload["skip_reason"] == DispatchSkipReason.AVAILABILITY_GATE
        assert _occupant_ids(payload) == [_RUN]
        assert payload["occupied"] == 1


class TestCallSitePin:
    """Every production occupancy call passes ``session_statuses=`` (§8.4)."""

    @staticmethod
    def _unpinned_calls() -> list[str]:
        offenders: list[str] = []
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name not in _PINNED_CALLS:
                    continue
                if not any(kw.arg == "session_statuses" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(_SRC_ROOT)}:{node.lineno}")
        return offenders

    def test_every_call_passes_session_statuses(self) -> None:
        assert self._unpinned_calls() == []

    def test_the_scan_sees_the_known_call_sites(self) -> None:
        """Guard the scan itself: it must find the production call sites."""
        seen: set[str] = set()
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in _PINNED_CALLS
                ):
                    seen.add(str(path.relative_to(_SRC_ROOT)))
        assert {
            "dispatch/tick.py",
            "dispatch/claim/lane_stats.py",
            "board.py",
            "cli/dev_queue/status.py",
            "doctor/wedge/blocked_on_user.py",
            "dispatch/lanes.py",
        } <= seen
