"""Plain ``Bindings`` builders shared by the bindings-block tests (#3703).

Functions, not fixtures: nothing is registered in ``tests/conftest.py``. A
test that needs a client builds it with ``tests._doctor_helpers.make_client``
and passes one of these as ``bindings=``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.models.bindings import Bindings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The renamed Linear tools the codex ``codex_apps`` host exposes (RFC 0019).
LINEAR_RENAMED_TOOLS: Final[Mapping[str, str]] = {
    "get": "linear_get_issue",
    "list_comments": "linear_list_comments",
    "comment": "linear_save_comment",
}


def bindings_github_cli() -> Bindings:
    """This repo's shape: tickets on GitHub through ``gh``, code host ``gh``."""
    return Bindings.model_validate(
        {
            "tickets": {"system": "github-issues", "cli": "gh"},
            "code_host": {"system": "github", "cli": "gh"},
        }
    )


def bindings_linear_mcp(
    *,
    tools: Mapping[str, str] | None = None,
    server: str = "linear",
    plugin_id: str | None = None,
    harnesses: Sequence[str] = ("codex", "opencode"),
) -> Bindings:
    """Linear on MCP: one server realization per harness in *harnesses*.

    *tools* names the ticket operations' tools (``TicketOp`` values as keys).
    *plugin_id* adds a claude-native plugin realization.
    """
    mcp: dict[str, dict[str, object]] = {}
    for harness in harnesses:
        entry: dict[str, object] = {"server": server}
        if tools:
            entry["tools"] = dict(tools)
        mcp[harness] = entry
    if plugin_id is not None:
        mcp["claude-native"] = {"plugin_id": plugin_id}
    return Bindings.model_validate({"tickets": {"system": "linear", "mcp": mcp}})
