"""Per-harness capability declarations (RFC 0019 H1, #3708).

Each dispatchable harness (claude-native, codex, opencode, local) declares what
it supports as one frozen, pure-data :class:`HarnessCapabilities`. The model is
published through ``cw schema show harness-capabilities``, so every field is a
closed ``StrEnum``, a ``frozenset`` of one, a ``bool`` or an ``int``: no dict
fields and no callables.

Declared, not enforced: the declarations are static data in H1. No dispatch
path reads them yet, and the existing predicates (``clean_mode_supported``,
``engine_supported``, ``finalize_block_details``, ``repair_entry_block_details``,
``CODEX_SUPPORTED_STAGES``) stay the enforcement point until H2. Auth, env and
headless load are unverified by these declarations.

Imported by submodule path (``cw.models.capabilities``), not re-exported from
``cw.models``. Depends only on ``cw.models.enums`` and ``cw.models.bindings``.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cw.models.bindings import Harness
from cw.models.enums import SandboxMode, Stage


class RepairEntry(StrEnum):
    """How a harness treats an adopted or feedback-carrying repair row."""

    SUPPORTED = "supported"
    BLOCKS_ADOPTED = "blocks_adopted"
    BLOCKS_ANY_FEEDBACK = "blocks_any_feedback"


class ToolNarrowing(StrEnum):
    """Granularity at which a harness can narrow the tools a session sees."""

    PER_TOOL = "per_tool"
    PER_SERVER = "per_server"
    NONE = "none"


class UsageSource(StrEnum):
    """Where a harness's usage figures come from.

    ``SIGNAL_STOP`` means a cost figure only, with no token totals.
    """

    SIGNAL_STOP = "signal_stop"
    CODEX_JSON_EVENTS = "codex_json_events"
    UNAVAILABLE = "unavailable"


class BindingProbeKind(StrEnum):
    """Which read reaches a harness's tickets binding, if any."""

    ENABLED_PLUGINS_BRANCH = "enabled_plugins_branch"
    BACKEND_MCP_CONFIG = "backend_mcp_config"
    NONE = "none"


class HarnessCapabilities(BaseModel):
    """What one harness declares it supports.

    ``sandbox_modes`` empty means "the harness manages its own sandbox", not
    "cannot write". Every field is required except ``schema_version``, so a
    new harness must declare everything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    harness: Harness
    stages: frozenset[Stage]
    skills: bool
    subagents: bool
    binding_probe: BindingProbeKind
    sandbox_modes: frozenset[SandboxMode]
    clean_mode_review: bool
    review_engine: bool
    repair_entry: RepairEntry
    tool_narrowing: ToolNarrowing
    usage_source: UsageSource


def in_enum_order[E: StrEnum](members: Iterable[E]) -> tuple[E, ...]:
    """Return *members* sorted by their declaration order in their enum.

    ``frozenset`` iteration order is not deterministic; doctor output and tests
    go through this so the order is the enum's own.
    """
    pool = list(members)
    if not pool:
        return ()
    rank = {member: index for index, member in enumerate(type(pool[0]))}
    return tuple(sorted(pool, key=rank.__getitem__))
