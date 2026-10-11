"""Tests for cw.executor core — StageExecutor Protocol, ClaudeNativeExecutor, resolvers.

RFC 0005 A2 / E1 / E2.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.exceptions import WorkerLaunchedError
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
    _store_liveness_handle,
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
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from tests.conftest import (
    _audit_failure_logged,
    _fail_audit_append,
    find_completed_session,
    fire_and_forget_breadcrumb,
    post_launch_attention_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import MagicMock

    from cw.executor.core import BlockedCtor, FireAndForgetRunner
    from cw.executor_diagnostics import ExecutorName
    from cw.models import LocalLivenessBackend, LocalLivenessHandle
    from cw.native_daemon import FakeNativeDaemonClient
    from tests.conftest import CapturedEvent


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
        preflight_fn=lambda _sess: preflight,
        blocked_ctor=_faf_blocked_ctor(worktree),
        runner=runner,
    )


def _kill_procs(runner: FakeFireAndForgetRunner) -> None:
    for proc in runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()


def _assert_worker_owned(
    sid: str,
    runner: FakeFireAndForgetRunner,
    pages: list[CapturedEvent],
    executor_name: LocalLivenessBackend,
    cause: str,
    record_mock: MagicMock,
) -> dict[str, object]:
    """Assert a post-launch failure kept the live worker owned (#2592).

    The session stays ACTIVE with no result, no handle, no completion event
    and no diagnostics bundle; its process still runs; exactly one page
    carries the backend's exact recovery breadcrumb. Returns that page.
    """
    session = next(s for s in load_state().sessions if s.id == sid)
    assert session.status == SessionStatus.ACTIVE
    assert session.completed_reason is None
    assert session.last_result is None
    assert session.local_liveness is None
    assert runner.procs[0].poll() is None
    record_mock.assert_not_called()
    assert list(diagnostics_bundle_dir(sid).glob("*")) == []
    payload = post_launch_attention_payload(pages)
    assert payload["breadcrumbs"] == fire_and_forget_breadcrumb(
        sid, executor_name, runner.procs[0].pid, cause
    )
    return payload


def _core_log_records(
    caplog: pytest.LogCaptureFixture, level: int
) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == "cw.executor.core" and r.levelno == level
    ]


def _assert_names_worker(
    record: logging.LogRecord, executor_name: str, pid: int, sid: str
) -> None:
    """Pin the log call's arguments: backend, pid, session id, ticket id."""
    message = record.getMessage()
    for part in (executor_name, str(pid), sid, f"ticket={_FAF_TICKET}"):
        assert part in message, (part, message)


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


@pytest.mark.parametrize("executor_name", ["aider", "opencode", "codex"])
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


def test_spawn_fire_and_forget_preflight_blocked_audit_failure_still_persists_result(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Executor-direct writers fail open (#2465): a broken audit inbox is
    logged, and the session still gets its EXECUTOR_DIRECT result and is
    completed."""
    worktree = make_git_repo("wt-faf-audit-failure")
    runner = FakeFireAndForgetRunner()
    blocked = make_blocked(
        ticket_id=_FAF_TICKET, worktree=worktree, reason="preflight_nope"
    )
    _fail_audit_append(monkeypatch)

    with (
        patch("cw.executor.core._record_orchestrator_event"),
        caplog.at_level(logging.WARNING, logger="cw.result"),
    ):
        sid = _faf_spawn(worktree, runner=runner, preflight=blocked)

    session = find_completed_session(load_state())
    assert session.id == sid
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == "preflight_nope"
    assert _audit_failure_logged(caplog, session_id=sid)


@pytest.mark.parametrize("executor_name", ["aider", "opencode", "codex"])
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
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """launch() raises → re-raised; session COMPLETED/CRASHED; bundle persisted.

    Pre-launch (#2592): no worker exists, so the raw error propagates (not
    ``WorkerLaunchedError``) and nothing is paged.
    """
    worktree = make_git_repo("wt-faf-unexpected")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)

    with (
        patch.object(runner, "launch", side_effect=OSError("exec boom")),
        patch("cw.executor.core._record_orchestrator_event") as record_mock,
        pytest.raises(OSError, match="exec boom") as excinfo,
    ):
        _faf_spawn(
            worktree,
            runner=runner,
            preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
            executor_name="opencode",
        )

    assert not isinstance(excinfo.value, WorkerLaunchedError)
    assert pages == []
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


def test_spawn_fire_and_forget_passes_created_session_to_preflight_fn(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """preflight_fn receives the persisted Session itself, not just its id.

    CodexExecutor's pre-flight needs ``sess.name`` for ``_write_hook_context``
    (#2388), which a bare sid cannot supply.
    """
    worktree = make_git_repo("wt-faf-preflight-session")
    seen: list[Session] = []
    blocked = make_blocked(
        ticket_id=_FAF_TICKET, worktree=worktree, reason="preflight_nope"
    )

    def _preflight(sess: Session) -> AutoDevResult:
        seen.append(sess)
        return blocked

    sid = _spawn_fire_and_forget(
        task=TicketTask(ticket_id=_FAF_TICKET, client="test", stage=Stage.IMPL),
        worktree=worktree,
        client=ClientConfig(name="test", workspace_path=worktree),
        stage=Stage.IMPL,
        executor_name="codex",
        preflight_fn=_preflight,
        blocked_ctor=_faf_blocked_ctor(worktree),
        runner=FakeFireAndForgetRunner(),
    )

    [sess] = seen
    assert sess.id == sid
    assert sess.name == f"test/{AUTO_DEV_LABEL_PREFIX}{_FAF_TICKET}"


def test_spawn_fire_and_forget_preflight_fn_raises_reraises_and_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """preflight_fn raises → re-raised; session COMPLETED/CRASHED; bundle persisted.

    The pre-flight call runs inside the skeleton's ``try:`` (#2388), so a
    pre-flight exception (e.g. CodexExecutor's ``_write_hook_context``) never
    leaks the already-persisted session ACTIVE. Pre-launch (#2592): the raw
    error propagates, not ``WorkerLaunchedError``, and nothing is paged.
    """
    worktree = make_git_repo("wt-faf-preflight-raises")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)

    def _boom(_sess: Session) -> AutoDevResult | _PreflightOK:
        msg = "preflight boom"
        raise OSError(msg)

    with (
        patch("cw.executor.core._record_orchestrator_event") as record_mock,
        pytest.raises(OSError, match="preflight boom") as excinfo,
    ):
        _spawn_fire_and_forget(
            task=TicketTask(ticket_id=_FAF_TICKET, client="test", stage=Stage.IMPL),
            worktree=worktree,
            client=ClientConfig(name="test", workspace_path=worktree),
            stage=Stage.IMPL,
            executor_name="codex",
            preflight_fn=_boom,
            blocked_ctor=_faf_blocked_ctor(worktree),
            runner=runner,
        )

    assert not isinstance(excinfo.value, WorkerLaunchedError)
    assert pages == []
    assert runner.calls == []
    record_mock.assert_not_called()
    session = find_completed_session(load_state())
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.CRASHED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.blocker.details == (
        "unexpected error during codex launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )
    [path] = list(diagnostics_bundle_dir(session.id).glob("codex-runtime_error-*.json"))
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.executor_name == "codex"
    assert failure.argv_sanitized == []


# ---------------------------------------------------------------------------
# #2592 — a failure after runner.launch keeps the live worker owned
# ---------------------------------------------------------------------------

# The page emitter's cap on the error text (cw.spawn), restated as a literal
# so a change to it shows up here.
_PAGE_ERROR_CAP = 500


def _launch_expecting_worker_launched(
    worktree: Path,
    runner: FakeFireAndForgetRunner,
    executor_name: LocalLivenessBackend,
) -> WorkerLaunchedError:
    with pytest.raises(WorkerLaunchedError) as excinfo:
        _faf_spawn(
            worktree,
            runner=runner,
            preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
            executor_name=executor_name,
        )
    return excinfo.value


@pytest.mark.parametrize("executor_name", ["aider", "opencode", "codex"])
def test_spawn_fire_and_forget_handle_store_failure_keeps_worker_owned(
    executor_name: LocalLivenessBackend,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Handle store fails on both attempts → worker kept, paged, not respawned.

    The parametrization drives both recovery-text variants: aider and opencode
    get the stop-then-close text, codex the do-nothing-while-alive text.
    """
    worktree = make_git_repo(f"wt-faf-store-fails-{executor_name}")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    caplog.set_level(logging.WARNING, logger="cw.executor.core")

    try:
        with (
            patch(
                "cw.executor.core._store_liveness_handle",
                side_effect=OSError("disk full"),
            ) as store_mock,
            patch("cw.executor.core._record_orchestrator_event") as record_mock,
        ):
            err = _launch_expecting_worker_launched(worktree, runner, executor_name)

        (session,) = load_state().sessions
        assert err.session_id == session.id
        assert err.surface_ref is None
        assert isinstance(err.__cause__, OSError)
        assert "Do not spawn it again" in str(err)
        assert store_mock.call_count == 2
        payload = _assert_worker_owned(
            session.id, runner, pages, executor_name, "OSError: disk full", record_mock
        )
        assert payload["session_name"] == f"test/{AUTO_DEV_LABEL_PREFIX}{_FAF_TICKET}"
        assert payload["client"] == "test"
        assert payload["ticket_id"] == _FAF_TICKET
        assert payload["lane"] == "default"
        assert payload["claude_session_id"] is None
        pid = runner.procs[0].pid
        [warning] = _core_log_records(caplog, logging.WARNING)
        [error] = _core_log_records(caplog, logging.ERROR)
        _assert_names_worker(warning, executor_name, pid, session.id)
        _assert_names_worker(error, executor_name, pid, session.id)
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_handle_store_retry_records_handle(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store fails once; the immediate retry records the handle, no page."""
    worktree = make_git_repo("wt-faf-store-retry")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    attempts: list[str] = []

    def _flaky(sid: str, handle: LocalLivenessHandle) -> None:
        attempts.append(sid)
        if len(attempts) == 1:
            msg = "disk full"
            raise OSError(msg)
        _store_liveness_handle(sid, handle)

    monkeypatch.setattr("cw.executor.core._store_liveness_handle", _flaky)
    caplog.set_level(logging.WARNING, logger="cw.executor.core")

    try:
        sid = _faf_spawn(
            worktree,
            runner=runner,
            preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
            executor_name="opencode",
        )

        assert attempts == [sid, sid]
        session = next(s for s in load_state().sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.local_liveness.pid == runner.procs[0].pid
        assert pages == []
        [warning] = _core_log_records(caplog, logging.WARNING)
        _assert_names_worker(warning, "opencode", runner.procs[0].pid, sid)
        assert _core_log_records(caplog, logging.ERROR) == []
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_start_time_read_raises_keeps_worker_owned(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """read_process_start_time_ns raising after launch → kept, paged, no store."""
    worktree = make_git_repo("wt-faf-start-time-raises")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)

    try:
        with (
            patch(
                "cw.executor.core.read_process_start_time_ns",
                side_effect=OverflowError("inf"),
            ),
            patch("cw.executor.core._store_liveness_handle") as store_mock,
            patch("cw.executor.core._record_orchestrator_event") as record_mock,
        ):
            err = _launch_expecting_worker_launched(worktree, runner, "aider")

        store_mock.assert_not_called()
        assert isinstance(err.__cause__, OverflowError)
        assert err.surface_ref is None
        _assert_worker_owned(
            err.session_id, runner, pages, "aider", "OverflowError: inf", record_mock
        )
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_post_launch_page_failure_is_swallowed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed page write never replaces the ``WorkerLaunchedError``."""
    worktree = make_git_repo("wt-faf-page-fails")
    runner = FakeFireAndForgetRunner()
    caplog.set_level(logging.ERROR, logger="cw.spawn")

    try:
        with (
            patch(
                "cw.executor.core._store_liveness_handle",
                side_effect=OSError("disk full"),
            ),
            patch("cw.spawn.record_event", side_effect=OSError("inbox unwritable")),
            patch("cw.executor.core._record_orchestrator_event") as record_mock,
        ):
            err = _launch_expecting_worker_launched(worktree, runner, "opencode")

        session = next(s for s in load_state().sessions if s.id == err.session_id)
        assert session.status == SessionStatus.ACTIVE
        assert runner.procs[0].poll() is None
        record_mock.assert_not_called()
        assert any(
            "spawn_post_launch_failed" in r.getMessage()
            and r.name == "cw.spawn"
            and r.levelno == logging.ERROR
            for r in caplog.records
        )
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_liveness_unavailable_event_failure_crash_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """Start time unreadable, then the completion event fails: the process was
    already killed, so the raw error propagates and nothing is paged."""
    worktree = make_git_repo("wt-faf-unavailable-event-fails")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)

    try:
        with (
            patch("cw.executor.core.read_process_start_time_ns", return_value=None),
            patch(
                "cw.executor.core._record_orchestrator_event",
                side_effect=OSError("events unwritable"),
            ),
            pytest.raises(OSError, match="events unwritable") as excinfo,
        ):
            _faf_spawn(
                worktree,
                runner=runner,
                preflight=_PreflightOK(argv=_FAF_ARGV, env=_FAF_ENV),
                executor_name="codex",
            )

        assert not isinstance(excinfo.value, WorkerLaunchedError)
        assert runner.procs[0].poll() is not None
        session = find_completed_session(load_state())
        assert session.completed_reason == CompletionReason.NORMAL
        result = AutoDevResult.model_validate(session.last_result)
        assert result.blocker is not None
        assert result.blocker.reason == LIVENESS_UNAVAILABLE
        assert pages == []
    finally:
        _kill_procs(runner)


def test_spawn_fire_and_forget_preflight_blocked_event_failure_reraises(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """Pre-flight blocked, then the completion event fails: no process exists,
    so the raw error propagates and nothing is paged."""
    worktree = make_git_repo("wt-faf-blocked-event-fails")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    blocked = make_blocked(
        ticket_id=_FAF_TICKET, worktree=worktree, reason="preflight_nope"
    )

    with (
        patch(
            "cw.executor.core._record_orchestrator_event",
            side_effect=OSError("events unwritable"),
        ),
        pytest.raises(OSError, match="events unwritable") as excinfo,
    ):
        _faf_spawn(worktree, runner=runner, preflight=blocked)

    assert not isinstance(excinfo.value, WorkerLaunchedError)
    assert runner.calls == []
    session = find_completed_session(load_state())
    assert session.completed_reason == CompletionReason.NORMAL
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == "preflight_nope"
    assert pages == []


@pytest.mark.parametrize("executor_name", ["aider", "codex"])
def test_spawn_fire_and_forget_recovery_text_survives_the_error_cap(
    executor_name: LocalLivenessBackend,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """The recovery text comes first, so the page's cap cuts only the cause."""
    worktree = make_git_repo(f"wt-faf-cap-{executor_name}")
    runner = FakeFireAndForgetRunner()
    pages = capture_events("cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION)

    try:
        with (
            patch(
                "cw.executor.core._store_liveness_handle",
                side_effect=OSError("x" * 600),
            ),
            patch("cw.executor.core._record_orchestrator_event"),
        ):
            err = _launch_expecting_worker_launched(worktree, runner, executor_name)

        sid = err.session_id
        breadcrumbs = str(post_launch_attention_payload(pages)["breadcrumbs"])
        assert breadcrumbs.startswith(
            fire_and_forget_breadcrumb(sid, executor_name, runner.procs[0].pid, "")
        )
        assert breadcrumbs.endswith("…")
        prefix = f"worker live (session {sid}): "
        assert len(breadcrumbs) == len(prefix) + _PAGE_ERROR_CAP + 1
    finally:
        _kill_procs(runner)


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
