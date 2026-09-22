"""Spawn-shape policy for the ``cw agent-spawn-pre`` hook (#2211).

#2017 established the rule this enforces: cw must own the launch of any agent
it is responsible for monitoring. A harness subagent never enters the session
roster, so cw cannot see it start, cannot observe what it does, and has no
channel to stop it. #2211 is the uncovered case — an impl worker forked a
subagent for a read-only lookup, the fork inherited the surrounding
implementation mandate, and it edited, committed and pushed to the live
feature branch before the parent's stop message won the race.

A fork is the worst shape available: it inherits the parent's full tool set
*and* its context, so neither capability nor prompt constrains it. This
module refuses that one shape, in headless dispatch workers only:

- an explicit ``fork`` (any case) or blank ``subagent_type`` → **deny**;
- ``subagent_type`` named but absent from the payload → **warn, allow**;
- anything else, or any shape it cannot classify → no verdict.

The omitted case is record-only rather than denied because the spawn-site
inventory needed to make denial safe is incomplete: ``review-sweep.md`` names
six roles (``Bug Hunter``, ``CLAUDE.md Auditor``, ...) that are not registered
agent types, so there is no correct ``subagent_type`` to retry with yet
(residual gap R7). Flipping that case to deny is gated on finishing that
inventory.

The deferred half of #2211 — refusing a *git mutation* from a non-writer
subagent, and the durable ``guard.subagent_action`` event that would record
one — is #2248. Nothing here records an event; the WARN line exists so the
record-only path is at least visible in the worker's own transcript.

Fail-open throughout, like every other cw hook: a missing or malformed
context, an unreadable config, or any unexpected shape yields no verdict.
The one deliberate exception is the explicit-fork deny, and even that ships
behind :attr:`~cw.models.OrchestratorConfig.subagent_spawn_guard_enabled` so
an operator can switch it off per-lane or globally without a code release.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

import click

from cw.cli._hook_io import find_cw_context
from cw.config import load_clients, load_orchestrator_config

# PreToolUse contract: exit 2 blocks the tool call and feeds stderr back to
# the agent. Same convention as cw guard-cwd and cw guard-busy-wait.
_SPAWN_BLOCK_EXIT = 2

# The refused values, compared case-insensitively after stripping. A blank
# string is grouped with "fork" rather than with an omitted key: a caller that
# sent the field and left it empty named a type and named nothing, which is
# the fork shape with extra steps.
_FORK_SUBAGENT_TYPES = frozenset({"", "fork"})

# The tools this hook is wired to (cw.spawn._AGENT_TOOL_MATCHER). Re-checked
# here so a future matcher widening cannot silently extend the policy to a
# tool whose payload shape it was never written against.
_AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

_SUBAGENT_TYPE_KEY = "subagent_type"

# Distinguishes "the key was not sent" from "the key was sent as null". Both
# end up allowed, but only via this sentinel can the blank-string case stay
# separable from them.
_KEY_ABSENT = object()

_DENY_REASON = (
    "BLOCKED (#2211): cw agent-spawn-pre refused a subagent spawn with "
    "subagent_type={value!r}. A forked subagent inherits this worker's tools "
    "AND its implementation mandate, and never enters cw's session roster -- "
    "cw cannot see it start, observe what it does, or stop it (#2017).\n"
    "Retry with an explicitly named subagent_type: "
    '"general-purpose" for real work, or "Read Only Helper" '
    "(tools: Read/Grep/Glob, no Bash) for an extraction or lookup that must "
    "not be able to write.\n"
    "False positive? Disable via subagent_spawn_guard_enabled: false "
    "(per-lane or global) in orchestrator.yaml -- see CONFIG_REFERENCE.md."
)

_OMITTED_REASON = (
    "cw agent-spawn-pre (#2211): subagent spawn with no subagent_type named. "
    "Allowed (record-only) pending a complete spawn-site inventory, but an "
    "unnamed spawn is unrostered -- cw cannot see or stop it (#2017). Name an "
    "explicit subagent_type."
)


class _SpawnVerdict(NamedTuple):
    """A decided spawn outcome: refused, or allowed-but-worth-recording."""

    denied: bool
    reason: str


def _warn_unexpected_shape(detail: str) -> None:
    """Emit the loud fail-open warning, mirroring ``cw guard-busy-wait``'s.

    This command is wired only to the ``^(Agent|Task)$`` matcher, so a
    ``tool_input`` that is not a dict — or a ``subagent_type`` that is not a
    string — is anomalous, never routine. A wrong inference about the payload
    shape must degrade to "policy does not fire, and says so on every call it
    does not fire for" rather than "policy silently never fires".
    """
    click.echo(
        f"WARN (cw agent-spawn-pre, #2211): unexpected Agent PreToolUse "
        f"payload shape -- {detail}. Failing open (this spawn was NOT "
        "classified).",
        err=True,
    )


def _context_str(context: dict[str, object], key: str) -> str | None:
    """Return ``context[key]`` when it is a non-empty string, else None."""
    value = context.get(key)
    return value if isinstance(value, str) and value else None


def active_headless_context(payload: dict[str, object]) -> dict[str, object] | None:
    """Return the cw context iff this spawn is inside a headless worker.

    Scoped deliberately: an operator's interactive session may fork a
    subagent for whatever they like, and a caller outside any cw worktree
    (``orchestrate-phase.md``, a detached gate worktree) is structurally
    exempt because no ancestor carries a context file.

    Uses the upward-walking :func:`~cw.cli._hook_io.find_cw_context` rather
    than an exact-path read so a worker whose cwd has moved into a
    subdirectory is still covered — the same reason #2210 introduced it.
    """
    cwd_value = payload.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        return None
    context = find_cw_context(Path(cwd_value))
    if context is None:
        return None
    return context if context.get("headless") is True else None


def _resolve_spawn_guard_enabled(client: str | None, lane: str | None) -> bool:
    """Resolve the guard's kill switch for *client*/*lane*.

    Precedence mirrors :func:`cw.cli.guard_busy_wait._resolve_settings`
    exactly: a non-None lane-level override wins in either direction, else
    the global default. A client absent from ``clients.yaml``, or a lane name
    it does not declare, falls through to the global value.

    Reloaded from disk on every invocation (each hook call is its own
    subprocess), so an operator's edit takes effect on the next spawn with no
    worker restart — which is the whole point of having a kill switch on a
    guard that can refuse work.
    """
    enabled = load_orchestrator_config().subagent_spawn_guard_enabled

    if client and lane:
        client_cfg = load_clients().get(client)
        if client_cfg is not None:
            for lane_cfg in client_cfg.effective_lanes:
                if lane_cfg.name != lane:
                    continue
                if lane_cfg.subagent_spawn_guard_enabled is not None:
                    enabled = lane_cfg.subagent_spawn_guard_enabled
                break

    return enabled


def _classify_subagent_type(raw: object) -> _SpawnVerdict | None:
    """Turn the payload's ``subagent_type`` value into a verdict, or None."""
    if raw is _KEY_ABSENT or raw is None:
        return _SpawnVerdict(denied=False, reason=_OMITTED_REASON)
    if not isinstance(raw, str):
        _warn_unexpected_shape(
            f"tool_input.subagent_type is {type(raw).__name__}, expected str"
        )
        return None
    if raw.strip().lower() in _FORK_SUBAGENT_TYPES:
        return _SpawnVerdict(denied=True, reason=_DENY_REASON.format(value=raw))
    return None


def classify_spawn(payload: dict[str, object] | None) -> _SpawnVerdict | None:
    """Return this spawn's verdict, or None to allow it silently."""
    if payload is None:
        return None
    context = active_headless_context(payload)
    if context is None:
        return None
    if not _resolve_spawn_guard_enabled(
        _context_str(context, "client"), _context_str(context, "lane")
    ):
        return None
    if payload.get("tool_name") not in _AGENT_TOOL_NAMES:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _warn_unexpected_shape(
            f"tool_input is {type(tool_input).__name__}, expected dict"
        )
        return None
    return _classify_subagent_type(tool_input.get(_SUBAGENT_TYPE_KEY, _KEY_ABSENT))


def enforce(verdict: _SpawnVerdict | None) -> None:
    """Apply *verdict* to the PreToolUse exit-code contract.

    Exits 2 on a denial (the spawn never runs, and the agent reads the reason
    back from stderr); prints and returns on a record-only warning; does
    nothing at all for no verdict.
    """
    if verdict is None:
        return
    if verdict.denied:
        click.echo(verdict.reason, err=True)
        sys.exit(_SPAWN_BLOCK_EXIT)
    click.echo(f"WARN: {verdict.reason}", err=True)
