"""Per-role tool bindings of a client (RFC 0019 R1, #3702).

A client's ``bindings`` block says, for each role cw needs an external system
for, which system plays it and how each harness reaches it: through an MCP
server (or, for the claude-native harness, a plugin) or through the one
existing GitHub CLI client. The roles are closed (:class:`Role`). Only the
``tickets`` role has an operation set (:class:`TicketOp`); the others gain
one by RFC amendment.

Imported by submodule path (``cw.models.bindings``), not re-exported from
``cw.models``. Depends only on ``cw.models.orchestrator_config.constants`` and
``cw.tracker``, neither of which imports ``cw.models``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, TRACKER_LINEAR

#: The tool-name charset. Mirrors ``cw.tracker_io._argv._NAME_RE`` /
#: ``is_valid_name``; the models layer cannot import ``cw.tracker_io``.
_TOOL_NAME_RE: Final = re.compile(r"[A-Za-z0-9_-]+")

#: The only accepted ``cli`` value: the existing GitHub client.
_GITHUB_CLI: Final = "gh"
#: The ``system`` a ``cli: gh`` binding must name on the code_host role.
_GITHUB_CODE_HOST: Final = "github"
_DEFAULT_SETTINGS_PATH: Final = ".claude/settings.json"


class Harness(StrEnum):
    """The harnesses an ``mcp`` map is keyed by (the backend constants)."""

    CLAUDE_NATIVE = CLAUDE_NATIVE_BACKEND
    LOCAL = LOCAL_BACKEND
    CODEX = CODEX_BACKEND
    OPENCODE = OPENCODE_BACKEND


class Role(StrEnum):
    """The closed set of roles a client can bind."""

    TICKETS = "tickets"
    KNOWLEDGE = "knowledge"
    CODE_HOST = "code_host"
    CHAT = "chat"


class TicketOp(StrEnum):
    """The ``tickets`` role's operations, the keys of a ``tools`` map."""

    GET = "get"
    LIST_COMMENTS = "list_comments"
    COMMENT = "comment"
    CLOSE = "close"
    CREATE = "create"


class TicketsSystem(StrEnum):
    """The systems the ``tickets`` role accepts (the tracker constants)."""

    GITHUB_ISSUES = TRACKER_GITHUB_ISSUES
    LINEAR = TRACKER_LINEAR


#: The ``system`` a ``cli: gh`` binding must name, per role that allows ``cli``.
_CLI_SYSTEMS: Final[dict[Role, str]] = {
    Role.TICKETS: TRACKER_GITHUB_ISSUES,
    Role.CODE_HOST: _GITHUB_CODE_HOST,
}


class McpRealization(BaseModel):
    """How one harness reaches a role's system over MCP.

    Exactly one of ``server`` (the MCP server name the harness's own config
    declares) or ``plugin_id`` (the claude-native ``enabledPlugins`` key) is
    set. ``tools`` optionally maps each ticket operation to the server's tool
    name; the tick-side tool-name check reads it (``cw.binding_tools``, #3744).
    """

    model_config = ConfigDict(extra="forbid")

    server: str | None = None
    plugin_id: str | None = None
    tools: dict[TicketOp, str] | None = None

    @field_validator("tools")
    @classmethod
    def _check_tools(
        cls, value: dict[TicketOp, str] | None
    ) -> dict[TicketOp, str] | None:
        for op, tool in (value or {}).items():
            if _TOOL_NAME_RE.fullmatch(tool) is None:
                msg = (
                    f"tools[{op.value}] {tool!r} is blank or not a tool name"
                    " (letters, digits, '_' and '-')"
                )
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_target(self) -> McpRealization:
        if (self.server is None) == (self.plugin_id is None):
            msg = "an mcp realization needs exactly one of server or plugin_id"
            raise ValueError(msg)
        if self.server is not None and not self.server.strip():
            msg = "mcp realization server is blank"
            raise ValueError(msg)
        return self


class BindingPreflight(BaseModel):
    """The tickets role's pre-dispatch MCP check (the former tracker-MCP gate).

    ``enabled`` opts the client into the check; ``settings_path`` is the
    claude-native settings file read from the ticket's branch. It is opaque.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    settings_path: str = _DEFAULT_SETTINGS_PATH


class RoleBinding(BaseModel):
    """One role's binding: a system plus exactly one realization.

    The realization is a non-empty ``mcp`` map keyed by harness, or ``cli``.
    An explicit ``null`` for either counts as absent, so a binding reloads
    from its own full dump. The role-aware rules live in :meth:`check`,
    which :class:`Bindings` calls with the role name.
    """

    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    mcp: dict[Harness, McpRealization] | None = None
    cli: Literal["gh"] | None = None

    def check(self, role: Role) -> None:
        """Raise ``ValueError`` naming ``bindings.<role>`` on a role-level rule."""
        where = f"bindings.{role.value}"
        if self.mcp is not None and not self.mcp:
            msg = f"{where}.mcp is empty; name at least one harness or use cli"
            raise ValueError(msg)
        has_mcp = bool(self.mcp)
        has_cli = self.cli is not None
        if has_mcp == has_cli:
            got = "both" if has_mcp else "neither"
            msg = f"{where} needs exactly one of a non-empty mcp map or cli; got {got}"
            raise ValueError(msg)
        for harness, realization in (self.mcp or {}).items():
            self._check_realization(where, role, harness, realization)
        if has_cli:
            self._check_cli(where, role)
        self._check_system(where, role)

    @staticmethod
    def _check_realization(
        where: str, role: Role, harness: Harness, realization: McpRealization
    ) -> None:
        if harness is Harness.LOCAL:
            msg = (
                f"{where}.mcp: harness {Harness.LOCAL.value!r} is not allowed"
                " -- the local backend has no MCP"
            )
            raise ValueError(msg)
        if realization.plugin_id is not None and harness is not Harness.CLAUDE_NATIVE:
            msg = (
                f"{where}.mcp[{harness.value}].plugin_id is only valid under"
                f" {Harness.CLAUDE_NATIVE.value}; use server"
            )
            raise ValueError(msg)
        if realization.tools is not None and role is not Role.TICKETS:
            msg = (
                f"{where}.mcp[{harness.value}].tools is not allowed: operations"
                " for this role are defined by an RFC amendment"
            )
            raise ValueError(msg)

    def _check_cli(self, where: str, role: Role) -> None:
        expected = _CLI_SYSTEMS.get(role)
        if expected is None:
            msg = f"{where}.cli is not allowed on this role; use mcp"
            raise ValueError(msg)
        if self.system != expected:
            got = "unset" if self.system is None else str(self.system)
            msg = f"{where}.cli: gh requires system {expected!r} (got {got})"
            raise ValueError(msg)

    def _check_system(self, where: str, role: Role) -> None:
        if role is Role.TICKETS or self.system is None:
            return
        if not self.system.strip():
            msg = f"{where}.system is blank"
            raise ValueError(msg)


class TicketsBinding(RoleBinding):
    """The ``tickets`` role: a tracker system, plus operator ids and preflight.

    ``operator_ids`` are the operator's identities on the tracker, kept
    exactly as written (no normalisation, no dedupe); no consumer reads them
    yet. ``preflight`` is always present and off by default.
    """

    system: TicketsSystem | None = None
    operator_ids: list[str] = Field(default_factory=list)
    preflight: BindingPreflight = Field(default_factory=BindingPreflight)

    @field_validator("operator_ids")
    @classmethod
    def _check_operator_ids(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.strip():
                msg = f"operator_ids entry {entry!r} is blank"
                raise ValueError(msg)
        return value

    def realization(self, backend: str) -> McpRealization | None:
        """The ``mcp`` realization for *backend*, or None."""
        for harness, realization in (self.mcp or {}).items():
            if harness == backend:
                return realization
        return None

    def mcp_server(self, backend: str) -> str | None:
        """The MCP server name *backend* reaches the tracker through, or None."""
        realization = self.realization(backend)
        return None if realization is None else realization.server

    def tool_name(self, backend: str, op: TicketOp) -> str | None:
        """The configured tool name for *op* under *backend*, or None.

        None when the harness has no realization, or the realization names no
        tool for *op*. Callers fall back to their own defaults.
        """
        realization = self.realization(backend)
        if realization is None or realization.tools is None:
            return None
        return realization.tools.get(op)

    @property
    def claude_plugin_id(self) -> str | None:
        """The claude-native ``enabledPlugins`` key of the tracker plugin, or None."""
        realization = self.realization(CLAUDE_NATIVE_BACKEND)
        return None if realization is None else realization.plugin_id


class Bindings(BaseModel):
    """A client's ``bindings`` block: one optional binding per :class:`Role`."""

    model_config = ConfigDict(extra="forbid")

    tickets: TicketsBinding | None = None
    knowledge: RoleBinding | None = None
    code_host: RoleBinding | None = None
    chat: RoleBinding | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_cli_templates(cls, data: object) -> object:
        """Refuse any ``cli`` other than ``gh``, naming the role and echoing it."""
        if not isinstance(data, dict):
            return data
        for role in Role:
            binding = data.get(role.value)
            if not isinstance(binding, dict):
                continue
            value = binding.get("cli")
            if value is not None and value != _GITHUB_CLI:
                msg = (
                    f"bindings.{role.value}.cli must be 'gh' (the existing GitHub"
                    f" client); got {value!r} -- argv templates and other"
                    " commands are not accepted"
                )
                raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _check_roles(self) -> Bindings:
        for role in Role:
            binding: RoleBinding | None = getattr(self, role.value)
            if binding is not None:
                binding.check(role)
        return self
