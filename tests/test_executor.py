"""Tests for cw.executor — StageExecutor Protocol, ClaudeNativeExecutor, LocalExecutor.

RFC 0005 A2 / F3.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.executor import (
    ClaudeNativeExecutor,
    LocalExecutor,
    StageExecutor,
    resolve_executor,
    resolve_executor_config,
)
from cw.local_runner import (
    AIDER_ERROR,
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    PLAN_MISSING,
    UNEXPECTED_ERROR,
    FakeAiderRunner,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    LOCAL_BACKEND,
    ClientConfig,
    LaneConfig,
    SessionStatus,
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


# ---------------------------------------------------------------------------
# RFC 0005 E1 — resolve_executor_config + resolve_executor
# ---------------------------------------------------------------------------


def test_resolve_executor_config_no_lane_pipeline(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Lane exists but has no pipeline → client pipeline executor config returned."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(model="sonnet")}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="default")

    config = resolve_executor_config(Stage.IMPL, task, client)

    assert config.model == "sonnet"
    assert config.backend == CLAUDE_NATIVE_BACKEND


def test_resolve_executor_config_lane_override(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Lane pipeline.executors[stage] wins over client pipeline.executors[stage]."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        worker_model="sonnet",
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(model="sonnet")}
        ),
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={Stage.IMPL: StageExecutorConfig(model="haiku")}
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    config = resolve_executor_config(Stage.IMPL, task, client)

    assert config.model == "haiku"


def test_resolve_executor_config_lane_missing_stage_falls_back_to_client(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Lane pipeline omits PLAN; client pipeline has PLAN → client fallback fires.

    Exercises level 2 of the three-level cascade: lane.executors[stage] absent,
    so client.executors[stage] is used rather than the bare default.
    """
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.PLAN: StageExecutorConfig(model="opus")}
        ),
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={Stage.IMPL: StageExecutorConfig(model="haiku")}
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    config = resolve_executor_config(Stage.PLAN, task, client)

    assert config.model == "opus"


def test_resolve_executor_config_lane_override_stage_not_in_lane_or_client(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Lane and client both have no entry for PLAN → default StageExecutorConfig."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={Stage.IMPL: StageExecutorConfig(model="haiku")}
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    config = resolve_executor_config(Stage.PLAN, task, client)

    assert config.backend == CLAUDE_NATIVE_BACKEND
    assert config.model is None


def test_resolve_executor_config_falsy_lane_skips_lookup(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """task.lane='' → lane lookup skipped entirely; client pipeline used."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(model="opus")}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="")

    config = resolve_executor_config(Stage.REVIEW, task, client)

    assert config.model == "opus"


def test_resolve_executor_returns_claude_native(
    tmp_config_dir: Path, tmp_path: Path, mock_native_daemon: FakeNativeDaemonClient
) -> None:
    """resolve_executor returns ClaudeNativeExecutor for default backend."""
    client = _make_client(tmp_path)
    task = TicketTask(ticket_id="T-1", client="test")

    executor = resolve_executor(task, client, native_daemon=mock_native_daemon)

    assert isinstance(executor, ClaudeNativeExecutor)


def test_resolve_executor_unknown_backend_raises(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """resolve_executor raises ValueError for an unrecognised backend."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(backend="alien")}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with pytest.raises(ValueError, match="unknown executor backend"):
        resolve_executor(task, client)


# ---------------------------------------------------------------------------
# RFC 0005 E2 — heterogeneous models end-to-end proof
# ---------------------------------------------------------------------------

_E2_OPUS_MODEL = "claude-opus-4-8"
_E2_SONNET_MODEL = "claude-sonnet-4-6-20251015"


@pytest.mark.parametrize(
    ("stage", "expected_model"),
    [
        (Stage.PLAN, _E2_OPUS_MODEL),
        (Stage.IMPL, _E2_SONNET_MODEL),
        (Stage.REVIEW, _E2_SONNET_MODEL),
    ],
)
def test_e2_heterogeneous_models_per_stage(
    stage: Stage,
    expected_model: str,
    tmp_config_dir: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Distinct models per stage all resolve and forward --model correctly.

    Pipeline: opus for PLAN, sonnet for IMPL and REVIEW. worker_model is
    unset so the stage model is the sole source of the flag. Assert that
    spawn_extra_args carries exactly one --model flag with the right value.
    """
    worktree = make_git_repo(f"wt-e2-{stage.value}")
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        pipeline=StagePipelineConfig(
            executors={
                Stage.PLAN: StageExecutorConfig(model=_E2_OPUS_MODEL),
                Stage.IMPL: StageExecutorConfig(model=_E2_SONNET_MODEL),
                Stage.REVIEW: StageExecutorConfig(model=_E2_SONNET_MODEL),
            }
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test")
    executor = ClaudeNativeExecutor(native_daemon=mock_native_daemon)

    executor.spawn(stage=stage, task=task, worktree=worktree, client=client)

    assert len(mock_native_daemon.spawn_extra_args) == 1
    args = mock_native_daemon.spawn_extra_args[0]
    assert args is not None
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == expected_model


# ---------------------------------------------------------------------------
# RFC 0005 F3 — LocalExecutor + resolve_executor LOCAL_BACKEND
# ---------------------------------------------------------------------------


def _make_local_client(tmp_path: Path, *, endpoint: str | None = None) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={
                Stage.IMPL: StageExecutorConfig(
                    backend=LOCAL_BACKEND,
                    model="qwen2.5-coder:32b",
                    endpoint=endpoint,
                )
            }
        ),
    )


def test_resolve_executor_returns_local_executor(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """resolve_executor returns LocalExecutor when backend=local."""
    client = _make_local_client(tmp_path, endpoint="http://localhost:1234/v1")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    executor = resolve_executor(task, client)

    assert isinstance(executor, LocalExecutor)
    assert isinstance(executor, StageExecutor)


def test_local_executor_blocked_endpoint_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """endpoint=None → blocked/endpoint_not_configured before runner is called."""
    worktree = make_git_repo("wt-local-ep-none")
    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    assert result_raw is not None
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == ENDPOINT_NOT_CONFIGURED


def test_local_executor_blocked_aider_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """shutil.which('aider') is None → blocked/aider_not_found."""
    worktree = make_git_repo("wt-local-aider-missing")
    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.shutil.which", return_value=None):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NOT_FOUND
    assert result.blocker.retry_eligible is True


def test_local_executor_blocked_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Absent .cw/plan.md → blocked/plan_missing."""
    worktree = make_git_repo("wt-local-plan-missing")
    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/aider"):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_spawn_runner_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Happy path: pre-flight passes → runner.run() is called once."""
    worktree = make_git_repo("wt-local-runner-path")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner(returncode=1, stderr="aider internal error")
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-99", client="test", stage=Stage.IMPL)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/aider"):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 1
    call = fake_runner.calls[0]
    argv = cast("list[str]", call["argv"])
    assert "openai/qwen" in " ".join(argv)

    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    # returncode=1 → aider_error blocked
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_ERROR


def test_local_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """LocalExecutor.stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config)

    schema = executor.stage_sentinel_schema(Stage.IMPL)

    assert schema == AutoDevResult.model_json_schema()


def test_local_executor_stage_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Happy path: aider exit 0 with new commit → stage_complete persisted."""
    worktree = make_git_repo("wt-local-stage-complete")
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(worktree)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "origin", "main"],
        check=True,
        capture_output=True,
    )
    # Simulate an aider commit above the fork point.
    (worktree / "aider_out.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "aider impl"],
        check=True,
        capture_output=True,
    )

    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-100", client="test", stage=Stage.IMPL)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/aider"):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    assert result_raw is not None
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage2_impl"
    assert len(result.commits) >= 1
    AutoDevResult.model_validate(result.model_dump(mode="json"))


def test_local_executor_exception_handler_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Uncaught exception in Steps 3-5 → session COMPLETED, exception re-raised."""
    worktree = make_git_repo("wt-local-exc-handler")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/aider"),
        patch("cw.executor.synthesize_result", side_effect=RuntimeError("git boom")),
        pytest.raises(RuntimeError, match="git boom"),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR


# ---------------------------------------------------------------------------
# RFC 0005 F3 #896 — LocalExecutor fetches plan from GitHub tracker fallback
# ---------------------------------------------------------------------------


def _write_tracker_config(workspace: Path, tracker: str) -> None:
    """Write a minimal .claude/project-config.yaml for the given tracker."""
    config_dir = workspace / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project-config.yaml").write_text(
        f"tracking:\n  primary:\n    system: {tracker}\n",
        encoding="utf-8",
    )


def test_local_executor_fetches_plan_from_tracker_when_absent(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues tracker + fetcher returns plan → aider runs."""
    workspace = make_git_repo("wt-tracker-fetch-workspace")
    worktree = make_git_repo("wt-tracker-fetch")
    _write_tracker_config(workspace, "github-issues")

    plan_body = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"

    fake_runner = FakeAiderRunner(returncode=1, stderr="aider ran")
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/aider"),
        patch(
            "cw.executor.GithubIssuePlanFetcher.fetch",
            return_value=plan_body,
        ),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    # Aider was called (pre-flight passed the plan check)
    assert len(fake_runner.calls) == 1


def test_local_executor_plan_missing_when_tracker_returns_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues tracker + fetcher returns None → plan_missing."""
    workspace = make_git_repo("wt-tracker-none-workspace")
    worktree = make_git_repo("wt-tracker-none")
    _write_tracker_config(workspace, "github-issues")

    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/aider"),
        patch("cw.executor.GithubIssuePlanFetcher.fetch", return_value=None),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_no_tracker_no_plan_is_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md, no tracker config → plan_missing (no fetch attempted)."""
    workspace = make_git_repo("wt-no-tracker-workspace")
    worktree = make_git_repo("wt-no-tracker")
    # No .claude/project-config.yaml in workspace → resolve_tracker returns None

    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/aider"):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_sets_plan_source_github_issue_existing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """plan_source='github_issue_existing' set when plan fetched from tracker."""
    from typing import Any

    from cw.local_runner import make_blocked

    workspace = make_git_repo("wt-plansrc-workspace")
    worktree = make_git_repo("wt-plansrc")
    _write_tracker_config(workspace, "github-issues")

    plan_body = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"
    captured_kwargs: list[dict[str, Any]] = []

    def _fake_synthesize(**kwargs: Any) -> AutoDevResult:
        captured_kwargs.append(kwargs)
        return make_blocked(
            ticket_id=kwargs["task"].ticket_id,
            worktree=kwargs["worktree"],
            reason=PLAN_MISSING,
        )

    fake_runner = FakeAiderRunner(returncode=0)
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/aider"),
        patch("cw.executor.GithubIssuePlanFetcher.fetch", return_value=plan_body),
        patch("cw.executor.synthesize_result", side_effect=_fake_synthesize),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["plan_source"] == "github_issue_existing"
