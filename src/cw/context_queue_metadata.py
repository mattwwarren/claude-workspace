"""The ``queue_metadata`` block of a worker's ``.claude/cw-context.json``.

Extracted from ``cw.spawn`` (#2702) so the block can grow without pushing that
module past the size ceiling. The schema history of each key lives beside
``cw.spawn.CW_CONTEXT_SCHEMA_VERSION``. Imports only ``cw.models``, so it
carries no import-cycle risk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import (
    MUST_FIX_OVERRIDE_KEY,
    PLAN_APPROVED_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
)
from cw.models.operator_resolution import OPERATOR_RESOLUTIONS_KEY

if TYPE_CHECKING:
    from cw.models import TicketTask


def build_queue_metadata(task: TicketTask) -> dict[str, object]:
    """The ``queue_metadata`` dict cw threads from *task*'s row to its worker."""
    return {
        "scope_hint": task.scope_hint,
        "plan_source": task.plan_source,
        "headless_timeout_override": task.headless_timeout_override,
        # #1794: Stage is a StrEnum, so json.dumps renders this as the plain
        # stage value (or null) with no .value call.
        "regressed_into_stage": task.regressed_into_stage,
        # #1730: sibling per-arrival marker -- true means this re-entry may
        # carry an operator send-back the review stage must treat as a binding
        # adjudication input.
        "pending_operator_comment": task.pending_operator_comment,
        # v7: tracker-neutral plan-approval evidence for the plan stage's
        # Checkpoint 1 (Large-scope carve-out).
        "plan_approved_at": (
            task.plan_approved_at.isoformat()
            if task.plan_approved_at is not None
            else None
        ),
        # v8 (#2102): the draft the approval above was bound to. Checkpoint 1
        # requires it to equal the resumed draft's own fingerprint before the
        # approval counts as evidence.
        PLAN_APPROVED_FINGERPRINT_KEY: task.plan_approved_fingerprint,
        # v9 (#2337): the operator's plan_scope_drift grant and the branch head
        # it is bound to, read by Step 2.5 gate 2.
        SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY: task.scope_drift_approved_extra_files,
        SCOPE_DRIFT_APPROVED_HEAD_KEY: task.scope_drift_approved_head,
        # v10 (#2205): the operator's MUST_FIX override, read by FINALIZE's
        # check_must_fix_override.py.
        MUST_FIX_OVERRIDE_KEY: (
            task.must_fix_override.model_dump(mode="json")
            if task.must_fix_override is not None
            else None
        ),
        # v12 (#2702): the row's `cw dev-queue resolve` entries, always a full
        # list in round order ([] when empty), read by auto-dev-plan.md Step 1b
        # as binding resolutions.
        # Why: delivered whole on every spawn; each entry is capped at 64 KiB
        # but the list has no total cap yet (#2727).
        OPERATOR_RESOLUTIONS_KEY: [
            resolution.model_dump(mode="json")
            for resolution in task.operator_resolutions
        ],
    }
