"""The MCP-allowlisted ``codex exec`` argv for the tracker-IO turn (#3057).

One launch site, built through the shared sandbox and approval seam
(:func:`cw.codex_review._exec_policy_argv`) with a hard-coded
``SandboxMode.READ_ONLY``: the turn writes nothing to disk, so no knob widens
it. Every other choice is fixed too:

* ``--skip-git-repo-check``: the turn runs in a scratch directory, not a repo.
* ``--ephemeral``: no resumable session file under ``~/.codex/sessions``.
* ``-c model_reasoning_effort=low``: posting text verbatim needs no depth.
* ``-c mcp_servers.<server>.enabled_tools=[...]``: the tracker's comment
  create and list tools only (:data:`TRACKER_COMMENT_TOOLS`).
* ``-c mcp_servers.<other>.enabled=false`` for every other server the
  user-layer config declares.

``--ignore-user-config`` is deliberately absent: the MCP server definition
(command, url, auth) lives in ``~/.codex/config.toml`` and cw must not learn
how to start a tracker's server (ADR-0013).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from cw.codex_review import _exec_policy_argv, _reasoning_effort_argv
from cw.models.enums import SandboxMode
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io._verify import TrackerIoReason

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

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


def comment_tools_for(tracker: str) -> tuple[str, str]:
    """The (create, list) tools for *tracker*, or ``TRACKER_UNSUPPORTED``."""
    tools = TRACKER_COMMENT_TOOLS.get(tracker)
    if tools is None:
        msg = f"no tracker-IO comment tools for tracker {tracker!r}"
        raise TrackerIoConfigError(TrackerIoReason.TRACKER_UNSUPPORTED, msg)
    return tools


def _disable_others(server: str, other_servers: Sequence[str]) -> list[str]:
    """``-c mcp_servers.<other>.enabled=false`` for each other declared server.

    A declared name that ``-c`` cannot address as one key segment (a dot, a
    space) is a configuration failure: continuing would leave an MCP server
    enabled outside the turn's explicit allowlist.
    """
    argv: list[str] = []
    seen = {server}
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


def build_tracker_io_argv(
    *,
    server: str,
    tracker: str,
    schema_path: Path,
    output_path: Path,
    other_servers: Sequence[str],
) -> list[str]:
    """Return the tracker-IO turn's ``codex exec`` argv (prompt goes on stdin).

    Raises :class:`TrackerIoConfigError` for an unsupported *tracker* or a
    *server* name that is not a plain identifier.
    """
    tools = comment_tools_for(tracker)
    validate_server_name(server)
    allowlist = json.dumps(list(tools), separators=(",", ":"))
    return [
        "codex",
        "exec",
        *_exec_policy_argv(SandboxMode.READ_ONLY),
        "--skip-git-repo-check",
        "--ephemeral",
        *_reasoning_effort_argv(TURN_REASONING_EFFORT),
        "-c",
        f"mcp_servers.{server}.enabled_tools={allowlist}",
        *_disable_others(server, other_servers),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
