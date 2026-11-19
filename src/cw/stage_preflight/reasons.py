"""The closed set of stage pre-flight park reasons (RFC 0019 H2, #3709).

Writers emit only :class:`PreflightReason` values. ``blocker.reason`` stays an
open enum (``docs/headless-contract.md`` §4.2), so rows parked before H2 still
carry the old stage strings. Every reader that classifies a reason goes through
:func:`canonical_reason`, which maps each old string to its new name. The alias
table is removed one release after H2 (see the removal ticket named in the
CHANGELOG).
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from cw.models.enums import DispatchSkipReason, Stage

if TYPE_CHECKING:
    from collections.abc import Mapping


class PreflightReason(StrEnum):
    """Why the stage pre-flight refused a spawn before any agent ran.

    ``STAGE_UNSUPPORTED`` replaces the per-backend stage reasons. The three
    capability reasons keep their pre-H2 names. The three ``BINDING_*`` values
    are the tick-side reachability dispositions (RFC 0019 H2b, #3744), which
    replace the tracker-MCP gate's ``tracker_mcp_gate``.
    """

    STAGE_UNSUPPORTED = "stage_unsupported"
    CLEAN_REVIEW_UNSUPPORTED_BACKEND = "clean_review_unsupported_backend"
    REVIEW_ENGINE_UNSUPPORTED_BACKEND = "review_engine_unsupported_backend"
    REPAIR_ENTRY_UNSUPPORTED_BACKEND = "repair_entry_unsupported_backend"
    BINDING_UNBOUND = "binding_unbound"
    BINDING_UNREACHABLE = "binding_unreachable"
    BINDING_TOOL_MISSING = "binding_tool_missing"


#: Pre-H2 reason strings and the reason each one is read as now.
LEGACY_REASON_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "finalize_unsupported_backend": PreflightReason.STAGE_UNSUPPORTED.value,
        "codex_review_only": PreflightReason.STAGE_UNSUPPORTED.value,
        **{
            f"opencode_{stage.value}_not_implemented": (
                PreflightReason.STAGE_UNSUPPORTED.value
            )
            for stage in Stage
        },
        # RFC 0019 H2b (#3744): the tracker-MCP gate's disposition, which
        # covered both unbound and unreachable, and its skip reason.
        "tracker_mcp_gate": PreflightReason.BINDING_UNREACHABLE.value,
        "tracker_mcp_gate_blocked": DispatchSkipReason.BINDING_BLOCKED.value,
    }
)


def canonical_reason(reason: str) -> str:
    """Return the current name for *reason*: an old alias maps, anything else stays.

    Idempotent, and the identity on every new or unrecognized string.
    """
    return LEGACY_REASON_ALIASES.get(reason, reason)
