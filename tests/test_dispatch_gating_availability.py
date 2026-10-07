"""Fleet-wide gh-availability preflight gate tests (RFC 0011 A5, #1157).

Covers ``cw.dispatch.gating``'s availability family: the TTL-cached
probe and its edge-triggered outage latch. Split out of
``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import (
    add_ticket,
    load_dev_queue,
)
from cw.dispatch import (
    _AVAILABILITY_OUTAGE_REASON,
    _AVAILABILITY_PROBE_TTL_SECONDS,
    dispatch_tick,
)
from cw.dispatch_state import (
    AvailabilityProbeCache,
    load_availability_probe_cache,
    save_availability_probe_cache,
)
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._dispatch_gating_helpers import _force_gh_unavailable

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# TestAvailabilityPreflightGate (RFC 0011 A5, #1157)
# ---------------------------------------------------------------------------


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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When the probe reports available, dispatch proceeds as usual."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5A", client="test-client"))

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_unavailable_holds_task_pending_no_attempt_consumed(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The core binding requirement: a gated PENDING task keeps attempts=0."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gated client emits dispatch.tick with skip_reason=availability_gate."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first bad probe fires the full fleet-wide attention payload."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cache-hit second tick within the TTL fires no second attention."""
        write_clients_yaml(sample_client_config)
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

        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5F", client="test-client"))

        calls: list[int] = []

        def _counting_unavailable_probe(**_kw: object) -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability",
            _counting_unavailable_probe,
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh success after an outage resets the latch and fires no event."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two ticks within the TTL trigger only one real probe call."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5H", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability", _counting_probe
        )

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 1

    def test_reprobes_after_ttl_expiry(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tick after the TTL window runs a fresh probe."""
        from freezegun import freeze_time

        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5I", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability", _counting_probe
        )

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

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability", _counting_probe
        )

        config = OrchestratorConfig(default_max_parallel=1)
        daemon = FakeNativeDaemonClient()
        dispatch_tick(config, native_daemon=daemon, auto_ff=False)

        assert len(calls) == 1

    def test_resolution_error_fails_open(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any resolution error fails open — dispatch proceeds (no gate)."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5J", client="test-client"))

        def _boom(**_kw: object) -> bool:
            msg = "probe blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability", _boom
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_no_push_notification_on_first_failure_emit(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outage escalation never calls fire_push_notification."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the availability gate is closed, _resolve_freshness is not called.

        Event-ordering alone doesn't prove short-circuit (MF1) — spy the
        freshness resolver and assert it is never invoked on the gated path.
        """
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A FINALIZE-stage PENDING task is also held by the single gate (S2)."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On the gated path neither freshness latch helper runs (S3).

        The freshness counter must stay frozen during an outage, not reset —
        prevents a future reorder from silently clearing real freshness latches.
        """
        write_clients_yaml(sample_client_config)
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
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-A5O", client="test-client"))

        calls: list[int] = []

        def _counting_probe(**_kw: object) -> bool:
            calls.append(1)
            return True

        monkeypatch.setattr(
            "cw.dispatch.gating.availability.check_gh_availability", _counting_probe
        )

        paused_config = simple_config.model_copy(update={"max_parallel_clients": 0})
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(paused_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        assert calls == []
