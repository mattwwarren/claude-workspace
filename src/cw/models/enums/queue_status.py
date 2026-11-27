"""Queue item status enum and the status-derived constants (#3739)."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_ON_USER = "blocked_on_user"
    # RFC 0007 Phase 3 (W3): a ticket parked for an explicit operator signoff
    # before it ships (occupies its lane slot the same as BLOCKED_ON_USER).
    # See GitHub #990.
    AWAITING_OPERATOR_SIGNOFF = "awaiting_operator_signoff"


# Statuses that occupy a lane's concurrency slot (RUNNING/BLOCKED_ON_USER
# already did per ADR-0006; AWAITING_OPERATOR_SIGNOFF joins them in #990 --
# a signoff-parked ticket is not eligible for re-dispatch, so it must not be
# double-counted as free capacity). Single source of truth for the 4+
# occupancy-membership tests duplicated across dispatch.py/board.py/
# config_cmds.py prior to this ticket.
OCCUPIED_LANE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)


# GitHub #2321 — the dependency hold: a BLOCKED_ON_USER row waiting on a PR
# merge. Stamped by ``cw dev-queue hold`` and by routing for a worker
# ``dependency_unmerged`` park carrying ``depends_on_pr``. Keyed by the
# ``cw.models`` slot predicate (``occupies_lane_slot``), so it lives here;
# ``cw.dev_queue.dispositions`` re-exports it. A row with a ``blocked_on_pr``
# id frees its lane slot; one without keeps it.
DEPENDENCY_HOLD_DISPOSITION: Final = "dependency_hold"


# Shared persisted discriminator for watches created by dependency holds.
WATCHED_PR_SOURCE_DEPENDENCY_HOLD: Final = "dependency_hold"


# Genuinely terminal QueueItemStatus values -- distinct from "outside
# OCCUPIED_LANE_STATUSES", which also includes PENDING (redispatch-eligible,
# not terminal). Single source of truth for the duplicate {COMPLETED, FAILED,
# CANCELLED} literal independently defined in
# cw.reconcile._shared._GENUINELY_TERMINAL_QUEUE_STATUSES and
# cw.reconcile.review_recipes.auto_fix_ci._REQUEUE_ELIGIBLE_STATUSES prior to
# GitHub #1692 review round 2; both modules now import this constant instead
# of defining their own copy, following the OCCUPIED_LANE_STATUSES precedent
# above.
TERMINAL_QUEUE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
    ]
)
