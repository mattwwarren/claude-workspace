"""The tickets binding's declared tool names, checked against a harness (#3744).

RFC 0019 H2b's tool-name check: when an opted-in client declares
``bindings.tickets.mcp.<harness>.tools``, does the harness expose those names?
Nothing on the dispatch tick may spawn a harness or open a connection, so each
harness registers a :data:`ToolListSource` that answers from local state only,
or none:

- **codex**: the layered ``enabled_tools`` allowlist on the server, read in
  process through :func:`cw.backend_mcp_config.read_enabled_tools`. A config
  allowlist, not a live exposure list: a name it excludes is verifiably
  missing, a name it allows is merely not excluded.
- **claude-native**, **opencode**, **local**: no source. Plugin tool names
  appear only after an MCP handshake, and opencode's are an open question, so
  the check is ``unknown`` and fails open.

The comparison :func:`missing_tools` is pure. A later operator-run probe
registers a source in :data:`TOOL_LIST_SOURCES` without touching it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from cw.backend_mcp_config import read_enabled_tools
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from cw.models.client import ClientConfig


@dataclass(frozen=True)
class ToolList:
    """What one source knows about a server's tools.

    ``known`` False means the source could not answer (fail open). When
    ``known``, ``allowed`` is the allowlist, or ``None`` when nothing is
    excluded. ``source`` names where the answer came from.
    """

    known: bool
    allowed: tuple[str, ...] | None
    source: str
    reason: str


#: A harness's tool-list reader: ``(server, root) -> ToolList``.
type ToolListSource = Callable[[str, Path], ToolList]


def _codex_enabled_tools(server: str, root: Path) -> ToolList:
    reading = read_enabled_tools(CODEX_BACKEND, server, root)
    return ToolList(
        known=reading.known,
        allowed=reading.tools,
        source=str(reading.path),
        reason=reading.reason,
    )


#: Per-harness tool-list sources; ``None`` means the harness has none.
TOOL_LIST_SOURCES: Mapping[str, ToolListSource | None] = MappingProxyType(
    {
        CODEX_BACKEND: _codex_enabled_tools,
        CLAUDE_NATIVE_BACKEND: None,
        OPENCODE_BACKEND: None,
        LOCAL_BACKEND: None,
    }
)


class ToolState(StrEnum):
    """The tool-name check's verdict for one harness."""

    #: No ``tools`` map is declared for this harness: nothing to check.
    NOT_DECLARED = "not_declared"
    #: Every declared name is allowed by a known tool list.
    OK = "ok"
    #: A known tool list excludes at least one declared name.
    MISSING = "missing"
    #: No source, or the source could not answer: fail open.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolCheck:
    """One tool-name check: the state, the missing names, and where it looked."""

    state: ToolState
    missing: tuple[str, ...]
    source: str | None
    reason: str


def declared_tools(client: ClientConfig, backend: str) -> tuple[str, ...]:
    """The tool names *client*'s tickets binding declares for *backend*.

    Every declared op, whichever the stage uses (``STAGE_NEEDS`` has no op
    granularity), de-duplicated in declaration order.
    """
    tickets = client.bindings.tickets
    realization = None if tickets is None else tickets.realization(backend)
    if realization is None or realization.tools is None:
        return ()
    return tuple(dict.fromkeys(realization.tools.values()))


def missing_tools(declared: Iterable[str], tool_list: ToolList) -> tuple[str, ...]:
    """The *declared* names a known *tool_list* excludes, sorted. Pure."""
    if not tool_list.known or tool_list.allowed is None:
        return ()
    allowed = set(tool_list.allowed)
    return tuple(sorted({name for name in declared if name not in allowed}))


def check_tools(
    client: ClientConfig,
    backend: str,
    server: str,
    root: Path,
    *,
    sources: Mapping[str, ToolListSource | None] = TOOL_LIST_SOURCES,
) -> ToolCheck:
    """Check *client*'s declared tool names for *backend* against its source."""
    declared = declared_tools(client, backend)
    if not declared:
        return ToolCheck(ToolState.NOT_DECLARED, (), None, "no tools declared")
    source = sources.get(backend)
    if source is None:
        return ToolCheck(ToolState.UNKNOWN, (), None, f"no tool list for {backend}")
    tool_list = source(server, root)
    if not tool_list.known:
        return ToolCheck(ToolState.UNKNOWN, (), tool_list.source, tool_list.reason)
    missing = missing_tools(declared, tool_list)
    state = ToolState.MISSING if missing else ToolState.OK
    return ToolCheck(state, missing, tool_list.source, tool_list.reason)
