"""Per-stage executor and pipeline configuration (RFC 0005 A1, dormant).

``StageExecutorConfig`` and ``StagePipelineConfig``. Depends on
``cw.models.orchestrator_config.constants`` (backend names, the pinned logger
name) and ``cw.models.enums``.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cw.models.enums import (
    DEFAULT_FIX_SANDBOX_MODE,
    DEFAULT_REVIEWER_SANDBOX_MODE,
    ReasoningEffort,
    SandboxMode,
    Stage,
)
from cw.models.orchestrator_config.constants import (
    _LOGGER_NAME,
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
)

_LOG = logging.getLogger(_LOGGER_NAME)


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
    # Codex-only (#1767): the `--sandbox` mode of each codex role, read from
    # the REVIEW stage config (the reviewer and the fix loop share it but have
    # opposite defaults, hence two fields). Reviewer defaults to read-only (a
    # reviewer reports, it does not repair) and the capability probe follows
    # it; the fix loop defaults to workspace-write and may not be read-only
    # (it would silently no-op every fix). Non-nullable: an unpinned sandbox
    # would leave it to ~/.codex/config.toml. Resolved like `model`: a lane's
    # stage config replaces the client's wholesale, so a lane omitting them
    # gets these defaults, not the client's values.
    reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE
    fix_sandbox_mode: SandboxMode = DEFAULT_FIX_SANDBOX_MODE

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

    @model_validator(mode="after")
    def _sandbox_modes_codex_only(self) -> StageExecutorConfig:
        # As for reasoning_effort, but the defaults are non-null, so an
        # explicit value equal to its default changes nothing and is allowed.
        if self.backend == CODEX_BACKEND:
            return self
        for field, default in _SANDBOX_MODE_DEFAULTS.items():
            if field in self.model_fields_set and getattr(self, field) != default:
                msg = (
                    f"{field} is only honored by the codex backend "
                    f"(got backend={self.backend!r})"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _sandbox_modes_keep_the_report_fix_boundary(self) -> StageExecutorConfig:
        if self.reviewer_sandbox_mode is not SandboxMode.READ_ONLY:
            _LOG.warning(
                "reviewer_sandbox_mode=%s weakens the report/fix boundary: "
                "a writable reviewer can silently repair what it should report",
                self.reviewer_sandbox_mode.value,
            )
        if self.fix_sandbox_mode is SandboxMode.READ_ONLY:
            msg = (
                "fix_sandbox_mode must not be read-only: the fix loop edits "
                "files, so a read-only sandbox would silently no-op every fix"
            )
            raise ValueError(msg)
        return self


# Field name -> default, for the codex-only check above.
_SANDBOX_MODE_DEFAULTS: dict[str, SandboxMode] = {
    "reviewer_sandbox_mode": DEFAULT_REVIEWER_SANDBOX_MODE,
    "fix_sandbox_mode": DEFAULT_FIX_SANDBOX_MODE,
}


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
