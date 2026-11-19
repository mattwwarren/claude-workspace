"""The adopted repair-entry predicate (#2944), shared by executors and gates.

Moved from ``cw.executor._repair_entry`` (RFC 0019 H2, #3709) so the stage
pre-flight and ``cw.stage_verification`` read it without importing the
executor package. Imported by submodule path, not re-exported from
``cw.models``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.models.tasks import TicketTask


def is_adopted_repair_entry(task: TicketTask) -> bool:
    """True for an adoption row carrying repair feedback (the #2944 carve-out key)."""
    return task.review_feedback is not None and task.branch is not None
