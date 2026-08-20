"""The ``cw guard-busy-wait`` PreToolUse hook handler (#1946).

The mechanical half of #1944's prose rule. #1944 rewrote the async dispatch
rules in ``auto-dev-{plan,impl,review}.md`` after finding a headless review
pass that spent 173 of its 234 ``Bash`` calls on ``true`` — a worker holding
its turn open waiting for an async subagent that no longer had a blocking
primitive to wait on. That pattern is worse than wasteful: ADR-0014 removed
every kill timer, so the only automated stuck-worker signal left is the
transcript-staleness liveness sweep, and a no-op poll loop keeps the
transcript fresh enough that the spinning worker classifies as LIVE and
``session.needs_attention`` never fires. The prose rule shipped in #1945;
this is the enforcement surface it cited.

Claude Code invokes this before every Bash tool call in a dispatched worker,
wired as the second command on the existing ``"Bash"`` PreToolUse matcher in
:data:`cw.spawn._HOOK_SETTINGS_TEMPLATE` (alongside ``cw guard-cwd``). It
reads the hook JSON from stdin and exits ``2`` (block) on three shapes:

1. a bare ``true`` / ``:`` no-op,
2. a bare ``sleep N`` with no follow-on work,
3. the same command repeated ``repeat_threshold`` times inside a rolling
   ``window_seconds`` window.

Everything else is a best-effort no-op (exit ``0``): a backgrounded call, an
unreadable stdin, a missing/malformed context, a contended lock, a disabled
guard, or any unexpected error. Mirrors ``cw guard-cwd``'s contract exactly —
a guard that crashed or blocked spuriously would wedge every Bash call in
every worker, which is strictly worse than not guarding.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import click

from cw.cli._base import main
from cw.cli._hook_io import (
    _read_cw_context,
    _read_hook_stdin_json,
    _write_cw_context_locked,
)
from cw.config import load_clients, load_orchestrator_config
from cw.events import record_event
from cw.models import OrchestratorEventType

# PreToolUse contract: exit 2 blocks the tool call and feeds stderr back to
# the agent; any other code (0 here) allows it. Same convention as
# cw guard-cwd's _GUARD_CWD_BLOCK_EXIT.
_GUARD_BUSY_WAIT_BLOCK_EXIT = 2

# cw-context.json state keys. Module-local rather than promoted into
# cw.models alongside AGENT_SPAWN_STAMP_KEY: nothing outside this module
# reads the block. The agent-spawn stamp earned its promotion because
# cw.reconcile._shared reads it from the other side; this guard has no such
# consumer, and inventing one would be speculative coupling.
_BUSY_WAIT_STATE_KEY = "busy_wait_guard"
_BUSY_WAIT_RECENT_COMMANDS_KEY = "recent_commands"
_BUSY_WAIT_COMMAND_HASH_KEY = "command_hash"
_BUSY_WAIT_TS_KEY = "ts"

# Hex chars of the sha256 digest kept (~64 bits). Collision probability is a
# non-concern at this scale — a handful of distinct commands per worker,
# pruned every window_seconds — and the short form keeps cw-context.json
# readable when an operator opens it to diagnose a false positive.
_BUSY_WAIT_HASH_PREFIX_LEN = 16

# Hard cap on retained entries, independent of the time window: a worker
# issuing hundreds of distinct commands inside one window must not grow the
# context file without bound.
_BUSY_WAIT_HISTORY_MAXLEN = 20

# Anchored at both ends. `true` and `:` are the shell's canonical no-ops;
# anything appended to them (`true && ./build.sh`) is real work.
_BARE_NOOP_RE = re.compile(r"^(true|:)\s*$")
# End-anchored so `sleep 5 && ./run_tests.sh` falls through — a sleep that
# precedes real work is a delay, not a busy-wait.
_BARE_SLEEP_RE = re.compile(r"^sleep\s+[0-9]+(\.[0-9]+)?[smhd]?\s*$")

_REASON_BARE_NOOP = "bare_noop"
_REASON_BARE_SLEEP = "bare_sleep"
_REASON_REPEAT_THRESHOLD = "repeat_threshold"

_REASON_LABELS = {
    _REASON_BARE_NOOP: "bare `true`/`:` no-op",
    _REASON_BARE_SLEEP: "bare `sleep` with no follow-on work",
    _REASON_REPEAT_THRESHOLD: "identical command repeated inside the window",
}


class _GuardSettings(NamedTuple):
    """The three knobs resolved for one hook invocation."""

    enabled: bool
    repeat_threshold: int
    window_seconds: int


class _BlockDecision(NamedTuple):
    """A decided block, carrying everything both records need."""

    reason: str
    command_hash: str
    client: str | None
    lane: str | None
    session_id: str | None
    repeat_threshold: int | None = None
    window_seconds: int | None = None


def _warn_unexpected_shape(detail: str) -> None:
    """Emit the single loud fail-open warning line #1946 R1 requires.

    This command is wired ONLY to the ``"Bash"`` PreToolUse matcher, so
    ``tool_input.command`` being absent or the wrong type is always
    anomalous, never routine — unlike an unreadable stdin, which is a
    pre-existing, silent, genuinely routine fail-open case shared with every
    other hook. No captured Bash-tool payload exists in this repo (only the
    Agent-tool shape, in ``tests/test_cli_agent_spawn_stamp.py``), so these
    field names are inferred. A wrong inference must degrade to "guard does
    not fire, and says so on every call it does not fire for" rather than
    "guard silently never fires and no fixture can catch it" (#1646's own
    warning about exactly this failure mode).
    """
    click.echo(
        f"WARN (cw guard-busy-wait, #1946): unexpected Bash PreToolUse "
        f"payload shape -- {detail}. Failing open (this call was NOT "
        "classified); the tool_input field-name inference in "
        "guard_busy_wait.py may need updating against a real payload.",
        err=True,
    )


def _extract_bash_command(payload: dict[str, object]) -> tuple[str | None, bool]:
    """Return ``(command, run_in_background)`` from a Bash PreToolUse payload.

    Defensive by design (see :func:`_warn_unexpected_shape`): every read is
    ``.get()``-based and type-checked, and a missing or wrong-type
    ``command`` is the routine "cannot classify this call, allow it" case,
    never a crash.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _warn_unexpected_shape(
            f"tool_input is {type(tool_input).__name__}, expected dict"
        )
        return None, False
    run_in_background_raw = tool_input.get("run_in_background", False)
    if isinstance(run_in_background_raw, bool):
        run_in_background = run_in_background_raw
    else:
        _warn_unexpected_shape(
            f"tool_input.run_in_background is "
            f"{type(run_in_background_raw).__name__}, expected bool"
        )
        run_in_background = False
    command = tool_input.get("command")
    if not isinstance(command, str):
        _warn_unexpected_shape(
            f"tool_input.command is {type(command).__name__}, expected str"
        )
        return None, run_in_background
    return command, run_in_background


def _hash_command(command: str) -> str:
    """Return a stable, truncated hash of the whitespace-normalized command.

    #1946 R7: shell commands routinely embed secrets inline (Authorization
    headers, exported API keys), and ``cw-context.json``'s only prior write
    precedent is a plain integer counter — this must not be the first time
    it holds attacker-useful content. Repeat detection needs equality, not
    the text itself.

    Normalization collapses runs of whitespace so ``git  status`` and
    ``git status`` count as the same command: an agent re-issuing a poll
    with incidental spacing changes is still busy-waiting.
    """
    normalized = " ".join(command.split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:_BUSY_WAIT_HASH_PREFIX_LEN]


def _resolve_settings(client: str | None, lane: str | None) -> _GuardSettings:
    """Resolve the guard's three knobs for *client*/*lane*.

    Precedence, mirroring :func:`cw.reconcile._shared.resolve_reap_policy`:
    a non-None lane-level override wins, else the global default. A client
    absent from ``clients.yaml``, or a lane name not declared for it, falls
    through to the global config — which is also the pre-#1946 behaviour for
    any context written before the ``lane`` key existed.

    Reloaded from disk on every invocation (each hook call is its own
    subprocess), so an operator's edit takes effect on the next Bash call
    with no worker restart.
    """
    global_cfg = load_orchestrator_config()
    enabled = global_cfg.busy_wait_guard_enabled
    repeat_threshold = global_cfg.busy_wait_guard_repeat_threshold
    window_seconds = global_cfg.busy_wait_guard_window_seconds

    if client and lane:
        client_cfg = load_clients().get(client)
        if client_cfg is not None:
            for lane_cfg in client_cfg.effective_lanes:
                if lane_cfg.name != lane:
                    continue
                if lane_cfg.busy_wait_guard_enabled is not None:
                    enabled = lane_cfg.busy_wait_guard_enabled
                if lane_cfg.busy_wait_guard_repeat_threshold is not None:
                    repeat_threshold = lane_cfg.busy_wait_guard_repeat_threshold
                if lane_cfg.busy_wait_guard_window_seconds is not None:
                    window_seconds = lane_cfg.busy_wait_guard_window_seconds
                break

    return _GuardSettings(enabled, repeat_threshold, window_seconds)


def _str_or_none(context: dict[str, object], key: str) -> str | None:
    """Return ``context[key]`` when it is a non-empty string, else None."""
    value = context.get(key)
    return value if isinstance(value, str) and value else None


def _entry_within(entry: object, cutoff: datetime) -> bool:
    """Return True iff *entry* is a well-formed record newer than *cutoff*."""
    if not isinstance(entry, dict):
        return False
    raw_ts = entry.get(_BUSY_WAIT_TS_KEY)
    if not isinstance(raw_ts, str):
        return False
    try:
        stamped = datetime.fromisoformat(raw_ts)
    except ValueError:
        return False
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return stamped >= cutoff


def _repeat_threshold_tripped(
    cwd_value: str, command_hash: str, settings: _GuardSettings
) -> bool:
    """Append this call to the rolling window; return whether it trips.

    The read-prune-count-append runs inside
    :func:`cw.cli._hook_io._write_cw_context_locked`, the shared locked
    read-modify-write primitive ``cw agent-spawn-pre`` and ``cw signal-stop``
    already use against the same file — this guard is its third consumer,
    exactly the case its docstring anticipates.

    That primitive's return value answers "did a write happen", not "should
    this call block", so the block decision rides out on a closure-captured
    cell instead. A lock exhaustion, a missing context file, or any parse
    error leaves the cell False — the fail-open outcome — because the
    primitive never calls the mutation at all in those cases.

    On a trip the offending call is deliberately NOT appended: the block
    already ends the call, and recording it would push the window one entry
    further with every retry, so a worker that ignored one block would need
    a fresh N repeats to trip again.
    """
    tripped = [False]
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.window_seconds)

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        block = context.get(_BUSY_WAIT_STATE_KEY)
        raw_entries = (
            block.get(_BUSY_WAIT_RECENT_COMMANDS_KEY, [])
            if isinstance(block, dict)
            else []
        )
        entries = raw_entries if isinstance(raw_entries, list) else []
        surviving = [entry for entry in entries if _entry_within(entry, cutoff)]
        matching = sum(
            1
            for entry in surviving
            if entry.get(_BUSY_WAIT_COMMAND_HASH_KEY) == command_hash
        )
        if matching >= settings.repeat_threshold - 1:
            tripped[0] = True
        else:
            surviving.append(
                {
                    _BUSY_WAIT_COMMAND_HASH_KEY: command_hash,
                    _BUSY_WAIT_TS_KEY: now.isoformat(),
                }
            )
        context[_BUSY_WAIT_STATE_KEY] = {
            _BUSY_WAIT_RECENT_COMMANDS_KEY: surviving[-_BUSY_WAIT_HISTORY_MAXLEN:]
        }
        return context

    _write_cw_context_locked(cwd_value, _mutate)
    return tripped[0]


def _classify_command(
    cwd_value: str,
    command: str,
    context: dict[str, object],
    settings: _GuardSettings,
) -> _BlockDecision | None:
    """Apply the three rejection rules in order; first match wins.

    Order matters: the two stateless shapes are checked before the
    repeat-window rule so a bare no-op trips on its very first call (the
    incident shape) rather than after N repeats, and so a blocked call never
    consumes a slot in the rolling window.
    """
    stripped = command.strip()
    command_hash = _hash_command(command)

    def _decision(
        reason: str,
        *,
        repeat_threshold: int | None = None,
        window_seconds: int | None = None,
    ) -> _BlockDecision:
        return _BlockDecision(
            reason=reason,
            command_hash=command_hash,
            client=_str_or_none(context, "client"),
            lane=_str_or_none(context, "lane"),
            session_id=_str_or_none(context, "session_id"),
            repeat_threshold=repeat_threshold,
            window_seconds=window_seconds,
        )

    if _BARE_NOOP_RE.match(stripped):
        return _decision(_REASON_BARE_NOOP)
    if _BARE_SLEEP_RE.match(stripped):
        return _decision(_REASON_BARE_SLEEP)
    if _repeat_threshold_tripped(cwd_value, command_hash, settings):
        return _decision(
            _REASON_REPEAT_THRESHOLD,
            repeat_threshold=settings.repeat_threshold,
            window_seconds=settings.window_seconds,
        )
    return None


def _classify() -> _BlockDecision | None:
    """Return the block decision for this hook invocation, or None to allow."""
    payload = _read_hook_stdin_json()
    if payload is None:
        return None
    cwd_value = payload.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        return None

    context = _read_cw_context(cwd_value) or {}
    settings = _resolve_settings(
        _str_or_none(context, "client"), _str_or_none(context, "lane")
    )
    # Gate before touching state: a disabled guard must leave no trace in
    # cw-context.json at all, not merely decline to block.
    if not settings.enabled:
        return None

    command, run_in_background = _extract_bash_command(payload)
    # An unclassifiable payload (already warned about) allows. So does a
    # backgrounded call: it returns immediately and cannot hold the turn
    # open, which is the shape this guard exists to stop — even when the
    # command itself is a no-op.
    if command is None or run_in_background:
        return None

    return _classify_command(cwd_value, command, context, settings)


def _threshold_text(decision: _BlockDecision) -> str:
    """Render the tripped threshold, or ``n/a`` for the stateless reasons."""
    if decision.repeat_threshold is None or decision.window_seconds is None:
        return "n/a"
    return f"{decision.repeat_threshold}/{decision.window_seconds}s"


def _block_message(decision: _BlockDecision) -> str:
    """Build the stderr record the blocked agent reads back (#1946 R8)."""
    label = _REASON_LABELS.get(decision.reason, decision.reason)
    return (
        f"BLOCKED (#1946): cw guard-busy-wait rejected — {label} "
        f"(command_hash={decision.command_hash}, "
        f"threshold={_threshold_text(decision)}).\n"
        "Never busy-wait: end the turn; the completion notification resumes "
        "you (see the async dispatch note in auto-dev-{plan,impl,review}.md, "
        "#1944).\n"
        "False positive? Disable via busy_wait_guard_enabled: false (per-lane "
        "or global) in orchestrator.yaml — see CONFIG_REFERENCE.md."
    )


def _record_block(decision: _BlockDecision) -> None:
    """Append the durable bus record for a block; never raise.

    Deliberately isolated in its own try/except rather than folded into
    :func:`guard_busy_wait`'s outer fail-open wrapper. Once classification
    has decided to block, a failure to *record* that block (event-bus I/O
    error, contention on the inbox) must never suppress the block itself —
    collapsing the two would silently convert "the event bus is briefly
    unwritable" into "guard-busy-wait allows a busy-wait through", the exact
    inversion of why this record exists.
    """
    try:
        record_event(
            OrchestratorEventType.GUARD_BUSY_WAIT_BLOCKED,
            {
                "client": decision.client,
                "lane": decision.lane,
                "reason": decision.reason,
                "command_hash": decision.command_hash,
                "repeat_threshold": decision.repeat_threshold,
                "window_seconds": decision.window_seconds,
            },
            correlation_id=decision.session_id,
        )
    except Exception:  # noqa: BLE001 — a failed record must not undo a block.
        return


@main.command(name="guard-busy-wait")
def guard_busy_wait() -> None:
    """Block a Bash tool call that is busy-waiting rather than doing work.

    Reads the PreToolUse hook JSON from stdin and consults
    ``<cwd>/.claude/cw-context.json`` for the client/lane whose config gates
    the guard. Exits 2 (block) on a confirmed busy-wait shape; exits 0
    (no-op) on everything else, including any unexpected error — the hook
    must never crash. See module docstring.
    """
    try:
        decision = _classify()
    except Exception:  # noqa: BLE001 — a hook must never crash; fail open.
        return
    if decision is None:
        return
    _record_block(decision)
    click.echo(_block_message(decision), err=True)
    sys.exit(_GUARD_BUSY_WAIT_BLOCK_EXIT)
