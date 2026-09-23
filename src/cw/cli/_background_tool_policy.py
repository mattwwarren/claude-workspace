"""Background-tool policy for the ``cw background-tool-guard-pre`` hook (#2303).

A headless DAEMON worker has exactly one completion-notification path back
into its own turn: the Stop hook's ``background_tasks`` list, which tracks the
Agent tool's subagent spawns (ADR-0003). A raw Bash call made with
``run_in_background: true``, or a task handed to the Monitor tool, has no such
path. A turn that ends waiting on either never resumes — the worker wedges.

#1886 is the open, broader class. #2251 fixed the one instance it could see —
``/prep-pr`` backgrounding a quality gate — in prose. #2303 is the signature
that survived it: three Stage-2 impl subagents (#2250, #2280, #2275) wedged
the same way on pipeline-dependent calls the prose never reached. This module
is the mechanical half, in the same "why this exists" spirit that
``cw.cli._subagent_policy`` carries for #2017/#2211. In headless dispatch
workers only it refuses:

- a Bash call with ``run_in_background: true`` → **deny**;
- any Monitor tool call → **deny**;
- anything else, or any shape it cannot classify → no verdict.

A shell-level detached launch followed by bounded foreground polls (#2291)
stays allowed — it never ends a turn waiting on a notification — and the
refusal text names it, so a worker with genuinely long work has a correct
retry.

Fail-open throughout, like every other cw hook, and gated behind
:attr:`~cw.models.OrchestratorConfig.background_tool_guard_enabled` so an
operator can switch it off per lane or globally without a code release.
Nothing here records an event: the pure decision lives here, and
:mod:`cw.cli.background_tool_guard_pre` owns the side effects.
"""

from __future__ import annotations

from typing import NamedTuple

import click

from cw.cli._hook_io import (
    _context_str,
    _extract_bash_command,
    active_headless_context,
    enforce,
    resolve_guard_enabled,
)
from cw.models import BASH_TOOL_NAME, MONITOR_TOOL_NAME

__all__ = [
    "_RefusalDecision",
    "classify_background_tool",
    "enforce",
]

# Shared by both refusal reasons below: what to do about it. One constant so
# the two messages cannot drift apart.
_RETRY_GUIDANCE = (
    "Retry in the foreground: wrap the command in a shell-level "
    "wall-clock guard, `timeout <N> <cmd>`, and set the Bash tool "
    "call's own `timeout` parameter to at least `N * 1000` ms (up to "
    '600000ms) -- "Run the gate command via the `Bash` tool with '
    "`timeout` set to `foreground_ceiling_s * 1000` (ms; the Bash "
    'tool accepts up to 600000ms)" (auto-dev.md Worker Execution '
    "Discipline rule 1). For work that genuinely exceeds 600s, use a "
    "shell-level detached launch followed by bounded foreground polls "
    "-- the #2291 pattern (launch via `setsid nohup <cmd> > <log> "
    "2>&1; echo $? > <rc>`, poll via `timeout <N> bash -c 'until "
    "[ -f <rc> ]; do sleep 5; done'`) -- which never ends a turn "
    "waiting on an untracked notification. Never `run_in_background: "
    "true` or the Monitor tool: neither has a completion-notification "
    "path for a headless DAEMON session.\n"
    "False positive? Disable via background_tool_guard_enabled: false "
    "(per-lane or global) in orchestrator.yaml -- see "
    "CONFIG_REFERENCE.md."
)

_BASH_DENY_REASON = (
    "BLOCKED (#2303): cw background-tool-guard-pre refused a "
    "backgrounded Bash call (run_in_background: true) in a headless "
    "worker. A backgrounded raw Bash call has no completion-"
    "notification path for a headless DAEMON session -- unlike the "
    "Agent tool's subagent spawn, which the Stop hook's "
    "background_tasks list actually tracks (ADR-0003) -- so a turn "
    "that ends waiting on one never resumes (#2250, #2280, #2275).\n"
    f"{_RETRY_GUIDANCE}"
)

_MONITOR_DENY_REASON = (
    "BLOCKED (#2303): cw background-tool-guard-pre refused the "
    "Monitor tool in a headless worker. Monitor exists to watch a "
    "background task for an interactive operator; a headless DAEMON "
    "session has no completion-notification path back into its own "
    "turn, so ending a turn to await a Monitor'd task wedges the "
    "worker the same way a backgrounded Bash call does (#2250, #2280, "
    "#2275).\n"
    f"{_RETRY_GUIDANCE}"
)


class _RefusalDecision(NamedTuple):
    """A decided refusal, carrying everything the stderr reason and event need."""

    reason: str
    tool_name: str
    client: str | None
    lane: str | None
    session_id: str | None
    ticket_id: str | None
    agent_id: str | None


def _warn_unexpected_shape(detail: str) -> None:
    """Emit the loud fail-open warning, attributed to this guard.

    Its own copy rather than ``cw guard-busy-wait``'s: the two guards share
    the Bash-payload extractor, and a shared warning would misattribute one
    guard's fail-open to the other.
    """
    click.echo(
        f"WARN (cw background-tool-guard-pre, #2303): unexpected PreToolUse "
        f"payload shape -- {detail}. Failing open (this call was NOT "
        "classified).",
        err=True,
    )


def _refusal_reason(payload: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(reason, tool_name)`` for a refused call, or None to allow.

    Branches on ``tool_name`` because one command backs both the
    :data:`~cw.models.BASH_TOOL_NAME` and :data:`~cw.models.MONITOR_TOOL_NAME`
    matchers — the same constants ``cw.spawn`` writes as those matchers.

    A Bash call is refused only when the extractor parsed it cleanly and
    ``run_in_background`` is exactly ``True``. Everything else fails open, as
    every sibling guard does: a missing or non-bool flag is already coerced
    to False (with a warning for the non-bool case), and an unparseable
    ``command`` comes back None after the extractor has told the worker this
    call was "NOT classified" — refusing it anyway would contradict that
    warning and block a call the guard never understood.
    """
    tool_name = payload.get("tool_name")
    if tool_name == MONITOR_TOOL_NAME:
        return _MONITOR_DENY_REASON, MONITOR_TOOL_NAME
    if tool_name != BASH_TOOL_NAME:
        return None
    command, run_in_background = _extract_bash_command(payload, _warn_unexpected_shape)
    if command is None or run_in_background is not True:
        return None
    return _BASH_DENY_REASON, BASH_TOOL_NAME


def classify_background_tool(
    payload: dict[str, object] | None,
) -> _RefusalDecision | None:
    """Return this call's refusal decision, or None to allow it silently.

    ``agent_id`` is read off the raw PreToolUse payload, not the context: it
    identifies the calling agent for this one tool call, which the per-worktree
    ``cw-context.json`` never carries.
    """
    if payload is None:
        return None
    context = active_headless_context(payload)
    if context is None:
        return None
    if not resolve_guard_enabled(
        _context_str(context, "client"),
        _context_str(context, "lane"),
        "background_tool_guard_enabled",
    ):
        return None
    refusal = _refusal_reason(payload)
    if refusal is None:
        return None
    reason, tool_name = refusal
    raw_agent_id = payload.get("agent_id")
    return _RefusalDecision(
        reason=reason,
        tool_name=tool_name,
        client=_context_str(context, "client"),
        lane=_context_str(context, "lane"),
        session_id=_context_str(context, "session_id"),
        ticket_id=_context_str(context, "ticket_id"),
        agent_id=raw_agent_id
        if isinstance(raw_agent_id, str) and raw_agent_id
        else None,
    )
