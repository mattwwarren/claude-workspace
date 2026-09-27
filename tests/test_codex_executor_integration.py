"""Integration: CodexExecutor's detached ``cw codex run`` job completes on its own.

RFC 0014 A2 (#2388) acceptance criterion: a codex review survives a simulated
serve restart — the driver process keeps running and completes the session.
Here the parent never holds any in-memory handle the child needs: spawn()
returns, and the real ``python -m cw codex run`` subprocess re-derives
everything from disk and completes the session through the door by itself.

Marked ``integration`` because it launches a real ``cw`` subprocess.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.config import load_state
from cw.dev_queue import add_ticket
from cw.executor import CodexExecutor
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    LastResultSource,
    QueueItemStatus,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from tests.test_cli import _write_clients_yaml_for_test

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.integration

_COMPLETION_DEADLINE_SECONDS = 120.0
_POLL_INTERVAL_SECONDS = 0.25


def _path_without_codex() -> str:
    """PATH minus every directory holding a ``codex`` executable.

    The child must not reach a real codex install on a dev machine: the test
    proves the process boundary, not a live review.
    """
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(
        d for d in dirs if d and shutil.which("codex", path=d) is None
    )


def _isolate_child_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point the child ``cw`` at this test's tmp config/state dirs.

    ``tmp_config_dir`` only patches the parent's already-imported
    ``cw.config`` constants; a real child computes them fresh from
    ``XDG_CONFIG_HOME``/``XDG_DATA_HOME`` (and ``HOME`` for the
    orchestrator config and ``gh``) at its own import time.
    """
    child_path = _path_without_codex()
    if shutil.which("git", path=child_path) is None:
        pytest.skip("git shares a PATH directory with codex; cannot isolate")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", child_path)


def _wait_for_terminal(sid: str) -> None:
    deadline = time.monotonic() + _COMPLETION_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        session = next(s for s in load_state().sessions if s.id == sid)
        if session.status is SessionStatus.COMPLETED:
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"session {sid} not COMPLETED within {_COMPLETION_DEADLINE_SECONDS}s")


def _kill_pid(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def test_detached_codex_run_completes_session_without_parent(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    worktree = make_worktree_with_change(
        "wt-codex-detached", filename="new.py", content="def broken():\n"
    )
    _isolate_child_environment(monkeypatch, tmp_path)
    _write_clients_yaml_for_test(tmp_config_dir, [("test", str(worktree))])
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-detached", client="test", stage=Stage.REVIEW)
    add_ticket(
        TicketTask(
            ticket_id=task.ticket_id,
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            created_at=task.created_at,
        )
    )
    executor = CodexExecutor(config=StageExecutorConfig(backend=CODEX_BACKEND))

    # Parent-side pre-flight only: the child's own codex genuinely need not
    # exist (RealCodexRunner turns FileNotFoundError into a returncode-127
    # result, so the child parks with a terminal result of its own).
    with patch("cw.executor.codex.shutil.which", return_value="/usr/bin/codex"):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=client
        )

    session = next(s for s in load_state().sessions if s.id == sid)
    assert session.local_liveness is not None
    pid = session.local_liveness.pid
    try:
        assert session.local_liveness.backend == "codex"
        _wait_for_terminal(sid)
    finally:
        _kill_pid(pid)

    session = next(s for s in load_state().sessions if s.id == sid)
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    assert session.last_result is not None
    assert session.local_liveness is not None
    assert session.local_liveness.backend == "codex"
    log_text = (worktree / ".cw" / "codex_driver.log").read_text(encoding="utf-8")
    assert "Traceback" not in log_text
