"""Tracker-operation wording for the opencode stage prompts (#3055, RFC 0018 S3).

cw resolves the client's tracker (``cw.tracker.tracker_kind``) and threads it
into the opencode PLAN and IMPL prompts, so the agent is told exactly how to
read and post on this tracker, and to fail loudly instead of improvising or
silently skipping a write it cannot make:

* **github-issues**: the ``gh`` read and post commands.
* **another tracker**: that tracker's MCP tools, named by the configured
  server prefix (``tracker_mcp_gate.servers["opencode"]``) when one is set.
* both of the above get the failure paragraph: a write with no tool, or any
  failed call, is ``tracker_tool_unavailable`` (a failed read stays
  ``plan_missing_context``).
* **unset**: no tracker is configured, so the agent is told to block.

REVIEW performs no tracker operations; both REVIEW prompts carry
:data:`REVIEW_NO_TRACKER_LINE`.

A leaf module, so ``opencode_runner`` (near the module-size ceiling) and
``opencode_clean_review`` share it without importing each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result.schema._vocab import (
    PLAN_MISSING_CONTEXT_BLOCKER_REASON,
    TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, TrackerClass

if TYPE_CHECKING:
    from cw.tracker import TrackerKind

#: The one line both opencode REVIEW prompts carry.
REVIEW_NO_TRACKER_LINE = "This stage performs no tracker operations."

_FAILURE_PARAGRAPH = (
    "If no tool for this tracker is available in this session, or a call "
    "fails, do not improvise or skip: emit status blocked with blocker.reason "
    f"`{TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON}` (for a write) or "
    f"`{PLAN_MISSING_CONTEXT_BLOCKER_REASON}` "
    "(for a read), with blocker.details naming the tracker, the operation and "
    "the error."
)

_UNSET_LINE = (
    f"Tracker unknown: emit blocked `{TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON}`."
)


def _github_line(ticket_id: str) -> str:
    return (
        f"Tracker: {TRACKER_GITHUB_ISSUES}. Read: `gh issue view {ticket_id} "
        "--json title,body,comments`. Post: `gh issue comment "
        f"{ticket_id} --body-file <file>`."
    )


def _other_line(system: str, server: str | None) -> str:
    prefix = "" if server is None else f" (names carry the `{server}_` prefix)"
    return (
        f"Tracker: {system}. Use the {system} MCP tools exposed in this "
        f"session{prefix}: the get-issue operation, the list-comments "
        "operation, and the create-comment operation. No shell command "
        "reaches this tracker."
    )


def tracker_ops_fragment(
    ticket_id: str, tracker: TrackerKind | None, server: str | None
) -> str:
    """The tracker-operations text for one opencode PLAN/IMPL prompt.

    *tracker* ``None`` reads as unset (the prompt builder's default).
    *server* is the opencode MCP server name, or ``None`` when no gate names
    one (the ``{server}_`` prefix parenthetical is then dropped).
    """
    if tracker is None or tracker.tracker_class is TrackerClass.UNSET:
        return _UNSET_LINE
    if tracker.tracker_class is TrackerClass.GITHUB:
        line = _github_line(ticket_id)
    else:
        line = _other_line(tracker.system or "", server)
    return f"{line} {_FAILURE_PARAGRAPH}"
