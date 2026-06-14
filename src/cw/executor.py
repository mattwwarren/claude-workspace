"""RFC 0005 A2 — StageExecutor seam + ClaudeNativeExecutor (dormant/unwired)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from cw.auto_dev_result import AutoDevResult
from cw.models import (
    ClientConfig,
    SessionPurpose,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient


@runtime_checkable
class StageExecutor(Protocol):
    """Protocol for executing a single pipeline stage (RFC 0005 A2)."""

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
    ) -> str:
        """Spawn the stage session; return the cw session id."""
        ...

    def stage_sentinel_schema(self, stage: Stage) -> dict[str, Any]:
        """Return the expected JSON schema for this stage's result sentinel."""
        ...


class ClaudeNativeExecutor:
    """StageExecutor backed by claude --bg via spawn_create_impl (RFC 0005 A2).

    # Why: A2 seam — wraps spawn_create_impl without modifying it.
    # --model forwarding uses client.model_copy to avoid double-flag.
    # stage_sentinel_schema bridges to AutoDevResult until A3 per-stage schemas.
    """

    def __init__(self, *, native_daemon: NativeDaemonClient | None = None) -> None:
        self._native_daemon = native_daemon

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
    ) -> str:
        stage_config = client.pipeline.executors.get(stage, StageExecutorConfig())
        effective_model = stage_config.model or client.worker_model
        effective_client = client.model_copy(update={"worker_model": effective_model})
        return spawn_create_impl(
            client=effective_client,
            worktree=worktree,
            prompt=f"auto-dev stage {stage.value} for ticket {task.ticket_id}",
            label=f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}/{stage.value}",
            ticket_id=task.ticket_id,
            lane=task.lane,
            headless=True,
            purpose=SessionPurpose.IMPL,
            permission_mode=None,
            parent=None,
            wall_clock_budget_seconds=None,
            native_daemon=self._native_daemon,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        # Why: per-stage result models do not exist until A3 (#614).
        # Bridge via the current monolith sentinel until then.
        return AutoDevResult.model_json_schema()
