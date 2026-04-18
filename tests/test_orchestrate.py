"""Tests for cw.orchestrate -- PR retirement and status snapshot."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket
from cw.events import read_events, record_event
from cw.models import (
    CompletionReason,
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.orchestrate import (
    OrchestratorStatus,
    orchestrator_status,
    retire_merged_prs,
)
from cw.pr_responder import PRDispatchRecord, save_dispatch_record

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_orchestrate_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect every orchestrator-relevant path to tmp_path."""
    config_dir = tmp_path / ".config" / "cw"
    state_dir = tmp_path / ".local" / "share" / "cw"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    clients_file = config_dir / "clients.yaml"
    state_file = state_dir / "sessions.json"
    history_dir = state_dir / "history"
    history_dir.mkdir(parents=True)
    queues_dir = state_dir / "queues"
    queues_dir.mkdir(parents=True)
    events_dir = state_dir / "events"
    events_dir.mkdir(parents=True)
    dev_queue_file = state_dir / "dev_queue.json"
    dev_queue_lock = state_dir / ".dev_queue.lock"
    review_monitor_dir = tmp_path / "review-monitor"
    review_monitor_dir.mkdir(parents=True)

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", clients_file)
    monkeypatch.setattr("cw.config.STATE_FILE", state_file)
    monkeypatch.setattr("cw.config.HISTORY_DIR", history_dir)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", dev_queue_lock)
    monkeypatch.setattr("cw.config.REVIEW_MONITOR_DIR", review_monitor_dir)

    monkeypatch.setattr("cw.events.EVENTS_DIR", events_dir)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_FILE", dev_queue_file)
    monkeypatch.setattr("cw.dev_queue.DEV_QUEUE_LOCK", dev_queue_lock)
    monkeypatch.setattr("cw.pr_responder.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.daemon.REVIEW_MONITOR_DIR", review_monitor_dir)
    monkeypatch.setattr("cw.orchestrate.REVIEW_MONITOR_DIR", review_monitor_dir)

    return tmp_path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return a workspace dir for sessions used in tests."""
    ws = tmp_path / "workspace" / "test-project"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
def adapter() -> FakeCmuxAdapter:
    """A fresh FakeCmuxAdapter for each test."""
    return FakeCmuxAdapter()


@pytest.fixture
def captured_runner() -> tuple[
    list[list[str]],
    subprocess.CompletedProcess[str],
]:
    """Provide a runner that records calls and returns success."""
    calls: list[list[str]] = []
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return completed

    # Returning the closure plus the calls list so tests can inspect both.
    return calls, _runner  # type: ignore[return-value]


@pytest.fixture
def fake_runner() -> Iterator[tuple[list[list[str]], object]]:
    """Yield (recorded_calls, runner_callable)."""
    calls: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return calls, _runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str,
    workspace: Path,
    *,
    surface_ref: str | None = "fake-pane-1",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    return Session(
        id=session_id,
        name=f"test-client/fix-ci/{session_id}",
        client="test-client",
        purpose=SessionPurpose.IMPL,
        status=status,
        workspace_path=workspace,
        surface_ref=surface_ref,
        started_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _seed_dispatch_record(
    repo: str,
    pr_number: int,
    role: str,
    session_id: str,
) -> None:
    record = PRDispatchRecord(active={f"{repo}#{pr_number}|{role}": session_id})
    save_dispatch_record(record)


def _seed_pr_merged(repo: str, pr_number: int, *, client: str = "test-client") -> str:
    event = record_event(
        OrchestratorEventType.PR_MERGED,
        {"client": client, "repo": repo, "pr_number": pr_number},
    )
    return event.id


def _write_monitor_file(
    review_monitor_dir: Path,
    repo: str,
    pr_number: int,
    *,
    status: str = "watching",
    role: str = "author",
    thread_status: dict[str, dict[str, bool]] | None = None,
) -> Path:
    pr_key = f"{repo}#{pr_number}"
    pr_data = {
        "role": role,
        "repo": repo,
        "repo_path": "/tmp/some-repo",
        "pr_number": pr_number,
        "status": status,
        "thread_status": thread_status or {},
        "delta_findings": [],
    }
    payload = {"active": {pr_key: pr_data}, "completed": {}}
    filename = repo.replace("/", "--") + ".json"
    path = review_monitor_dir / filename
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---------------------------------------------------------------------------
# Tests: retire_merged_prs
# ---------------------------------------------------------------------------


class TestRetireMergedPRs:
    def test_no_events_returns_empty_list(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """When there are no pr.merged events, retirement is a no-op."""
        _calls, runner = fake_runner
        retired = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]
        assert retired == []
        assert adapter.calls["close"] == []

    def test_retires_correlated_session(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        workspace: Path,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """A pr.merged event marks the correlated session COMPLETED."""
        # Set up state with one ACTIVE session linked via dispatch record.
        sess = _make_session("sess0001", workspace, surface_ref="pane-7")
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 42, "fix-ci", "sess0001")
        _seed_pr_merged("owner/repo", 42)

        calls, runner = fake_runner
        retired = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]

        # Returned list contains the session ID.
        assert retired == ["sess0001"]

        # review_monitor.py was invoked with the right arguments.
        assert len(calls) == 1
        cmd = calls[0]
        assert "complete" in cmd
        assert "42" in cmd
        assert "owner/repo" in cmd

        # Session row was updated.
        state = load_state()
        updated = next(s for s in state.sessions if s.id == "sess0001")
        assert updated.status == SessionStatus.COMPLETED
        assert updated.completed_reason == CompletionReason.HANDOFF
        assert updated.completed_at is not None

        # Cmux surface was closed.
        assert adapter.calls["close"] == [("pane-7",)]

        # session.completed event was emitted.
        events = read_events(event_types=[OrchestratorEventType.SESSION_COMPLETED])
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] == "sess0001"
        assert payload["reason"] == CompletionReason.HANDOFF.value
        assert payload["pr_number"] == 42
        assert payload["repo"] == "owner/repo"

    def test_idempotent_second_call_noop(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        workspace: Path,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """A second tick with no new events returns empty and does no work."""
        sess = _make_session("sess0010", workspace)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 9, "fix-ci", "sess0010")
        _seed_pr_merged("owner/repo", 9)

        calls, runner = fake_runner
        first = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]
        assert first == ["sess0010"]
        assert len(calls) == 1

        # Second call: cursor advanced, nothing left to do.
        second = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]
        assert second == []
        assert len(calls) == 1  # no additional review-monitor invocations

    def test_dispatch_record_entry_removed(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        workspace: Path,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """The dispatch record entry is dropped after retirement."""
        sess = _make_session("sess0020", workspace)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 11, "address-review", "sess0020")
        _seed_pr_merged("owner/repo", 11)

        _calls, runner = fake_runner
        retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]

        from cw.pr_responder import load_dispatch_record

        record = load_dispatch_record()
        assert record.active == {}

    def test_no_dispatch_match_only_calls_review_monitor(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        workspace: Path,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """A merged PR with no correlated sessions still cleans monitor state."""
        save_state(CwState(sessions=[]))
        _seed_pr_merged("owner/other", 5)

        calls, runner = fake_runner
        retired = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]

        assert retired == []
        assert len(calls) == 1
        assert adapter.calls["close"] == []

    def test_completed_session_skipped_but_record_dropped(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        workspace: Path,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """An already-COMPLETED session is not re-closed but its record is removed."""
        sess = _make_session("sess0030", workspace, status=SessionStatus.COMPLETED)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 14, "fix-ci", "sess0030")
        _seed_pr_merged("owner/repo", 14)

        _calls, runner = fake_runner
        retired = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]

        assert retired == []
        assert adapter.calls["close"] == []

        from cw.pr_responder import load_dispatch_record

        assert load_dispatch_record().active == {}

    def test_invalid_pr_number_advances_cursor(
        self,
        tmp_orchestrate_dirs: Path,
        adapter: FakeCmuxAdapter,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """A pr.merged event without a usable pr_number is skipped, cursor advances."""
        record_event(
            OrchestratorEventType.PR_MERGED,
            {"client": "test-client", "repo": "owner/repo", "pr_number": "bogus"},
        )

        calls, runner = fake_runner
        retired = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]

        assert retired == []
        # review_monitor.py was NOT invoked because pr_number was invalid.
        assert calls == []

        # Second call is a no-op (cursor advanced).
        retired_second = retire_merged_prs(adapter=adapter, runner=runner)  # type: ignore[arg-type]
        assert retired_second == []


# ---------------------------------------------------------------------------
# Tests: orchestrator_status
# ---------------------------------------------------------------------------


class TestOrchestratorStatus:
    def test_empty_state(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """Snapshot of an empty system has empty lists, populated metadata."""
        snapshot = orchestrator_status()
        assert isinstance(snapshot, OrchestratorStatus)
        assert snapshot.pending_tickets == []
        assert snapshot.running_sessions == []
        assert snapshot.monitored_prs == []
        assert snapshot.recent_events == []
        # generated_at should be a recent UTC timestamp
        assert snapshot.generated_at.tzinfo is not None

    def test_includes_pending_ticket(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """Pending dev-queue tickets show up in the snapshot."""
        add_ticket(
            TicketTask(
                ticket_id="GEN-100",
                client="test-client",
                priority=5,
                status=QueueItemStatus.PENDING,
            )
        )
        # A RUNNING task should NOT appear under pending_tickets.
        add_ticket(
            TicketTask(
                ticket_id="GEN-101",
                client="test-client",
                priority=1,
                status=QueueItemStatus.RUNNING,
            )
        )

        snapshot = orchestrator_status()
        ids = [t.ticket_id for t in snapshot.pending_tickets]
        assert "GEN-100" in ids
        assert "GEN-101" not in ids

    def test_includes_active_and_idle_sessions(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """ACTIVE and IDLE sessions appear; COMPLETED ones do not."""
        save_state(
            CwState(
                sessions=[
                    _make_session("a1", workspace, status=SessionStatus.ACTIVE),
                    _make_session("i1", workspace, status=SessionStatus.IDLE),
                    _make_session("c1", workspace, status=SessionStatus.COMPLETED),
                ]
            )
        )
        snapshot = orchestrator_status()
        ids = sorted(s.id for s in snapshot.running_sessions)
        assert ids == ["a1", "i1"]

    def test_includes_monitored_prs(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """PRs in review-monitor state show up with unresolved counts."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        _write_monitor_file(
            review_dir,
            "owner/repo",
            42,
            status="watching",
            thread_status={"t1": {"resolved": False}, "t2": {"resolved": True}},
        )
        snapshot = orchestrator_status()
        assert len(snapshot.monitored_prs) == 1
        pr = snapshot.monitored_prs[0]
        assert pr.repo == "owner/repo"
        assert pr.pr_number == 42
        assert pr.unresolved_threads == 1
        assert pr.status == "watching"

    def test_recent_events_capped(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """recent_events is bounded to the last 20 events."""
        # Emit 25 events to verify capping at 20.
        for i in range(25):
            record_event(
                OrchestratorEventType.TICKET_ENQUEUED,
                {"ticket_id": f"GEN-{i}"},
            )
        snapshot = orchestrator_status()
        assert len(snapshot.recent_events) == 20
        # Should be the LAST 20, so first should be GEN-5.
        assert snapshot.recent_events[0].payload["ticket_id"] == "GEN-5"
        assert snapshot.recent_events[-1].payload["ticket_id"] == "GEN-24"

    def test_serialises_to_json(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """The snapshot round-trips through JSON cleanly."""
        save_state(CwState(sessions=[_make_session("s1", workspace)]))
        snapshot = orchestrator_status()
        as_json = snapshot.model_dump_json()
        parsed = json.loads(as_json)

        assert "generated_at" in parsed
        assert "running_sessions" in parsed
        assert parsed["running_sessions"][0]["id"] == "s1"
