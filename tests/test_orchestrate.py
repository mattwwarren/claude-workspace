"""Tests for cw.orchestrate -- PR retirement and status snapshot."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import add_ticket
from cw.events import read_events, record_event
from cw.exceptions import CwError
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
    MissingWorkerEntry,
    OrchestratorStatus,
    WorkerEntry,
    orchestrator_parent,
    orchestrator_status,
    orchestrator_workers,
    retire_merged_prs,
)
from cw.pr_responder import PRDispatchRecord, save_dispatch_record

if TYPE_CHECKING:
    from collections.abc import Iterator


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

    def test_no_matches_skips_platform_adapter_resolution(
        self,
        tmp_orchestrate_dirs: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_runner: tuple[list[list[str]], object],
    ) -> None:
        """Regression: when no session matches, the adapter factory is not called.

        `RealCmuxAdapter.__init__` crashes on non-Darwin. If
        `retire_merged_prs` eagerly called `get_cmux_adapter()` before
        checking whether any sessions needed closing, a Linux user with
        zero matches would crash even though no adapter work is needed.
        """
        from cw import orchestrate as orch

        def _boom() -> None:
            msg = "RealCmuxAdapter requires macOS"
            raise CwError(msg)

        monkeypatch.setattr(orch, "get_cmux_adapter", _boom)

        save_state(CwState(sessions=[]))
        _seed_pr_merged("owner/other", 99)

        _calls, runner = fake_runner
        retired = orch.retire_merged_prs(runner=runner)  # type: ignore[arg-type]
        assert retired == []


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
            assert w.last_activity is not None

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
        assert "deleted-parent" in result.output or "not found" in result.output

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
