"""Per-stage executor and pipeline configuration (RFC 0005 A1, dormant).

``StageExecutorConfig`` and ``StagePipelineConfig``. Depends on
``cw.models.orchestrator_config.constants`` (backend names) and
``cw.models.enums``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cw.models.enums import ReasoningEffort, Stage
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
)


class StageExecutorConfig(BaseModel):
    """Executor configuration for a single pipeline stage (RFC 0005 A1, dormant)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = CLAUDE_NATIVE_BACKEND
    model: str | None = None
    endpoint: str | None = None  # OpenAI-compatible base URL for local backend
    # Codex-only: pinned as `-c model_reasoning_effort=<value>` on every codex
    # reviewer and fix invocation, which beats ~/.codex/config.toml. Defaults
    # to "high" (#1711 R1: a reviewer's failure mode is a missed MUST_FIX, so
    # depth beats token savings) -- a starting position, not a benchmarked
    # optimum. Explicit null unpins it, leaving codex's own config in force.
    # Resolved like `model`: a lane's stage config replaces the client's
    # wholesale (resolve_executor_config), so a lane omitting it gets high.
    reasoning_effort: ReasoningEffort | None = ReasoningEffort.HIGH

    @model_validator(mode="after")
    def _reasoning_effort_codex_only(self) -> StageExecutorConfig:
        # Loud rather than silently ignored: no other backend reads it, so a
        # value here would look pinned while changing nothing.
        # model_fields_set, not the value: the high default rides on every
        # stage config, including claude-native ones that never read it.
        if (
            "reasoning_effort" in self.model_fields_set
            and self.reasoning_effort is not None
            and self.backend != CODEX_BACKEND
        ):
            msg = (
                "reasoning_effort is only honored by the codex backend "
                f"(got backend={self.backend!r})"
            )
            raise ValueError(msg)
        return self


class StagePipelineConfig(BaseModel):
    """Per-client (or per-lane) pipeline configuration (RFC 0005 A1, dormant)."""

    model_config = ConfigDict(extra="forbid")

    stages: list[Stage] = Field(
        default_factory=lambda: [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
    )
    executors: dict[Stage, StageExecutorConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stages_unique(self) -> StagePipelineConfig:
        if len(self.stages) != len(set(self.stages)):
            msg = "pipeline stages must be unique"
            raise ValueError(msg)
        return self
