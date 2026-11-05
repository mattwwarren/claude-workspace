"""Repair-entry guard for the self-contained IMPL executors (#2944).

An adopted non-green PR enters at IMPL carrying synthesized repair feedback
and no plan (RFC 0016 B1). The claude-native IMPL path reads that row through
``.claude/commands/auto-dev-impl.md`` ("Repair entry: no plan"), which binds
the feedback to the live PR, skips plan recovery and limits scope to the
named conflicts and checks. The OpenCode and local IMPL prompts do not read
``queue_metadata.review_feedback``: the OpenCode prompt falls back to the
ticket's ``plan-spec-reviewed`` comment and then runs as ordinary impl, and the
local runner blocks only on a missing plan. Either would edit a repair row
without the feedback or the repair-only scope.

:func:`repair_entry_block_details` therefore fails closed in those two
pre-flights, before any agent runs. Parity for those executors is tracked in
#2983, which decides between failing closed and implementing the contract.
Leaf module: no logging, no events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import Stage

if TYPE_CHECKING:
    from cw.models import TicketTask

#: Backend-neutral ``blocker.reason``; the backend is named in ``details``.
REPAIR_ENTRY_UNSUPPORTED_BACKEND = "repair_entry_unsupported_backend"

_DETAILS = (
    "This row is an adopted non-green PR entering at impl with repair feedback"
    " (queue_metadata.review_feedback and an adoption branch), and backend={backend}"
    " does not read that feedback or apply the repair-only scope. Blocked before any"
    " agent runs. The repair entry is specified for the claude-native impl path;"
    " the opencode and local executors are covered by #2983. To proceed: run this"
    " ticket on the claude-native executor, or remove the row and re-add it"
    " with cw dev-queue add --adopt-pr."
)


def is_adopted_repair_entry(task: TicketTask) -> bool:
    """True for an adoption row carrying repair feedback (the #2944 carve-out key)."""
    return task.review_feedback is not None and task.branch is not None


def repair_entry_block_details(
    task: TicketTask, stage: Stage, backend: str
) -> str | None:
    """Details text when this spawn must fail closed; ``None`` otherwise.

    Blocks only an IMPL spawn of an adopted repair row. A plain impl row with
    feedback but no adoption branch, a REVIEW or FINALIZE spawn, and every
    other row return ``None``.
    """
    if stage != Stage.IMPL or not is_adopted_repair_entry(task):
        return None
    return _DETAILS.format(backend=backend)
