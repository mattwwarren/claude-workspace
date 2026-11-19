"""The gate record an unreachable MCP outbox binding files for the operator (O2b, R25).

A stdlib-only leaf: the outbox engine and the tests import it, and it imports
no ``cw`` module. :func:`file_gate_record` is the one function that touches
``~/.claude``; it never raises. A missing, failing or hung ``needs_you.py``
returns ``False`` and the engine retries on the next tick (R13).
"""

from __future__ import annotations

import json
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_NEEDS_YOU_TIMEOUT_SECONDS = 10


class UnreachableCause(StrEnum):
    """Why a binding is unreachable. Gate-record vocabulary only (R13).

    This grows no closed event or reason enum: the outbox event keeps the
    closed ``binding_unreachable`` reason, and this set picks the record's text.
    """

    TOOL_UNAUTHENTICATED = "tool_unauthenticated"
    CONFIG_UNREADABLE = "config_unreadable"
    NO_REALIZATION = "no_realization"
    MCP_NOT_ENABLED = "mcp_not_enabled"
    TRACKER_UNSUPPORTED = "tracker_unsupported"
    CODEX_UNAVAILABLE = "codex_unavailable"


def _needs_you_script() -> Path:
    return Path.home() / ".claude" / "scripts" / "needs_you.py"


def file_gate_record(payload: Mapping[str, object]) -> bool:
    """Pipe *payload* to ``needs_you.py add``; ``True`` only on a zero exit.

    Fail-open for every cause: an absent script, a non-zero exit, a timeout or
    an ``OSError`` returns ``False`` and never raises (R13).
    """
    script = _needs_you_script()
    if not script.is_file():
        return False
    try:
        result = subprocess.run(
            [str(script), "add"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=_NEEDS_YOU_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _system_label(system: str | None) -> str:
    return f"{system} " if system else ""


def build_gate_payload(
    client: str,
    binding: str,
    cause: UnreachableCause,
    *,
    system: str | None,
    detail: str = "",
) -> dict[str, str]:
    """The needs-you record for one unreachable binding, reason-aware (R25).

    Every text names the client and the binding. ``TOOL_UNAUTHENTICATED`` carries
    ``<system>_mcp_unauthenticated`` in ``title`` and ``question``, because
    needs-you has no reason field. ``system=None`` (a non-tickets role) is worded
    without a system name.
    """
    where = f"outbox binding {binding!r} for client {client!r}"
    sys_name = _system_label(system).strip()
    if cause is UnreachableCause.TOOL_UNAUTHENTICATED:
        marker = f"{system}_mcp_unauthenticated" if system else "mcp_unauthenticated"
        title = f"{where}: {marker}"
        question = (
            f"codex reported the {sys_name or 'tracker'} MCP tools unavailable "
            f"({marker}). Re-authenticate the {sys_name or 'tracker'} MCP access "
            f"for codex (run /mcp), then drain. If authentication is current, "
            "the plugin-app key apps.<id>.default_tools_enabled=true may not be "
            "accepted by this codex (unverified premise, ADR-0023)."
        )
        why = "Entries for this binding stay pending until the tools are reachable."
    elif cause is UnreachableCause.CONFIG_UNREADABLE:
        title = f"{where}: codex config unreadable"
        question = (
            "A codex config layer exists but cannot be read or parsed, so cw cannot "
            f"verify which servers are disabled: {detail or 'see the drain log'}. "
            "Fix or remove the named config file(s), then drain."
        )
        why = "A write-capable turn is refused until every config layer is readable."
    elif cause is UnreachableCause.NO_REALIZATION:
        title = f"{where}: no codex realization"
        question = (
            f"The {binding} binding has no codex realization, so cw cannot post to "
            "it. Add a codex MCP realization for this tickets binding."
        )
        why = "Entries for this binding stay pending until a realization exists."
    elif cause is UnreachableCause.MCP_NOT_ENABLED:
        title = f"{where}: MCP server not enabled"
        question = (
            "The tracker's MCP server is absent or disabled in the codex config. "
            "Enable it in the user codex config, then drain."
        )
        why = "Entries for this binding stay pending until the server is enabled."
    elif cause is UnreachableCause.TRACKER_UNSUPPORTED:
        title = f"{where}: no comment tools"
        question = (
            "No comment tools resolve for this tracker. Name them in the tickets "
            "binding (comment and list-comments tools), then drain."
        )
        why = "Entries for this binding stay pending until the tools resolve."
    else:
        title = f"{where}: codex unavailable"
        question = (
            "codex could not be launched for the tracker-IO turn. Check that codex "
            "is installed and on PATH, then drain."
        )
        why = "Entries for this binding stay pending until codex launches."
    return {
        "kind": "gate",
        "title": title,
        "question": question,
        "why_it_matters": why,
        "recommendation": "Fix the named cause, then run cw outbox drain.",
    }
