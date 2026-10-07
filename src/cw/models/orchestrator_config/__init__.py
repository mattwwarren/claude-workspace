"""Orchestrator and lane configuration models.

Package split (#2497). The historical flat ``orchestrator_config.py`` module
(1313 lines) is now a package, one submodule per concern, wired in strict
dependency order so each submodule only imports from those above it (no
cycles):

- ``constants`` — config defaults, backend names, the per-worktree relative
  paths and cw-context.json keys, ``extract_unresolved_spawn_count``, and
  ``_LOGGER_NAME``. The DAG root: imports nothing from ``cw``.
- ``concurrency`` — ``LaneConcurrencyOverride``, ``ClientConcurrencyOverride``,
  ``ConcurrencyOverrides``. A leaf: pydantic and the stdlib only.
- ``hooks`` — ``HookRule``, ``EventHookRegistry``. A leaf: pydantic only.
- ``stage`` — ``StageExecutorConfig``, ``StagePipelineConfig``. Depends on
  ``constants`` and ``cw.models.enums``.
- ``lane`` — ``LaneConfig`` and ``CODEX_TIER_CLAIM_SUPPRESSION``. Depends on
  ``stage``, ``cw.models.tasks`` (for the shared recipe-key validators) and
  ``cw.models.enums``.
- ``operator_forward`` — ``OperatorChannelForward`` and its default
  forward-sets. Depends on ``cw.models.enums`` only.
- ``orchestrator`` — ``OrchestratorConfig``. Depends on ``constants``,
  ``operator_forward`` and ``cw.models.enums``.

Submodules import each other by full submodule path, never from this package
``__init__``, which would re-enter a partially initialised package. Every
submodule that logs uses ``logging.getLogger(_LOGGER_NAME)``, so records keep
the pre-split ``cw.models.orchestrator_config`` logger name.

This ``__init__`` only re-exports, so every ``from cw.models.orchestrator_config
import X`` call site stays unchanged. See ``cw.models.__init__`` for the full
``cw.models`` DAG.
"""

from __future__ import annotations

from cw.models.orchestrator_config.concurrency import (
    ClientConcurrencyOverride,
    ConcurrencyOverrides,
    LaneConcurrencyOverride,
)
from cw.models.orchestrator_config.constants import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    BASH_TOOL_NAME,
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    CONTEXT_JSON_RELATIVE_PATH,
    DEFAULT_DISK_PRESSURE_MIN_FREE_GB,
    DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION,
    DEFAULT_DISK_PRESSURE_MIN_FREE_INODES,
    DEFAULT_GLOBAL_ATTEMPT_CEILING,
    HOOK_CONTEXT_RELATIVE_PATH,
    LOCAL_BACKEND,
    MONITOR_TOOL_NAME,
    OPENCODE_BACKEND,
    STAGED_EMIT_RESULT_KEY,
    WORKER_TMPDIR_RELATIVE_PATH,
    extract_unresolved_spawn_count,
)
from cw.models.orchestrator_config.hooks import EventHookRegistry, HookRule
from cw.models.orchestrator_config.lane import CODEX_TIER_CLAIM_SUPPRESSION, LaneConfig
from cw.models.orchestrator_config.operator_forward import (
    _DEFAULT_OPERATOR_EVENT_TYPES,
    _DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES,
    OperatorChannelForward,
)
from cw.models.orchestrator_config.orchestrator import (
    _USAGE_LIMIT_BACKOFF_SECONDS,
    OrchestratorConfig,
)
from cw.models.orchestrator_config.stage import (
    StageExecutorConfig,
    StagePipelineConfig,
)

__all__ = [
    "AGENT_SPAWN_LAST_STAMPED_AT_KEY",
    "AGENT_SPAWN_STAMP_KEY",
    "AGENT_SPAWN_UNRESOLVED_COUNT_KEY",
    "BASH_TOOL_NAME",
    "CLAUDE_NATIVE_BACKEND",
    "CODEX_BACKEND",
    "CODEX_TIER_CLAIM_SUPPRESSION",
    "CONTEXT_JSON_RELATIVE_PATH",
    "DEFAULT_DISK_PRESSURE_MIN_FREE_GB",
    "DEFAULT_DISK_PRESSURE_MIN_FREE_INODES",
    "DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION",
    "DEFAULT_GLOBAL_ATTEMPT_CEILING",
    "HOOK_CONTEXT_RELATIVE_PATH",
    "LOCAL_BACKEND",
    "MONITOR_TOOL_NAME",
    "OPENCODE_BACKEND",
    "STAGED_EMIT_RESULT_KEY",
    "WORKER_TMPDIR_RELATIVE_PATH",
    "_DEFAULT_OPERATOR_EVENT_TYPES",
    "_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES",
    "_USAGE_LIMIT_BACKOFF_SECONDS",
    "ClientConcurrencyOverride",
    "ConcurrencyOverrides",
    "EventHookRegistry",
    "HookRule",
    "LaneConcurrencyOverride",
    "LaneConfig",
    "OperatorChannelForward",
    "OrchestratorConfig",
    "StageExecutorConfig",
    "StagePipelineConfig",
    "extract_unresolved_spawn_count",
]
