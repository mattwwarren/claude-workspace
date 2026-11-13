"""The read-only ``codex exec`` argv for the codex PLAN turn (#3060, RFC 0018 C1).

The one ``codex exec`` launch site in :mod:`cw.codex_plan`. Its sandbox and
approval policy come from the shared seam
(:func:`cw.codex_review._exec_policy_argv`) called with a fixed
``SandboxMode.READ_ONLY`` member: the PLAN turn writes nothing (cw writes the
plan artifacts outside the sandbox), so there is no knob, and the REVIEW-stage
``reviewer_sandbox_mode`` / ``fix_sandbox_mode`` settings are never read.

Fixed choices:

* ``--json`` + ``--ephemeral``: the JSONL event stream (model-capacity
  detection reads it) and no resumable session file.
* ``--output-schema`` / ``-o``: the strict ``{plan_markdown, sentinel}``
  document, in a uuid scratch directory the driver owns.
* No ``--skip-git-repo-check``: the turn runs in the ticket worktree.
* MCP: on a non-GitHub tracker the codex server named by
  ``tracker_mcp_gate.servers.codex`` gets the tracker's read tools only
  (:data:`PLAN_TRACKER_READ_TOOLS`); every other declared server is disabled.
  On GitHub (cw prefetches the ticket) no server is enabled at all.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from cw.codex_review import (
    _AUDIT_ARGV_FLAGS,
    _exec_policy_argv,
    _reasoning_effort_argv,
)
from cw.models.enums import SandboxMode
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import TrackerIoConfigError, TrackerIoReason
from cw.tracker_io._argv import _disable_others, validate_server_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

#: The PLAN turn's sandbox and approval policy: the seam's read-only output,
#: named once. :func:`require_read_only_argv` compares a launch against it.
PLAN_READ_ONLY_ARGV: Final[tuple[str, ...]] = tuple(
    _exec_policy_argv(SandboxMode.READ_ONLY)
)

#: Per tracker system: the read tools a non-GitHub PLAN turn may call. The
#: names are the ones the claude-native plan doc uses for Linear.
PLAN_TRACKER_READ_TOOLS: Mapping[str, tuple[str, str]] = {
    TRACKER_LINEAR: ("get_issue", "list_comments"),
}


class PlanArgvPolicyError(ValueError):
    """A PLAN argv does not carry exactly the fixed read-only policy."""


def read_tools_for(tracker: str) -> tuple[str, str]:
    """The read tools for *tracker*; raises ``TrackerIoConfigError`` when none."""
    tools = PLAN_TRACKER_READ_TOOLS.get(tracker)
    if tools is None:
        msg = f"no codex PLAN read tools for tracker {tracker!r}"
        raise TrackerIoConfigError(TrackerIoReason.TRACKER_UNSUPPORTED, msg)
    return tools


def _mcp_argv(
    mcp_server: str | None, tracker: str | None, other_servers: Sequence[str]
) -> list[str]:
    """The MCP ``-c`` overrides: the read allowlist, or every server disabled."""
    if mcp_server is None or tracker is None:
        return _disable_others("", other_servers)
    tools = read_tools_for(tracker)
    validate_server_name(mcp_server)
    allowlist = json.dumps(list(tools), separators=(",", ":"))
    return [
        "-c",
        f"mcp_servers.{mcp_server}.enabled_tools={allowlist}",
        *_disable_others(mcp_server, other_servers),
    ]


def build_plan_argv(
    *,
    schema_path: Path,
    output_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    mcp_server: str | None,
    tracker: str | None,
    other_servers: Sequence[str],
) -> list[str]:
    """Return the PLAN turn's ``codex exec`` argv (the prompt goes on stdin).

    *mcp_server* and *tracker* are both set on a non-GitHub tracker and both
    ``None`` on GitHub. Raises :class:`~cw.tracker_io.TrackerIoConfigError`
    for a tracker with no read allowlist or a server name that is not a plain
    identifier.
    """
    argv = [
        "codex",
        "exec",
        *PLAN_READ_ONLY_ARGV,
        *_AUDIT_ARGV_FLAGS,
        *_reasoning_effort_argv(reasoning_effort),
        *_mcp_argv(mcp_server, tracker, other_servers),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model:
        argv += ["-m", model]
    return argv


def require_read_only_argv(argv: Sequence[str]) -> None:
    """Refuse any PLAN argv whose policy is not exactly :data:`PLAN_READ_ONLY_ARGV`.

    The policy block must follow ``codex exec`` contiguously and its flag must
    appear once, so neither a wider mode nor a missing approval override nor a
    second, later sandbox flag can reach the launch.
    """
    width = len(PLAN_READ_ONLY_ARGV)
    if (
        tuple(argv[2 : 2 + width]) != PLAN_READ_ONLY_ARGV
        or list(argv).count(PLAN_READ_ONLY_ARGV[0]) != 1
    ):
        msg = "codex PLAN argv does not carry the fixed sandbox and approval policy"
        raise PlanArgvPolicyError(msg)
