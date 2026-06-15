"""Tests for cw.executor — StageExecutor Protocol + ClaudeNativeExecutor.

RFC 0005 A2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult
from cw.executor import ClaudeNativeExecutor, StageExecutor
from cw.models import (
    ClientConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.native_daemon import FakeNativeDaemonClient


def _make_client(tmp_path: Path, *, worker_model: str | None = None) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=tmp_path,
        worker_model=worker_model,
    )


def test_spawn_no_model(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """worker_model=None, no stage executor config → no --model in spawn_extra_args."""
    worktree = make_git_repo("wt-no-model")
    client = _make_client(worktree, worker_model=None)
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_extra_args) == 1
    args = mock_native_daemon.spawn_extra_args[0]
    assert args is None or "--model" not in (args or [])


def test_spawn_client_worker_model(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """client.worker_model='sonnet', no stage config → ['--model', 'sonnet'] in args."""
    worktree = make_git_repo("wt-client-model")
    client = _make_client(worktree, worker_model="sonnet")
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    args = mock_native_daemon.spawn_extra_args[0]
    assert args is not None
    assert "--model" in args
    assert args[args.index("--model") + 1] == "sonnet"


def test_spawn_stage_model_wins(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """stage_config.model='haiku' wins over client.worker_model='sonnet'.

    Exactly one --model flag in spawn_extra_args.
    """
    worktree = make_git_repo("wt-stage-model")
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        worker_model="sonnet",
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(model="haiku")}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    args = mock_native_daemon.spawn_extra_args[0]
    assert args is not None
    # Exactly one --model flag
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == "haiku"


def test_stage_sentinel_schema(
    tmp_config_dir: Path,
) -> None:
    """stage_sentinel_schema returns AutoDevResult.model_json_schema()."""
    executor = ClaudeNativeExecutor()
    schema = executor.stage_sentinel_schema(Stage.IMPL)
    assert schema == AutoDevResult.model_json_schema()


def test_isinstance_check(
    tmp_config_dir: Path,
) -> None:
    """isinstance(ClaudeNativeExecutor(), StageExecutor) is True."""
    assert isinstance(ClaudeNativeExecutor(), StageExecutor)


def test_spawn_prompt_contains_stage_command(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn emits /auto-dev-<stage> <ticket_id> --headless as the prompt."""
    worktree = make_git_repo("wt-prompt")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_calls) == 1
    _cwd, prompt = mock_native_daemon.spawn_calls[0]
    assert prompt == "/auto-dev-plan T-1 --headless"


def test_spawn_label_strips_stage_suffix(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn uses AUTO_DEV_LABEL_PREFIX + ticket_id only (no /stage.value suffix)."""

    worktree = make_git_repo("wt-label")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-2", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    from cw.config import load_state

    state = load_state()
    assert len(state.sessions) == 1
    sess = state.sessions[0]
    # Label must not contain the stage suffix
    assert "/impl" not in sess.name
    assert "T-2" in sess.name


def test_spawn_wall_clock_budget_forwarded(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """wall_clock_budget_seconds kwarg is accepted and spawn succeeds."""
    worktree = make_git_repo("wt-budget")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-3", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(
        stage=Stage.IMPL,
        task=task,
        worktree=worktree,
        client=client,
        wall_clock_budget_seconds=3600,
    )

    assert len(mock_native_daemon.spawn_calls) == 1


def test_spawn_wall_clock_budget_none_default(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Default wall_clock_budget_seconds=None does not raise."""
    worktree = make_git_repo("wt-budget-none")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-4", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_calls) == 1


def test_spawn_parent_forwarded(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """parent kwarg is accepted and spawn succeeds when parent session exists."""
    from cw.config import load_state, save_state
    from cw.models import Session, SessionPurpose, SessionStatus

    worktree = make_git_repo("wt-parent")
    client = _make_client(worktree)

    # Seed a parent session so spawn_create_impl can validate the linkage.
    parent_sess = Session(
        id="parent-session-id",
        name="test/orchestrator",
        client="test",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=worktree,
    )
    state = load_state()
    state.sessions.append(parent_sess)
    save_state(state)

    task = TicketTask(ticket_id="T-5", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(
        stage=Stage.PLAN,
        task=task,
        worktree=worktree,
        client=client,
        parent="parent-session-id",
    )

    assert len(mock_native_daemon.spawn_calls) == 1
