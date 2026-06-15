"""Tests for cw.executor — StageExecutor Protocol + ClaudeNativeExecutor.

RFC 0005 A2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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


# ---------------------------------------------------------------------------
# RFC 0005 B2 — prompt/label/param assertions
# ---------------------------------------------------------------------------


def test_spawn_prompt_format(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn prompt is /auto-dev-<stage> <ticket_id> --headless."""
    worktree = make_git_repo("wt-prompt")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-42", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_calls) == 1
    assert mock_native_daemon.spawn_calls[0][1] == "/auto-dev-plan T-42 --headless"


def test_spawn_label_no_stage_suffix(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """label does NOT contain stage value (R7 worktree reuse invariant).

    The label flows through spawn_create_impl → session.name as
    ``{client}/{label}``. Assert it equals ``test/auto-dev/T-42`` (no stage
    suffix) so all stages share one branch/worktree.
    """
    from cw.config import load_state

    worktree = make_git_repo("wt-label")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-42", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_calls) == 1
    state = load_state()
    session_names = [s.name for s in state.sessions]
    assert any("auto-dev/T-42" in name for name in session_names), (
        f"Expected session name containing 'auto-dev/T-42', got {session_names}"
    )
    assert not any("auto-dev/T-42/impl" in name for name in session_names), (
        f"Session name must not contain stage suffix, got {session_names}"
    )


@pytest.mark.parametrize(
    ("stage", "expected_cmd"),
    [
        (Stage.PLAN, "/auto-dev-plan"),
        (Stage.IMPL, "/auto-dev-impl"),
        (Stage.REVIEW, "/auto-dev-review"),
        (Stage.FINALIZE, "/auto-dev-finalize"),
    ],
)
def test_spawn_prompt_per_stage(
    stage: Stage,
    expected_cmd: str,
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Each Stage produces the correct /auto-dev-<stage> command in the prompt."""
    worktree = make_git_repo(f"wt-{stage.value}")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=stage, task=task, worktree=worktree, client=client)

    prompt = mock_native_daemon.spawn_calls[0][1]
    assert prompt.startswith(expected_cmd), f"Expected {expected_cmd!r}, got {prompt!r}"
    assert "T-1 --headless" in prompt


def test_spawn_wall_clock_budget_forwarded(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """wall_clock_budget_seconds is forwarded to spawn_create_impl."""
    worktree = make_git_repo("wt-budget")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    # If wall_clock_budget_seconds is forwarded, spawn_create_impl writes
    # it into cw-context.json. We just verify no error and spawn called.
    executor.spawn(
        stage=Stage.PLAN,
        task=task,
        worktree=worktree,
        client=client,
        wall_clock_budget_seconds=3600,
    )

    assert len(mock_native_daemon.spawn_calls) == 1


def test_spawn_parent_forwarded(
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """parent param is accepted by executor.spawn (no error)."""
    worktree = make_git_repo("wt-parent")
    client = _make_client(worktree)
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    # parent=None is valid (no parent session validation when None)
    executor.spawn(
        stage=Stage.IMPL,
        task=task,
        worktree=worktree,
        client=client,
        parent=None,
    )

    assert len(mock_native_daemon.spawn_calls) == 1
