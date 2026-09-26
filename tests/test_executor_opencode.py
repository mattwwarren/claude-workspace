"""Tests for cw.executor.opencode — OpencodeExecutor (#1669)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.executor import OpencodeExecutor, resolve_executor
from cw.executor.core import FakeFireAndForgetRunner
from cw.local_runner import LIVENESS_UNAVAILABLE, UNEXPECTED_ERROR
from cw.models import (
    OPENCODE_BACKEND,
    ClientConfig,
    LastResultSource,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    OpencodeRunner,
)
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# OpencodeExecutor (#1669)
# ---------------------------------------------------------------------------


def test_resolve_executor_opencode(tmp_config_dir: Path) -> None:
    """resolve_executor returns OpencodeExecutor for OPENCODE_BACKEND."""
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp"),
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(backend=OPENCODE_BACKEND)}
        ),
    )
    executor = resolve_executor(task, client)
    assert isinstance(executor, OpencodeExecutor)


def test_opencode_executor_unsupported_stage_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """HARDEN stage → blocked/opencode_harden_not_implemented."""
    worktree = make_git_repo("wt-opencode-wrong-stage")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.HARDEN)

    executor.spawn(stage=Stage.HARDEN, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.stage_reached == STAGE4A_MERGE_GATE
    assert result.blocker is not None
    assert result.blocker.reason == "opencode_harden_not_implemented"


def test_opencode_executor_blocked_binary_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """opencode_available() is False → blocked/opencode_not_found."""
    worktree = make_git_repo("wt-opencode-binary-missing")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.FINALIZE)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(
            stage=Stage.FINALIZE, task=task, worktree=worktree, client=client
        )

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.blocker.retry_eligible is True
    assert result.stage_reached == STAGE4A_MERGE_GATE


def test_opencode_executor_blocked_binary_missing_plan_stage_marker(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """PLAN-stage binary-missing failure carries stage1_plan, never stage2_impl.

    A stage2_impl marker on a PLAN-stage blocked sentinel classifies as a
    later-stage self-escalation and dispatch walks task.stage forward
    PLAN→IMPL (_resolve_stage_walk), silently skipping planning.
    """
    worktree = make_git_repo("wt-opencode-binary-missing-plan")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.stage_reached == "stage1_plan"
    assert result.blocker.stage == "stage1_plan"


def test_opencode_executor_spawn_runner_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Happy path: FINALIZE pre-flight passes → launch() called, session left ACTIVE.

    Fire-and-forget: liveness handle stored, no result written, session ACTIVE.
    The argv's trailing positional is the finalize prompt (not the plan message).
    """
    worktree = make_git_repo("wt-opencode-runner-path")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="genhealth/glm-5.2")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.FINALIZE)

    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.FINALIZE, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        assert call["argv"][0] == "opencode"
        assert "--format" in call["argv"]
        assert "--pure" in call["argv"]
        assert "--auto" in call["argv"]
        assert "--model" in call["argv"]
        prompt = call["argv"][-1]
        assert "auto-dev-finalize.md" in prompt
        assert "T-1" in prompt
        # FINALIZE's prompt points at the command file -- it never gets a
        # session_id threaded in (build_stage_prompt's finalize branch takes
        # no session_id param).
        assert "--session-id" not in prompt

        state = load_state()
        session = next(s for s in state.sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.local_liveness.pid > 0
        assert session.local_liveness.backend == "opencode"
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_spawn_impl_stage(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """IMPL stage spawn → launch() called with impl prompt, session left ACTIVE.

    Verifies that opencode is no longer FINALIZE-only: the IMPL stage
    pre-flight builds a self-contained impl prompt (the auto-dev-impl.md
    command file requires Claude Code-only machinery, so the prompt must
    not point at it).
    """
    worktree = make_git_repo("wt-opencode-impl-stage")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="genhealth/glm-5.2")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        assert call["argv"][0] == "opencode"
        assert "--auto" in call["argv"]
        prompt = call["argv"][-1]
        assert "auto-dev-impl.md" not in prompt
        assert "T-1" in prompt
        assert "stage2_impl" in prompt
        assert "Auto-Dev-Stage: impl-complete" in prompt

        state = load_state()
        session = next(s for s in state.sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_spawn_liveness_unavailable(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """start_time None → blocked/liveness_unavailable, session COMPLETED."""
    worktree = make_git_repo("wt-opencode-liveness-unavailable")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.FINALIZE)

    try:
        with (
            patch("cw.executor.opencode.opencode_available", return_value=True),
            patch("cw.executor.core.read_process_start_time_ns", return_value=None),
        ):
            executor.spawn(
                stage=Stage.FINALIZE, task=task, worktree=worktree, client=client
            )

        state = load_state()
        session = find_completed_session(state)
        result = AutoDevResult.model_validate(session.last_result)
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == LIVENESS_UNAVAILABLE
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_exception_handler(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Unexpected error during launch → blocked/unexpected_error, session COMPLETED."""

    _boom_msg = "boom"

    class _ExplodingRunner:
        def launch(self, *args: object) -> object:
            raise RuntimeError(_boom_msg)

    worktree = make_git_repo("wt-opencode-explode")

    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(
        config=config, runner=cast("OpencodeRunner", _ExplodingRunner())
    )
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.FINALIZE)

    with (
        patch("cw.executor.opencode.opencode_available", return_value=True),
        pytest.raises(RuntimeError, match=_boom_msg),
    ):
        executor.spawn(
            stage=Stage.FINALIZE, task=task, worktree=worktree, client=client
        )

    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR


def test_opencode_executor_spawn_threads_session_id_into_prompt(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The sid the spawn skeleton creates threads into the pre-flight prompt.

    _spawn_fire_and_forget creates the session and obtains sid BEFORE calling
    preflight_fn() -- this proves the sid embedded in the prompt's
    --session-id is the SAME one the session was created under, not a
    placeholder or a mismatched value.
    """
    worktree = make_git_repo("wt-opencode-session-id")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        prompt = fake_runner.calls[0]["argv"][-1]
        assert f"--session-id {sid}" in prompt
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=OPENCODE_BACKEND)
    executor = OpencodeExecutor(config=config)
    schema = executor.stage_sentinel_schema(Stage.IMPL)
    assert "properties" in schema
    assert "status" in schema["properties"]
