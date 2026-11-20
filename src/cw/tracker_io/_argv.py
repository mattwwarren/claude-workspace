"""The MCP-allowlisted ``codex exec`` argv for the tracker-IO turn (#3057).

One launch site, built through the shared sandbox and approval seam
(:func:`cw.codex_review._exec_policy_argv`) with a hard-coded
``SandboxMode.READ_ONLY``: the turn writes nothing to disk, so no knob widens
it. Every other choice is fixed too:

* ``--skip-git-repo-check``: the turn runs in a scratch directory, not a repo.
* ``--ephemeral``: no resumable session file under ``~/.codex/sessions``.
* ``-c model_reasoning_effort=low``: posting text verbatim needs no depth.
* ``-c mcp_servers.<server>.enabled_tools=[...]``: the tracker's comment
  create and list tools only (:data:`TRACKER_COMMENT_TOOLS`); a refresh turn
  allows the ``get`` and ``list`` tools only (:func:`turn_tools`).
* ``-c mcp_servers.<other>.enabled=false`` for every other server the
  user and system layers declare.

``--ignore-user-config`` is deliberately absent: the MCP server definition
(command, url, auth) lives in ``~/.codex/config.toml`` and cw must not learn
how to start a tracker's server (ADR-0013).
"""

from __future__ import annotations

import json
import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.codex_review import (
    _AUDIT_ARGV_FLAGS,
    _exec_policy_argv,
    _reasoning_effort_argv,
)
from cw.models.bindings import TicketOp
from cw.models.capabilities import ToolNarrowing
from cw.models.enums import SandboxMode
from cw.models.orchestrator_config.constants import CODEX_BACKEND
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io._verify import TrackerIoReason

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from cw.models.bindings import TicketsBinding

_log = logging.getLogger(__name__)

#: Per tracker system: the (create, list) comment tools the turn may call.
#: The names are the ones the claude-native stage docs use for Linear.
TRACKER_COMMENT_TOOLS: Mapping[str, tuple[str, str]] = {
    TRACKER_LINEAR: ("create_comment", "list_comments"),
}

#: The turn's reasoning-effort pin.
TURN_REASONING_EFFORT = "low"

# A name ``-c mcp_servers.<name>....`` can address as one dotted-key segment.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


class TrackerIoConfigError(ValueError):
    """The turn cannot be built; *reason* is the closed-vocabulary cause."""

    def __init__(self, reason: TrackerIoReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_valid_name(name: str) -> bool:
    """True when *name* is a non-empty ``[A-Za-z0-9_-]`` run (no newline)."""
    return _NAME_RE.fullmatch(name) is not None


def validate_server_name(server: str) -> str:
    """Return *server*, or raise ``TrackerIoConfigError(MCP_SERVER_UNNAMED)``."""
    if not is_valid_name(server):
        msg = f"MCP server name {server!r} is not a [A-Za-z0-9_-]+ identifier"
        raise TrackerIoConfigError(TrackerIoReason.MCP_SERVER_UNNAMED, msg)
    return server


def resolve_ticket_tool(
    tickets: TicketsBinding | None,
    backend: str,
    op: TicketOp,
    default: str | None,
) -> str | None:
    """The binding's tool name for *op* under *backend*, else *default*."""
    if tickets is not None:
        named = tickets.tool_name(backend, op)
        if named is not None:
            return named
    return default


def comment_tools_for(
    tracker: str,
    tickets: TicketsBinding | None = None,
    backend: str = CODEX_BACKEND,
) -> tuple[str, str]:
    """The (create, list) tools for *tracker* under *backend*.

    A tool the ``tickets`` binding names wins; the table is the default for
    each. Raises ``TRACKER_UNSUPPORTED`` when either tool resolves to nothing.
    """
    defaults = TRACKER_COMMENT_TOOLS.get(tracker)
    create_default, list_default = defaults if defaults is not None else (None, None)
    create = resolve_ticket_tool(tickets, backend, TicketOp.COMMENT, create_default)
    listing = resolve_ticket_tool(
        tickets, backend, TicketOp.LIST_COMMENTS, list_default
    )
    if create is None or listing is None:
        msg = f"no tracker-IO comment tools for tracker {tracker!r}"
        raise TrackerIoConfigError(TrackerIoReason.TRACKER_UNSUPPORTED, msg)
    return create, listing


def _disable_others(
    server: str,
    other_servers: Sequence[str],
    *,
    disable_target: bool = False,
) -> list[str]:
    """Disable each declared MCP server outside the app's active surface.

    A declared name that ``-c`` cannot address as one key segment (a dot, a
    space) is a configuration failure: continuing would leave an MCP server
    enabled outside the turn's explicit allowlist.
    """
    argv: list[str] = []
    seen = set() if disable_target else {server}
    for other in other_servers:
        if other in seen:
            continue
        seen.add(other)
        if not is_valid_name(other):
            msg = (
                f"declared non-target MCP server {other!r} cannot be disabled "
                "with a safe -c override"
            )
            raise TrackerIoConfigError(TrackerIoReason.MCP_SERVER_UNNAMED, msg)
        argv += ["-c", f"mcp_servers.{other}.enabled=false"]
    return argv


class TurnKind(StrEnum):
    """Which courier turn an allowlist is for (O2b, R7).

    REFRESH is the T2b read turn (#3760): one read-only turn that returns a
    ticket snapshot. The enum stays open for further kinds.
    """

    POST = "post"
    VERIFY = "verify"
    REFRESH = "refresh"


def turn_tools(
    kind: TurnKind, post_tools: tuple[str, str], get_tool: str | None = None
) -> tuple[str, ...]:
    """The tool allowlist for *kind*.

    POST gets create and list, VERIFY list only, REFRESH the ``get`` and list
    tools (*get_tool* is required for it).
    """
    if kind is TurnKind.POST:
        return post_tools
    if kind is TurnKind.REFRESH:
        if get_tool is None:
            msg = "a REFRESH turn needs a get tool"
            raise ValueError(msg)
        return (get_tool, post_tools[1])
    return (post_tools[1],)


#: Per tracker system: the ``get`` tool the snapshot turn reads one issue with.
TRACKER_GET_TOOLS: Mapping[str, str] = {TRACKER_LINEAR: "get_issue"}


def get_tool_for(
    tracker: str,
    tickets: TicketsBinding | None = None,
    backend: str = CODEX_BACKEND,
) -> str:
    """The ``get`` tool for *tracker*: the binding's name wins, else the table.

    Raises ``TRACKER_UNSUPPORTED`` when nothing resolves (O2b, R8).
    """
    get = resolve_ticket_tool(
        tickets, backend, TicketOp.GET, TRACKER_GET_TOOLS.get(tracker)
    )
    if get is None:
        msg = f"no tracker-IO get tool for tracker {tracker!r}"
        raise TrackerIoConfigError(TrackerIoReason.TRACKER_UNSUPPORTED, msg)
    return get


def narrowing_for(server: str, declared: Sequence[str]) -> ToolNarrowing:
    """Narrowing for *server*: a declared server is per tool, an app is per server.

    *declared* is the user and system layers' server names (R3). This refines
    H1's codex ``PER_TOOL`` declaration per binding; a server absent from those
    layers is a plugin app and is addressed by ``apps.<id>``.
    """
    return ToolNarrowing.PER_TOOL if server in declared else ToolNarrowing.PER_SERVER


def build_tracker_io_argv(
    *,
    server: str,
    tools: tuple[str, ...],
    schema_path: Path,
    output_path: Path,
    other_servers: Sequence[str],
    narrowing: ToolNarrowing = ToolNarrowing.PER_TOOL,
) -> list[str]:
    """Return the tracker-IO turn's ``codex exec`` argv (prompt goes on stdin).

    *tools* is the allowlist for this turn (see :func:`turn_tools`). Under
    ``PER_TOOL`` it becomes ``mcp_servers.<server>.enabled_tools``. Under
    ``PER_SERVER`` or ``NONE`` (a plugin app, #3695) the server is addressed as
    ``apps.<server>.default_tools_enabled=true`` and *tools* is ignored: the
    app's full surface is what the snapshot delta then checks (R5, R19).
    The ``=true`` key is an unverified premise until run on a plugin-app Linear
    host (R23). Raises :class:`TrackerIoConfigError` for a *server* name that is
    not a plain identifier.
    """
    validate_server_name(server)
    if narrowing is ToolNarrowing.PER_TOOL:
        allowlist = json.dumps(list(tools), separators=(",", ":"))
        scoped = [
            "-c",
            f"mcp_servers.{server}.enabled_tools={allowlist}",
        ]
    else:
        scoped = ["-c", f"apps.{server}.default_tools_enabled=true"]
    return [
        "codex",
        "exec",
        *_exec_policy_argv(SandboxMode.READ_ONLY),
        "--skip-git-repo-check",
        *_AUDIT_ARGV_FLAGS,
        *_reasoning_effort_argv(TURN_REASONING_EFFORT),
        *scoped,
        *_disable_others(
            server,
            other_servers,
            disable_target=narrowing is not ToolNarrowing.PER_TOOL,
        ),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
