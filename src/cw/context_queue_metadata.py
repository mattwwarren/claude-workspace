"""The ``queue_metadata`` block of a worker's ``.claude/cw-context.json``.

Extracted from ``cw.spawn`` (#2702) so the block can grow without pushing that
module past the size ceiling. The schema history of each key lives beside
``cw.spawn.CW_CONTEXT_SCHEMA_VERSION``. Imports only the stdlib and
``cw.models``, so it carries no import-cycle risk. Also owns the
``operator_resolutions_source`` identity (#2728) computed from the list.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from cw.models import (
    MUST_FIX_OVERRIDE_KEY,
    PLAN_APPROVED_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
)
from cw.models.operator_resolution import OPERATOR_RESOLUTIONS_KEY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import TicketTask
    from cw.models.operator_resolution import OperatorResolution

OPERATOR_RESOLUTIONS_SOURCE_KEY = "operator_resolutions_source"
"""The ``queue_metadata`` wire key carrying the list's ``row:<N>:<sha>`` identity."""
REVIEW_APPROVAL_KEY = "review_approval"
"""The ``queue_metadata`` wire key carrying the row's review approval (#2767)."""

_ROW_SOURCE_PREFIX = "row"
_ROW_SOURCE_SEPARATOR = ":"
_JSON_COMPACT_SEPARATORS = (",", ":")
_DIGEST_ENCODING = "ascii"


def operator_resolutions_source(
    resolutions: Sequence[OperatorResolution],
) -> str | None:
    """The content-bound identity ``row:<N>:<sha256>`` of a row's resolutions (#2728).

    ``N`` is the highest ``round``; the digest covers every entry text in round
    order, so a re-enqueued row whose earlier rounds differ never reuses a
    surviving draft's identity. ``None`` for an empty list.

    The digest input is the compact JSON array of the texts with the default
    ``ensure_ascii``: pure ASCII bytes, unambiguous across entry boundaries,
    and a lone surrogate cannot raise at encode time.
    """
    if not resolutions:
        return None
    ordered = sorted(resolutions, key=lambda resolution: resolution.round)
    payload = json.dumps(
        [resolution.text for resolution in ordered],
        separators=_JSON_COMPACT_SEPARATORS,
    ).encode(_DIGEST_ENCODING)
    digest = hashlib.sha256(payload).hexdigest()
    return _ROW_SOURCE_SEPARATOR.join(
        (_ROW_SOURCE_PREFIX, str(ordered[-1].round), digest)
    )


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
        # v13 (#2728): `row:<N>:<sha256>` over the whole list, computed here so
        # the worker never hashes; null when the list is empty.
        OPERATOR_RESOLUTIONS_SOURCE_KEY: operator_resolutions_source(
            task.operator_resolutions
        ),
        # v14 (#2767): the row's REVIEW-stage approval record, read by
        # auto-dev-finalize.md Step 4.0 before a large ticket ships.
        REVIEW_APPROVAL_KEY: _review_approval(task),
    }


def _review_approval(task: TicketTask) -> dict[str, str | None] | None:
    """``{approved_at, approver_kind, reviewed_sha}``, or None when unstamped.

    ``reviewed_sha`` stays None when the review sentinel carried no head;
    Step 4.0 parks on that rather than treating it as a match.
    """
    if task.review_approved_at is None:
        return None
    return {
        "approved_at": task.review_approved_at.isoformat(),
        "approver_kind": task.review_approver_kind,
        "reviewed_sha": task.review_approved_sha,
    }
