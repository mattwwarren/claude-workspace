"""Enums and enum-derived constants for cw's persisted models (DAG root).

``cw.models.enums`` is a package (#3739). Each submodule holds one concern and
imports nothing from ``cw``, so the package stays the DAG root of
``cw.models``: every other submodule in the package imports from here.

Submodules:

* ``session`` — session purposes, statuses, completion, origin, last-result
  source and liveness.
* ``queue_status`` — ``QueueItemStatus`` and its occupancy and terminal sets.
* ``reap`` — ``ReapReason`` and ``ReapPolicy``.
* ``pipeline`` — ``Stage``, ``PipelinePosition``, ``stage_position`` and the
  stage-identifier vocabulary.
* ``event_types`` — ``OrchestratorEventType`` (kept whole; see its docstring).
* ``dispatch`` — ``DispatchSkipReason``.
* ``codex`` — harvest outcome, reasoning effort and sandbox modes.

This ``__init__`` only re-exports. Import paths ``cw.models.enums.<name>`` are
unchanged by the split.
"""

from __future__ import annotations

from cw.models.enums.codex import (
    DEFAULT_FIX_SANDBOX_MODE,
    DEFAULT_REVIEWER_SANDBOX_MODE,
    CodexHarvestOutcome,
    ReasoningEffort,
    SandboxMode,
)
from cw.models.enums.dispatch import DispatchSkipReason
from cw.models.enums.event_types import OrchestratorEventType
from cw.models.enums.pipeline import (
    STAGE_IDENTIFIERS,
    PipelinePosition,
    Stage,
    StageIdentifier,
    stage_position,
)
from cw.models.enums.queue_status import (
    DEPENDENCY_HOLD_DISPOSITION,
    OCCUPIED_LANE_STATUSES,
    TERMINAL_QUEUE_STATUSES,
    WATCHED_PR_SOURCE_DEPENDENCY_HOLD,
    QueueItemStatus,
)
from cw.models.enums.reap import ReapPolicy, ReapReason
from cw.models.enums.session import (
    TERMINAL_SESSION_STATUSES,
    WORKER_PURPOSES,
    CompletionReason,
    LastResultSource,
    LivenessBucket,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)

__all__ = [
    "DEFAULT_FIX_SANDBOX_MODE",
    "DEFAULT_REVIEWER_SANDBOX_MODE",
    "DEPENDENCY_HOLD_DISPOSITION",
    "OCCUPIED_LANE_STATUSES",
    "STAGE_IDENTIFIERS",
    "TERMINAL_QUEUE_STATUSES",
    "TERMINAL_SESSION_STATUSES",
    "WATCHED_PR_SOURCE_DEPENDENCY_HOLD",
    "WORKER_PURPOSES",
    "CodexHarvestOutcome",
    "CompletionReason",
    "DispatchSkipReason",
    "LastResultSource",
    "LivenessBucket",
    "OrchestratorEventType",
    "PipelinePosition",
    "QueueItemStatus",
    "ReapPolicy",
    "ReapReason",
    "ReasoningEffort",
    "SandboxMode",
    "SessionOrigin",
    "SessionPurpose",
    "SessionStatus",
    "Stage",
    "StageIdentifier",
    "stage_position",
]
