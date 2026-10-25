"""Dev-queue management for orchestrator ticket dispatch.

Package split (#1317-#1318, complete). The historical flat ``cw.dev_queue``
module is now a package of focused submodules:

* ``attention`` — the shared ``task_attention_state`` predicate backing every
  "needs attention" count (CLI ``NEEDS_ATTN``, statusline ``!N``), plus the
  session-derived ``session_attention_state`` ATTENTION cell (#2153).
* ``migrate`` — the pure dict-in / dict-out schema-normalisation layer.
* ``storage`` — the on-disk persistence layer (file locks + plan/queue
  load & save).
* ``dispositions`` — the leaf ``TicketTask.disposition`` vocabulary
  (``HOLD_DISPOSITIONS`` & co.) and the sentinel-derivation helpers
  (``_derive_disposition``, ``_extract_pr_url`` & co.), split out of
  ``lifecycle`` (#2719).
* ``lifecycle`` — task status transitions, terminal-status constants,
  stage-pointer helpers, and the terminal-wait poll loop.
* ``crud`` — operator-facing queue mutations (add/remove/cancel/move/clear/
  prune) and the ticket-resolution helpers (resolve/list/find).
* ``approval`` — the plan/review approval + operator-signoff-clearing gates.
* ``scope_drift`` — the operator's grant of a ``plan_scope_drift`` park
  (``approve_scope_drift_ticket``), split out of ``approval`` (#2719).
* ``must_fix_override`` — the operator's override of a codex MUST_FIX park.
* ``operator_resolutions`` — the operator's answers to a plan park, recorded
  on the row by ``cw dev-queue resolve`` (#2702).
* ``review_records`` — operator review decisions recorded on the row
  (``cw review settle``'s disposition ledger, #2701).
* ``plan_promotion`` — promote an approved ``.cw/plan-draft.md`` to
  ``.cw/plan.md`` on the approve gate's plan->impl advance.
* ``requeue`` — re-run a stage, regress, or clear a salvage park.
* ``drain`` — batch-resume every Rule-5 availability park (RFC 0011 A4).
* ``gates`` — the leaf approval-gate predicate, ``DRAIN_DISPOSITIONS`` and
  the single ``APPROVAL_GATE_REASON`` definition (#2692), shared by
  ``approval``, ``drain`` and ``next_commands`` without an import cycle.
* ``gate_backoff`` — the leaf gate-block retry backoff (#2717): stamps
  ``TicketTask.gate_block`` and the shared ``next_eligible_at`` wait at a
  ``merge_gate_blocked``/``prior_pipeline_pr_open`` park. Not re-exported.
* ``refusal`` — loads a refused row's bound session for the
  ``next_commands`` refusal hint (#2692).
* ``next_commands`` — the pure state -> legal-next-command table behind
  ``cw dev-queue show`` (#2691) and every refusal's hint (#2692).
  Deliberately NOT re-exported here: a function named like its submodule
  would rebind ``cw.dev_queue.next_commands`` from the module to the
  function. Import it from the submodule.

This ``__init__`` re-exports the full historical public + private surface so
every ``from cw.dev_queue import X`` import site and downstream call path keeps
working unchanged.
"""

from __future__ import annotations

from cw.dev_queue.approval import (
    BODY_DRIFT_WARNING_KEY,
    _approve_ticket_locked,
    approve_ticket,
    revoke_plan_approval,
)
from cw.dev_queue.attention import (
    DEAD_SESSION_PAGED_STATE,
    session_attention_state,
    task_attention_state,
)
from cw.dev_queue.crud import (
    DEFAULT_PRUNE_OLDER_THAN_DAYS,
    _find_ticket,
    _newest_by_created_at,
    _prune_age_basis,
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    move_ticket,
    prune_tickets,
    register_watched_pr,
    remove_ticket,
    resolve_client,
    running_task_for_session,
    select_clearable_tickets,
    select_prunable_tickets,
)
from cw.dev_queue.dispositions import (
    _PRE_DISPATCH_STALE_PR_REASON,
    _PRE_DISPATCH_TRACKER_MCP_REASON,
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    EMPTY_DIFF_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    HOLD_DISPOSITIONS,
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    SIGNOFF_GATE_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
    STALE_DISPATCH_GATE_DISPOSITION,
    TRACKER_MCP_GATE_DISPOSITION,
    UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION,
    _derive_disposition,
    _extract_pr_url,
    _extract_pr_url_or_info,
    _hold_aware_disposition,
    _result_blocker_reason,
)
from cw.dev_queue.drain import (
    drain_held_tickets,
    select_held_tickets,
)
from cw.dev_queue.gates import APPROVAL_GATE_REASON, DRAIN_DISPOSITIONS
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    _advance_task_pointer,
    _local_plan_body,
    _marker_version,
    _plan_body_signoff_ok,
    _stage_regress,
    _stamp_salvage_stage,
    _tracker_allows_github_fetch,
    consume_completed_sessions,
    transition_task_status,
    wait_for_terminal,
)
from cw.dev_queue.migrate import migrate_dev_queue
from cw.dev_queue.must_fix_override import approve_must_fix_override_ticket
from cw.dev_queue.operator_resolutions import record_operator_resolution
from cw.dev_queue.plan_promotion import promote_plan_draft
from cw.dev_queue.requeue import (
    _apply_requeue_stage,
    _impl_bypass_plan_available,
    classify_requeue_live_session_error,
    is_pending_requeue_noop,
    precheck_requeue,
    requeue_ticket,
    unblock_ticket,
)
from cw.dev_queue.review_records import (
    TERMINAL_ROW_STATUSES,
    RecordedDispositions,
    find_ticket_row,
    record_finding_dispositions,
)
from cw.dev_queue.scope_drift import approve_scope_drift_ticket
from cw.dev_queue.storage import (
    _lock,
    dev_queue_lock,
    load_dev_queue,
    load_plan,
    plan_path,
    save_dev_queue,
    save_plan,
)
from cw.exceptions import LaneNotFoundError

__all__ = [
    "APPROVAL_GATE_REASON",
    "AWAITING_OPERATOR_DISPOSITION",
    "BODY_DRIFT_WARNING_KEY",
    "BRANCH_STALENESS_GATE_DISPOSITION",
    "DEAD_SESSION_PAGED_STATE",
    "DEFAULT_PRUNE_OLDER_THAN_DAYS",
    "DRAIN_DISPOSITIONS",
    "EMPTY_DIFF_GATE_DISPOSITION",
    "FINALIZE_GATE_HELD_DISPOSITION",
    "HOLD_DISPOSITIONS",
    "REVIEW_HEALTH_GATE_DISPOSITION",
    "REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION",
    "REVIEW_STALENESS_GATE_DISPOSITION",
    "SIGNOFF_GATE_DISPOSITION",
    "STALE_DISPATCH_DISPOSITION",
    "STALE_DISPATCH_GATE_DISPOSITION",
    "TERMINAL_ROW_STATUSES",
    "TRACKER_MCP_GATE_DISPOSITION",
    "UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION",
    "_PLAN_SOUNDNESS_MARKER",
    "_PLAN_SPEC_MARKER",
    "_PRE_DISPATCH_STALE_PR_REASON",
    "_PRE_DISPATCH_TRACKER_MCP_REASON",
    "LaneNotFoundError",
    "RecordedDispositions",
    "_advance_task_pointer",
    "_apply_requeue_stage",
    "_approve_ticket_locked",
    "_derive_disposition",
    "_extract_pr_url",
    "_extract_pr_url_or_info",
    "_find_ticket",
    "_hold_aware_disposition",
    "_impl_bypass_plan_available",
    "_local_plan_body",
    "_lock",
    "_marker_version",
    "_newest_by_created_at",
    "_plan_body_signoff_ok",
    "_prune_age_basis",
    "_result_blocker_reason",
    "_stage_regress",
    "_stamp_salvage_stage",
    "_tracker_allows_github_fetch",
    "add_ticket",
    "approve_must_fix_override_ticket",
    "approve_scope_drift_ticket",
    "approve_ticket",
    "cancel_task_for_session",
    "cancel_ticket",
    "classify_requeue_live_session_error",
    "clear_tickets",
    "consume_completed_sessions",
    "dev_queue_lock",
    "drain_held_tickets",
    "find_ticket_row",
    "is_pending_requeue_noop",
    "list_tickets",
    "load_dev_queue",
    "load_plan",
    "migrate_dev_queue",
    "move_ticket",
    "plan_path",
    "precheck_requeue",
    "promote_plan_draft",
    "prune_tickets",
    "record_finding_dispositions",
    "record_operator_resolution",
    "register_watched_pr",
    "remove_ticket",
    "requeue_ticket",
    "resolve_client",
    "revoke_plan_approval",
    "running_task_for_session",
    "save_dev_queue",
    "save_plan",
    "select_clearable_tickets",
    "select_held_tickets",
    "select_prunable_tickets",
    "session_attention_state",
    "task_attention_state",
    "transition_task_status",
    "unblock_ticket",
    "wait_for_terminal",
]
