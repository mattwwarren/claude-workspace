"""Pydantic models for session state and client configuration.

Package split (#1320). The historical flat ``cw.models`` module (1531 lines) is
now a package, one submodule per concern, wired in strict dependency order so
each submodule only imports from those above it (no cycles):

- ``enums`` — StrEnums and enum-derived frozenset/tuple constants. The DAG
  root: depends on nothing else in the package.
- ``events`` — ``OrchestratorEvent``, ``PrState``, ``WatchedPr``.
- ``tasks`` — ``TicketTask``, ``DispatchPlan``, ``DevQueueStore``, the shared
  recipe-key validators, ``DEV_QUEUE_SCHEMA_VERSION``, ``DEFAULT_LANE``.
- ``orchestrator_config`` — lane/pipeline/orchestrator config models and their
  operator-forward defaults.
- ``session`` — ``LocalLivenessHandle``, ``Session``.
- ``client`` — ``ClientConfig`` and ``DEFAULT_AUTO_PURPOSES``.
- ``state`` — ``CwState`` and ``CW_STATE_SCHEMA_VERSION`` (the DAG leaf).

This ``__init__`` re-exports the full historical public + private surface so
every ``from cw.models import X`` import site keeps working unchanged.
"""

from __future__ import annotations

from cw.models.client import DEFAULT_AUTO_PURPOSES, ClientConfig
from cw.models.enums import (
    OCCUPIED_LANE_STATUSES,
    WORKER_PURPOSES,
    CompletionReason,
    DispatchSkipReason,
    LastResultSource,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
)
from cw.models.events import OrchestratorEvent, PrState, WatchedPr
from cw.models.orchestrator_config import (
    _DEFAULT_OPERATOR_EVENT_TYPES,
    _DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES,
    _USAGE_LIMIT_BACKOFF_SECONDS,
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    CONTEXT_JSON_RELATIVE_PATH,
    DEFAULT_GLOBAL_ATTEMPT_CEILING,
    LOCAL_BACKEND,
    ClientConcurrencyOverride,
    ConcurrencyOverrides,
    EventHookRegistry,
    HookRule,
    LaneConcurrencyOverride,
    LaneConfig,
    OperatorChannelForward,
    OrchestratorConfig,
    StageExecutorConfig,
    StagePipelineConfig,
)
from cw.models.session import LocalLivenessHandle, Session
from cw.models.state import CW_STATE_SCHEMA_VERSION, CwState
from cw.models.tasks import (
    _SAFE_TICKET_ID,
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    DevQueueStore,
    DispatchPlan,
    TicketTask,
    _validate_gate_recipe_keys,
    _validate_review_recipe_keys,
)

__all__ = [
    "CLAUDE_NATIVE_BACKEND",
    "CODEX_BACKEND",
    "CONTEXT_JSON_RELATIVE_PATH",
    "CW_STATE_SCHEMA_VERSION",
    "DEFAULT_AUTO_PURPOSES",
    "DEFAULT_GLOBAL_ATTEMPT_CEILING",
    "DEFAULT_LANE",
    "DEFAULT_STAGE",
    "DEV_QUEUE_SCHEMA_VERSION",
    "LOCAL_BACKEND",
    "OCCUPIED_LANE_STATUSES",
    "WORKER_PURPOSES",
    "_DEFAULT_OPERATOR_EVENT_TYPES",
    "_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES",
    "_SAFE_TICKET_ID",
    "_USAGE_LIMIT_BACKOFF_SECONDS",
    "ClientConcurrencyOverride",
    "ClientConfig",
    "CompletionReason",
    "ConcurrencyOverrides",
    "CwState",
    "DevQueueStore",
    "DispatchPlan",
    "DispatchSkipReason",
    "EventHookRegistry",
    "HookRule",
    "LaneConcurrencyOverride",
    "LaneConfig",
    "LastResultSource",
    "LivenessBucket",
    "LocalLivenessHandle",
    "OperatorChannelForward",
    "OrchestratorConfig",
    "OrchestratorEvent",
    "OrchestratorEventType",
    "PrState",
    "QueueItemStatus",
    "ReapPolicy",
    "ReapReason",
    "Session",
    "SessionOrigin",
    "SessionPurpose",
    "SessionStatus",
    "Stage",
    "StageExecutorConfig",
    "StagePipelineConfig",
    "TicketTask",
    "WatchedPr",
    "_validate_gate_recipe_keys",
    "_validate_review_recipe_keys",
]
