"""Repair-entry guard for the self-contained IMPL executors (#2944, RFC 0018 B1).

An adopted non-green PR enters at IMPL carrying synthesized repair feedback
and no plan (RFC 0016 B1). The claude-native IMPL path reads that row through
``.claude/commands/auto-dev-impl.md`` ("Repair entry: no plan"), which binds
the feedback to the live PR, skips plan recovery and limits scope to the
named conflicts and checks. The local IMPL runner does not read
``queue_metadata.review_feedback`` and blocks only on a missing plan, so it
would edit an adopted repair row without the feedback or the repair-only scope.

The OpenCode IMPL prompt does not read the feedback either, so the opencode
pre-flight refuses any IMPL row carrying feedback (``block_any_feedback``).
#2983 decided that parity question fail-closed for opencode (RFC 0018 B1,
#3059).

:func:`repair_entry_block_details` therefore fails closed in those pre-flights,
before any agent runs. Leaf module: no logging, no events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import Stage

if TYPE_CHECKING:
    from cw.models import TicketTask

#: Backend-neutral ``blocker.reason``; the backend is named in ``details``.
REPAIR_ENTRY_UNSUPPORTED_BACKEND = "repair_entry_unsupported_backend"

_DETAILS = (
    "This row is an IMPL spawn carrying repair feedback"
    " (queue_metadata.review_feedback), and backend={backend} does not read that"
    " feedback or apply the repair-only scope. Blocked before any agent runs. The"
    " repair entry is specified for the claude-native impl path; #2983 decided"
    " fail-closed for opencode and local adopted rows (RFC 0018 B1, #3059). To"
    " proceed: run this ticket on the claude-native executor, or re-add it without"
    " --review-feedback-file or with cw dev-queue add --adopt-pr."
)


def is_adopted_repair_entry(task: TicketTask) -> bool:
    """True for an adoption row carrying repair feedback (the #2944 carve-out key)."""
    return task.review_feedback is not None and task.branch is not None


def repair_entry_block_details(
    task: TicketTask,
    stage: Stage,
    backend: str,
    *,
    block_any_feedback: bool = False,
) -> str | None:
    """Details text when this spawn must fail closed; ``None`` otherwise.

    Blocks only an IMPL spawn of a row that carries repair feedback. The local
    executor (default ``block_any_feedback=False``) blocks only an adopted row
    (feedback and adoption branch). The opencode executor passes
    ``block_any_feedback=True``, so any feedback row is refused. A REVIEW or
    FINALIZE spawn, a row without feedback, and every other row return ``None``.
    """
    if stage != Stage.IMPL or task.review_feedback is None:
        return None
    if not (block_any_feedback or is_adopted_repair_entry(task)):
        return None
    return _DETAILS.format(backend=backend)
