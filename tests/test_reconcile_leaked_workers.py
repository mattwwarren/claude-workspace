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

from cw.events import read_events
from cw.models import CwState, OrchestratorEventType, SessionStatus
from cw.native_daemon import FakeNativeDaemonClient
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
