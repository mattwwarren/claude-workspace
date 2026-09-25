"""Per-stage executor resolution with lane overrides (RFC 0005 E1, #1286)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.executor.codex import CodexExecutor
from cw.executor.core import ClaudeNativeExecutor
from cw.executor.local import LocalExecutor
from cw.executor.opencode import OpencodeExecutor
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)

if TYPE_CHECKING:
    from cw.executor.core import StageExecutor
    from cw.native_daemon import NativeDaemonClient


def _lane_pipeline(
    client: ClientConfig, lane: str | None
) -> StagePipelineConfig | None:
    """Return the named lane's pipeline override, or ``None``.

    The single lane walk shared by :func:`resolve_executor_config` and
    :func:`resolve_pipeline_stages`. A falsy ``lane``, an unknown lane, and a
    lane that declares no ``pipeline`` all yield ``None`` so callers fall back
    to ``client.pipeline``.
    """
    if not lane:
        return None
    for lane_cfg in client.effective_lanes:
        if lane_cfg.name == lane and lane_cfg.pipeline is not None:
            return lane_cfg.pipeline
    return None


def resolve_executor_config(
    stage: Stage,
    task: TicketTask,
    client: ClientConfig,
) -> StageExecutorConfig:
    """Return the effective StageExecutorConfig for a stage, with lane override (E1).

    Three-level priority: lane stage config > client stage config > default.
    """
    lane_pipeline = _lane_pipeline(client, task.lane)
    if lane_pipeline is not None:
        lane_stage_config = lane_pipeline.executors.get(stage)
        if lane_stage_config is not None:
            return lane_stage_config
    return client.pipeline.executors.get(stage, StageExecutorConfig())


def resolve_pipeline_stages(task: TicketTask, client: ClientConfig) -> list[Stage]:
    """Return the effective pipeline ``stages`` for a task, with lane override.

    An explicitly configured lane ``stages`` list overrides the client default;
    a lane pipeline containing only executor overrides inherits the client's
    stages. The same lane walk (:func:`_lane_pipeline`) gives
    :func:`resolve_executor_config` its three-level priority: lane stage config
    > client stage config > default.
    """
    lane_pipeline = _lane_pipeline(client, task.lane)
    if lane_pipeline is not None and "stages" in lane_pipeline.model_fields_set:
        return lane_pipeline.stages
    return client.pipeline.stages


def resolve_executor(
    task: TicketTask,
    client: ClientConfig,
    *,
    native_daemon: NativeDaemonClient | None = None,
) -> StageExecutor:
    """Return the executor for task.stage, selected by backend (RFC 0005 E1)."""
    config = resolve_executor_config(task.stage, task, client)
    if config.backend == LOCAL_BACKEND:
        return LocalExecutor(config=config)
    if config.backend == CODEX_BACKEND:
        return CodexExecutor(config=config)
    if config.backend == CLAUDE_NATIVE_BACKEND:
        return ClaudeNativeExecutor(config=config, native_daemon=native_daemon)
    if config.backend == OPENCODE_BACKEND:
        return OpencodeExecutor(config=config)
    msg = f"unknown executor backend: {config.backend!r}"
    raise ValueError(msg)
