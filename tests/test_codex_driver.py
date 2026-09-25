"""Tests for cw.codex_driver — the ``cw codex run`` subprocess entry point (#2386).

RFC 0014 S1 / ADR-0018: a detached ``cw codex run`` subprocess replaces the
in-process daemon thread (``cw.codex_background``) for the codex review stage.
This module owns the driver's testable core, ``run_codex_review_stage``, which
re-derives the session/task/client/executor-config the thread path used to
receive via Python closure, then calls
``cw.codex_background._run_codex_review_and_complete`` directly — the same
payload the thread path produces for the same inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.codex_driver import (
    STAGE_IMPL,
    STAGE_REVIEW,
    run_codex_review_stage,
)
from cw.codex_runner import FakeCodexRunner
from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import (
    CwState,
    DevQueueStore,
    LastResultSource,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from tests.conftest import _make_daemon_session, add_bare_origin, git_in
from tests.test_cli import _write_clients_yaml_for_test

if TYPE_CHECKING:
    from collections.abc import Callable


def _reviewer_doc() -> str:
    return json.dumps(
        {
            "reviewer_role": "Code Quality Reviewer",
            "status": "ok",
            "detail": "reviewed; no issues found.",
            "findings": [],
        }
    )


def _worktree_with_change(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """A repo on a feature branch, pushed to a bare origin, with a real diff."""
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    (repo / "new.py").write_text("def broken():\n", encoding="utf-8")
    git_in(repo, "add", "new.py")
    git_in(repo, "commit", "-m", "add new.py")
    add_bare_origin(repo)
    return repo


def _seed(
    *,
    tmp_config_dir: Path,
    worktree: Path,
    client_name: str = "test",
    ticket_id: str = "T-1",
    session_id: str = "sess-1",
) -> None:
    """Seed a Session + a matching RUNNING dev-queue row, plus clients.yaml."""
    _write_clients_yaml_for_test(tmp_config_dir, [(client_name, str(worktree))])
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id=session_id,
                    client=client_name,
                    worktree_path=worktree,
                )
            ]
        )
    )
    from cw.dev_queue import save_dev_queue

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client=client_name,
                    stage=Stage.REVIEW,
                    status=QueueItemStatus.RUNNING,
                    session_id=session_id,
                )
            ]
        )
    )


def test_run_codex_review_stage_completes_session_via_door_with_fake_runner(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The driver's core reproduces the thread path's completion payload."""
    worktree = _worktree_with_change(make_git_repo, "wt-driver-clean")
    _seed(tmp_config_dir=tmp_config_dir, worktree=worktree)
    runner = FakeCodexRunner(returncode=0, output_file_content=_reviewer_doc())

    with patch("cw.codex_background._post_review_comment") as post_mock:
        run_codex_review_stage(
            ticket_id="T-1",
            session_id="sess-1",
            wall_clock_budget_seconds=None,
            runner=runner,
        )

    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result_source is LastResultSource.EXECUTOR_DIRECT
    assert session.last_result is not None
    assert session.last_result["status"] == "stage_complete"
    post_mock.assert_called_once()


def test_run_codex_review_stage_no_running_task_raises(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A session with no matching RUNNING dev-queue row is refused."""
    worktree = _worktree_with_change(make_git_repo, "wt-driver-no-task")
    _write_clients_yaml_for_test(tmp_config_dir, [("test", str(worktree))])
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(id="sess-2", client="test", worktree_path=worktree)
            ]
        )
    )

    with pytest.raises(CwError, match="T-missing"):
        run_codex_review_stage(
            ticket_id="T-missing",
            session_id="sess-2",
            wall_clock_budget_seconds=None,
        )


def test_run_codex_review_stage_unknown_session_raises(
    tmp_config_dir: Path,
) -> None:
    """No session at all in state → CwError."""
    with pytest.raises(CwError, match="sess-unknown"):
        run_codex_review_stage(
            ticket_id="T-1",
            session_id="sess-unknown",
            wall_clock_budget_seconds=None,
        )


def test_run_codex_review_stage_no_worktree_path_raises(
    tmp_config_dir: Path,
) -> None:
    """A session with no worktree_path cannot be reviewed → CwError."""
    _write_clients_yaml_for_test(tmp_config_dir, [("test", "/tmp/does-not-matter")])
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(id="sess-3", client="test", worktree_path=None)
            ]
        )
    )
    from cw.dev_queue import save_dev_queue

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-1",
                    client="test",
                    stage=Stage.REVIEW,
                    status=QueueItemStatus.RUNNING,
                    session_id="sess-3",
                )
            ]
        )
    )

    with pytest.raises(CwError, match="worktree"):
        run_codex_review_stage(
            ticket_id="T-1",
            session_id="sess-3",
            wall_clock_budget_seconds=None,
        )


def test_codex_driver_module_not_imported_by_executor() -> None:
    """D-1 process-boundary invariant: cw.executor never imports cw.codex_driver.

    codex_driver.py owns the ``cw codex run`` subprocess entry point and is
    invoked only as a subprocess of CodexExecutor.spawn() (RFC 0014 S1) — a
    module-level (or any) import the other way would collapse that boundary.
    """
    repo_root = Path(__file__).resolve().parent.parent
    executor_files = list((repo_root / "src" / "cw" / "executor").rglob("*.py"))
    assert executor_files, "expected to find files under src/cw/executor"
    for path in executor_files:
        text = path.read_text(encoding="utf-8")
        assert "codex_driver" not in text, f"{path} references codex_driver"


def test_stage_constants_are_stage_agnostic_strings() -> None:
    assert STAGE_REVIEW == "review"
    assert STAGE_IMPL == "impl"
