"""Dev-queue task models: TicketTask, DispatchPlan, DevQueueStore.

Package split (#2999). The flat ``cw.models.tasks`` module (1259 lines) is now
a package, one submodule per concern. No submodule imports this ``__init__``;
each imports its siblings by full path, in this order (no cycles):

- ``schema`` — ``DEV_QUEUE_SCHEMA_VERSION`` with its v3.. history comment,
  ``ReviewApproverKind``, ``DEFAULT_LANE`` and ``DEFAULT_STAGE``. A leaf. A
  schema bump and its history line land here.
- ``keys`` — the ``*_KEY`` wire constants, including the single definition of
  ``PARK_ON_ABANDONED_EXIT_KEY``. A leaf.
- ``validators`` — the shared recipe-key validators and the ticket id / branch
  charsets. Imports ``keys``.
- ``records`` — ``PendingFixDispatch``, ``LaunchedFixWorker``,
  ``LaunchMarker``, ``UsageLimitAct``, ``MustFixOverride`` and
  ``ReviewEngineAllow``. Pydantic only.
- ``ticket_task`` — ``TicketTask``, one class kept whole: a pydantic class
  cannot declare fields in another module, and its field order is the
  persisted ``dev_queue.json`` layout. New field *types* go in a sibling
  submodule (or ``records`` if small); only the field line lands here.
- ``store`` — ``occupies_lane_slot``, ``DispatchPlan`` and ``DevQueueStore``.
  Imports ``ticket_task``.

This ``__init__`` only re-exports, so every ``from cw.models.tasks import X``
site keeps working unchanged. Importing it loads ``ticket_task``, which pulls
in ``FindingDisposition``'s owner (``cw.review_finding_dispositions``), as the
unsplit module did.
"""

from __future__ import annotations

from cw.models.tasks.keys import (
    MUST_FIX_OVERRIDE_KEY,
    PARK_ON_ABANDONED_EXIT_KEY,
    PLAN_APPROVED_FINGERPRINT_KEY,
    PLAN_DRAFT_FINGERPRINT_KEY,
    PLAN_PROMOTED_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
)
from cw.models.tasks.records import (
    LaunchedFixWorker,
    LaunchMarker,
    MustFixOverride,
    PendingFixDispatch,
    ReviewEngineAllow,
    UsageLimitAct,
)
from cw.models.tasks.schema import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    ReviewApproverKind,
)
from cw.models.tasks.store import DevQueueStore, DispatchPlan, occupies_lane_slot
from cw.models.tasks.ticket_task import TicketTask
from cw.models.tasks.validators import (
    _SAFE_TICKET_BRANCH,
    _SAFE_TICKET_ID,
    _validate_gate_recipe_keys,
    _validate_park_on_abandoned_exit_keys,
    _validate_review_recipe_keys,
)

__all__ = [
    "DEFAULT_LANE",
    "DEFAULT_STAGE",
    "DEV_QUEUE_SCHEMA_VERSION",
    "MUST_FIX_OVERRIDE_KEY",
    "PARK_ON_ABANDONED_EXIT_KEY",
    "PLAN_APPROVED_FINGERPRINT_KEY",
    "PLAN_DRAFT_FINGERPRINT_KEY",
    "PLAN_PROMOTED_KEY",
    "SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY",
    "SCOPE_DRIFT_APPROVED_HEAD_KEY",
    "_SAFE_TICKET_BRANCH",
    "_SAFE_TICKET_ID",
    "DevQueueStore",
    "DispatchPlan",
    "LaunchMarker",
    "LaunchedFixWorker",
    "MustFixOverride",
    "PendingFixDispatch",
    "ReviewApproverKind",
    "ReviewEngineAllow",
    "TicketTask",
    "UsageLimitAct",
    "_validate_gate_recipe_keys",
    "_validate_park_on_abandoned_exit_keys",
    "_validate_review_recipe_keys",
    "occupies_lane_slot",
]
