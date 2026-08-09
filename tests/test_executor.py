"""Tests for cw.executor — StageExecutor Protocol, ClaudeNativeExecutor, LocalExecutor.

RFC 0005 A2 / F3.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.exceptions import EmitSessionNotFoundError
from cw.executor import (
    ClaudeNativeExecutor,
    LocalExecutor,
    OpencodeExecutor,
    StageExecutor,
    _local_preflight,
    _PreflightOK,
    resolve_executor,
    resolve_executor_config,
)
from cw.executor_diagnostics import (
    ExecutorFailure,
    diagnostics_bundle_dir,
    render_bundle_path,
)
from cw.local_runner import (
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    LIVENESS_UNAVAILABLE,
    PLAN_MISSING,
    UNEXPECTED_ERROR,
    FakeAiderRunner,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    LaneConfig,
    LastResultSource,
    LocalLivenessHandle,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_NOT_FOUND,
    FakeOpencodeRunner,
    OpencodeRunner,
)
from cw.result import EmitOutcome
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    config = resolve_executor_config(Stage.IMPL, task, client)
    executor = ClaudeNativeExecutor(config=config, native_daemon=mock_native_daemon)

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
    executor = ClaudeNativeExecutor(config=StageExecutorConfig())
    schema = executor.stage_sentinel_schema(Stage.IMPL)
    assert schema == AutoDevResult.model_json_schema()


def test_isinstance_check(
    tmp_config_dir: Path,
) -> None:
    """isinstance(ClaudeNativeExecutor(config=...), StageExecutor) is True."""
    assert isinstance(ClaudeNativeExecutor(config=StageExecutorConfig()), StageExecutor)


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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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
    executor = ClaudeNativeExecutor(
        config=StageExecutorConfig(), native_daemon=mock_native_daemon
    )

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


def test_resolve_executor_config_lane_reasoning_effort_beats_client(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#1711: reasoning_effort rides the SAME lane > client > default precedence
    every other StageExecutorConfig field does — no separate resolution path."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={
                Stage.REVIEW: StageExecutorConfig(
                    backend=CODEX_BACKEND, reasoning_effort="high"
                )
            }
        ),
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={
                        Stage.REVIEW: StageExecutorConfig(
                            backend=CODEX_BACKEND, reasoning_effort="medium"
                        )
                    }
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    assert (
        resolve_executor_config(Stage.REVIEW, task, client).reasoning_effort == "medium"
    )


def test_resolve_executor_config_reasoning_effort_default_tier_is_high(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Neither lane nor client configures REVIEW → the bare StageExecutorConfig()
    fallback IS the "default" tier, so the field default resolves."""
    client = ClientConfig(name="test", workspace_path=tmp_path)
    task = TicketTask(ticket_id="T-1", client="test")

    assert (
        resolve_executor_config(Stage.REVIEW, task, client).reasoning_effort == "high"
    )


def test_resolve_executor_config_fix_profile_mode_lane_beats_client(
    tmp_path: Path,
) -> None:
    """The write-profile rollout gate uses lane > client > default resolution."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={
                Stage.REVIEW: StageExecutorConfig(
                    backend=CODEX_BACKEND,
                    codex_fix_lean_profile_mode="shadow",
                )
            }
        ),
        lanes=[
            LaneConfig(
                name="canary",
                pipeline=StagePipelineConfig(
                    executors={
                        Stage.REVIEW: StageExecutorConfig(
                            backend=CODEX_BACKEND,
                            codex_fix_lean_profile_mode="enabled",
                        )
                    }
                ),
            )
        ],
    )
    task = TicketTask(
        ticket_id="T-fix-profile", client="test", stage=Stage.REVIEW, lane="canary"
    )

    resolved = resolve_executor_config(Stage.REVIEW, task, client)

    assert resolved.codex_fix_lean_profile_mode == "enabled"
    assert StageExecutorConfig().codex_fix_lean_profile_mode == "off"


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
    config = resolve_executor_config(stage, task, client)
    executor = ClaudeNativeExecutor(config=config, native_daemon=mock_native_daemon)

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
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == ENDPOINT_NOT_CONFIGURED


def test_local_executor_blocked_aider_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """aider_available() is False → blocked/aider_not_found."""
    worktree = make_git_repo("wt-local-aider-missing")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.aider_available", return_value=False):
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
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.aider_available", return_value=True):
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
    """Happy path: pre-flight passes → launch() called once, session left ACTIVE.

    The fire-and-forget launch records a liveness handle and returns the sid; it
    does NOT block, synthesize a result, or write last_result — reconcile/local
    harvest completes the session once the process exits.
    """
    worktree = make_git_repo("wt-local-runner-path")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-99", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.aider_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        argv = cast("list[str]", call["argv"])
        assert "openai/qwen" in " ".join(argv)

        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        # Session stays ACTIVE; liveness handle recorded; no result synthesized.
        assert session.status == SessionStatus.ACTIVE
        assert isinstance(session.local_liveness, LocalLivenessHandle)
        assert session.local_liveness.pid == fake_runner.procs[0].pid
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """LocalExecutor.stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config)

    schema = executor.stage_sentinel_schema(Stage.IMPL)

    assert schema == AutoDevResult.model_json_schema()


def test_local_executor_launch_records_liveness_and_returns_active(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Pre-flight passes → session ACTIVE with a LocalLivenessHandle; sid returned."""
    worktree = make_git_repo("wt-local-launch-active")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-100", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.aider_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert isinstance(sid, str)
        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        assert session.status == SessionStatus.ACTIVE
        assert isinstance(session.local_liveness, LocalLivenessHandle)
        # Start-time was captured (live process) — a positive ns value.
        assert session.local_liveness.start_time_ns > 0
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_exception_handler_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """launch() raises OSError → session COMPLETED + UNEXPECTED_ERROR, re-raised."""
    worktree = make_git_repo("wt-local-exc-handler")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.aider_available", return_value=True),
        patch.object(fake_runner, "launch", side_effect=OSError("exec boom")),
        pytest.raises(OSError, match="exec boom"),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert (
        result.blocker.details == "unexpected error during aider launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )


def test_local_executor_proc_stat_unreadable_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """read_process_start_time_ns returns None → session COMPLETED, no exception.

    When /proc/<pid>/stat is unreadable immediately after launch (process exited
    before exec or /proc transiently unavailable), storing start_time_ns=0 would
    make every liveness check return False — triggering premature harvest while
    aider is still running. Instead the orphan is killed and the session completes
    synchronously with LIVENESS_UNAVAILABLE so dispatch retries (no liveness handle
    persisted, no exception propagated).
    """
    import contextlib

    worktree = make_git_repo("wt-proc-unreadable")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-proc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.aider_available", return_value=True),
        patch("cw.executor.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    # FakeAiderRunner spawned a sleep process; the None path kills it but
    # suppress in case it already exited.
    for proc in fake_runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.local_liveness is None  # no stale handle recorded
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == LIVENESS_UNAVAILABLE
    # details carries the liveness detail plus a diagnostics-bundle pointer
    # (#1239) — exact match, since the session id is now captured.
    assert result.blocker.details == (
        f"process {fake_runner.procs[-1].pid} start-time unavailable "
        f"[diagnostics: {render_bundle_path(sid)}]"
    )


def test_local_executor_emit_session_not_found_logs_and_skips(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """emit_result_locked raises EmitSessionNotFoundError → spawn() returns
    normally and the session's status is not force-completed (R4).

    Exercises _complete_session_via_door's not-found catch on the LocalExecutor
    main completion path (Site 1, pre-flight-blocked branch).
    """
    worktree = make_git_repo("wt-local-emit-not-found")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch(
        "cw.executor.emit_result_locked",
        side_effect=EmitSessionNotFoundError("not found", session_id="ignored"),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = next((s for s in state.sessions if s.id == sid), None)
    assert session is not None
    assert session.status == SessionStatus.ACTIVE
    assert session.last_result is None


def test_local_executor_door_refusal_still_completes_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Door refusal (terminal result already recorded by another writer) still
    transitions the session to COMPLETED and emits SESSION_COMPLETED (R5) --
    refusal affects only the last_result write, not status/event bookkeeping.
    """
    worktree = make_git_repo("wt-local-door-refusal")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    def _refuse(
        payload: dict[str, object], sid: str, *, source: LastResultSource
    ) -> EmitOutcome:
        del payload, source
        return EmitOutcome(
            session_id=sid,
            result=None,
            prior_status="shipped",
            refused=True,
            existing_result={"status": "shipped"},
            existing_source=LastResultSource.STOP_HOOK_HARVEST,
        )

    with (
        patch("cw.executor.emit_result_locked", side_effect=_refuse),
        patch("cw.executor._record_orchestrator_event") as record_mock,
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    state = load_state()
    session = next((s for s in state.sessions if s.id == sid), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    record_mock.assert_called_once()


def test_local_executor_liveness_unavailable_persists_runtime_error_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The LIVENESS_UNAVAILABLE branch persists a runtime_error bundle (#1239)."""
    import contextlib

    worktree = make_git_repo("wt-liveness-diag")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-liveness-diag", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.aider_available", return_value=True),
        patch("cw.executor.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    for proc in fake_runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()

    # Filename now carries an occurred_at timestamp suffix (#1330 item 7).
    [path] = list(diagnostics_bundle_dir(sid).glob("aider-runtime_error-*.json"))
    assert path.exists()
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "runtime_error"
    assert failure.executor_name == "aider"
    # Aider argv is redacted wholesale on the --message value.
    assert "--message" in failure.argv_sanitized
    idx = failure.argv_sanitized.index("--message")
    assert failure.argv_sanitized[idx + 1].startswith("<redacted:")


def test_local_executor_unexpected_error_persists_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The generic except branch persists a runtime_error bundle (#1239)."""
    worktree = make_git_repo("wt-unexpected-diag")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-unexpected-diag", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.aider_available", return_value=True),
        patch.object(fake_runner, "launch", side_effect=OSError("exec boom")),
        pytest.raises(OSError, match="exec boom"),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    # spawn raised, so recover sid from the created session in state.
    session = next(s for s in load_state().sessions if s.last_result is not None)
    # Filename now carries an occurred_at timestamp suffix (#1330 item 7).
    [path] = list(diagnostics_bundle_dir(session.id).glob("aider-runtime_error-*.json"))
    assert path.exists()
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "runtime_error"
    assert failure.executor_name == "aider"


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

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.aider_available", return_value=True),
            patch(
                "cw.executor.GithubIssuePlanFetcher.fetch",
                return_value=plan_body,
            ),
        ):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        # aider was launched (pre-flight passed the plan check)
        assert len(fake_runner.calls) == 1
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_plan_missing_when_tracker_returns_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues tracker + fetcher returns None → plan_missing."""
    workspace = make_git_repo("wt-tracker-none-workspace")
    worktree = make_git_repo("wt-tracker-none")
    _write_tracker_config(workspace, "github-issues")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.aider_available", return_value=True),
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

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with patch("cw.executor.aider_available", return_value=True):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_launch_reached_after_tracker_fetch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Plan fetched from tracker → launch() reached and liveness recorded.

    The fire-and-forget launch no longer synthesizes a result inline, so there
    is no plan_source threading to assert; the observable contract is that
    pre-flight passed (aider was launched) and the session carries a handle.
    """
    workspace = make_git_repo("wt-plansrc-workspace")
    worktree = make_git_repo("wt-plansrc")
    _write_tracker_config(workspace, "github-issues")

    plan_body = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.aider_available", return_value=True),
            patch("cw.executor.GithubIssuePlanFetcher.fetch", return_value=plan_body),
        ):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        assert isinstance(session.local_liveness, LocalLivenessHandle)
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_preflight_success_returns_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_local_preflight returns _PreflightOK on all-checks-pass.

    Locks in the discriminated-union contract: callers narrow with
    isinstance(_PreflightOK) instead of testing the first element for None.
    """
    worktree = make_git_repo("wt-preflight-ok")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-ok", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.endpoint == "http://localhost:1234/v1"
    assert result.model == "qwen"
    assert "do the thing" in result.task_message


def test_local_preflight_ok_model_none_defaults_to_empty_string(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_PreflightOK.model is '' when config.model is None."""
    worktree = make_git_repo("wt-preflight-model-none")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model=None, endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-mnone", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.model == ""


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


def test_opencode_executor_blocked_binary_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """opencode_available() is False → blocked/opencode_not_found."""
    worktree = make_git_repo("wt-opencode-binary-missing")
    fake_runner = FakeOpencodeRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.opencode_available", return_value=False):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.blocker.retry_eligible is True


def test_opencode_executor_blocked_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Absent .cw/plan.md → blocked/plan_missing."""
    worktree = make_git_repo("wt-opencode-plan-missing")
    fake_runner = FakeOpencodeRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.opencode_available", return_value=True):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_opencode_executor_spawn_runner_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Happy path: pre-flight passes → launch() called, session left ACTIVE.

    Fire-and-forget: liveness handle stored, no result written, session ACTIVE.
    """
    worktree = make_git_repo("wt-opencode-runner-path")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeOpencodeRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="genhealth/glm-5.2")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        assert call["argv"][0] == "opencode"
        assert "--format" in call["argv"]
        assert "--pure" in call["argv"]
        assert "--model" in call["argv"]

        state = load_state()
        session = next(s for s in state.sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.local_liveness.pid > 0
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
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeOpencodeRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.opencode_available", return_value=True),
            patch("cw.executor.read_process_start_time_ns", return_value=None),
        ):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
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
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(
        config=config, runner=cast("OpencodeRunner", _ExplodingRunner())
    )
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.opencode_available", return_value=True),
        pytest.raises(RuntimeError, match=_boom_msg),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR


def test_opencode_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=OPENCODE_BACKEND)
    executor = OpencodeExecutor(config=config)
    schema = executor.stage_sentinel_schema(Stage.IMPL)
    assert "properties" in schema
    assert "status" in schema["properties"]
