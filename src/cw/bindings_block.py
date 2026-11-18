"""The one bindings-block renderer (RFC 0019 R2, #3703).

Every harness gets the same text: a delimited ``Bindings`` block that tells the
agent how to read and post on the client's tracker and which system plays each
other role. The block is written into ``cw-context.json`` (``bindings_block``)
at spawn and inlined into the codex and opencode stage prompts, so the context
key and the prompt carry the same bytes.

Output is deterministic: roles render in :class:`Role` order, operations in
:class:`TicketOp` order, and there are no timestamps. The BEGIN/END lines make
"exactly one block" a plain count.

A leaf module. It imports the bindings model, the tracker constants and the
blocker vocabulary, so ``opencode_runner``, ``spawn`` and the codex prompt
builders can import it without an import cycle. ``ClientConfig`` appears only
in a ``TYPE_CHECKING`` import.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result.schema._vocab import (
    PLAN_MISSING_CONTEXT_BLOCKER_REASON,
    TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON,
)
from cw.models.bindings import (
    Bindings,
    Harness,
    McpRealization,
    Role,
    RoleBinding,
    TicketOp,
    TicketsBinding,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from cw.models.client import ClientConfig

BINDINGS_SECTION: Final = "Bindings"
BINDINGS_BEGIN: Final = f"=== BEGIN CW-INJECTED: {BINDINGS_SECTION} ==="
BINDINGS_END: Final = f"=== END CW-INJECTED: {BINDINGS_SECTION} ==="

#: The one line both opencode REVIEW prompts and the codex REVIEW path carry.
REVIEW_NO_TRACKER_LINE: Final = "This stage performs no tracker operations."

_PLACEHOLDER_ID: Final = "<ticket-id>"
_SAFE_RE: Final = re.compile(r"[A-Za-z0-9_.-]+")

_FAILURE_PARAGRAPH: Final = (
    "If no tool for this tracker is available in this session, or a call "
    "fails, do not improvise or skip: emit status blocked with blocker.reason "
    f"`{TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON}` (for a write) or "
    f"`{PLAN_MISSING_CONTEXT_BLOCKER_REASON}` "
    "(for a read), with blocker.details naming the tracker, the operation and "
    "the error."
)

_UNSET_LINE: Final = (
    f"Tracker unknown: emit blocked `{TRACKER_TOOL_UNAVAILABLE_BLOCKER_REASON}`."
)

_OP_PHRASES: Final[dict[TicketOp, str]] = {
    TicketOp.GET: "the get-issue operation",
    TicketOp.LIST_COMMENTS: "the list-comments operation",
    TicketOp.COMMENT: "the create-comment operation",
    TicketOp.CLOSE: "the close operation",
    TicketOp.CREATE: "the create operation",
}

#: Operations listed whether or not the binding names a tool for them.
_ALWAYS_OPS: Final = (TicketOp.GET, TicketOp.LIST_COMMENTS, TicketOp.COMMENT)


def _quote(value: str) -> str:
    """Pass a safe name through; JSON-quote anything else.

    A newline or a fake END line in an operator string cannot break the
    one-block invariant once quoted, and ordinary names stay byte-identical.
    """
    return value if _SAFE_RE.fullmatch(value) else json.dumps(value)


def _join(items: list[str]) -> str:
    """Join with an Oxford comma; the generic phrase list is never empty."""
    *head, last = items
    if not head:
        return last
    if len(head) == 1:
        return f"{head[0]} and {last}"
    return ", ".join(head) + ", and " + last


def _github_line(ticket_id: str) -> str:
    return (
        f"Tracker: {TRACKER_GITHUB_ISSUES}. Read: `gh issue view {ticket_id} "
        "--json title,body,comments`. Post: `gh issue comment "
        f"{ticket_id} --body-file <file>`."
    )


def _label(tickets: TicketsBinding | None, tracker_system: str | None) -> str | None:
    if tickets is not None and tickets.system is not None:
        return str(tickets.system)
    if tracker_system:
        return _quote(tracker_system)
    return None


def _op_phrases(tickets: TicketsBinding | None, harness: Harness) -> str:
    """Operation phrases in :class:`TicketOp` order, each with its tool if named."""
    phrases: list[str] = []
    for op in TicketOp:
        tool = None if tickets is None else tickets.tool_name(harness.value, op)
        if op not in _ALWAYS_OPS and tool is None:
            continue
        phrase = _OP_PHRASES[op]
        if tool is not None:
            phrase += f" (tool `{tool}`)"
        phrases.append(phrase)
    return _join(phrases)


def _decor(realization: McpRealization) -> str:
    if realization.plugin_id is not None:
        return f" (plugin `{_quote(realization.plugin_id)}`)"
    server = _quote(realization.server or "")
    if realization.tools:
        return f" (MCP server `{server}`)"
    return f" (names carry the `{server}_` prefix)"


def _mcp_text(label: str | None, decor: str, ops: str) -> str:
    if label is None:
        return (
            f"Use the tracker MCP tools exposed in this session{decor}: {ops}. "
            "No shell command reaches this tracker."
        )
    return (
        f"Tracker: {label}. Use the {label} MCP tools exposed in this "
        f"session{decor}: {ops}. No shell command reaches this tracker."
    )


def _not_reachable_text(label: str | None, harness: Harness) -> str:
    body = (
        f"is not reachable from harness {harness.value}: the tickets binding "
        "configures no MCP realization for it. No shell command reaches this "
        "tracker."
    )
    if label is None:
        return f"The tracker {body}"
    return f"Tracker: {label}. This tracker {body}"


def _tickets_lines(
    tickets: TicketsBinding | None,
    harness: Harness,
    ticket_id: str,
    tracker_system: str | None,
) -> list[str]:
    """The tickets section: its line, then the failure paragraph where it applies."""
    if tickets is None:
        return _derived_lines(harness, ticket_id, tracker_system)
    if tickets.cli == "gh":
        return [_github_line(ticket_id), _FAILURE_PARAGRAPH]
    return _bound_mcp_lines(tickets, harness, tracker_system)


def _derived_lines(
    harness: Harness, ticket_id: str, tracker_system: str | None
) -> list[str]:
    """The back-compat tickets lines for a client with no ``tickets`` binding."""
    if not tracker_system:
        return [_UNSET_LINE]
    if tracker_system == TRACKER_GITHUB_ISSUES:
        return [_github_line(ticket_id), _FAILURE_PARAGRAPH]
    text = _mcp_text(_quote(tracker_system), "", _op_phrases(None, harness))
    return [text, _FAILURE_PARAGRAPH]


def _bound_mcp_lines(
    tickets: TicketsBinding,
    harness: Harness,
    tracker_system: str | None,
) -> list[str]:
    """The tickets lines for a bound MCP realization (or its absence)."""
    if tickets.mcp is None:
        return [_UNSET_LINE]
    label = _label(tickets, tracker_system)
    realization = tickets.realization(harness.value)
    if realization is None:
        return [_not_reachable_text(label, harness), _FAILURE_PARAGRAPH]
    text = _mcp_text(label, _decor(realization), _op_phrases(tickets, harness))
    return [text, _FAILURE_PARAGRAPH]


def _role_line(role: Role, binding: RoleBinding | None, harness: Harness) -> str | None:
    if binding is None:
        return None
    group = "" if binding.system is None else f" ({_quote(binding.system)})"
    if binding.cli is not None:
        return f"{role.value}{group}: cli gh."
    realization = (binding.mcp or {}).get(harness)
    if realization is None:
        return f"{role.value}{group}: not reachable from harness {harness.value}."
    if realization.plugin_id is not None:
        return f"{role.value}{group}: MCP plugin {_quote(realization.plugin_id)}."
    return f"{role.value}{group}: MCP server {_quote(realization.server or '')}."


def render_bindings_block(
    bindings: Bindings,
    harness: Harness,
    *,
    ticket_id: str | None = None,
    tracker_system: str | None = None,
) -> str:
    """The delimited bindings block for *harness*, ending in a newline.

    *ticket_id* ``None`` renders the ``<ticket-id>`` placeholder.
    *tracker_system* is the project tracker (``tracking.primary.system``): the
    label fallback, and the derived tickets line when ``bindings.tickets`` is
    unset (the back-compat path for clients with no bindings).
    """
    tid = ticket_id or _PLACEHOLDER_ID
    lines = [
        BINDINGS_BEGIN,
        *_tickets_lines(bindings.tickets, harness, tid, tracker_system),
    ]
    others: tuple[tuple[Role, RoleBinding | None], ...] = (
        (Role.KNOWLEDGE, bindings.knowledge),
        (Role.CODE_HOST, bindings.code_host),
        (Role.CHAT, bindings.chat),
    )
    for role, binding in others:
        line = _role_line(role, binding, harness)
        if line is not None:
            lines.append(line)
    lines.append(BINDINGS_END)
    return "\n".join(lines) + "\n"


def bindings_block_for(
    client: ClientConfig, harness: Harness, ticket_id: str | None
) -> str:
    """The block for *client* on *harness*, with the project tracker resolved.

    The one place that reads ``resolve_tracker``, so the renderer itself stays
    config-driven.
    """
    tracker_system = (
        None
        if client.workspace_path is None
        else resolve_tracker(client.workspace_path)
    )
    return render_bindings_block(
        client.bindings,
        harness,
        ticket_id=ticket_id,
        tracker_system=tracker_system,
    )
