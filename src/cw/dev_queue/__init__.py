"""Dev-queue management for orchestrator ticket dispatch.

Package split (#1317-#1318, complete). The historical flat ``cw.dev_queue``
module is now a package of focused submodules:

* ``migrate`` — the pure dict-in / dict-out schema-normalisation layer.
* ``storage`` — the on-disk persistence layer (file locks + plan/queue
  load & save).
* ``lifecycle`` — task status transitions, disposition/terminal-status
  constants, stage-pointer helpers, and the terminal-wait poll loop.
* ``crud`` — operator-facing queue mutations (add/remove/cancel/move/clear)
  and the ticket-resolution helpers (resolve/list/find).
* ``approval`` — the plan/review approval + operator-signoff-clearing gates.
* ``requeue`` — re-run a stage, regress, or clear a salvage park.
* ``drain`` — batch-resume every Rule-5 availability park (RFC 0011 A4).

This ``__init__`` re-exports the full historical public + private surface so
every ``from cw.dev_queue import X`` import site and downstream call path keeps
working unchanged.
"""

from __future__ import annotations

from cw.dev_queue.approval import _approve_ticket_locked, approve_ticket
from cw.dev_queue.crud import (
    _find_ticket,
    _newest_by_created_at,
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    move_ticket,
    register_watched_pr,
    remove_ticket,
    resolve_client,
)
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    AWAITING_OPERATOR_DISPOSITION,
    HOLD_DISPOSITIONS,
    SIGNOFF_GATE_DISPOSITION,
    _advance_task_pointer,
    _derive_disposition,
    _extract_pr_url,
    _hold_aware_disposition,
    _result_blocker_reason,
    _stage_regress,
    consume_completed_sessions,
    transition_task_status,
    wait_for_terminal,
)
from cw.dev_queue.drain import (
    DRAIN_DISPOSITIONS,
    drain_held_tickets,
    select_held_tickets,
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
    "DRAIN_DISPOSITIONS",
    "HOLD_DISPOSITIONS",
    "SIGNOFF_GATE_DISPOSITION",
    "_PLAN_SOUNDNESS_MARKER",
    "_PLAN_SPEC_MARKER",
    "LaneNotFoundError",
    "_advance_task_pointer",
    "_apply_requeue_stage",
    "_approve_ticket_locked",
    "_derive_disposition",
    "_extract_pr_url",
    "_find_ticket",
    "_hold_aware_disposition",
    "_lock",
    "_newest_by_created_at",
    "_result_blocker_reason",
    "_stage_regress",
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
    "register_watched_pr",
    "remove_ticket",
    "requeue_ticket",
    "resolve_client",
    "save_dev_queue",
    "save_plan",
    "select_held_tickets",
    "transition_task_status",
    "unblock_ticket",
    "wait_for_terminal",
]
