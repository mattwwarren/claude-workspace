"""Claim-time disk-pressure preflight gate tests (#1887, #2470).

Covers ``cw.dispatch.gating.disk_pressure``: the free-bytes
and free-inodes dimensions and the per-client ``host_tmp_exhausted``
latch. Split out of ``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import (
    add_ticket,
    load_dev_queue,
)
from cw.disk import DiskUsage, InodeUsage
from cw.dispatch import dispatch_tick
from cw.dispatch_state import load_host_tmp_probe_cache
from cw.events import read_events
from cw.models import (
    DEFAULT_DISK_PRESSURE_MIN_FREE_GB,
    ClientConfig,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._dispatch_gating_helpers import _force_ssh_key_unavailable

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorEvent


# ---------------------------------------------------------------------------
# TestDiskPressurePreflightGate (#1887, split from #1858)
# ---------------------------------------------------------------------------


def _force_disk_pressure_gated(
    monkeypatch: pytest.MonkeyPatch, *, free_gb: float = 0.5
) -> None:
    """Force the claim-time disk-pressure probe to report a nearly-full mount.

    Overrides the autouse ``_mock_disk_usage`` default (which reports 250 GB
    free) on the same ``cw.dispatch.gating.disk_pressure.check_disk_usage`` seam.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.disk_pressure.check_disk_usage",
        lambda _path: DiskUsage(total_gb=500.0, free_gb=free_gb),
    )


class TestDiskPressurePreflightGate:
    """Claim-time disk-pressure preflight gate (#1887, split from #1858).

    A ``shutil.disk_usage`` probe of the client's worktree-base mount runs as
    the third per-client pre-claim gate in ``dispatch_tick``'s client loop,
    after the fleet-wide gh-availability and SSH-agent-key gates and before
    the per-client freshness gate (whose ``git pull --ff-only`` would
    otherwise write more data onto an already-tight disk). On pressure the
    client stays PENDING (no claim, no ``attempts`` consumed) and an operator
    WARN line is emitted once per client per dispatch-loop run.
    """

    def test_available_spawns_normally(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """With plenty of free space, dispatch proceeds as usual."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1A", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_low_disk_holds_task_pending_no_attempt_consumed(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The core binding requirement: a gated PENDING task keeps attempts=0."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1B", client="test-client", attempts=0))
        _force_disk_pressure_gated(monkeypatch)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        assert daemon.spawn_calls == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-D1B")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0

    def test_low_disk_emits_dispatch_tick_disk_pressure_gate(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gated client emits dispatch.tick with skip_reason=disk_pressure_gate."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1C", client="test-client"))
        _force_disk_pressure_gated(monkeypatch, free_gb=1.25)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-d1-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        ticks = [
            e
            for e in events
            if e.payload.get("skip_reason") == DispatchSkipReason.DISK_PRESSURE_GATE
        ]
        assert len(ticks) == 1
        payload = ticks[0].payload
        assert payload["client"] == "test-client"
        assert payload["claimed"] == 0
        assert payload["pending"] == 1
        assert payload["disk_free_gb"] == 1.25
        assert payload["disk_min_free_gb"] == DEFAULT_DISK_PRESSURE_MIN_FREE_GB

    def test_low_disk_emits_operator_warn_line_once_per_client(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator WARN line is deduplicated across ticks in one run."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1D", client="test-client"))
        _force_disk_pressure_gated(monkeypatch)

        lines: list[str] = []
        warned_disk_pressure: set[str] = set()
        daemon = FakeNativeDaemonClient()
        for _ in range(2):
            dispatch_tick(
                simple_config,
                native_daemon=daemon,
                auto_ff=False,
                emit=lines.append,
                warned_disk_pressure=warned_disk_pressure,
            )

        matches = [
            ln for ln in lines if ln.startswith("WARN test-client: worktree disk low")
        ]
        assert len(matches) == 1
        assert warned_disk_pressure == {"test-client"}

    def test_ssh_key_gate_takes_precedence_over_disk_pressure_gate(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both probes forced bad: SSH_KEY_GATE wins, not DISK_PRESSURE_GATE."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1E", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        _force_disk_pressure_gated(monkeypatch)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-d1-precedence-ssh",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.SSH_KEY_GATE

    def test_disk_pressure_gate_takes_precedence_over_freshness_gate(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Low disk + stale repo: DISK_PRESSURE_GATE wins over FRESHNESS_GATE."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1F", client="test-client"))
        _force_disk_pressure_gated(monkeypatch)
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.is_main_behind_origin",
            lambda _client, **_kw: (True, "aaa", "bbb", 3),
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.freshness.check_main_ff_safety",
            lambda _client, **_kw: "behind",
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        events = read_events(
            consumer="test-d1-precedence-fresh",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert len(events) == 1
        assert events[0].payload["skip_reason"] == DispatchSkipReason.DISK_PRESSURE_GATE

    def test_gate_disabled_bypasses_skip_and_emits_bypass_event(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """disk_pressure_gate_enabled=False bypasses the pressure skip — the
        client dispatches normally, a DISK_PRESSURE_GATE_BYPASSED event is
        recorded, and no dispatch.tick DISK_PRESSURE_GATE skip is recorded."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1G", client="test-client"))
        _force_disk_pressure_gated(monkeypatch, free_gb=1.5)

        bypass_config = simple_config.model_copy(
            update={"disk_pressure_gate_enabled": False}
        )
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(bypass_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

        bypass_events = read_events(
            consumer="test-d1-bypass",
            event_types=[OrchestratorEventType.DISK_PRESSURE_GATE_BYPASSED],
        )
        assert len(bypass_events) == 1
        assert bypass_events[0].payload["client"] == "test-client"
        assert bypass_events[0].payload["disk_free_gb"] == 1.5
        assert (
            bypass_events[0].payload["disk_min_free_gb"]
            == DEFAULT_DISK_PRESSURE_MIN_FREE_GB
        )

        tick_events = read_events(
            consumer="test-d1-bypass-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        skip_ticks = [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.DISK_PRESSURE_GATE
        ]
        assert skip_ticks == []

    def test_probe_oserror_fails_open_and_dispatches(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unprobeable mount is not evidence of pressure: the gate fails open."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-D1H", client="test-client"))

        probe_error = "mount went away"

        def _raise(_path: Path) -> DiskUsage:
            raise OSError(probe_error)

        monkeypatch.setattr("cw.dispatch.gating.disk_pressure.check_disk_usage", _raise)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1
        tick_events = read_events(
            consumer="test-d1-probe-error",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        skip_ticks = [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.DISK_PRESSURE_GATE
        ]
        assert skip_ticks == []


# ---------------------------------------------------------------------------
# TestHostTmpInodePressureGate (#2470)
# ---------------------------------------------------------------------------


def _force_inode_usage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    total_inodes: int = 1_000_000,
    free_inodes: int = 10_000,
) -> None:
    """Force the inode probe on every mount (overrides conftest's roomy default)."""
    monkeypatch.setattr(
        "cw.dispatch.gating.disk_pressure.check_inode_usage",
        lambda _path: InodeUsage(total_inodes=total_inodes, free_inodes=free_inodes),
    )


def _disk_pressure_skips(consumer: str) -> list[OrchestratorEvent]:
    return [
        e
        for e in read_events(
            consumer=consumer, event_types=[OrchestratorEventType.DISPATCH_TICK]
        )
        if e.payload.get("skip_reason") == DispatchSkipReason.DISK_PRESSURE_GATE
    ]


def _host_tmp_attention(consumer: str) -> list[OrchestratorEvent]:
    return [
        e
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        if e.payload.get("paused_status") == "host_tmp_exhausted"
    ]


class TestHostTmpInodePressureGate:
    """Inode dimension of the claim-time disk-pressure gate (#2470).

    A tmpfs can exhaust its inodes while bytes remain plentiful (the
    2026-09-27 ENOSPC incident). The gate refuses a spawn when free inodes
    fall below ``max(disk_pressure_min_free_inodes,
    disk_pressure_min_free_inode_fraction x total)`` and fires one per-client,
    edge-triggered ``session.needs_attention(host_tmp_exhausted)``.
    """

    def test_low_inodes_gate_with_inode_payload(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bytes fine, inodes low: DISK_PRESSURE_GATE skip carrying inode fields."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1A", client="test-client", attempts=0))
        _force_inode_usage(monkeypatch, free_inodes=10_000)

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-I1A")
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0
        skips = _disk_pressure_skips("test-i1-skip")
        assert len(skips) == 1
        assert skips[0].payload["disk_free_inodes"] == 10_000
        assert skips[0].payload["disk_min_free_inodes"] == 50_000
        assert skips[0].payload["disk_free_gb"] == 250.0

    def test_low_inodes_emit_inode_specific_operator_warn_line(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1W", client="test-client"))
        _force_inode_usage(monkeypatch, free_inodes=10_000)

        lines: list[str] = []
        dispatch_tick(
            simple_config,
            native_daemon=FakeNativeDaemonClient(),
            auto_ff=False,
            emit=lines.append,
        )

        assert any(
            ln.startswith("WARN test-client: worktree mount low on inodes")
            and "10,000 free, need 50,000" in ln
            for ln in lines
        )

    def test_attention_latch_is_edge_triggered_and_resets_on_healthy_probe(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fires once per outage episode, per client; a healthy probe re-arms it."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1B", client="test-client"))
        daemon = FakeNativeDaemonClient()

        _force_inode_usage(monkeypatch, free_inodes=10_000)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        fired = _host_tmp_attention("test-i1-latch-1")
        assert len(fired) == 1
        payload = fired[0].payload
        assert payload["client"] == "test-client"
        assert payload["session_id"] == ""
        assert payload["ticket_id"] is None
        assert load_host_tmp_probe_cache()["test-client"].latched is True

        _force_inode_usage(monkeypatch, free_inodes=900_000)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        assert load_host_tmp_probe_cache()["test-client"].latched is False

        _force_inode_usage(monkeypatch, free_inodes=10_000)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        assert len(_host_tmp_attention("test-i1-latch-2")) == 2

    def test_latch_is_per_client_and_isolated(
        self,
        sample_client_config: ClientConfig,
        make_git_repo: Callable[[str], Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One client's exhausted mount neither gates nor latches another client."""
        other = ClientConfig(
            name="other-client",
            workspace_path=make_git_repo("workspace/other-project"),
            default_branch="main",
            worktree_base=tmp_path / "worktrees-other",
        )
        write_clients_yaml(sample_client_config, other)
        add_ticket(TicketTask(ticket_id="GEN-I1C", client="test-client"))
        add_ticket(TicketTask(ticket_id="GEN-I1D", client="other-client"))
        exhausted_base = sample_client_config.worktree_base
        assert exhausted_base is not None

        def _probe(path: Path) -> InodeUsage:
            free = 10_000 if path == exhausted_base else 900_000
            return InodeUsage(total_inodes=1_000_000, free_inodes=free)

        monkeypatch.setattr(
            "cw.dispatch.gating.disk_pressure.check_inode_usage", _probe
        )
        config = OrchestratorConfig(
            tick_interval_seconds=30,
            per_client_max_parallel={"test-client": 1, "other-client": 1},
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1
        other_task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-I1D")
        assert other_task.status == QueueItemStatus.RUNNING
        skips = _disk_pressure_skips("test-i1-iso-skip")
        assert [e.payload["client"] for e in skips] == ["test-client"]
        fired = _host_tmp_attention("test-i1-iso-attn")
        assert [e.payload["client"] for e in fired] == ["test-client"]
        cache = load_host_tmp_probe_cache()
        assert cache["test-client"].latched is True
        assert "other-client" not in cache

    @pytest.mark.parametrize(
        ("total_inodes", "free_inodes", "gated"),
        [
            # Absolute floor dominates: 5% of 200K is only 10K.
            (200_000, 40_000, True),
            # R1 example 1: 1M-inode tmpfs refuses below 50K free.
            (1_000_000, 49_999, True),
            (1_000_000, 50_000, False),
            # R1 example 2: fraction dominates -- 50M-inode mount refuses
            # below 2.5M free even though 2M is far above the 50K floor.
            (50_000_000, 2_000_000, True),
            (50_000_000, 2_500_000, False),
        ],
    )
    def test_threshold_is_max_of_floor_and_fraction(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
        total_inodes: int,
        free_inodes: int,
        gated: bool,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1E", client="test-client"))
        _force_inode_usage(
            monkeypatch, total_inodes=total_inodes, free_inodes=free_inodes
        )

        result = dispatch_tick(
            simple_config, native_daemon=FakeNativeDaemonClient(), auto_ff=False
        )

        assert result.spawned == (0 if gated else 1)
        assert len(_disk_pressure_skips("test-i1-threshold")) == (1 if gated else 0)

    def test_gate_disabled_bypasses_but_still_fires_attention(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The informational signal is independent of the enforcement bypass."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1F", client="test-client"))
        _force_inode_usage(monkeypatch, free_inodes=10_000)
        bypass_config = simple_config.model_copy(
            update={"disk_pressure_gate_enabled": False}
        )

        result = dispatch_tick(
            bypass_config, native_daemon=FakeNativeDaemonClient(), auto_ff=False
        )

        assert result.spawned == 1
        bypass_events = read_events(
            consumer="test-i1-bypass",
            event_types=[OrchestratorEventType.DISK_PRESSURE_GATE_BYPASSED],
        )
        assert len(bypass_events) == 1
        assert bypass_events[0].payload["disk_free_inodes"] == 10_000
        assert bypass_events[0].payload["disk_min_free_inodes"] == 50_000
        assert _disk_pressure_skips("test-i1-bypass-tick") == []
        assert len(_host_tmp_attention("test-i1-bypass-attn")) == 1

    def test_zero_total_inodes_never_gates(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """btrfs reports f_files=f_ffree=0: the inode dimension does not apply."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1G", client="test-client"))
        _force_inode_usage(monkeypatch, total_inodes=0, free_inodes=0)

        result = dispatch_tick(
            simple_config, native_daemon=FakeNativeDaemonClient(), auto_ff=False
        )

        assert result.spawned == 1
        assert _disk_pressure_skips("test-i1-btrfs") == []
        assert _host_tmp_attention("test-i1-btrfs-attn") == []

    def test_inode_probe_oserror_fails_open(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1H", client="test-client"))
        probe_error = "statvfs failed"

        def _raise(_path: Path) -> InodeUsage:
            raise OSError(probe_error)

        monkeypatch.setattr(
            "cw.dispatch.gating.disk_pressure.check_inode_usage", _raise
        )

        result = dispatch_tick(
            simple_config, native_daemon=FakeNativeDaemonClient(), auto_ff=False
        )

        assert result.spawned == 1
        assert _disk_pressure_skips("test-i1-oserror") == []

    def test_custom_thresholds_thread_from_config(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """disk_pressure_min_free_inodes/_fraction reach the gate from config."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-I1J", client="test-client"))
        _force_inode_usage(monkeypatch, total_inodes=1_000_000, free_inodes=150_000)
        strict = simple_config.model_copy(
            update={
                "disk_pressure_min_free_inodes": 10_000,
                "disk_pressure_min_free_inode_fraction": 0.2,
            }
        )

        result = dispatch_tick(
            strict, native_daemon=FakeNativeDaemonClient(), auto_ff=False
        )

        assert result.spawned == 0
        skips = _disk_pressure_skips("test-i1-custom")
        assert skips[0].payload["disk_min_free_inodes"] == 200_000
