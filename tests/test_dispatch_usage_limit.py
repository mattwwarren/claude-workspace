"""Per-client usage-limit back-off windows (#1409 review round 1).

Before this ticket ``dispatch_state.json`` held ONE ``usage_limited_until``
scalar that gated every client in the fleet. That was tolerable while the
window was a flat ``usage_limit_backoff_seconds`` (default 3600s, re-evaluated
every tick, so other clients got periodic retries) — but writing a real parsed
reset into it turns a leaky one-hour block into a solid multi-hour lockout for
clients that never hit a limit.

The key is now the client name. Everything here exercises that: the gate skips
only the named client, the sidecar round-trips a mapping, a pre-#1409 scalar on
disk loads without crashing, and the loop arms (and audits) one window per
client.

Kept out of ``tests/test_dispatch.py`` deliberately — that file is already
~15k lines. Fixtures mirror ``tests/test_dispatch_host_capacity.py``, which
made the same split for the same reason.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw.dispatch.loop
from cw.dev_queue import add_ticket
from cw.dispatch import DispatchTickResult, dispatch_tick
from cw.dispatch.loop import run_dispatch_loop
from cw.dispatch_state import load_usage_limited_until, save_usage_limited_until
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient

if TYPE_CHECKING:
    from collections.abc import Callable

_CLIENTS = ("client-a", "client-b", "client-c")


@pytest.fixture
def tmp_dispatch_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; state isolation is handled by the autouse fixture."""
    return tmp_config_dir


@pytest.fixture
def fleet(
    make_git_repo: Callable[[str], Path], tmp_path: Path
) -> dict[str, ClientConfig]:
    """Three independent clients, each with its own real git workspace."""
    return {
        name: ClientConfig(
            name=name,
            workspace_path=make_git_repo(f"workspace/{name}"),
            default_branch="main",
            worktree_base=tmp_path / "worktrees" / name,
        )
        for name in _CLIENTS
    }


@pytest.fixture
def fleet_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel=dict.fromkeys(_CLIENTS, 1),
    )


def _make_clients_yaml(tmp_path: Path, *clients: ClientConfig) -> None:
    """Write a minimal clients.yaml for the given clients."""
    config_dir = tmp_path / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["clients:\n"]
    for client in clients:
        lines.append(f"  {client.name}:\n")
        lines.append(f"    workspace_path: {client.workspace_path}\n")
        lines.append(f"    default_branch: {client.default_branch}\n")
        if client.worktree_base is not None:
            lines.append(f"    worktree_base: {client.worktree_base}\n")
    (config_dir / "clients.yaml").write_text("".join(lines))


def _usage_limited_skips() -> set[str]:
    """Client names that got a ``skip_reason=usage_limited`` tick event."""
    return {
        event.payload["client"]
        for event in read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])
        if event.payload.get("skip_reason") == DispatchSkipReason.USAGE_LIMITED
    }


class TestPerClientGate:
    """One client's window must not park the rest of the fleet."""

    def test_one_limited_client_does_not_block_the_others(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        for name in _CLIENTS:
            add_ticket(TicketTask(ticket_id=f"GEN-{name}", client=name))
        future = datetime.now(UTC) + timedelta(hours=4)

        result = dispatch_tick(
            fleet_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until={"client-a": future},
        )

        assert result.spawned == 2
        assert _usage_limited_skips() == {"client-a"}

    def test_limited_client_resumes_once_its_window_lapses(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        add_ticket(TicketTask(ticket_id="GEN-lapsed", client="client-a"))
        past = datetime.now(UTC) - timedelta(seconds=1)

        result = dispatch_tick(
            fleet_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until={"client-a": past},
        )

        assert result.spawned == 1
        assert _usage_limited_skips() == set()

    def test_whole_fleet_limited_still_reports_no_fresh_detection(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
    ) -> None:
        """Every client limited reproduces the pre-#1409 early return exactly.

        ``usage_limit_detected`` stays False so the loop does not re-arm (and
        thereby extend) a window that is merely still open.
        """
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        for name in _CLIENTS:
            add_ticket(TicketTask(ticket_id=f"GEN-all-{name}", client=name))
        future = datetime.now(UTC) + timedelta(hours=4)

        result = dispatch_tick(
            fleet_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until=dict.fromkeys(_CLIENTS, future),
        )

        assert result.spawned == 0
        assert result.usage_limit_detected is False
        assert result.usage_limit_clients == {}
        assert _usage_limited_skips() == set(_CLIENTS)

    def test_spawn_detection_is_keyed_to_the_client_that_hit_it(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        add_ticket(TicketTask(ticket_id="GEN-detect", client="client-b"))
        reset_at = datetime.now(UTC) + timedelta(hours=3)
        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True
        daemon.usage_limit_reset_at = reset_at

        result = dispatch_tick(fleet_config, native_daemon=daemon)

        assert result.usage_limit_detected is True
        assert result.usage_limit_clients == {"client-b": reset_at}

    def test_reconcile_detection_covers_every_known_client_flat(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transcript-derived limit names no client, so it arms the fleet.

        None per client means "no parsed reset" — the loop resolves each to the
        flat ``usage_limit_backoff_seconds``, which is what the pre-#1409
        single scalar did on this path.
        """
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        add_ticket(TicketTask(ticket_id="GEN-reconcile", client="client-a"))
        monkeypatch.setattr("cw.dispatch.tick._reconcile_usage_limited", lambda: True)

        result = dispatch_tick(fleet_config, native_daemon=FakeNativeDaemonClient())

        assert result.usage_limit_detected is True
        assert result.usage_limit_clients == dict.fromkeys(_CLIENTS, None)


class TestSidecarShape:
    """``dispatch_state.json``'s ``usage_limited_until`` is a mapping now."""

    def test_round_trips_per_client_windows(self, tmp_config_dir: Path) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        later = datetime.now(UTC) + timedelta(hours=5)

        save_usage_limited_until({"client-a": future, "client-b": later})

        assert load_usage_limited_until() == {"client-a": future, "client-b": later}

    def test_lapsed_entries_are_dropped_per_client(self, tmp_config_dir: Path) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        past = datetime.now(UTC) - timedelta(hours=1)

        save_usage_limited_until({"client-a": past, "client-b": future})

        assert load_usage_limited_until() == {"client-b": future}

    def test_empty_mapping_clears_every_window(self, tmp_config_dir: Path) -> None:
        save_usage_limited_until({"client-a": datetime.now(UTC) + timedelta(hours=1)})
        save_usage_limited_until({})

        assert load_usage_limited_until() == {}

    def test_legacy_scalar_on_disk_loads_without_crashing(
        self, tmp_config_dir: Path
    ) -> None:
        """A pre-#1409 sidecar is dropped, not fanned out — and never raises.

        Dropping keeps this module free of any notion of what clients exist,
        and costs at most one tick: the next spawn re-hits the limit and re-arms
        the window under the name of the client that actually hit it.
        """
        import json

        import cw.dispatch_state

        sidecar = cw.dispatch_state.DISPATCH_STATE_FILE
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            json.dumps({"usage_limited_until": "2099-01-01T00:00:00+00:00"})
        )

        assert load_usage_limited_until() == {}

    def test_malformed_entries_are_skipped_not_fatal(
        self, tmp_config_dir: Path
    ) -> None:
        import json

        import cw.dispatch_state

        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        sidecar = cw.dispatch_state.DISPATCH_STATE_FILE
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            json.dumps(
                {
                    "usage_limited_until": {
                        "client-a": "not-a-timestamp",
                        "client-b": 12345,
                        # Naive: the sidecar compares against an aware "now",
                        # so an offset-less value has no defined meaning here.
                        "client-c": "2099-01-01T00:00:00",
                        "client-d": future,
                    }
                }
            )
        )

        assert load_usage_limited_until() == {
            "client-d": datetime.fromisoformat(future)
        }


class TestMergeAndArm:
    """Loop-side helpers: merge never shortens, arming is per client."""

    def test_merge_takes_the_later_window_per_client(
        self, tmp_config_dir: Path
    ) -> None:
        from cw.dispatch.loop import _merge_persisted_usage_limited_until

        now = datetime.now(UTC)
        save_usage_limited_until(
            {"client-a": now + timedelta(hours=5), "client-c": now + timedelta(hours=2)}
        )

        merged = _merge_persisted_usage_limited_until(
            {"client-a": now + timedelta(hours=1), "client-b": now + timedelta(hours=3)}
        )

        assert merged == {
            "client-a": now + timedelta(hours=5),
            "client-b": now + timedelta(hours=3),
            "client-c": now + timedelta(hours=2),
        }

    def test_window_is_active_when_any_client_is_limited(self) -> None:
        from cw.dispatch.loop import _usage_limit_window_is_active

        now = datetime.now(UTC)

        assert _usage_limit_window_is_active({}) is False
        assert _usage_limit_window_is_active({"a": now - timedelta(1)}) is False
        assert (
            _usage_limit_window_is_active(
                {"a": now - timedelta(1), "b": now + timedelta(1)}
            )
            is True
        )

    def test_arm_resolves_each_client_independently(self, tmp_events_dir: Path) -> None:
        """Parsed reset for one client, flat fallback for the other.

        Also pins the 7-day clamp per client: an implausibly distant reset
        falls back to the flat window rather than parking that client for a
        month.
        """
        from cw.dispatch.loop import _arm_usage_limit_windows

        now = datetime.now(UTC)
        parsed = now + timedelta(hours=3)

        armed = _arm_usage_limit_windows(
            {"client-z": now + timedelta(minutes=5)},
            {
                "client-a": parsed,
                "client-b": None,
                "client-c": now + timedelta(days=30),
            },
            backoff_seconds=3600,
        )

        assert armed["client-a"] == parsed
        assert armed["client-b"] > now
        assert armed["client-b"] < now + timedelta(seconds=3601)
        assert armed["client-c"] < now + timedelta(seconds=3601)
        # Untouched clients keep their existing window.
        assert armed["client-z"] == now + timedelta(minutes=5)


class TestUsageLimitArmedEvent:
    """#1409 review round 1: the set side needs a durable audit record too."""

    def test_emits_one_event_per_armed_client(self, tmp_events_dir: Path) -> None:
        from cw.dispatch.loop import _arm_usage_limit_windows

        now = datetime.now(UTC)
        parsed = now + timedelta(hours=2)

        _arm_usage_limit_windows(
            {}, {"client-a": parsed, "client-b": None}, backoff_seconds=3600
        )

        events = read_events(event_types=[OrchestratorEventType.USAGE_LIMIT_ARMED])
        by_client = {event.payload["client"]: event.payload for event in events}

        assert set(by_client) == {"client-a", "client-b"}
        assert by_client["client-a"]["until"] == parsed.isoformat()
        assert by_client["client-a"]["source"] == "parsed_reset"
        assert by_client["client-b"]["source"] == "flat_backoff"

    def test_event_type_is_documented(self) -> None:
        """docs/events.md is the operator-facing contract for the bus."""
        docs = Path(__file__).resolve().parents[1] / "docs" / "events.md"

        assert (
            f"### `{OrchestratorEventType.USAGE_LIMIT_ARMED.value}`" in docs.read_text()
        )


class TestConcurrentWriterSurvivesTheSave:
    """#1409 review round 3: closes the merge-before-save gap.

    ``run_dispatch_loop`` only merges the on-disk sidecar once, at the TOP of
    each tick (#1346). The arm-and-save block runs AFTER the tick, so a
    second writer (``--force``, #1362) landing a different client's window in
    that gap used to be erased outright: ``save_usage_limited_until`` persists
    the whole in-memory mapping, which never saw the concurrent write.
    """

    def test_second_writer_window_is_not_erased(
        self,
        tmp_dispatch_dirs: Path,
        fleet: dict[str, ClientConfig],
        fleet_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_clients_yaml(tmp_dispatch_dirs, *fleet.values())
        add_ticket(TicketTask(ticket_id="GEN-race", client="client-a"))

        daemon = FakeNativeDaemonClient()
        daemon.raise_usage_limit = True
        reset_at = datetime.now(UTC) + timedelta(hours=2)
        daemon.usage_limit_reset_at = reset_at
        concurrent_until = datetime.now(UTC) + timedelta(hours=5)

        call_count = 0
        original_tick = cw.dispatch.loop.dispatch_tick

        def racing_tick(*args: object, **kwargs: object) -> DispatchTickResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                result = original_tick(*args, **kwargs)
                # A second `cw --force` process arms client-b's window
                # AFTER this loop's tick-start merge already ran, but
                # BEFORE this loop's own post-tick save below.
                save_usage_limited_until({"client-b": concurrent_until})
                daemon.raise_usage_limit = False
                return result
            raise KeyboardInterrupt

        monkeypatch.setattr("cw.dispatch.loop.dispatch_tick", racing_tick)
        monkeypatch.setattr("cw.dispatch.loop.time.sleep", lambda _: None)

        with contextlib.suppress(KeyboardInterrupt):
            run_dispatch_loop(native_daemon=daemon)

        on_disk = load_usage_limited_until()
        assert on_disk["client-a"] == reset_at
        assert on_disk["client-b"] == concurrent_until
