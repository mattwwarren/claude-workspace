"""Opencode-specific reconcile tests (#1671 R1/R2).

R1: No process-tree kill tests (scope removed). Cancellation = stop tracking +
park BLOCKED_ON_USER; the orchestrator session kills strays. Tests verify the
typed recoverable queue state (task parked, liveness retained).

R2: Reuse LocalLivenessHandle + reconcile/local.py sweep. Tests verify
live-but-abandoned opencode process is NOT harvested (process alive), and a
dead process IS harvested.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import freezegun

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.local_runner import read_process_start_time_ns
from cw.models import (
    CwState,
    DevQueueStore,
    LocalLivenessHandle,
    QueueItemStatus,
    Session,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    OPENCODE_NO_OUTPUT,
)
from cw.reconcile import (
    ProposedAction,
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
)
from tests._reconcile_helpers import _write_staged_clients_yaml
from tests.conftest import _make_daemon_session


def _mk_opencode_session(
    sid: str,
    worktree: Path,
    liveness: LocalLivenessHandle,
    *,
    started_at: datetime | None = None,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """Build an ACTIVE, DAEMON-origin session with a local_liveness handle."""
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        worktree_path=worktree,
        surface_ref=None,
        started_at=started_at or datetime(2026, 1, 1, tzinfo=UTC),
        stage=Stage.FINALIZE,
        local_liveness=liveness,
        status=status,
    )


def _write_opencode_log(worktree: Path, events: list[dict[str, object]]) -> Path:
    """Write a minimal opencode JSONL log at worktree/.cw/opencode.log."""
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# R2: Live process is NOT harvested
# ---------------------------------------------------------------------------


def test_live_opencode_process_not_harvested(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A live opencode process (alive PID) is NOT a harvest candidate."""
    worktree = make_git_repo("wt-opencode-live")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        start_time_ns = read_process_start_time_ns(proc.pid)
        assert start_time_ns is not None, "test setup: could not read start_time"

        liveness = LocalLivenessHandle(pid=proc.pid, start_time_ns=start_time_ns)
        sess = _mk_opencode_session("ses-live", worktree, liveness)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="T-live",
                        client="client-a",
                        stage=Stage.FINALIZE,
                        status=QueueItemStatus.RUNNING,
                        session_id="ses-live",
                    )
                ]
            )
        )

        candidates = _detect_local_harvest_candidates(load_state())
        assert candidates == []
    finally:
        proc.kill()
        proc.wait()


def test_dead_opencode_process_is_harvested(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A dead opencode process (PID exited) IS a harvest candidate."""
    worktree = make_git_repo("wt-opencode-dead")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    dead_handle = LocalLivenessHandle(pid=999_999_999, start_time_ns=1)
    sess = _mk_opencode_session("ses-dead", worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-dead",
                    client="client-a",
                    stage=Stage.FINALIZE,
                    status=QueueItemStatus.RUNNING,
                    session_id="ses-dead",
                )
            ]
        )
    )

    candidates = _detect_local_harvest_candidates(load_state())
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.HARVEST_LOCAL_COMPLETE
    assert candidates[0].session_id == "ses-dead"


# ---------------------------------------------------------------------------
# R1: Cancellation retains liveness for harvest detection
# ---------------------------------------------------------------------------


def test_cancelled_opencode_task_retains_liveness(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A BLOCKED_ON_USER task retains local_liveness for later harvest.

    Cancellation = stop tracking + park the task BLOCKED_ON_USER. The session
    stays ACTIVE with its liveness handle so harvest can later detect the
    dead process and complete the session. This test verifies the state shape
    that cancellation produces — it does NOT test a kill path (process-tree
    kill is removed from cw's scope per #1669 R2).
    """
    worktree = make_git_repo("wt-opencode-cancelled")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        start_time_ns = read_process_start_time_ns(proc.pid)
        assert start_time_ns is not None

        liveness = LocalLivenessHandle(pid=proc.pid, start_time_ns=start_time_ns)
        sess = _mk_opencode_session("ses-cancelled", worktree, liveness)
        save_state(CwState(sessions=[sess]))

        # Simulate cancellation: park the task BLOCKED_ON_USER.
        # The session stays ACTIVE with liveness retained — harvest detects
        # the dead process later when it exits.
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="T-cancelled",
                        client="client-a",
                        stage=Stage.FINALIZE,
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id="ses-cancelled",
                    )
                ]
            )
        )

        # Session is still ACTIVE with liveness retained
        state = load_state()
        session = next(s for s in state.sessions if s.id == "ses-cancelled")
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.local_liveness.pid == proc.pid

        # Task is BLOCKED_ON_USER
        task = load_dev_queue().tasks[0]
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        # While the process is alive, harvest does not touch this session
        # (it's ACTIVE + has liveness + process alive → not a candidate)
        candidates = _detect_local_harvest_candidates(load_state())
        assert candidates == []
    finally:
        proc.kill()
        proc.wait()


def test_opencode_harvest_no_sentinel_parks_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Dead opencode process with no sentinel → OPENCODE_NO_OUTPUT blocked result.

    This is the cancellation recovery path: when a cancelled opencode process
    eventually dies and harvest runs, the absence of a sentinel in the log
    produces a typed OPENCODE_NO_OUTPUT blocked result (retry_eligible=True).
    """
    worktree = make_git_repo("wt-opencode-no-sentinel")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    _write_opencode_log(
        worktree, [{"type": "text", "part": {"text": "no sentinel here"}}]
    )

    dead_handle = LocalLivenessHandle(pid=999_999_999, start_time_ns=1)
    sess = _mk_opencode_session("ses-no-sentinel", worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-no-sentinel",
                    client="client-a",
                    stage=Stage.FINALIZE,
                    status=QueueItemStatus.RUNNING,
                    session_id="ses-no-sentinel",
                )
            ]
        )
    )

    candidates = _detect_local_harvest_candidates(load_state())
    assert len(candidates) == 1

    with freezegun.freeze_time("2026-01-01 12:00:00"):
        _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

    state = load_state()
    session = next(s for s in state.sessions if s.id == "ses-no-sentinel")
    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT
    assert result.blocker.retry_eligible is True


# ---------------------------------------------------------------------------
# R2: Recycled PID is NOT harvested (start-time mismatch)
# ---------------------------------------------------------------------------


def test_opencode_recycled_pid_not_harvested(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A PID whose start-time no longer matches is treated as dead → harvested.

    This is the PID-recycle defense: if the original process exited and the PID
    was reused by an unrelated process, the start-time mismatch causes
    _local_process_alive to return False, and the session IS harvested.
    The harvest then reads the opencode log for the sentinel.
    """
    worktree = make_git_repo("wt-opencode-recycled")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    # Use a real PID but a wrong start_time_ns — simulates PID recycling
    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wrong_handle = LocalLivenessHandle(pid=proc.pid, start_time_ns=1)
        sess = _mk_opencode_session("ses-recycled", worktree, wrong_handle)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="T-recycled",
                        client="client-a",
                        stage=Stage.FINALIZE,
                        status=QueueItemStatus.RUNNING,
                        session_id="ses-recycled",
                    )
                ]
            )
        )

        candidates = _detect_local_harvest_candidates(load_state())
        assert len(candidates) == 1
        assert candidates[0].session_id == "ses-recycled"
    finally:
        proc.kill()
        proc.wait()
