"""Unit tests for cw.reconcile.leaked_workers (#2480).

Covers the shared detect/stop authority behind both the unconditional
reconcile-pass sweep (``cw.reconcile.core``) and the ``cw doctor --reap``
wedge (``cw.doctor.wedge``): a daemon roster worker whose ``surface_ref``
names a cw session already TERMINAL, or no cw session at all, is a leaked
worker that must be stopped and audited, never counted as occupying a
worktree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.events import read_events
from cw.models import (
    CwState,
    OrchestratorEvent,
    OrchestratorEventType,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.deferred import (
    DeferredReconcileJobs,
    defer_surface_stop,
    run_post_lock_jobs,
)
from cw.reconcile.leaked_workers import (
    LeakedWorker,
    find_leaked_daemon_workers,
    stop_leaked_daemon_worker,
    sweep_leaked_daemon_workers,
)
from tests.conftest import _make_daemon_session


class TestFindLeakedDaemonWorkers:
    def test_roster_unreadable_returns_none(self) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True

        assert find_leaked_daemon_workers(CwState(), daemon=daemon) is None

    def test_empty_roster_returns_empty_list(self) -> None:
        daemon = FakeNativeDaemonClient()

        assert find_leaked_daemon_workers(CwState(), daemon=daemon) == []

    def test_worker_matching_non_terminal_session_is_not_leaked(
        self, tmp_path: Path
    ) -> None:
        """An idle/active/backgrounded session still vouches for its worker."""
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.IDLE, surface_ref=short_id
                )
            ]
        )

        assert find_leaked_daemon_workers(state, daemon=daemon) == []

    def test_worker_matching_completed_session_is_leaked(self, tmp_path: Path) -> None:
        """#2480: the core bug -- a finished worker's roster entry outlives
        its session."""
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1", status=SessionStatus.COMPLETED, surface_ref=short_id
        )
        state = CwState(sessions=[session])

        leaked = find_leaked_daemon_workers(state, daemon=daemon)

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", session)]

    def test_worker_matching_timed_out_session_is_leaked(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")
        session = _make_daemon_session(
            id="s1", status=SessionStatus.TIMED_OUT, surface_ref=short_id
        )
        state = CwState(sessions=[session])

        leaked = find_leaked_daemon_workers(state, daemon=daemon)

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", session)]

    def test_worker_with_no_matching_session_is_leaked(self, tmp_path: Path) -> None:
        """A roster worker with no cw session at all (never tracked, or the
        record is gone) is leaked too, with ``session=None``."""
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(tmp_path / "wt")

        leaked = find_leaked_daemon_workers(CwState(), daemon=daemon)

        assert leaked == [LeakedWorker(short_id, tmp_path / "wt", None)]

    def test_mixed_roster_only_reports_the_leaked_entries(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        live_id = daemon.seed_live_worker(tmp_path / "live")
        leaked_id = daemon.seed_live_worker(tmp_path / "leaked")
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

        leaked = find_leaked_daemon_workers(state, daemon=daemon)

        assert leaked is not None
        assert [w.short_id for w in leaked] == [leaked_id]


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

        stopped = sweep_leaked_daemon_workers(state, daemon=daemon)

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

        assert sweep_leaked_daemon_workers(state, daemon=daemon) == []
        assert daemon.stop_calls == []

    def test_no_op_when_roster_unreadable(self, tmp_config_dir: Path) -> None:
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True

        assert sweep_leaked_daemon_workers(CwState(), daemon=daemon) == []
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
        second = daemon.seed_live_worker(tmp_path / "second")
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

        found = sweep_leaked_daemon_workers(state, daemon=daemon, deferred=sink)

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
            sweep_leaked_daemon_workers(CwState(), daemon=daemon, deferred=sink) == []
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
        other = daemon.seed_live_worker(tmp_path / "other")
        state = CwState(
            sessions=[
                _make_daemon_session(
                    id="s1", status=SessionStatus.COMPLETED, surface_ref=already
                )
            ]
        )
        sink = DeferredReconcileJobs()
        defer_surface_stop(sink, already)

        found = sweep_leaked_daemon_workers(state, daemon=daemon, deferred=sink)

        assert sorted(found) == sorted([already, other])
        assert [job.label for job in sink.post_lock] == [
            f"surface_stop:{already}",
            f"leaked_worker_stop:{other}",
        ]

        run_post_lock_jobs(sink)

        assert daemon.stop_calls == [already, other]
        events = _leaked_audit_events("test_deferred_sweep_skip")
        assert [e.payload["short_id"] for e in events] == [other]
