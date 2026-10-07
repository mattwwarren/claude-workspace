"""SSH-agent-key preflight gate tests (#927).

Covers ``cw.dispatch.gating``'s SSH-key family: the per-tick probe, the
push-remote-scheme keying (#1495) and the gate bypass (#1437). Split
out of ``tests/test_dispatch.py`` (#2503).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.dev_queue import (
    add_ticket,
    load_dev_queue,
)
from cw.dispatch import dispatch_tick
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._clients_yaml import write_clients_yaml
from tests._dispatch_gating_helpers import (
    _force_gh_unavailable,
    _force_ssh_key_unavailable,
)

# ---------------------------------------------------------------------------
# TestSshKeyPreflightGate (#927)
# ---------------------------------------------------------------------------


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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
    ) -> None:
        """When the probe reports available, dispatch proceeds as usual."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1A", client="test-client"))

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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gated client emits dispatch.tick with skip_reason=ssh_key_gate."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator error line is deduplicated across ticks in one run."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both probes forced unavailable: AVAILABILITY_GATE wins, not SSH_KEY_GATE."""
        write_clients_yaml(sample_client_config)
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
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSH unavailable + stale repo: SSH_KEY_GATE wins over FRESHNESS_GATE."""
        write_clients_yaml(sample_client_config)
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

    def test_gate_disabled_bypasses_skip_and_emits_bypass_event(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1437: ssh_key_gate_enabled=False bypasses the probe-failure
        skip — the client dispatches normally, an SSH_KEY_GATE_BYPASSED event
        is recorded, and no dispatch.tick SSH_KEY_GATE skip is recorded."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1G", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)

        bypass_config = simple_config.model_copy(update={"ssh_key_gate_enabled": False})
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(bypass_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

        bypass_events = read_events(
            consumer="test-s1-bypass",
            event_types=[OrchestratorEventType.SSH_KEY_GATE_BYPASSED],
        )
        assert len(bypass_events) == 1
        assert bypass_events[0].payload["client"] == "test-client"

        tick_events = read_events(
            consumer="test-s1-bypass-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        skip_ticks = [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ]
        assert skip_ticks == []

    def test_http_remote_skips_probe_and_dispatches(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1495: an HTTP(S) push remote never engages the SSH probe.

        Even with the probe forced unavailable, the client dispatches, no
        SSH_KEY_GATE skip is recorded, and the probe is never even called.
        """
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1I", client="test-client"))
        probe_calls: list[bool] = []

        def _probe(**_kw: object) -> bool:
            probe_calls.append(True)
            return False

        monkeypatch.setattr(
            "cw.dispatch.gating.ssh_key.check_ssh_key_available", _probe
        )
        monkeypatch.setattr(
            "cw.dispatch.gating.ssh_key.push_remote_scheme", lambda _p: "http"
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1
        assert probe_calls == []
        tick_events = read_events(
            consumer="test-s1-http-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        assert [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ] == []

    def test_local_remote_skips_probe_and_dispatches(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1495: a local-path push remote is exempt the same way."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1J", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        monkeypatch.setattr(
            "cw.dispatch.gating.ssh_key.push_remote_scheme", lambda _p: "local"
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 1

    def test_unknown_remote_scheme_still_gates(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1495: a scheme-resolution failure keeps the gate fail-closed."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1K", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        monkeypatch.setattr(
            "cw.dispatch.gating.ssh_key.push_remote_scheme", lambda _p: "unknown"
        )

        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0
        tick_events = read_events(
            consumer="test-s1-unknown-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        skips = [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ]
        assert len(skips) == 1
        assert skips[0].payload["remote_scheme"] == "unknown"

    def test_skip_and_bypass_events_record_remote_scheme(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1495: both ssh-gate events name the transport that engaged
        the probe, so a false gate is diagnosable from events alone."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1L", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        daemon = FakeNativeDaemonClient()

        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)
        bypass_config = simple_config.model_copy(update={"ssh_key_gate_enabled": False})
        dispatch_tick(bypass_config, native_daemon=daemon, auto_ff=False)

        skips = [
            e
            for e in read_events(
                consumer="test-s1-scheme-tick",
                event_types=[OrchestratorEventType.DISPATCH_TICK],
            )
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ]
        assert [e.payload["remote_scheme"] for e in skips] == ["ssh"]
        bypasses = read_events(
            consumer="test-s1-scheme-bypass",
            event_types=[OrchestratorEventType.SSH_KEY_GATE_BYPASSED],
        )
        assert [e.payload["remote_scheme"] for e in bypasses] == ["ssh"]

    def test_probe_scope_uses_repo_path_when_set(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1495: the scheme is resolved against the client's repo."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1M", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)
        probed: list[Path] = []

        def _scheme(path: Path) -> str:
            probed.append(path)
            return "http"

        monkeypatch.setattr("cw.dispatch.gating.ssh_key.push_remote_scheme", _scheme)

        daemon = FakeNativeDaemonClient()
        dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        expected = sample_client_config.repo_path or sample_client_config.workspace_path
        assert probed == [expected]

    def test_gate_enforced_by_default_still_skips_and_no_bypass_event(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1437: default (ssh_key_gate_enabled=True) is unchanged —
        client still skipped, SSH_KEY_GATE skip still recorded, and the new
        bypass event is NOT recorded."""
        write_clients_yaml(sample_client_config)
        add_ticket(TicketTask(ticket_id="GEN-S1H", client="test-client"))
        _force_ssh_key_unavailable(monkeypatch)

        assert simple_config.ssh_key_gate_enabled is True
        daemon = FakeNativeDaemonClient()
        result = dispatch_tick(simple_config, native_daemon=daemon, auto_ff=False)

        assert result.spawned == 0

        tick_events = read_events(
            consumer="test-s1-enforced-tick",
            event_types=[OrchestratorEventType.DISPATCH_TICK],
        )
        skip_ticks = [
            e
            for e in tick_events
            if e.payload.get("skip_reason") == DispatchSkipReason.SSH_KEY_GATE
        ]
        assert len(skip_ticks) == 1

        bypass_events = read_events(
            consumer="test-s1-enforced-bypass",
            event_types=[OrchestratorEventType.SSH_KEY_GATE_BYPASSED],
        )
        assert bypass_events == []
