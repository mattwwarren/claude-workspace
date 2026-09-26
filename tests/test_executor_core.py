"""Tests for cw.executor core — StageExecutor Protocol, ClaudeNativeExecutor, resolvers.

RFC 0005 A2 / E1 / E2.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.executor import (
    ClaudeNativeExecutor,
    StageExecutor,
    _lane_pipeline,
    resolve_executor,
    resolve_executor_config,
    resolve_pipeline_stages,
)
from cw.executor.core import (
    FakeFireAndForgetRunner,
    _persist_runtime_error_diagnostics,
    _PreflightOK,
    _spawn_fire_and_forget,
)
from cw.executor_diagnostics import (
    ExecutorFailure,
    diagnostics_bundle_dir,
    render_bundle_path,
)
from cw.local_runner import (
    LIVENESS_UNAVAILABLE,
    UNEXPECTED_ERROR,
    make_blocked,
    read_process_start_time_ns,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    ClientConfig,
    CompletionReason,
    LaneConfig,
    LastResultSource,
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.executor.core import BlockedCtor, FireAndForgetRunner
    from cw.executor_diagnostics import ExecutorName
    from cw.models import LocalLivenessBackend
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


def test_resolve_executor_config_lane_without_stage_does_not_reach_later_lane(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A matched lane pipeline lacking the stage stops the walk (``break``).

    Pins the byte-identical behavior of the lane walk shared with
    ``resolve_pipeline_stages``: the first lane whose name matches AND that
    declares a pipeline is authoritative, so a later lane sharing the name is
    never consulted for the executor entry.
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
            ),
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={Stage.PLAN: StageExecutorConfig(model="sonnet")}
                ),
            ),
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    config = resolve_executor_config(Stage.PLAN, task, client)

    assert config.model == "opus"


# ---------------------------------------------------------------------------
# #1286 — lane pipeline walk (_lane_pipeline) + resolve_pipeline_stages
# ---------------------------------------------------------------------------


def test_lane_pipeline_returns_named_lanes_pipeline(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    lane_pipeline = StagePipelineConfig(stages=[Stage.PLAN, Stage.REVIEW])
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        lanes=[
            LaneConfig(name="other"),
            LaneConfig(name="debt", pipeline=lane_pipeline),
        ],
    )

    assert _lane_pipeline(client, "debt") is lane_pipeline


@pytest.mark.parametrize("lane", [None, "", "missing", "no-pipeline"])
def test_lane_pipeline_returns_none_when_no_lane_pipeline_applies(
    tmp_config_dir: Path, tmp_path: Path, lane: str | None
) -> None:
    """Falsy lane, unknown lane, and a lane with no pipeline all yield ``None``."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        lanes=[
            LaneConfig(name="no-pipeline"),
            LaneConfig(name="debt", pipeline=StagePipelineConfig(stages=[Stage.PLAN])),
        ],
    )

    assert _lane_pipeline(client, lane) is None


def test_resolve_pipeline_stages_lane_override(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A lane declaring a pipeline wins over the client default stages."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    stages=[Stage.PLAN, Stage.REVIEW, Stage.FINALIZE]
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    assert resolve_pipeline_stages(task, client) == [
        Stage.PLAN,
        Stage.REVIEW,
        Stage.FINALIZE,
    ]


def test_resolve_pipeline_stages_lane_executor_only_uses_client_stages(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A lane executor override does not replace a custom client stage list."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(stages=[Stage.PLAN, Stage.REVIEW]),
        lanes=[
            LaneConfig(
                name="debt",
                pipeline=StagePipelineConfig(
                    executors={Stage.PLAN: StageExecutorConfig(model="opus")}
                ),
            )
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    assert resolve_pipeline_stages(task, client) == [Stage.PLAN, Stage.REVIEW]


def test_resolve_pipeline_stages_lane_without_pipeline_uses_client_default(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(stages=[Stage.PLAN, Stage.IMPL]),
        lanes=[LaneConfig(name="debt")],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="debt")

    assert resolve_pipeline_stages(task, client) == [Stage.PLAN, Stage.IMPL]


def test_resolve_pipeline_stages_falsy_lane_uses_client_default(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """``TicketTask.lane`` is ``str``; ``""`` is its falsy case."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(stages=[Stage.PLAN, Stage.IMPL]),
        lanes=[
            LaneConfig(name="debt", pipeline=StagePipelineConfig(stages=[Stage.REVIEW]))
        ],
    )
    task = TicketTask(ticket_id="T-1", client="test", lane="")

    assert resolve_pipeline_stages(task, client) == [Stage.PLAN, Stage.IMPL]


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
# #2369 — shared fire-and-forget spawn skeleton (_spawn_fire_and_forget)
# ---------------------------------------------------------------------------

_FAF_TICKET = "T-faf"
_FAF_ARGV = ["tool", "--message", "do the thing"]
_FAF_ENV = {"PATH": "/usr/bin"}


def _faf_blocked_ctor(worktree: Path) -> BlockedCtor:
    def _blocked(*, reason: str, details: str) -> AutoDevResult:
        return make_blocked(
            ticket_id=_FAF_TICKET, worktree=worktree, reason=reason, details=details
        )

    return _blocked


def _faf_spawn(
    worktree: Path,
    *,
    runner: FireAndForgetRunner,
    preflight: AutoDevResult | _PreflightOK,
    executor_name: LocalLivenessBackend = "aider",
) -> str:
    return _spawn_fire_and_forget(
        task=TicketTask(ticket_id=_FAF_TICKET, client="test", stage=Stage.IMPL),
        worktree=worktree,
        client=ClientConfig(name="test", workspace_path=worktree),
        stage=Stage.IMPL,
        executor_name=executor_name,
        preflight_fn=lambda _sid: preflight,
        blocked_ctor=_faf_blocked_ctor(worktree),
        runner=runner,
    )


def _kill_procs(runner: FakeFireAndForgetRunner) -> None:
    for proc in runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()


def test_fake_fire_and_forget_runner_records_call_and_returns_live_proc(
    tmp_path: Path,
) -> None:
    """FakeFireAndForgetRunner.launch() records argv/cwd/env; returns a live proc."""
    runner = FakeFireAndForgetRunner()

    proc = runner.launch(tmp_path, _FAF_ARGV, _FAF_ENV)
    try:
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call["argv"] == _FAF_ARGV
        assert call["cwd"] == tmp_path
        assert call["env"] == _FAF_ENV
        # The returned process is alive (a real 'sleep 60').
        assert proc.poll() is None
        assert read_process_start_time_ns(proc.pid) is not None
        assert runner.procs == [proc]
    finally:
        _kill_procs(runner)


@pytest.mark.parametrize("executor_name", ["aider", "opencode"])
def test_spawn_fire_and_forget_happy_path_stores_backend(
    executor_name: LocalLivenessBackend,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Launch succeeds → session ACTIVE with a handle tagged by executor_name."""
    worktree = make_git_repo(f"wt-faf-happy-{executor_name}")
    runner = FakeFireAndForgetRunner()

    try:
        sid = _faf_spawn(
            worktree,
            runner=runner,
            preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
            executor_name=executor_name,
        )

        assert runner.calls == [{"argv": _FAF_ARGV, "cwd": worktree, "env": _FAF_ENV}]
        session = next(s for s in load_state().sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.origin == SessionOrigin.DAEMON
        assert session.name == f"test/{AUTO_DEV_LABEL_PREFIX}{_FAF_TICKET}"
        assert session.last_result is None
        assert session.local_liveness is not None
        assert session.local_liveness.pid == runner.procs[0].pid
        assert session.local_liveness.backend == executor_name
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_preflight_blocked_completes_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Pre-flight blocked → no launch; COMPLETED via EXECUTOR_DIRECT; event emitted."""
    worktree = make_git_repo("wt-faf-preflight-blocked")
    runner = FakeFireAndForgetRunner()
    blocked = make_blocked(
        ticket_id=_FAF_TICKET, worktree=worktree, reason="preflight_nope"
    )

    with patch("cw.executor.core._record_orchestrator_event") as record_mock:
        sid = _faf_spawn(worktree, runner=runner, preflight=blocked)

    assert runner.calls == []
    session = find_completed_session(load_state())
    assert session.id == sid
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.NORMAL
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == "preflight_nope"
    record_mock.assert_called_once_with(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": sid,
            "ticket_id": _FAF_TICKET,
            "session_name": session.name,
        },
    )


@pytest.mark.parametrize("executor_name", ["aider", "opencode"])
def test_spawn_fire_and_forget_liveness_unavailable(
    executor_name: LocalLivenessBackend,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """start-time unreadable → orphan killed, no handle, runtime_error bundle."""
    worktree = make_git_repo(f"wt-faf-liveness-{executor_name}")
    runner = FakeFireAndForgetRunner()

    try:
        with patch("cw.executor.core.read_process_start_time_ns", return_value=None):
            sid = _faf_spawn(
                worktree,
                runner=runner,
                preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
                executor_name=executor_name,
            )

        # The orphan was killed and reaped by the helper.
        assert runner.procs[0].poll() is not None
        session = find_completed_session(load_state())
        assert session.id == sid
        assert session.status == SessionStatus.COMPLETED
        assert session.local_liveness is None
        result = AutoDevResult.model_validate(session.last_result)
        assert result.blocker is not None
        assert result.blocker.reason == LIVENESS_UNAVAILABLE
        assert result.blocker.details == (
            f"process {runner.procs[0].pid} start-time unavailable "
            f"[diagnostics: {render_bundle_path(sid)}]"
        )
        [path] = list(
            diagnostics_bundle_dir(sid).glob(f"{executor_name}-runtime_error-*.json")
        )
        failure = ExecutorFailure.model_validate_json(path.read_text())
        assert failure.category == "runtime_error"
        assert failure.executor_name == executor_name
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_unexpected_error_reraises_and_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """launch() raises → re-raised; session COMPLETED/CRASHED; bundle persisted."""
    worktree = make_git_repo("wt-faf-unexpected")
    runner = FakeFireAndForgetRunner()

    with (
        patch.object(runner, "launch", side_effect=OSError("exec boom")),
        patch("cw.executor.core._record_orchestrator_event") as record_mock,
        pytest.raises(OSError, match="exec boom"),
    ):
        _faf_spawn(
            worktree,
            runner=runner,
            preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
            executor_name="opencode",
        )

    record_mock.assert_not_called()
    session = find_completed_session(load_state())
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.CRASHED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.blocker.details == (
        "unexpected error during opencode launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )
    [path] = list(
        diagnostics_bundle_dir(session.id).glob("opencode-runtime_error-*.json")
    )
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.executor_name == "opencode"


@pytest.mark.parametrize("executor_name", ["aider", "opencode"])
def test_persist_runtime_error_diagnostics_writes_bundle(
    executor_name: ExecutorName,
    tmp_config_dir: Path,
) -> None:
    """Both executor names write a <name>-runtime_error-*.json bundle."""
    _persist_runtime_error_diagnostics(
        executor_name=executor_name,
        session_id="sid-diag",
        argv=[],
        details="boom detail",
    )

    [path] = list(
        diagnostics_bundle_dir("sid-diag").glob(f"{executor_name}-runtime_error-*.json")
    )
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "runtime_error"
    assert failure.executor_name == executor_name
    assert "boom detail" in failure.stderr_excerpt
