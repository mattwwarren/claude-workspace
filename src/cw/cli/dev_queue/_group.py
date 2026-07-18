"""Shared ``dev-queue`` click group and cross-submodule constants.

Defines the ``dev_queue`` group object once so every command submodule
(``crud``, ``status``, ``run``, ``wait``, ``tasks``) can decorate its commands
with ``@dev_queue.command(...)``. Also holds the ``_WAIT_EXIT_*`` exit-code
constants re-exported at the package top level (consumed by
``cw.cli.__init__``) plus the two status-rendering constants.
"""

from __future__ import annotations

from cw.cli._base import main
from cw.models import QueueItemStatus

# Statuses considered "active" for default filtering in list / status.
_ACTIVE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.PENDING,
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    }
)

# Suffix appended to a lane breakdown line when that lane is paused (operator or
# circuit breaker). Pinned exact string — asserted verbatim in tests. See #875.
_PAUSED_LANE_MARKER = " [PAUSED]"

_WAIT_EXIT_FAILED: int = 1
_WAIT_EXIT_BLOCKED: int = 2
_WAIT_EXIT_ATTENTION: int = 3
# Ticket parked AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3, #990).
_WAIT_EXIT_SIGNOFF: int = 4
_WAIT_EXIT_TIMEOUT: int = 124


@main.group(name="dev-queue")
def dev_queue() -> None:
    """Manage the orchestrator development queue."""
