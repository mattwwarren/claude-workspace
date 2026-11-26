"""Dev-queue requeue + unblock: re-run a stage, regress, or clear a salvage park.

``cw.dev_queue.requeue`` is a package (#3739). It was one flat module of about
1000 lines. The split is by concern:

* ``plan_guard`` — impl-bypass plan check (``_impl_bypass_plan_available``).
* ``stage_gate`` — stage resolution: the idempotent-PENDING predicate
  (``is_pending_requeue_noop``, #2692), stage validation and classification,
  and the stage move itself (``_apply_requeue_stage``).
* ``review_delivery`` — review re-entry deliverability.
* ``live_session`` — the #2275 live-session guard and its refusal tags.
* ``core`` — ``precheck_requeue`` and ``requeue_ticket``, the entry points.
* ``unblock`` — ``unblock_ticket`` (salvage-park clearing).

Submodules import their siblings by full path, never from this package. This
``__init__`` only re-exports, so ``cw.dev_queue`` and the existing private
importers keep resolving here.
"""

from __future__ import annotations

from cw.dev_queue.requeue.core import precheck_requeue, requeue_ticket
from cw.dev_queue.requeue.live_session import (
    RequeueRefusalTag,
    _refuse_if_live_session,
    classify_requeue_live_session_error,
)
from cw.dev_queue.requeue.plan_guard import (
    _impl_bypass_plan_available,
    _impl_bypass_worktree_path,
    _ImplBypassPlanCheck,
)
from cw.dev_queue.requeue.stage_gate import (
    _apply_requeue_stage,
    is_pending_requeue_noop,
)
from cw.dev_queue.requeue.unblock import unblock_ticket

__all__ = [
    "RequeueRefusalTag",
    "_ImplBypassPlanCheck",
    "_apply_requeue_stage",
    "_impl_bypass_plan_available",
    "_impl_bypass_worktree_path",
    "_refuse_if_live_session",
    "classify_requeue_live_session_error",
    "is_pending_requeue_noop",
    "precheck_requeue",
    "requeue_ticket",
    "unblock_ticket",
]
