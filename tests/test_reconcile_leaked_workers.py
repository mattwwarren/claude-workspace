"""Unit tests for cw.reconcile.leaked_workers (#2480, #2670).

Covers the shared detect/stop authority behind both the unconditional
reconcile-pass sweep (``cw.reconcile.locked_sweeps``) and the ``cw doctor --reap``
wedge (``cw.doctor.wedge``). A daemon roster worker is leaked when:

- its ``surface_ref`` names a cw session already TERMINAL (unconditional), or
- no cw session names it AND it is evidenced orphaned (#2670): older than
  ``LEAKED_WORKER_GRACE_SECONDS`` and homed strictly inside a configured
  client's cw worktree base.

Everything else is spared, so a ``spawn_bg`` worker that cw has not recorded
yet, or a hand-launched non-cw worker, is never stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.events import read_events
from cw.models import (
    CwState,
    OrchestratorEvent,
    OrchestratorEventType,
    SessionStatus,
)
from cw.models.client import ClientConfig
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import SPAWN_GRACE_SECONDS
from cw.reconcile.deferred import (
    _CARRYOVER,
    DeferredReconcileJobs,
    PostLockJob,
    StopKind,
    defer_surface_stop,
    run_post_lock_jobs,
)
from cw.reconcile.fix_dispatch_hold import _ATTENTION_GRACE
from cw.reconcile.leaked_workers import (
    LEAKED_WORKER_GRACE_SECONDS,
    LeakedWorker,
    find_leaked_daemon_workers,
    stop_leaked_daemon_worker,
    sweep_leaked_daemon_workers,
)
from cw.worktree import effective_worktree_bases, resolve_worktree_base
from tests._reconcile_helpers import ms_of
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_GRACE = LEAKED_WORKER_GRACE_SECONDS
_OLD_MS = ms_of(_NOW) - (_GRACE + 1) * 1000


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """The explicit cw worktree base of the ``client`` fixture."""
    return tmp_path / "worktrees"


@pytest.fixture
def client(tmp_path: Path, base: Path) -> ClientConfig:
    return ClientConfig(
        name="client-a",
        workspace_path=tmp_path / "ws",
        worktree_base=base,
    )


def _clients(client: ClientConfig) -> dict[str, ClientConfig]:
    return {client.name: client}


def _orphan(daemon: FakeNativeDaemonClient, base: Path) -> str:
    """Seed an unrecorded worker at ``base/dev-T-1`` aged past the grace."""
    return daemon.seed_live_worker(base / "dev-T-1", started_at_ms=_OLD_MS)


def _over(tmp_path: Path) -> dict[str, ClientConfig]:
    """A client whose worktree base is *tmp_path*, so every ``tmp_path/x``
    worker is inside a cw base for the absent arm's evidence."""
    return {
        "client-a": ClientConfig(
            name="client-a", workspace_path=tmp_path / "ws", worktree_base=tmp_path
        )
    }


def _find(
    state: CwState,
    daemon: FakeNativeDaemonClient,
    clients: Mapping[str, ClientConfig],
    now: datetime = _NOW,
) -> list[LeakedWorker] | None:
    return find_leaked_daemon_workers(state, daemon=daemon, now=now, clients=clients)


class TestFindLeakedDaemonWorkers:
    def test_roster_unreadable_returns_none(self, client: ClientConfig) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True

        assert _find(CwState(), daemon, _clients(client)) is None

    def test_empty_roster_returns_empty_list(self, client: ClientConfig) -> None:
        daemon = FakeNativeDaemonClient()

        assert _find(CwState(), daemon, _clients(client)) == []

    def test_worker_matching_non_terminal_session_is_not_leaked(
        self, base: Path, client: ClientConfig
    ) -> None:
        """An idle/active/backgrounded session still vouches for its worker."""
        daemon = FakeNativeDaemonClient()
        short_id = _orphan(daemon, base)
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.IDLE, surface_ref=short_id
                )
            ]
        )

        assert _find(state, daemon, _clients(client)) == []

    def test_worker_matching_completed_session_is_leaked(self, tmp_path: Path) -> None:
        """#2480: the core bug -- a finished worker's roster entry outlives
        its session. The terminal arm is unconditional: no startedAt, no clients."""
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1", status=SessionStatus.COMPLETED, surface_ref=short_id
        )
        state = CwState(sessions=[session])

        leaked = _find(state, daemon, {})

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", session)]

    def test_worker_matching_timed_out_session_is_leaked(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1", status=SessionStatus.TIMED_OUT, surface_ref=short_id
        )
        state = CwState(sessions=[session])

        leaked = _find(state, daemon, {})

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", session)]

    def test_worker_with_no_matching_session_is_leaked(
        self, base: Path, client: ClientConfig
    ) -> None:
        """A roster worker no cw session names is leaked when evidenced orphaned
        (#2670): old enough and inside a configured worktree base."""
        daemon = FakeNativeDaemonClient()
        short_id = _orphan(daemon, base)

        leaked = _find(CwState(), daemon, _clients(client))

        assert leaked == [LeakedWorker(short_id, base / "dev-T-1", None)]

    def test_mixed_roster_only_reports_the_leaked_entries(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        live_id = _orphan(daemon, base)
        leaked_id = daemon.seed_live_worker(base / "leaked", started_at_ms=_OLD_MS)
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.ACTIVE, surface_ref=live_id
                ),
                _make_daemon_session(
                    id="s2", status=SessionStatus.COMPLETED, surface_ref=leaked_id
                ),
            ]
        )

        leaked = _find(state, daemon, _clients(client))

        assert leaked is not None
        assert [w.short_id for w in leaked] == [leaked_id]


class TestUnrecordedWorkerEvidence:
    """#2670: the absent arm needs age and a cw-worktree cwd as evidence."""

    def test_spawn_race_worker_survives_inside_grace(
        self, base: Path, client: ClientConfig
    ) -> None:
        """A worker spawn_bg'd but not yet recorded is spared inside the grace,
        and stopped once the grace has passed."""
        daemon = FakeNativeDaemonClient()
        clients = _clients(client)
        spawned_at = _NOW
        with freeze_time(spawned_at):
            daemon.spawn_bg(cwd=base / "dev-T-1", prompt="p")

        just_inside = spawned_at + timedelta(seconds=_GRACE - 1)
        with freeze_time(just_inside):
            assert _find(CwState(), daemon, clients, now=just_inside) == []
            assert (
                sweep_leaked_daemon_workers(
                    CwState(), daemon=daemon, now=just_inside, clients=clients
                )
                == []
            )
        assert daemon.stop_calls == []

        past_grace = spawned_at + timedelta(seconds=_GRACE)
        with freeze_time(past_grace):
            assert (
                sweep_leaked_daemon_workers(
                    CwState(), daemon=daemon, now=past_grace, clients=clients
                )
                != []
            )
        assert len(daemon.stop_calls) == 1

    def test_non_cw_cwd_is_spared_however_old(
        self, tmp_path: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        elsewhere = tmp_path / "elsewhere" / "x"
        daemon.seed_live_worker(
            elsewhere, started_at_ms=ms_of(_NOW) - _GRACE * 10 * 1000
        )

        assert _find(CwState(), daemon, _clients(client)) == []
        assert (
            sweep_leaked_daemon_workers(
                CwState(), daemon=daemon, now=_NOW, clients=_clients(client)
            )
            == []
        )
        assert daemon.stop_calls == []

    def test_genuine_leak_is_stopped_and_audited(
        self, tmp_config_dir: Path, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = _orphan(daemon, base)

        found = sweep_leaked_daemon_workers(
            CwState(), daemon=daemon, now=_NOW, clients=_clients(client)
        )

        assert found == [short_id]
        assert daemon.stop_calls == [short_id]
        events = read_events(
            consumer="test_genuine_orphan_audited",
            event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
        )
        assert len(events) == 1
        assert events[0].payload["session_id"] is None

    @pytest.mark.parametrize(
        ("age_ms", "stopped"),
        [
            (_GRACE * 1000 - 1, False),
            (_GRACE * 1000, True),
            ((_GRACE + 1) * 1000, True),
        ],
    )
    def test_grace_boundary_is_inclusive_at_the_threshold(
        self, base: Path, client: ClientConfig, age_ms: int, stopped: bool
    ) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(base / "dev-T-1", started_at_ms=ms_of(_NOW) - age_ms)

        leaked = _find(CwState(), daemon, _clients(client))

        assert (leaked != []) is stopped

    def test_missing_started_at_spares_the_worker(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(base / "dev-T-1")

        far = _NOW + timedelta(days=3650)
        assert _find(CwState(), daemon, _clients(client), now=far) == []

    def test_empty_clients_spare_every_unrecorded_worker(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        _orphan(daemon, base)

        assert _find(CwState(), daemon, {}) == []

    def test_terminal_session_is_still_found_with_empty_clients(
        self, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1", status=SessionStatus.COMPLETED, surface_ref=short_id
        )

        leaked = _find(CwState(sessions=[session]), daemon, {})

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", session)]

    def test_cwd_equal_to_base_and_sibling_prefix_are_spared(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(base, started_at_ms=_OLD_MS)
        daemon.seed_live_worker(
            base.parent / "worktrees-evil" / "x", started_at_ms=_OLD_MS
        )

        assert _find(CwState(), daemon, _clients(client)) == []

    def test_hashed_default_base_is_evidenced(self, tmp_path: Path) -> None:
        """A client without ``worktree_base`` is evidenced under its hashed
        sibling base as well as its resolved default."""
        client = ClientConfig(name="client-h", workspace_path=tmp_path / "ws")
        primary = resolve_worktree_base(client)
        hashed = next(b for b in effective_worktree_bases(client) if b != primary)
        daemon = FakeNativeDaemonClient()
        under_hashed = daemon.seed_live_worker(
            hashed / "dev-T-1", started_at_ms=_OLD_MS
        )

        leaked = _find(CwState(), daemon, _clients(client))

        assert leaked is not None
        assert [w.short_id for w in leaked] == [under_hashed]

    def test_union_of_all_clients_bases_is_used(
        self, tmp_path: Path, base: Path, client: ClientConfig
    ) -> None:
        other_base = tmp_path / "worktrees-b"
        other = ClientConfig(
            name="client-b", workspace_path=tmp_path / "ws-b", worktree_base=other_base
        )
        daemon = FakeNativeDaemonClient()
        under_b = daemon.seed_live_worker(other_base / "dev-T-1", started_at_ms=_OLD_MS)
        daemon.seed_live_worker(tmp_path / "neither" / "x", started_at_ms=_OLD_MS)
        clients = {client.name: client, other.name: other}

        leaked = _find(CwState(), daemon, clients)

        assert leaked is not None
        assert [w.short_id for w in leaked] == [under_b]

    def test_started_at_is_epoch_milliseconds_and_tz_invariant(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(base / "young", started_at_ms=ms_of(_NOW) - 10_000)
        old = daemon.seed_live_worker(base / "old", started_at_ms=ms_of(_NOW) - 200_000)
        pacific = _NOW.astimezone(timezone(timedelta(hours=-8)))

        utc = _find(CwState(), daemon, _clients(client))
        local = _find(CwState(), daemon, _clients(client), now=pacific)

        assert utc == local
        assert utc is not None
        assert [w.short_id for w in utc] == [old]

    def test_naive_now_is_rejected(self, client: ClientConfig) -> None:
        daemon = FakeNativeDaemonClient()
        with pytest.raises(ValueError, match="timezone-aware"):
            _find(CwState(), daemon, _clients(client), now=_NOW.replace(tzinfo=None))

    def test_recorded_non_terminal_session_protects_an_old_in_base_worker(
        self, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = _orphan(daemon, base)
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.BACKGROUNDED, surface_ref=short_id
                )
            ]
        )

        assert _find(state, daemon, _clients(client)) == []

    def test_mixed_roster_stops_terminal_and_genuine_orphan_only(
        self, tmp_config_dir: Path, base: Path, client: ClientConfig, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        live = daemon.seed_live_worker(base / "live", started_at_ms=_OLD_MS)
        terminal = daemon.seed_live_worker(tmp_path / "term")
        daemon.seed_live_worker(base / "young", started_at_ms=ms_of(_NOW) - 5_000)
        daemon.seed_live_worker(tmp_path / "foreign", started_at_ms=_OLD_MS)
        orphan = _orphan(daemon, base)
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.ACTIVE, surface_ref=live
                ),
                _make_daemon_session(
                    id="s2", status=SessionStatus.COMPLETED, surface_ref=terminal
                ),
            ]
        )

        found = sweep_leaked_daemon_workers(
            state, daemon=daemon, now=_NOW, clients=_clients(client)
        )

        assert sorted(found) == sorted([terminal, orphan])

    def test_genuine_orphan_queues_one_job_and_young_queues_none(
        self, tmp_config_dir: Path, base: Path, client: ClientConfig
    ) -> None:
        daemon = FakeNativeDaemonClient()
        orphan = _orphan(daemon, base)
        daemon.seed_live_worker(base / "young", started_at_ms=ms_of(_NOW) - 5_000)
        sink = DeferredReconcileJobs()

        sweep_leaked_daemon_workers(
            CwState(),
            daemon=daemon,
            now=_NOW,
            clients=_clients(client),
            deferred=sink,
        )

        assert [job.label for job in sink.post_lock] == [f"leaked_worker_stop:{orphan}"]

    def test_constant_ordering_pins_the_grace(self) -> None:
        assert SPAWN_GRACE_SECONDS < _GRACE < _ATTENTION_GRACE.total_seconds()
        assert _GRACE > 10.0  # _ROSTER_POLL_TIMEOUT_SECS, the spawn-to-record poll


class TestStopLeakedDaemonWorker:
    def test_stops_and_emits_audit_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1",
            name="client-a/auto-dev/GEN-1",
            status=SessionStatus.COMPLETED,
            surface_ref=short_id,
        )
        worker = LeakedWorker(short_id, tmp_path / "wt", session)

        stop_leaked_daemon_worker(worker, daemon=daemon)

        assert daemon.stop_calls == [short_id]
        events = read_events(
            consumer="test_stop_leaked_worker_emits",
            event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["short_id"] == short_id
        assert payload["cwd"] == str(tmp_path / "wt")
        assert payload["session_id"] == "s1"
        assert payload["session_status"] == "completed"
        assert payload["ticket_id"] == "GEN-1"
        assert events[0].correlation_id == "GEN-1"

    def test_stops_and_emits_for_a_sessionless_worker(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        worker = LeakedWorker(short_id, tmp_path / "wt", None)

        stop_leaked_daemon_worker(worker, daemon=daemon)

        assert daemon.stop_calls == [short_id]
        events = read_events(
            consumer="test_stop_leaked_worker_sessionless",
            event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] is None
        assert payload["session_status"] is None
        assert payload["ticket_id"] is None


class TestSweepLeakedDaemonWorkers:
    def test_stops_every_leaked_worker_and_returns_their_ids(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        leaked_id = daemon.seed_live_worker(tmp_path / "leaked")
        live_id = daemon.seed_live_worker(tmp_path / "live")
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.COMPLETED, surface_ref=leaked_id
                ),
                _make_daemon_session(
                    id="s2", status=SessionStatus.ACTIVE, surface_ref=live_id
                ),
            ]
        )

        stopped = sweep_leaked_daemon_workers(
            state, daemon=daemon, now=_NOW, clients={}
        )

        assert stopped == [leaked_id]
        assert daemon.stop_calls == [leaked_id]
        assert live_id in daemon.list_live_session_short_ids()

    def test_no_op_when_nothing_leaked(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        live_id = daemon.seed_live_worker(tmp_path / "live")
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.ACTIVE, surface_ref=live_id
                )
            ]
        )

        assert (
            sweep_leaked_daemon_workers(state, daemon=daemon, now=_NOW, clients={})
            == []
        )
        assert daemon.stop_calls == []

    def test_no_op_when_roster_unreadable(self, tmp_config_dir: Path) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True

        assert (
            sweep_leaked_daemon_workers(CwState(), daemon=daemon, now=_NOW, clients={})
            == []
        )
        assert daemon.stop_calls == []


def _leaked_audit_events(consumer: str) -> list[OrchestratorEvent]:
    return read_events(
        consumer=consumer,
        event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
    )


class TestSweepLeakedDaemonWorkersDeferred:
    """#1232: with reconcile()'s post-lock sink the sweep queues each
    stop-and-audit instead of running it under sessions_lock."""

    def test_queues_one_job_per_leaked_worker_and_runs_it_at_the_drain(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        first = daemon.seed_live_worker(tmp_path / "first")
        second = daemon.seed_live_worker(tmp_path / "second", started_at_ms=_OLD_MS)
        live_id = daemon.seed_live_worker(tmp_path / "live")
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1",
                    name="client-a/auto-dev/GEN-1",
                    status=SessionStatus.COMPLETED,
                    surface_ref=first,
                ),
                _make_daemon_session(
                    id="s2", status=SessionStatus.ACTIVE, surface_ref=live_id
                ),
            ]
        )
        sink = DeferredReconcileJobs()

        found = sweep_leaked_daemon_workers(
            state, daemon=daemon, now=_NOW, clients=_over(tmp_path), deferred=sink
        )

        assert sorted(found) == sorted([first, second])
        assert sorted(job.label for job in sink.post_lock) == sorted(
            [f"leaked_worker_stop:{first}", f"leaked_worker_stop:{second}"]
        )
        assert daemon.stop_calls == []
        assert _leaked_audit_events("test_deferred_sweep_before_drain") == []

        run_post_lock_jobs(sink)

        assert sorted(daemon.stop_calls) == sorted([first, second])
        events = _leaked_audit_events("test_deferred_sweep_after_drain")
        assert sorted(str(e.payload["short_id"]) for e in events) == sorted(
            [first, second]
        )
        assert live_id in daemon.list_live_session_short_ids()

    def test_unreadable_roster_queues_nothing(self, tmp_config_dir: Path) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True
        sink = DeferredReconcileJobs()

        assert (
            sweep_leaked_daemon_workers(
                CwState(), daemon=daemon, now=_NOW, clients={}, deferred=sink
            )
            == []
        )
        assert sink.post_lock == []

    def test_skips_a_worker_whose_surface_stop_is_already_queued(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stalled sweep runs first in the same tick and queues a stop for
        every session it completes; the leaked sweep must not stop that worker
        a second time nor audit it as leaked. It is still reported found."""
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        already = daemon.seed_live_worker(tmp_path / "already")
        other = daemon.seed_live_worker(tmp_path / "other", started_at_ms=_OLD_MS)
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.COMPLETED, surface_ref=already
                )
            ]
        )
        sink = DeferredReconcileJobs()
        defer_surface_stop(sink, already)

        found = sweep_leaked_daemon_workers(
            state, daemon=daemon, now=_NOW, clients=_over(tmp_path), deferred=sink
        )

        assert sorted(found) == sorted([already, other])
        assert [job.label for job in sink.post_lock] == [
            f"surface_stop:{already}",
            f"leaked_worker_stop:{other}",
        ]

        run_post_lock_jobs(sink)

        assert daemon.stop_calls == [already, other]
        events = _leaked_audit_events("test_deferred_sweep_skip")
        assert [e.payload["short_id"] for e in events] == [other]


class TestSweepJobsAreCappedStops:
    """#2551: leaked-worker stops are RECOVERABLE stops, so they count toward
    the per-tick cap (an untagged job would bypass the bound) and a surplus
    one is dropped because the next sweep re-queues it."""

    def test_sink_jobs_are_recoverable_stops(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(tmp_path / "leaked", started_at_ms=_OLD_MS)
        sink = DeferredReconcileJobs()

        sweep_leaked_daemon_workers(
            CwState(), daemon=daemon, now=_NOW, clients=_over(tmp_path), deferred=sink
        )

        assert [job.stop for job in sink.post_lock] == [StopKind.RECOVERABLE]

    def test_sweep_skips_a_worker_whose_stop_is_carried_over(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        worker = daemon.seed_live_worker(tmp_path / "leaked", started_at_ms=_OLD_MS)
        _CARRYOVER.put(
            [
                PostLockJob(
                    label=f"surface_stop:{worker}",
                    run=lambda: None,
                    stop=StopKind.CARRIED,
                )
            ]
        )
        sink = DeferredReconcileJobs()

        found = sweep_leaked_daemon_workers(
            CwState(), daemon=daemon, now=_NOW, clients=_over(tmp_path), deferred=sink
        )

        assert found == [worker]
        assert sink.post_lock == []
