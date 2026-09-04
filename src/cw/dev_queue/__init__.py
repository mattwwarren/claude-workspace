"""Dev-queue management for orchestrator ticket dispatch.

Package split (#1317-#1318, complete). The historical flat ``cw.dev_queue``
module is now a package of focused submodules:

* ``attention`` — the shared ``task_attention_state`` predicate backing every
  "needs attention" count (CLI ``NEEDS_ATTN``, statusline ``!N``).
* ``migrate`` — the pure dict-in / dict-out schema-normalisation layer.
* ``storage`` — the on-disk persistence layer (file locks + plan/queue
  load & save).
* ``lifecycle`` — task status transitions, disposition/terminal-status
  constants, stage-pointer helpers, and the terminal-wait poll loop.
* ``crud`` — operator-facing queue mutations (add/remove/cancel/move/clear/
  prune) and the ticket-resolution helpers (resolve/list/find).
* ``approval`` — the plan/review approval + operator-signoff-clearing gates.
* ``requeue`` — re-run a stage, regress, or clear a salvage park.
* ``drain`` — batch-resume every Rule-5 availability park (RFC 0011 A4).

This ``__init__`` re-exports the full historical public + private surface so
every ``from cw.dev_queue import X`` import site and downstream call path keeps
working unchanged.
"""

from __future__ import annotations

from cw.dev_queue.approval import _approve_ticket_locked, approve_ticket
from cw.dev_queue.attention import task_attention_state
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
    select_clearable_tickets,
    select_prunable_tickets,
)
from cw.dev_queue.drain import (
    DRAIN_DISPOSITIONS,
    drain_held_tickets,
    select_held_tickets,
)
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    _PRE_DISPATCH_STALE_PR_REASON,
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    EMPTY_DIFF_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    HOLD_DISPOSITIONS,
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    SIGNOFF_GATE_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
    STALE_DISPATCH_GATE_DISPOSITION,
    _advance_task_pointer,
    _derive_disposition,
    _extract_pr_url,
    _extract_pr_url_or_info,
    _hold_aware_disposition,
    _local_plan_body,
    _marker_version,
    _plan_body_signoff_ok,
    _result_blocker_reason,
    _stage_regress,
    _stamp_salvage_stage,
    _tracker_allows_github_fetch,
    consume_completed_sessions,
    transition_task_status,
    wait_for_terminal,
)
from cw.dev_queue.migrate import migrate_dev_queue
from cw.dev_queue.requeue import (
    _apply_requeue_stage,
    requeue_ticket,
    unblock_ticket,
)
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
    "AWAITING_OPERATOR_DISPOSITION",
    "BRANCH_STALENESS_GATE_DISPOSITION",
    "DEFAULT_PRUNE_OLDER_THAN_DAYS",
    "DRAIN_DISPOSITIONS",
    "EMPTY_DIFF_GATE_DISPOSITION",
    "FINALIZE_GATE_HELD_DISPOSITION",
    "HOLD_DISPOSITIONS",
    "REVIEW_HEALTH_GATE_DISPOSITION",
    "REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION",
    "SIGNOFF_GATE_DISPOSITION",
    "STALE_DISPATCH_DISPOSITION",
    "STALE_DISPATCH_GATE_DISPOSITION",
    "_PLAN_SOUNDNESS_MARKER",
    "_PLAN_SPEC_MARKER",
    "_PRE_DISPATCH_STALE_PR_REASON",
    "LaneNotFoundError",
    "_advance_task_pointer",
    "_apply_requeue_stage",
    "_approve_ticket_locked",
    "_derive_disposition",
    "_extract_pr_url",
    "_extract_pr_url_or_info",
    "_find_ticket",
    "_hold_aware_disposition",
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
    "approve_ticket",
    "cancel_task_for_session",
    "cancel_ticket",
    "clear_tickets",
    "consume_completed_sessions",
    "dev_queue_lock",
    "drain_held_tickets",
    "list_tickets",
    "load_dev_queue",
    "load_plan",
    "migrate_dev_queue",
    "move_ticket",
    "plan_path",
    "prune_tickets",
    "register_watched_pr",
    "remove_ticket",
    "requeue_ticket",
    "resolve_client",
    "save_dev_queue",
    "save_plan",
    "select_clearable_tickets",
    "select_held_tickets",
    "select_prunable_tickets",
    "task_attention_state",
    "transition_task_status",
    "unblock_ticket",
    "wait_for_terminal",
]
