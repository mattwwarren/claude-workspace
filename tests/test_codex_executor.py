"""Tests for cw.executor.CodexExecutor — REVIEW-only codex backend.

RFC 0005 F1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from cw.auto_dev_result import AutoDevResult
from cw.codex_runner import FakeCodexRunner
from cw.config import load_state
from cw.executor import (
    CODEX_ERROR,
    CODEX_NOT_FOUND,
    CODEX_REVIEW_ONLY,
    CODEX_TIMEOUT,
    CodexExecutor,
    StageExecutor,
    _build_codex_argv,
    resolve_executor,
)
from cw.local_runner import make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _persisted_result(client_workspace: Path | None = None) -> AutoDevResult:
    """Load the single persisted last_result and validate it."""
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    assert result_raw is not None
    return AutoDevResult.model_validate(result_raw)


def test_codex_executor_wrong_stage_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn() on a non-REVIEW stage → blocked/codex_review_only."""
    worktree = make_git_repo("wt-codex-wrong-stage")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 0
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_ONLY


def test_codex_executor_codex_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """shutil.which('codex') is None → blocked/codex_not_found."""
    worktree = make_git_repo("wt-codex-missing")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value=None):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 0
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_NOT_FOUND


def test_codex_executor_timeout(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Runner timed_out → blocked/codex_timeout, retry_eligible=True."""
    worktree = make_git_repo("wt-codex-timeout")
    runner = FakeCodexRunner(timed_out=True)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/codex"):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 1
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_TIMEOUT
    assert result.blocker.retry_eligible is True


def test_codex_executor_nonzero_exit(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Runner returncode != 0 → blocked/codex_error with stderr tail."""
    worktree = make_git_repo("wt-codex-nonzero")
    runner = FakeCodexRunner(returncode=1, stderr="codex internal error")
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/codex"):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 1
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_ERROR


def test_codex_executor_stage_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Runner exit 0 with findings → stage_complete, session COMPLETED."""
    worktree = make_git_repo("wt-codex-complete")
    runner = FakeCodexRunner(returncode=0, stdout="## Findings\n\nLooks good.")
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.executor._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 1
    post_mock.assert_called_once()
    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage3_review"
    # Round-trips through the strict validator.
    AutoDevResult.model_validate(result.model_dump(mode="json"))

    state = load_state()
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED


def test_resolve_executor_returns_codex_executor(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """resolve_executor returns CodexExecutor when backend=codex."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend=CODEX_BACKEND)}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    executor = resolve_executor(task, client)

    assert isinstance(executor, CodexExecutor)
    assert isinstance(executor, StageExecutor)


def test_build_codex_argv_with_model() -> None:
    """A model maps to a trailing -m flag."""
    argv = _build_codex_argv("gpt-4", "main")
    assert argv == ["codex", "exec", "review", "--base", "main", "-m", "gpt-4"]


def test_build_codex_argv_no_model() -> None:
    """A None model omits the -m flag."""
    argv = _build_codex_argv(None, "main")
    assert argv == ["codex", "exec", "review", "--base", "main"]


def test_make_blocked_backward_compat(tmp_path: Path) -> None:
    """make_blocked without stage_reached defaults to stage2_impl."""
    result = make_blocked(
        ticket_id="T-1", worktree=tmp_path, reason="some_reason"
    )
    assert result.stage_reached == "stage2_impl"
    assert result.blocker is not None
    assert result.blocker.stage == "stage2_impl"


def test_make_blocked_stage_reached_override(tmp_path: Path) -> None:
    """make_blocked propagates an explicit stage_reached to both fields."""
    result = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="some_reason",
        stage_reached="stage3_review",
    )
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.stage == "stage3_review"
