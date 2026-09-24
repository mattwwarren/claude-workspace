"""Pydantic models for session state and client configuration.

Package split (#1320). The historical flat ``cw.models`` module (1531 lines) is
now a package, one submodule per concern, wired in strict dependency order so
each submodule only imports from those above it (no cycles):

- ``enums`` — StrEnums and enum-derived frozenset/tuple constants. The DAG
  root: depends on nothing else in the package.
- ``events`` — ``OrchestratorEvent``, ``PrState``, ``WatchedPr``.
- ``focus`` — ``FocusEntry`` (the ``cw focus`` session pointer, #1644). Also a
  DAG root: imports nothing else in the package.
- ``tasks`` — ``TicketTask``, ``DispatchPlan``, ``DevQueueStore``, the shared
  recipe-key validators, ``DEV_QUEUE_SCHEMA_VERSION``, ``DEFAULT_LANE``, and
  the ``PLAN_*_FINGERPRINT_KEY`` wire-key constants (#2102).
- ``orchestrator_config`` — lane/pipeline/orchestrator config models and their
  operator-forward defaults.
- ``park_comment_marker`` — ``ParkCommentMarker`` and its reader, the #2135
  worker-recorded park evidence. Its own module rather than a tenant of
  ``orchestrator_config`` (where the sibling ``agent_spawn_stamp`` accessors
  live) only because that file is already past the module-size convention.
- ``session_inbox`` — ``SessionInboxMessage``, one operator message in a
  session's inbound mailbox (#2212). Also a DAG root.
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
    TERMINAL_QUEUE_STATUSES,
    TERMINAL_SESSION_STATUSES,
    WORKER_PURPOSES,
    CompletionReason,
    DispatchSkipReason,
    LastResultSource,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    ReasoningEffort,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
)
from cw.models.events import OrchestratorEvent, PrState, WatchedPr
from cw.models.focus import FocusEntry
from cw.models.orchestrator_config import (
    _DEFAULT_OPERATOR_EVENT_TYPES,
    _DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES,
    _USAGE_LIMIT_BACKOFF_SECONDS,
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    BASH_TOOL_NAME,
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    CONTEXT_JSON_RELATIVE_PATH,
    DEFAULT_DISK_PRESSURE_MIN_FREE_GB,
    DEFAULT_GLOBAL_ATTEMPT_CEILING,
    HOOK_CONTEXT_RELATIVE_PATH,
    LOCAL_BACKEND,
    MONITOR_TOOL_NAME,
    OPENCODE_BACKEND,
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
    extract_unresolved_spawn_count,
)
from cw.models.park_comment_marker import (
    PARK_COMMENT_MARKER_KEY,
    ParkCommentMarker,
    read_park_comment_marker,
)
from cw.models.session import LocalLivenessHandle, Session
from cw.models.session_inbox import SessionInboxMessage
from cw.models.state import CW_STATE_SCHEMA_VERSION, CwState
from cw.models.tasks import (
    _SAFE_TICKET_ID,
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    PARK_ON_ABANDONED_EXIT_KEY,
    PLAN_APPROVED_FINGERPRINT_KEY,
    PLAN_DRAFT_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    DevQueueStore,
    DispatchPlan,
    PendingFixDispatch,
    TicketTask,
    UsageLimitAct,
    _validate_gate_recipe_keys,
    _validate_review_recipe_keys,
    occupies_lane_slot,
)

__all__ = [
    "AGENT_SPAWN_LAST_STAMPED_AT_KEY",
    "AGENT_SPAWN_STAMP_KEY",
    "AGENT_SPAWN_UNRESOLVED_COUNT_KEY",
    "BASH_TOOL_NAME",
    "CLAUDE_NATIVE_BACKEND",
    "CODEX_BACKEND",
    "CONTEXT_JSON_RELATIVE_PATH",
    "CW_STATE_SCHEMA_VERSION",
    "DEFAULT_AUTO_PURPOSES",
    "DEFAULT_DISK_PRESSURE_MIN_FREE_GB",
    "DEFAULT_GLOBAL_ATTEMPT_CEILING",
    "DEFAULT_LANE",
    "DEFAULT_STAGE",
    "DEV_QUEUE_SCHEMA_VERSION",
    "HOOK_CONTEXT_RELATIVE_PATH",
    "LOCAL_BACKEND",
    "MONITOR_TOOL_NAME",
    "OCCUPIED_LANE_STATUSES",
    "OPENCODE_BACKEND",
    "PARK_COMMENT_MARKER_KEY",
    "PARK_ON_ABANDONED_EXIT_KEY",
    "PLAN_APPROVED_FINGERPRINT_KEY",
    "PLAN_DRAFT_FINGERPRINT_KEY",
    "SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY",
    "SCOPE_DRIFT_APPROVED_HEAD_KEY",
    "TERMINAL_QUEUE_STATUSES",
    "TERMINAL_SESSION_STATUSES",
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
    "FocusEntry",
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
    "ParkCommentMarker",
    "PendingFixDispatch",
    "PrState",
    "QueueItemStatus",
    "ReapPolicy",
    "ReapReason",
    "ReasoningEffort",
    "Session",
    "SessionInboxMessage",
    "SessionOrigin",
    "SessionPurpose",
    "SessionStatus",
    "Stage",
    "StageExecutorConfig",
    "StagePipelineConfig",
    "TicketTask",
    "UsageLimitAct",
    "WatchedPr",
    "_validate_gate_recipe_keys",
    "_validate_review_recipe_keys",
    "extract_unresolved_spawn_count",
    "occupies_lane_slot",
    "read_park_comment_marker",
]
