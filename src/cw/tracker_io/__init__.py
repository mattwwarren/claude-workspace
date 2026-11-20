"""The tracker-IO agent turn for authorized tracker writes (#3057, RFC 0018 A2).

On a non-GitHub tracker the daemon has no tracker client (ADR-0013), so a
write it must make itself -- today only the codex REVIEW verdict comment --
goes through one cw-launched ``codex exec`` turn with an explicit MCP
allowlist (the tracker's comment create and list tools). cw plans the parts,
builds the argv, parses the agent's JSON report and verifies it; the agent
does the tracker I/O. The verification is self-attested: the ids and
read-back windows are model-echoed, not proof of delivery.

The O2b courier (RFC 0019, ADR-0023) runs post, verify and snapshot turns for
the outbox drain: :func:`courier_drain` returns a :class:`CourierResult`, and a
plugin app is checked by a before/after :func:`compare_snapshots` delta.

Modules: :mod:`._argv` (the launch site and narrowing), :mod:`._prompt` (prompts
and result schema), :mod:`._verify` (reasons, public state, checks),
:mod:`._snapshot_check` (the delta), :mod:`._dedup` (courier parts and
provenance), :mod:`._turn` (the single-turn orchestration,
:func:`post_parts_via_turn`), :mod:`._drain` (the courier turn loop),
:mod:`._refresh` (the read-only snapshot refresh turn, #3760).
"""

from __future__ import annotations

from cw.tracker_io._argv import (
    TRACKER_COMMENT_TOOLS,
    TrackerIoConfigError,
    TurnKind,
    build_tracker_io_argv,
    comment_tools_for,
    get_tool_for,
    narrowing_for,
    turn_tools,
)
from cw.tracker_io._dedup import CourierPart
from cw.tracker_io._drain import (
    CourierContext,
    CourierDisposition,
    CourierResult,
    courier_drain,
)
from cw.tracker_io._prompt import (
    READBACK_WINDOW,
    REFRESH_SCHEMA,
    RESULT_SCHEMA,
    RefreshDocument,
    TurnPart,
    TurnResult,
    parse_turn_result,
    render_prompt,
)
from cw.tracker_io._refresh import courier_refresher, courier_snapshot
from cw.tracker_io._snapshot_check import (
    SNAPSHOT_SCHEMA,
    IssueSnapshot,
    compare_snapshots,
)
from cw.tracker_io._turn import (
    TURN_TIMEOUT_SECONDS,
    TrackerIoContext,
    TrackerIoOutcome,
    plan_turn_parts,
    post_parts_via_turn,
)
from cw.tracker_io._verify import (
    PostedComment,
    PublicState,
    TrackerIoReason,
    VerifyOutcome,
    public_state_for,
    verify_turn_result,
)

__all__ = [
    "READBACK_WINDOW",
    "REFRESH_SCHEMA",
    "RESULT_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "TRACKER_COMMENT_TOOLS",
    "TURN_TIMEOUT_SECONDS",
    "CourierContext",
    "CourierDisposition",
    "CourierPart",
    "CourierResult",
    "IssueSnapshot",
    "PostedComment",
    "PublicState",
    "RefreshDocument",
    "TrackerIoConfigError",
    "TrackerIoContext",
    "TrackerIoOutcome",
    "TrackerIoReason",
    "TurnKind",
    "TurnPart",
    "TurnResult",
    "VerifyOutcome",
    "build_tracker_io_argv",
    "comment_tools_for",
    "compare_snapshots",
    "courier_drain",
    "courier_refresher",
    "courier_snapshot",
    "get_tool_for",
    "narrowing_for",
    "parse_turn_result",
    "plan_turn_parts",
    "post_parts_via_turn",
    "public_state_for",
    "render_prompt",
    "turn_tools",
    "verify_turn_result",
]
