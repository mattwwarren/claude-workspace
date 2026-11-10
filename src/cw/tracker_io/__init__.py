"""The tracker-IO agent turn for daemon-only tracker writes (#3057, RFC 0018 A2).

On a non-GitHub tracker the daemon has no tracker client (ADR-0013), so a
write it must make itself -- today only the codex REVIEW verdict comment --
goes through one cw-launched ``codex exec`` turn with an explicit MCP
allowlist (the tracker's comment create and list tools). cw plans the parts,
builds the argv, parses the agent's JSON report and verifies it; the agent
does the tracker I/O. The verification is self-attested: the ids and
read-back windows are model-echoed, not proof of delivery.

Modules: :mod:`._argv` (the launch site), :mod:`._prompt` (prompt and result
schema), :mod:`._verify` (reasons, public state, checks), :mod:`._turn` (the
orchestration, :func:`post_parts_via_turn`).
"""

from __future__ import annotations

from cw.tracker_io._argv import (
    TRACKER_COMMENT_TOOLS,
    TrackerIoConfigError,
    build_tracker_io_argv,
    comment_tools_for,
)
from cw.tracker_io._prompt import (
    READBACK_WINDOW,
    RESULT_SCHEMA,
    TurnPart,
    TurnResult,
    parse_turn_result,
    render_prompt,
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
    "RESULT_SCHEMA",
    "TRACKER_COMMENT_TOOLS",
    "TURN_TIMEOUT_SECONDS",
    "PostedComment",
    "PublicState",
    "TrackerIoConfigError",
    "TrackerIoContext",
    "TrackerIoOutcome",
    "TrackerIoReason",
    "TurnPart",
    "TurnResult",
    "VerifyOutcome",
    "build_tracker_io_argv",
    "comment_tools_for",
    "parse_turn_result",
    "plan_turn_parts",
    "post_parts_via_turn",
    "public_state_for",
    "render_prompt",
    "verify_turn_result",
]
