"""Tests for cw.orchestrate -- PR retirement and status snapshot."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    CompletionReason,
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.orchestrate import (
    MissingWorkerEntry,
    OrchestratorStatus,
    PRDispatchRecord,
    TickSummary,
    WorkerEntry,
    clear_completed_pr_sessions,
    orchestrator_parent,
    orchestrator_status,
    orchestrator_workers,
    retire_merged_prs,
    save_dispatch_record,
)
from cw.reconcile import ProposedAction

_RunnerFn = Callable[[list[str]], subprocess.CompletedProcess[str]]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_orchestrate_dirs(tmp_config_dir: Path) -> Path:
    """Return tmp_path; state isolation is handled by the autouse fixture.

    Also creates the review-monitor directory so tests that seed monitor
    files can write them.
    """
    (tmp_config_dir / "review-monitor").mkdir(parents=True, exist_ok=True)
    return tmp_config_dir


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return a workspace dir for sessions used in tests."""
    ws = tmp_path / "workspace" / "test-project"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
def captured_runner() -> tuple[
    list[list[str]],
    _RunnerFn,
]:
    """Provide a runner that records calls and returns success."""
    calls: list[list[str]] = []
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return completed

    # Returning the closure plus the calls list so tests can inspect both.
    return calls, _runner


@pytest.fixture
def fake_runner() -> tuple[list[list[str]], _RunnerFn]:
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
    ci_status: str | None = None,
    mergeable: bool | None = None,
) -> Path:
    pr_key = f"{repo}#{pr_number}"
    pr_data: dict[str, object] = {
        "role": role,
        "repo": repo,
        "repo_path": "/tmp/some-repo",
        "pr_number": pr_number,
        "status": status,
        "thread_status": thread_status or {},
        "delta_findings": [],
    }
    if ci_status is not None:
        pr_data["ci_status"] = ci_status
    if mergeable is not None:
        pr_data["mergeable"] = mergeable
    payload = {"monitored": {pr_key: pr_data}, "completed": {}}
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
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """When there are no pr.merged events, retirement is a no-op."""
        _calls, runner = fake_runner
        retired = retire_merged_prs(runner=runner)
        assert retired == []

    def test_retires_correlated_session(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """A pr.merged event marks the correlated session COMPLETED."""
        # Set up state with one ACTIVE session linked via dispatch record.
        sess = _make_session("sess0001", workspace, surface_ref="pane-7")
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 42, "fix-ci", "sess0001")
        _seed_pr_merged("owner/repo", 42)

        calls, runner = fake_runner
        retired = retire_merged_prs(runner=runner)

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
        workspace: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """A second tick with no new events returns empty and does no work."""
        sess = _make_session("sess0010", workspace)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 9, "fix-ci", "sess0010")
        _seed_pr_merged("owner/repo", 9)

        calls, runner = fake_runner
        first = retire_merged_prs(runner=runner)
        assert first == ["sess0010"]
        assert len(calls) == 1

        # Second call: cursor advanced, nothing left to do.
        second = retire_merged_prs(runner=runner)
        assert second == []
        assert len(calls) == 1  # no additional review-monitor invocations

    def test_dispatch_record_entry_removed(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """The dispatch record entry is dropped after retirement."""
        sess = _make_session("sess0020", workspace)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 11, "address-review", "sess0020")
        _seed_pr_merged("owner/repo", 11)

        _calls, runner = fake_runner
        retire_merged_prs(runner=runner)

        from cw.orchestrate import load_dispatch_record

        record = load_dispatch_record()
        assert record.active == {}

    def test_no_dispatch_match_only_calls_review_monitor(
        self,
        tmp_orchestrate_dirs: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """A merged PR with no correlated sessions still cleans monitor state."""
        save_state(CwState(sessions=[]))
        _seed_pr_merged("owner/other", 5)

        calls, runner = fake_runner
        retired = retire_merged_prs(runner=runner)

        assert retired == []
        assert len(calls) == 1

    def test_completed_session_skipped_but_record_dropped(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """An already-COMPLETED session is not re-closed but its record is removed."""
        sess = _make_session("sess0030", workspace, status=SessionStatus.COMPLETED)
        save_state(CwState(sessions=[sess]))
        _seed_dispatch_record("owner/repo", 14, "fix-ci", "sess0030")
        _seed_pr_merged("owner/repo", 14)

        _calls, runner = fake_runner
        retired = retire_merged_prs(runner=runner)

        assert retired == []

        from cw.orchestrate import load_dispatch_record

        assert load_dispatch_record().active == {}

    def test_invalid_pr_number_advances_cursor(
        self,
        tmp_orchestrate_dirs: Path,
        fake_runner: tuple[list[list[str]], _RunnerFn],
    ) -> None:
        """A pr.merged event without a usable pr_number is skipped, cursor advances."""
        record_event(
            OrchestratorEventType.PR_MERGED,
            {"client": "test-client", "repo": "owner/repo", "pr_number": "bogus"},
        )

        calls, runner = fake_runner
        retired = retire_merged_prs(runner=runner)

        assert retired == []
        # review_monitor.py was NOT invoked because pr_number was invalid.
        assert calls == []

        # Second call is a no-op (cursor advanced).
        retired_second = retire_merged_prs(runner=runner)
        assert retired_second == []


# ---------------------------------------------------------------------------
# CLI tests: cw orchestrate retire
# ---------------------------------------------------------------------------


class TestOrchestrateRetireCli:
    def test_no_sessions_retired(
        self,
        tmp_orchestrate_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CLI prints 'No sessions retired.' when nothing was retired."""
        monkeypatch.setattr(
            "cw.cli.retire_merged_prs",
            lambda **_kw: [],
        )
        cli_runner = CliRunner()
        result = cli_runner.invoke(main, ["orchestrate", "retire"])
        assert result.exit_code == 0
        assert "No sessions retired." in result.output

    def test_sessions_retired_prints_ids(
        self,
        tmp_orchestrate_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CLI lists retired session IDs when sessions were retired."""
        monkeypatch.setattr(
            "cw.cli.retire_merged_prs",
            lambda **_kw: ["sess-abc", "sess-def"],
        )
        cli_runner = CliRunner()
        result = cli_runner.invoke(main, ["orchestrate", "retire"])
        assert result.exit_code == 0
        assert "Retired 2 session(s)" in result.output
        assert "sess-abc" in result.output
        assert "sess-def" in result.output


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

    def test_last_tick_by_client_from_dispatch_tick_event(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """dispatch.tick event populates last_tick_by_client in the snapshot."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "test-client",
                "claimed": 1,
                "pending": 2,
                "running": 1,
                "cap": 2,
                "skip_reason": "none",
            },
        )
        snapshot = orchestrator_status()
        assert "test-client" in snapshot.last_tick_by_client
        tick = snapshot.last_tick_by_client["test-client"]
        assert tick.claimed == 1
        assert tick.pending == 2
        assert tick.running == 1
        assert tick.cap == 2
        assert tick.skip_reason == "none"
        assert tick.tick_at is not None

    def test_last_tick_by_client_multiple_clients(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """Each client gets its own last_tick_by_client entry; latest event wins."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "client-a",
                "claimed": 1,
                "pending": 0,
                "running": 1,
                "cap": 2,
                "skip_reason": "none",
            },
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "client-b",
                "claimed": 0,
                "pending": 3,
                "running": 0,
                "cap": 2,
                "skip_reason": "no_pending",
            },
        )
        # Second event for client-a — should overwrite the first
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "client-a",
                "claimed": 0,
                "pending": 1,
                "running": 0,
                "cap": 2,
                "skip_reason": "cap_full",
            },
        )
        snapshot = orchestrator_status()
        assert "client-a" in snapshot.last_tick_by_client
        assert "client-b" in snapshot.last_tick_by_client
        # Latest event for client-a wins
        assert snapshot.last_tick_by_client["client-a"].skip_reason == "cap_full"
        assert snapshot.last_tick_by_client["client-b"].skip_reason == "no_pending"

    def test_last_tick_by_client_empty_when_no_events(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """No DISPATCH_TICK events → last_tick_by_client is empty."""
        snapshot = orchestrator_status()
        assert snapshot.last_tick_by_client == {}

    def test_last_tick_skips_non_string_client(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """DISPATCH_TICK events with non-string client are ignored."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": 123,  # non-string — should be skipped
                "claimed": 1,
                "pending": 0,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
            },
        )
        snapshot = orchestrator_status()
        assert snapshot.last_tick_by_client == {}

    def test_last_tick_skips_bad_numeric_payload(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """DISPATCH_TICK events with non-castable numeric fields are skipped."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "bad-client",
                "claimed": "not-a-number",
                "pending": "also-not-a-number",
                "running": "nope",
                "cap": "nope",
                "skip_reason": "none",
            },
        )
        # The event has a valid client but bad numeric fields — raises
        # ValueError from int() which we catch and skip.
        snapshot = orchestrator_status()
        # Skipped due to ValueError — bad-client absent from map.
        assert "bad-client" not in snapshot.last_tick_by_client

    def test_load_monitored_prs_uses_monitored_key(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """_load_monitored_prs reads the 'monitored' key (not 'active')."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        _write_monitor_file(
            review_dir,
            "owner/repo",
            99,
            status="watching",
            role="author",
        )
        # After _write_monitor_file update, file uses 'monitored' key.
        # _load_monitored_prs must read it.
        snapshot = orchestrator_status()
        assert len(snapshot.monitored_prs) == 1
        assert snapshot.monitored_prs[0].pr_number == 99

    def test_monitored_pr_ci_status_and_mergeable_populated(
        self, tmp_orchestrate_dirs: Path
    ) -> None:
        """ci_status and mergeable are read from the monitor file when present."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        _write_monitor_file(
            review_dir, "owner/repo", 42, ci_status="success", mergeable=True
        )
        snapshot = orchestrator_status()
        assert len(snapshot.monitored_prs) == 1
        pr = snapshot.monitored_prs[0]
        assert pr.ci_status == "success"
        assert pr.mergeable is True

    def test_monitored_pr_ci_status_and_mergeable_none_when_absent(
        self, tmp_orchestrate_dirs: Path
    ) -> None:
        """When ci_status/mergeable absent from monitor file, fields are None."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        _write_monitor_file(review_dir, "owner/repo", 42)  # no new kwargs
        snapshot = orchestrator_status()
        pr = snapshot.monitored_prs[0]
        assert pr.ci_status is None
        assert pr.mergeable is None

    def test_monitored_pr_mergeable_false_preserved(
        self, tmp_orchestrate_dirs: Path
    ) -> None:
        """mergeable=False is preserved (not coerced to None by or-None patterns)."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        _write_monitor_file(review_dir, "owner/repo", 42, mergeable=False)
        snapshot = orchestrator_status()
        pr = snapshot.monitored_prs[0]
        assert pr.mergeable is False
        assert pr.ci_status is None

    def test_serialises_to_json(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """The snapshot round-trips through JSON cleanly."""
        review_dir = tmp_orchestrate_dirs / "review-monitor"
        # ci_status/mergeable absent → None (backward compat)
        _write_monitor_file(review_dir, "owner/repo", 42)
        save_state(CwState(sessions=[_make_session("s1", workspace)]))
        snapshot = orchestrator_status()
        as_json = snapshot.model_dump_json()
        parsed = json.loads(as_json)

        assert "generated_at" in parsed
        assert "running_sessions" in parsed
        assert parsed["running_sessions"][0]["id"] == "s1"
        assert "monitored_prs" in parsed
        assert len(parsed["monitored_prs"]) == 1
        pr_json = parsed["monitored_prs"][0]
        # null guard: must serialize to null, not be excluded
        assert pr_json["ci_status"] is None
        assert pr_json["mergeable"] is None


# ---------------------------------------------------------------------------
# Tests: orchestrator_workers
# ---------------------------------------------------------------------------


def _make_worker(
    session_id: str,
    workspace: Path,
    *,
    branch: str | None = "feat/x",
    parent_id: str | None = None,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    return Session(
        id=session_id,
        name=f"test-client/impl/{session_id}",
        client="test-client",
        purpose=SessionPurpose.IMPL,
        status=status,
        workspace_path=workspace,
        surface_ref=f"pane-{session_id}",
        branch=branch,
        started_at=datetime(2025, 3, 1, 10, 0, 0, tzinfo=UTC),
        parent_session_id=parent_id,
    )


class TestOrchestratorWorkers:
    def test_no_workers_returns_empty(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """orchestrator_workers on a session with no workers returns empty lists."""
        orch = _make_session("orch01", workspace)
        save_state(CwState(sessions=[orch]))

        present, missing = orchestrator_workers("orch01")
        assert present == []
        assert missing == []

    def test_no_workers_cli_exits_zero_human(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """cw orchestrate workers <id> with no workers: exit 0, empty output."""
        orch = _make_session("orch02", workspace)
        save_state(CwState(sessions=[orch]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch02"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_no_workers_cli_exits_zero_json(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """cw orchestrate workers --json with no workers: exit 0, empty JSON array."""
        orch = _make_session("orch03", workspace)
        save_state(CwState(sessions=[orch]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch03", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_workers_present_returned(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Workers in state appear in present list with correct fields."""
        orch = _make_session("orch10", workspace)
        orch.worker_session_ids = ["wrk01", "wrk02"]
        worker1 = _make_worker("wrk01", workspace, parent_id="orch10", branch="feat/a")
        worker2 = _make_worker("wrk02", workspace, parent_id="orch10", branch="feat/b")
        save_state(CwState(sessions=[orch, worker1, worker2]))

        present, missing = orchestrator_workers("orch10")
        assert len(present) == 2
        assert missing == []
        ids = {w.id for w in present}
        assert ids == {"wrk01", "wrk02"}
        for w in present:
            assert isinstance(w, WorkerEntry)
            assert w.status == "active"
            assert w.branch is not None
            # _last_activity falls back to started_at when no other timestamps
            # are set; _make_worker pins that to a fixed value.
            assert w.last_activity == datetime(2025, 3, 1, 10, 0, 0, tzinfo=UTC)

    def test_missing_worker_labelled(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """A worker ID not in state appears as a MissingWorkerEntry."""
        orch = _make_session("orch11", workspace)
        orch.worker_session_ids = ["wrk10", "ghost99"]
        worker = _make_worker("wrk10", workspace, parent_id="orch11")
        save_state(CwState(sessions=[orch, worker]))

        present, missing = orchestrator_workers("orch11")
        assert len(present) == 1
        assert present[0].id == "wrk10"
        assert len(missing) == 1
        assert isinstance(missing[0], MissingWorkerEntry)
        assert missing[0].id == "ghost99"
        assert missing[0].missing is True

    def test_missing_worker_in_human_output(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Human output labels missing workers as 'status=missing'."""
        orch = _make_session("orch12", workspace)
        orch.worker_session_ids = ["ghost88"]
        save_state(CwState(sessions=[orch]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch12"])
        assert result.exit_code == 0
        assert "ghost88" in result.output
        assert "missing" in result.output

    def test_missing_worker_in_json_output(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """JSON output includes missing sentinel for deleted worker sessions."""
        orch = _make_session("orch13", workspace)
        orch.worker_session_ids = ["ghost77"]
        save_state(CwState(sessions=[orch]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch13", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["id"] == "ghost77"
        assert data[0]["missing"] is True

    def test_json_output_round_trips_present_worker(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """--json output for present workers contains expected keys."""
        orch = _make_session("orch14", workspace)
        orch.worker_session_ids = ["wrk20"]
        worker = _make_worker("wrk20", workspace, parent_id="orch14", branch="feat/z")
        save_state(CwState(sessions=[orch, worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch14", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        entry = data[0]
        assert entry["id"] == "wrk20"
        assert entry["status"] == "active"
        assert entry["branch"] == "feat/z"
        assert "last_activity" in entry
        assert "missing" not in entry

    def test_unknown_orchestrator_exits_nonzero(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Unknown orchestrator ID raises CwError; CLI exits nonzero."""
        save_state(CwState(sessions=[]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "no-such-id"])
        assert result.exit_code != 0
        assert "no-such-id" in result.output

    def test_unknown_orchestrator_error_direct(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """orchestrator_workers raises CwError for unknown ID."""
        save_state(CwState(sessions=[]))

        with pytest.raises(CwError, match="no-such-id"):
            orchestrator_workers("no-such-id")

    def test_lookup_by_name(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """orchestrate workers accepts session name as well as ID."""
        orch = _make_session("orch15", workspace)
        save_state(CwState(sessions=[orch]))

        # Use the name field for lookup (set by _make_session).
        present, missing = orchestrator_workers(orch.name)
        assert present == []
        assert missing == []

    def test_worker_branch_none_shown_in_human_output(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """A worker with branch=None shows '(none)' in human output."""
        orch = _make_session("orch16", workspace)
        orch.worker_session_ids = ["wrk30"]
        worker = _make_worker("wrk30", workspace, parent_id="orch16", branch=None)
        save_state(CwState(sessions=[orch, worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "workers", "orch16"])
        assert result.exit_code == 0
        assert "(none)" in result.output


# ---------------------------------------------------------------------------
# Tests: orchestrator_parent
# ---------------------------------------------------------------------------


class TestOrchestratorParent:
    def test_no_parent_returns_none(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Worker with no parent_session_id returns None from orchestrator_parent."""
        worker = _make_worker("wrk50", workspace)
        save_state(CwState(sessions=[worker]))

        result = orchestrator_parent("wrk50")
        assert result is None

    def test_no_parent_cli_human_prints_no_parent(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """cw orchestrate parent <id> with no parent: exits 0, prints 'no parent'."""
        worker = _make_worker("wrk51", workspace)
        save_state(CwState(sessions=[worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "parent", "wrk51"])
        assert result.exit_code == 0
        assert "no parent" in result.output

    def test_no_parent_cli_json_prints_null(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """cw orchestrate parent --json with no parent: exits 0, prints 'null'."""
        worker = _make_worker("wrk52", workspace)
        save_state(CwState(sessions=[worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "parent", "wrk52", "--json"])
        assert result.exit_code == 0
        assert result.output.strip() == "null"

    def test_parent_resolved_correctly(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Worker with parent_session_id resolves to the parent session."""
        orch = _make_session("orch50", workspace)
        orch.worker_session_ids = ["wrk60"]
        worker = _make_worker("wrk60", workspace, parent_id="orch50")
        save_state(CwState(sessions=[orch, worker]))

        entry = orchestrator_parent("wrk60")
        assert entry is not None
        assert entry.id == "orch50"
        assert entry.status == "active"

    def test_parent_json_output_round_trips(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """--json output for parent contains expected keys."""
        orch = _make_session("orch51", workspace)
        orch.worker_session_ids = ["wrk61"]
        worker = _make_worker("wrk61", workspace, parent_id="orch51")
        save_state(CwState(sessions=[orch, worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "parent", "wrk61", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "orch51"
        assert data["status"] == "active"
        assert "surface_ref" in data

    def test_missing_parent_exits_nonzero(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Worker whose parent has been deleted: CwError, CLI exits nonzero."""
        worker = _make_worker("wrk70", workspace, parent_id="deleted-parent")
        save_state(CwState(sessions=[worker]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "parent", "wrk70"])
        assert result.exit_code != 0
        # Both substrings should appear: the missing parent ID is named, and
        # the user is told it's not found. Disjunction would mask a regression
        # where one of the two was dropped from the message.
        assert "deleted-parent" in result.output
        assert "not found" in result.output

    def test_missing_parent_raises_cwerror_direct(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """orchestrator_parent raises CwError (not crashes) for missing parent."""
        worker = _make_worker("wrk71", workspace, parent_id="gone-parent")
        save_state(CwState(sessions=[worker]))

        with pytest.raises(CwError, match="gone-parent"):
            orchestrator_parent("wrk71")

    def test_unknown_worker_exits_nonzero(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Unknown worker ID raises CwError; CLI exits nonzero."""
        save_state(CwState(sessions=[]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "parent", "no-such-worker"])
        assert result.exit_code != 0
        assert "no-such-worker" in result.output

    def test_lookup_worker_by_name(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """orchestrate parent accepts session name as well as ID."""
        orch = _make_session("orch52", workspace)
        orch.worker_session_ids = ["wrk80"]
        worker = _make_worker("wrk80", workspace, parent_id="orch52")
        save_state(CwState(sessions=[orch, worker]))

        # Lookup worker by name.
        entry = orchestrator_parent(worker.name)
        assert entry is not None
        assert entry.id == "orch52"

    def test_parent_handles_none_surface_ref(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """ParentEntry.surface_ref is None when the parent session has no surface."""
        orch = _make_session("orch53", workspace, surface_ref=None)
        orch.worker_session_ids = ["wrk81"]
        worker = _make_worker("wrk81", workspace, parent_id="orch53")
        save_state(CwState(sessions=[orch, worker]))

        entry = orchestrator_parent("wrk81")
        assert entry is not None
        assert entry.surface_ref is None


# ---------------------------------------------------------------------------
# Tests: last_stage derivation from stage events (issue #173)
# ---------------------------------------------------------------------------


class TestRunningSessionLastStage:
    def test_running_session_last_stage_picks_most_recent(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Last STAGE_ENTERED event wins per session_id."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                    _make_session("s2", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s1_plan_reviewed",
                "started_at": "2026-05-23T13:00:00Z",
            },
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:01:00Z",
            },
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s2",
                "ticket_id": "174",
                "stage": "s1_plan_generated",
                "started_at": "2026-05-23T13:02:00Z",
            },
        )

        snapshot = orchestrator_status()
        by_id = {s.id: s for s in snapshot.running_sessions}
        assert by_id["s1"].last_stage == "s2_impl_started"
        assert by_id["s2"].last_stage == "s1_plan_generated"

    def test_running_session_last_stage_none_when_no_events(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """A running session with no stage events has last_stage=None."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )
        snapshot = orchestrator_status()
        assert len(snapshot.running_sessions) == 1
        assert snapshot.running_sessions[0].last_stage is None

    def test_running_session_last_stage_ignores_completed_session_events(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """COMPLETED sessions don't appear in running_sessions even with events."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                    _make_session("c1", workspace, status=SessionStatus.COMPLETED),
                ]
            )
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:00:00Z",
            },
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "c1",
                "ticket_id": "172",
                "stage": "done",
                "started_at": "2026-05-23T13:01:00Z",
            },
        )

        snapshot = orchestrator_status()
        ids = [s.id for s in snapshot.running_sessions]
        assert "c1" not in ids
        by_id = {s.id: s for s in snapshot.running_sessions}
        assert by_id["s1"].last_stage == "s2_impl_started"

    def test_running_session_last_stage_ignores_non_stage_events(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """PR_MERGED and other non-stage events don't redefine last_stage."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s1_plan_reviewed",
                "started_at": "2026-05-23T13:00:00Z",
            },
        )
        record_event(
            OrchestratorEventType.PR_MERGED,
            {"session_id": "s1", "pr_number": 42},
        )

        snapshot = orchestrator_status()
        by_id = {s.id: s for s in snapshot.running_sessions}
        assert by_id["s1"].last_stage == "s1_plan_reviewed"

    def test_running_session_last_stage_handles_errored_event(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """STAGE_ERRORED is deliberately ignored when deriving last_stage."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:00:00Z",
            },
        )
        record_event(
            OrchestratorEventType.STAGE_ERRORED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:01:00Z",
                "error_kind": "agent_block",
            },
        )

        snapshot = orchestrator_status()
        by_id = {s.id: s for s in snapshot.running_sessions}
        assert by_id["s1"].last_stage == "s2_impl_started"


# ---------------------------------------------------------------------------
# Tests: CLI orchestrate status surfaces last_stage (issue #173)
# ---------------------------------------------------------------------------


class TestCliOrchestrateStatusLastStage:
    def test_cli_orchestrate_status_includes_last_stage(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Running-sessions section includes last_stage=<value> when set."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )
        record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "s1",
                "ticket_id": "173",
                "stage": "s2_impl_started",
                "started_at": "2026-05-23T13:00:00Z",
            },
        )

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "status"])
        assert result.exit_code == 0, result.output
        assert "last_stage=s2_impl_started" in result.output

    def test_cli_orchestrate_status_omits_last_stage_when_none(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Sessions with no stage events show placeholder in last_stage field."""
        save_state(
            CwState(
                sessions=[
                    _make_session("s1", workspace, status=SessionStatus.ACTIVE),
                ]
            )
        )

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "status"])
        assert result.exit_code == 0, result.output
        expected = "(unknown — global auto-dev.md not yet emitting stage events)"
        assert expected in result.output


# ---------------------------------------------------------------------------
# clear_completed_pr_sessions
# ---------------------------------------------------------------------------


class TestClearCompletedPrSessions:
    def test_removes_completed_session_dispatch_key(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Dispatch keys for completed sessions are removed."""
        dispatch_key = "owner/repo#42|fix-ci"
        session_id = "abc12345"
        record = PRDispatchRecord(active={dispatch_key: session_id})
        save_dispatch_record(record)

        state = CwState(
            sessions=[
                Session(
                    id=session_id,
                    name="myproject/fix-ci-42",
                    client="myproject",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.COMPLETED,
                    workspace_path=workspace,
                )
            ]
        )

        clear_completed_pr_sessions(state)

        from cw.orchestrate import load_dispatch_record

        updated = load_dispatch_record()
        assert dispatch_key not in updated.active

    def test_retains_active_session_dispatch_key(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """Dispatch keys for non-completed sessions are kept."""
        dispatch_key = "owner/repo#99|address-review"
        session_id = "def67890"
        record = PRDispatchRecord(active={dispatch_key: session_id})
        save_dispatch_record(record)

        state = CwState(
            sessions=[
                Session(
                    id=session_id,
                    name="myproject/address-review-99",
                    client="myproject",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=workspace,
                )
            ]
        )

        clear_completed_pr_sessions(state)

        from cw.orchestrate import load_dispatch_record

        updated = load_dispatch_record()
        assert dispatch_key in updated.active
        assert updated.active[dispatch_key] == session_id

    def test_no_op_when_dispatch_record_empty(
        self,
        tmp_orchestrate_dirs: Path,
        workspace: Path,
    ) -> None:
        """No error when dispatch record is empty."""
        state = CwState(sessions=[])
        clear_completed_pr_sessions(state)  # should not raise


# ---------------------------------------------------------------------------
# Tests: TickSummary.lanes field + _extract_lanes helper (issue #561)
# ---------------------------------------------------------------------------


class TestTickSummaryLanes:
    def test_tick_summary_lanes_defaults_empty(self) -> None:
        """TickSummary without lanes kwarg has lanes == {}."""
        tick = TickSummary(
            claimed=0,
            pending=0,
            running=0,
            cap=1,
            skip_reason="none",
            tick_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
        )
        assert tick.lanes == {}

    def test_tick_summary_lanes_round_trip(self) -> None:
        """TickSummary with lanes serialises and deserialises with lanes intact."""
        tick = TickSummary(
            claimed=0,
            pending=0,
            running=0,
            cap=1,
            skip_reason="none",
            tick_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            lanes={"fast": {"claimed": 1, "running": 0, "pending": 2}},
        )
        parsed = json.loads(tick.model_dump_json())
        assert parsed["lanes"]["fast"]["claimed"] == 1
        assert parsed["lanes"]["fast"]["pending"] == 2

    def test_latest_tick_by_client_preserves_lanes(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """DISPATCH_TICK event with lanes key populates TickSummary.lanes."""
        from cw.events import read_events
        from cw.orchestrate import _latest_tick_by_client

        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "lane-client",
                "claimed": 1,
                "pending": 1,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                "lanes": {"fast": {"claimed": 1, "running": 0, "pending": 1}},
            },
        )
        events = read_events()
        result = _latest_tick_by_client(events)
        assert "lane-client" in result
        assert result["lane-client"].lanes == {
            "fast": {"claimed": 1, "running": 0, "pending": 1}
        }

    def test_latest_tick_by_client_legacy_event_no_lanes_key(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """DISPATCH_TICK event without lanes key yields TickSummary.lanes == {}."""
        from cw.events import read_events
        from cw.orchestrate import _latest_tick_by_client

        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "legacy-client",
                "claimed": 0,
                "pending": 1,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                # no "lanes" key — pre-#558 format
            },
        )
        events = read_events()
        result = _latest_tick_by_client(events)
        assert "legacy-client" in result
        assert result["legacy-client"].lanes == {}

    def test_orchestrate_status_json_guard(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """orchestrator_status snapshot includes lanes when present in tick event."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "json-client",
                "claimed": 1,
                "pending": 0,
                "running": 1,
                "cap": 2,
                "skip_reason": "none",
                "lanes": {"fast": {"claimed": 1, "running": 1, "pending": 0}},
            },
        )
        snapshot = orchestrator_status()
        parsed = json.loads(snapshot.model_dump_json())
        assert "json-client" in parsed["last_tick_by_client"]
        assert "lanes" in parsed["last_tick_by_client"]["json-client"]
        fast_lane = parsed["last_tick_by_client"]["json-client"]["lanes"]["fast"]
        assert fast_lane["claimed"] == 1

    def test_orchestrate_status_json_legacy_lanes_empty(
        self,
        tmp_orchestrate_dirs: Path,
    ) -> None:
        """orchestrator_status snapshot has lanes == {} for legacy tick events."""
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "legacy-json-client",
                "claimed": 0,
                "pending": 2,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                # no lanes key
            },
        )
        snapshot = orchestrator_status()
        parsed = json.loads(snapshot.model_dump_json())
        assert "legacy-json-client" in parsed["last_tick_by_client"]
        assert parsed["last_tick_by_client"]["legacy-json-client"]["lanes"] == {}


# ---------------------------------------------------------------------------
# TestOrchestrateStart — Phase 4b: cw orchestrate start --lane
# ---------------------------------------------------------------------------


def _write_client_with_lane(
    tmp_config_dir: Path,
    lane_name: str = "impl",
) -> Path:
    """Write a clients.yaml with one client declaring a named lane.

    Initialises the workspace as a minimal git repo so _validate_worktree
    inside spawn_create_impl accepts it. Returns the workspace path.
    """
    import os
    import subprocess

    from cw.config import clients_file

    workspace = tmp_config_dir / "workspace" / "test-client"
    workspace.mkdir(parents=True, exist_ok=True)

    # Initialise a bare-minimum git repo (validate_worktree runs git rev-parse).
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            check=True,
            env=clean_env,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "cw test")
    _git("commit", "--allow-empty", "-m", "initial")

    clients_path = clients_file()
    clients_path.parent.mkdir(parents=True, exist_ok=True)
    clients_path.write_text(
        f"clients:\n"
        f"  test-client:\n"
        f"    workspace_path: {workspace}\n"
        f"    default_branch: main\n"
        f"    lanes:\n"
        f"      - name: {lane_name}\n"
        f"        max_parallel: 1\n"
    )
    return workspace


class TestOrchestrateStart:
    """Tests for `cw orchestrate start --lane` command (Phase 4b)."""

    def test_start_happy_path(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start --lane <declared> spawns ORCHESTRATE session with correct metadata."""
        _write_client_with_lane(tmp_config_dir, "impl")
        monkeypatch.setattr(
            "cw.cli.get_native_daemon_client", lambda: mock_native_daemon
        )

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "start", "--lane", "impl"])

        assert result.exit_code == 0, result.output
        assert "impl" in result.output

        state = load_state()
        assert len(state.sessions) == 1
        sess = state.sessions[0]
        assert sess.purpose == SessionPurpose.ORCHESTRATE
        assert sess.lane == "impl"
        assert sess.client == "test-client"

    def test_start_undeclared_lane(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start --lane <undeclared> exits non-zero with LaneNotFoundError message."""
        _write_client_with_lane(tmp_config_dir, "impl")
        monkeypatch.setattr(
            "cw.cli.get_native_daemon_client", lambda: mock_native_daemon
        )

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "start", "--lane", "no-such-lane"])

        assert result.exit_code != 0
        assert "no-such-lane" in result.output

    def test_start_rejects_live_duplicate(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second start on a lane with a live ORCHESTRATE session is rejected."""
        workspace = _write_client_with_lane(tmp_config_dir, "impl")
        monkeypatch.setattr(
            "cw.cli.get_native_daemon_client", lambda: mock_native_daemon
        )

        # Seed a live ORCHESTRATE session for the lane.
        existing = Session(
            id="orch-live-01",
            name="test-client/orchestrate/impl",
            client="test-client",
            purpose=SessionPurpose.ORCHESTRATE,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
            lane="impl",
        )
        save_state(CwState(sessions=[existing]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "start", "--lane", "impl"])

        assert result.exit_code != 0
        assert "impl" in result.output
        assert "orch-live-01" in result.output

    def test_start_allows_rebind_on_terminal(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start succeeds when prior ORCHESTRATE session for lane is COMPLETED."""
        workspace = _write_client_with_lane(tmp_config_dir, "impl")
        monkeypatch.setattr(
            "cw.cli.get_native_daemon_client", lambda: mock_native_daemon
        )

        # Prior ORCHESTRATE session is COMPLETED (terminal) — rebind allowed.
        old = Session(
            id="orch-done-01",
            name="test-client/orchestrate/impl",
            client="test-client",
            purpose=SessionPurpose.ORCHESTRATE,
            status=SessionStatus.COMPLETED,
            workspace_path=workspace,
            lane="impl",
        )
        save_state(CwState(sessions=[old]))

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrate", "start", "--lane", "impl"])

        assert result.exit_code == 0, result.output
        state = load_state()
        # Two sessions now: the old completed one + new active one.
        orch_sessions = [
            s for s in state.sessions if s.purpose == SessionPurpose.ORCHESTRATE
        ]
        assert len(orch_sessions) == 2
        new_sess = next(s for s in orch_sessions if s.id != "orch-done-01")
        assert new_sess.lane == "impl"
        assert new_sess.status == SessionStatus.ACTIVE

    def test_start_json_output(
        self,
        tmp_config_dir: Path,
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json emits parseable JSON with session_id, lane, client keys."""
        _write_client_with_lane(tmp_config_dir, "impl")
        monkeypatch.setattr(
            "cw.cli.get_native_daemon_client", lambda: mock_native_daemon
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["orchestrate", "start", "--lane", "impl", "--json"]
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "session_id" in data
        assert data["lane"] == "impl"
        assert data["client"] == "test-client"


# ---------------------------------------------------------------------------
# cw orchestrate run — drain + authorize
# ---------------------------------------------------------------------------


def _mk_orchestrate_session(
    sid: str,
    lane: str,
    client: str = "client-a",
    status: SessionStatus = SessionStatus.COMPLETED,
) -> Session:
    """Create an ORCHESTRATE-purpose binding session for test setup."""
    return Session(
        id=sid,
        name=f"{client}/orchestrate/{lane}",
        client=client,
        purpose=SessionPurpose.ORCHESTRATE,
        status=status,
        lane=lane,
        workspace_path=Path("/tmp/ws"),
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def _mk_impl_session(
    sid: str,
    lane: str = DEFAULT_LANE,
    client: str = "client-a",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """Create an IMPL-purpose active session for reap candidate tests."""
    return Session(
        id=sid,
        name=f"{client}/impl/{sid}",
        client=client,
        purpose=SessionPurpose.IMPL,
        status=status,
        lane=lane,
        workspace_path=Path("/tmp/ws"),
        surface_ref=f"surf-{sid}",
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def _emit_reap_event(
    session_id: str,
    lane: str,
    proposed_action: ProposedAction,
    client: str = "client-a",
) -> None:
    """Emit a SESSION_REAP_PROPOSED event for testing."""
    record_event(
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        {
            "session_id": session_id,
            "session_name": f"{client}/impl/{session_id}",
            "client": client,
            "ticket_id": None,
            "lane": lane,
            "proposed_action": proposed_action.value,
            "reason": None,
            "evidence": {},
        },
        correlation_id=session_id,
    )


@pytest.fixture
def run_env(tmp_orchestrate_dirs: Path) -> Path:
    """Set up a minimal client config with a lane for orchestrate run tests."""
    from cw.config import clients_file

    clients_path = clients_file()
    clients_path.parent.mkdir(parents=True, exist_ok=True)
    clients_path.write_text(
        "clients:\n"
        "  client-a:\n"
        "    workspace_path: /tmp/ws\n"
        "    lanes:\n"
        "      - name: default\n"
        "        reap_policy: signal_only\n"
        "      - name: lane-x\n"
        "        reap_policy: signal_only\n"
        "      - name: lane-y\n"
        "        reap_policy: signal_only\n"
    )
    return tmp_orchestrate_dirs


def test_orchestrate_run_drain_authorizes_revert_task(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain authorizes REVERT_TASK by calling _reap_session_by_selector."""
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return True

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    # Seed state with an active IMPL session
    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_impl_session("s1", lane="default"))
    save_state(state)

    _emit_reap_event("s1", "default", ProposedAction.REVERT_TASK)
    count = _drain_reap_proposals("client-a", "default")

    assert count == 1
    assert reap_calls == ["s1"]


def test_orchestrate_run_drain_authorizes_crash_complete(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain authorizes CRASH_COMPLETE by calling _reap_session_by_selector."""
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return True

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_impl_session("s2", lane="default"))
    save_state(state)

    _emit_reap_event("s2", "default", ProposedAction.CRASH_COMPLETE)
    count = _drain_reap_proposals("client-a", "default")

    assert count == 1
    assert reap_calls == ["s2"]


def test_orchestrate_run_drain_logs_and_leaves_park_blocked(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain does NOT reap for PARK_BLOCKED_ON_USER.

    Leaves at BLOCKED_ON_USER routing for operator review.
    """
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return True

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_impl_session("s3", lane="default"))
    save_state(state)

    _emit_reap_event("s3", "default", ProposedAction.PARK_BLOCKED_ON_USER)
    count = _drain_reap_proposals("client-a", "default")

    assert count == 1
    assert reap_calls == []  # NOT reaped


def test_orchestrate_run_drain_idempotent_replay(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain is a no-op for already-terminal sessions (idempotent replay)."""
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return True

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    from cw.config import load_state, save_state

    # Session already COMPLETED
    state = load_state()
    state.sessions.append(
        _mk_impl_session("s4", lane="default", status=SessionStatus.COMPLETED)
    )
    save_state(state)

    _emit_reap_event("s4", "default", ProposedAction.REVERT_TASK)
    count = _drain_reap_proposals("client-a", "default")

    assert count == 1
    assert reap_calls == []  # No reap — already terminal


def test_orchestrate_run_drain_backgrounded_proceeds_to_reap_check(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BACKGROUNDED sessions are not treated as terminal by the idempotency guard (R3).

    A BACKGROUNDED session passes the outer guard and proceeds to
    _reap_session_by_selector, which applies its own inner lock-guarded check.
    """
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return False  # inner guard would reject BACKGROUNDED

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(
        _mk_impl_session("s5", lane="default", status=SessionStatus.BACKGROUNDED)
    )
    save_state(state)

    _emit_reap_event("s5", "default", ProposedAction.REVERT_TASK)
    count = _drain_reap_proposals("client-a", "default")

    assert count == 1
    # BACKGROUNDED is in the live set — drain forwards to _reap_session_by_selector
    assert reap_calls == ["s5"]


def test_orchestrate_run_lane_isolation_adversarial(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane-X drain MUST NOT reap a lane-Y session (adversarial isolation test)."""
    from cw.cli import _drain_reap_proposals

    reap_calls: list[str] = []

    def fake_reap(selector: str, **kwargs: object) -> bool:
        reap_calls.append(selector)
        return True

    monkeypatch.setattr("cw.cli._reap_session_by_selector", fake_reap)

    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_impl_session("lane-y-session", lane="lane-y"))
    save_state(state)

    # Emit event for lane-Y
    _emit_reap_event("lane-y-session", "lane-y", ProposedAction.REVERT_TASK)

    # Drain lane-X — must not touch lane-Y session
    count = _drain_reap_proposals("client-a", "lane-x")

    # lane-X consumer does not count lane-Y events as processed
    assert count == 0
    state_after = load_state()
    lane_y_session = next(s for s in state_after.sessions if s.id == "lane-y-session")
    assert lane_y_session.status == SessionStatus.ACTIVE  # Unchanged


def test_orchestrate_run_lane_validation_raises(
    run_env: Path,
) -> None:
    """--lane with unknown lane raises LaneNotFoundError."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["orchestrate", "run", "--lane", "nonexistent-lane", "--once"]
    )
    assert result.exit_code != 0
    assert "nonexistent-lane" in (result.output + str(result.exception or ""))


def test_orchestrate_run_binding_gate_raises_without_binding(
    run_env: Path,
) -> None:
    """cw orchestrate run errors if no ORCHESTRATE binding exists for lane."""
    runner = CliRunner()
    result = runner.invoke(main, ["orchestrate", "run", "--lane", "lane-x", "--once"])
    assert result.exit_code != 0
    combined = result.output + str(result.exception or "")
    assert "No ORCHESTRATE binding" in combined


def test_orchestrate_run_once_flag_exits(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--once flag: drains once and exits (does not poll)."""
    # Seed a binding so the binding gate passes
    from cw.config import load_state, save_state

    state = load_state()
    # Default status is COMPLETED — matches 4b's self-completing binding
    state.sessions.append(_mk_orchestrate_session("binding-1", lane="lane-x"))
    save_state(state)

    drain_calls: list[tuple[str, str]] = []

    def counting_drain(client: str, lane: str) -> int:
        drain_calls.append((client, lane))
        return 0

    monkeypatch.setattr("cw.cli._drain_reap_proposals", counting_drain)

    runner = CliRunner()
    result = runner.invoke(main, ["orchestrate", "run", "--lane", "lane-x", "--once"])

    assert result.exit_code == 0
    assert len(drain_calls) == 1  # Exactly one drain, then exit


def test_orchestrate_run_keyboard_interrupt(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll loop: KeyboardInterrupt exits with code 130 and stop message."""
    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_orchestrate_session("binding-1", lane="lane-x"))
    save_state(state)

    def raising_drain(client: str, lane: str) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("cw.cli._drain_reap_proposals", raising_drain)

    runner = CliRunner()
    result = runner.invoke(main, ["orchestrate", "run", "--lane", "lane-x"])

    assert result.exit_code == 130
    assert "orchestrate run: stopped." in result.output


def test_orchestrate_run_keyboard_interrupt_during_sleep(
    run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll loop: KeyboardInterrupt during sleep also exits with code 130."""
    from cw.config import load_state, save_state

    state = load_state()
    state.sessions.append(_mk_orchestrate_session("binding-2", lane="lane-y"))
    save_state(state)

    def noop_drain(client: str, lane: str) -> int:
        return 0

    def raising_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("cw.cli._drain_reap_proposals", noop_drain)
    monkeypatch.setattr("cw.cli.time.sleep", raising_sleep)

    runner = CliRunner()
    result = runner.invoke(main, ["orchestrate", "run", "--lane", "lane-y"])

    assert result.exit_code == 130
    assert "orchestrate run: stopped." in result.output
