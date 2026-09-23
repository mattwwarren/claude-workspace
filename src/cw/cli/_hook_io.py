"""Shared stdin/file JSON helpers for Claude Code hook handlers (#940).

Both the Stop hook (``cw signal-stop``, ``cli/stop_hook.py``) and the
PreToolUse guard (``cw guard-cwd``, ``cli/guard.py``) read a JSON hook
payload from stdin, and both may then load ``.claude/cw-context.json`` from
a ``cwd`` the payload names. Extracted here so a fix to either read path
(a new stdin/JSON edge case, a cw-context.json shape change) can't drift
between the two — previously each hook reimplemented this independently.

``_context_lock``/``_write_cw_context_locked`` (#1947) extend this module to
the *write* side of the same file: originally ``cw.cli.agent_spawn_stamp``
owned the only writer (its Pre/Post ``agent_spawn_stamp`` pair, #1646).
#1947 found that pair hollow — ``PostToolUse:Agent`` fires at async-launch
return, not subagent completion — and replaced the Post half with a write
from ``cw signal-stop`` driven by the hook payload's own ``background_tasks``
list. Two independent hook commands now need the identical
lock-then-read-then-mutate-then-atomic-write discipline against the same
file, so it lives here rather than in either caller.

``_context_str`` (#2211) is here for the same reason one level down: the
readers return ``dict[str, object]``, so every guard that pulls a scalar
(``client``, ``lane``, ``session_id``) out of a context needs the identical
narrowing. ``cw guard-busy-wait`` and ``cw agent-spawn-pre`` had independently
grown byte-identical copies of it before this module took ownership.

``active_headless_context``/``enforce`` (#2303) followed once a second
PreToolUse guard (``cw background-tool-guard-pre``) needed the same
"headless workers only" scoping and exit-code contract ``cw agent-spawn-pre``
already had, and ``_extract_bash_command`` came with them from
``cw guard-busy-wait`` — taking a ``warn`` callback so each guard's fail-open
warning keeps its own attribution.

``resolve_guard_enabled``/``find_lane_config`` (#2303 round 2) are the kill
switch every default-on PreToolUse guard shares: a global default in
``orchestrator.yaml`` with a bidirectional per-lane override in
``clients.yaml``. ``cw agent-spawn-pre`` and ``cw background-tool-guard-pre``
had grown structurally identical resolvers against two field names, and
``cw guard-busy-wait`` a third copy of the same lane scan inside its
three-knob ``_resolve_settings``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from cw.atomic import atomic_write_text
from cw.config import load_clients, load_orchestrator_config
from cw.models import HOOK_CONTEXT_RELATIVE_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cw.models import LaneConfig

# The default-on guard toggles resolved by resolve_guard_enabled. Each names a
# field declared on BOTH OrchestratorConfig (the global default) and
# LaneConfig (the ``None``-means-inherit lane override); the Literal keeps a
# misspelt toggle a type error rather than an AttributeError inside a hook
# that fails open and so would never surface it.
GuardToggle = Literal["subagent_spawn_guard_enabled", "background_tool_guard_enabled"]

_LOCK_SUFFIX = ".lock"
# Bounded, non-blocking lock acquisition. A plain blocking ``LOCK_EX`` would be
# wrong here in a way the dev_queue_lock precedent is not: both callers
# (PreToolUse, Stop) run synchronously inside the live worker's own turn, so
# blocking on contention hangs the worker itself rather than stalling a
# background dispatch tick. Exhausting the budget fails open (skip the write)
# instead.
_LOCK_TIMEOUT_SECS_DEFAULT = 0.5
_LOCK_RETRY_INTERVAL_SECS = 0.01

# PreToolUse contract: exit 2 blocks the tool call and feeds stderr back to
# the agent. Same convention as cw guard-cwd and cw guard-busy-wait.
_PRETOOLUSE_BLOCK_EXIT = 2


@contextlib.contextmanager
def _context_lock(context_path: Path) -> Iterator[bool]:
    """Hold a per-worktree lock around *context_path*; yield whether acquired.

    Scoped to ``<worktree>/.claude/cw-context.json.lock`` rather than the
    process-wide ``dev_queue_lock`` — the contention this serialises is
    between hooks of the same worker, and nothing else should ever wait on it.

    Yields ``False`` (rather than raising) when the retry budget expires, so
    the caller's fail-open path is an ordinary branch, not exception handling.
    """
    lock_path = context_path.with_name(context_path.name + _LOCK_SUFFIX)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECS_DEFAULT
    with lock_path.open("w") as handle:
        acquired = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_RETRY_INTERVAL_SECS)
                continue
            acquired = True
            break
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write_cw_context_locked(
    cwd_value: str, mutate_fn: Callable[[dict[str, object]], dict[str, object]]
) -> bool:
    """Read-modify-write ``<cwd>/.claude/cw-context.json`` under its lock.

    *mutate_fn* receives the parsed context dict and returns the dict to
    write back (in place or a replacement — either is fine, only the return
    value is used). Returns ``True`` iff the write happened.

    Best-effort like every other helper in this module: a missing context
    file, lock-acquisition exhaustion, an unreadable/malformed context, or
    any unexpected error while building/writing the new payload all yield a
    silent ``False`` — never raises. A hook write path must never crash or
    block the tool call / turn boundary it's wrapping (#1646, #1947).
    """
    context_path = Path(cwd_value) / HOOK_CONTEXT_RELATIVE_PATH
    if not context_path.is_file():
        return False
    try:
        with _context_lock(context_path) as acquired:
            if not acquired:
                return False
            context = _read_cw_context(cwd_value)
            if context is None:
                return False
            updated = mutate_fn(context)
            atomic_write_text(context_path, json.dumps(updated, indent=2) + "\n")
    except Exception:  # noqa: BLE001 — hook writes must fail open, never crash.
        return False
    return True


def _read_hook_stdin_json() -> dict[str, object] | None:
    """Return the parsed JSON object from stdin, or None on any failure.

    Best-effort: unreadable stdin, an empty body, malformed JSON, or a
    non-object payload all yield None (silent no-op) — a hook must never
    crash or block the tool call it's wrapping.
    """
    try:
        stdin_text = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if not stdin_text:
        return None
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_cw_context(cwd: str) -> dict[str, object] | None:
    """Return the parsed ``<cwd>/.claude/cw-context.json``, or None on failure.

    Best-effort: a missing file, unreadable file, malformed JSON, or a
    non-object payload all yield None.
    """
    context_path = Path(cwd) / HOOK_CONTEXT_RELATIVE_PATH
    if not context_path.is_file():
        return None
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return context if isinstance(context, dict) else None


def _context_str(context: dict[str, object], key: str) -> str | None:
    """Return ``context[key]`` when it is a non-empty string, else None.

    The readers above hand back ``dict[str, object]`` because the file is
    only known to be a JSON object, so every guard that wants a scalar out of
    it — ``client``, ``lane``, ``session_id`` — needs the same
    isinstance-and-non-empty narrowing. Collapsing the empty string to None
    matters: an empty ``client`` must not resolve a lane override, and it is
    the shape a partially-written context actually produces.
    """
    value = context.get(key)
    return value if isinstance(value, str) and value else None


def find_cw_context(start: Path) -> dict[str, object] | None:
    """Search *start* and its parents for a ``.claude/cw-context.json`` (#2210).

    The hooks above are handed an exact worktree root by Claude Code, so
    :func:`_read_cw_context` looks in one place. An operator-run CLI command is
    not: it may be invoked from anywhere inside the tree, so it needs the
    upward walk ``.claude/scripts/check_not_main_checkout.py`` already does.
    This delegates the actual read to :func:`_read_cw_context` rather than
    parsing the file a second way — one parser, two search strategies — and
    joins the shared :data:`~cw.models.HOOK_CONTEXT_RELATIVE_PATH` rather than
    re-spelling the path, so this discovery and #2226's guard cannot drift
    onto two different files later.

    Returns the NEAREST context, or None when no ancestor has one. Same
    fail-open contract as every other cw context guard: a missing, unreadable
    or malformed file is indistinguishable from "not a dispatch worker", which
    is the safe direction for a guard that refuses work.
    """
    resolved = start.resolve()
    for candidate in [resolved, *resolved.parents]:
        if (candidate / HOOK_CONTEXT_RELATIVE_PATH).is_file():
            return _read_cw_context(str(candidate))
    return None


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


def enforce(reason: str | None) -> None:
    """Apply *reason* to the PreToolUse exit-code contract.

    Exits 2 when there is a reason — the spawn never runs, and the agent reads
    the reason back from stderr, which is how it learns what to retry with.
    Does nothing at all for None.
    """
    if reason is None:
        return
    click.echo(reason, err=True)
    sys.exit(_PRETOOLUSE_BLOCK_EXIT)


def _extract_bash_command(
    payload: dict[str, object], warn: Callable[[str], None]
) -> tuple[str | None, bool]:
    """Return ``(command, run_in_background)`` from a Bash PreToolUse payload.

    Defensive by design: every read is ``.get()``-based and type-checked, and
    a missing or wrong-type ``command`` is the routine "cannot classify this
    call, allow it" case, never a crash. Every anomalous shape is reported
    through *warn*, the calling guard's own attributed fail-open warning.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        warn(f"tool_input is {type(tool_input).__name__}, expected dict")
        return None, False
    run_in_background_raw = tool_input.get("run_in_background", False)
    if isinstance(run_in_background_raw, bool):
        run_in_background = run_in_background_raw
    else:
        warn(
            f"tool_input.run_in_background is "
            f"{type(run_in_background_raw).__name__}, expected bool"
        )
        run_in_background = False
    command = tool_input.get("command")
    if not isinstance(command, str):
        warn(f"tool_input.command is {type(command).__name__}, expected str")
        return None, run_in_background
    return command, run_in_background


def find_lane_config(client: str | None, lane: str | None) -> LaneConfig | None:
    """Return *client*'s declared *lane* from ``clients.yaml``, or None.

    None when either name is missing or empty, the client is not in
    ``clients.yaml``, or it declares no lane of that name — every one of which
    means "no lane override", so a caller falls through to the global default.
    Reloaded from disk on every call: each hook invocation is its own
    subprocess, so an operator's edit takes effect on the next tool call.
    """
    if not client or not lane:
        return None
    client_cfg = load_clients().get(client)
    if client_cfg is None:
        return None
    for lane_cfg in client_cfg.effective_lanes:
        if lane_cfg.name == lane:
            return lane_cfg
    return None


def resolve_guard_enabled(
    client: str | None, lane: str | None, toggle: GuardToggle
) -> bool:
    """Resolve a default-on guard's kill switch for *client*/*lane*.

    A non-None lane-level override wins in either direction, else the global
    default in ``orchestrator.yaml`` — the precedence
    :func:`cw.cli.guard_busy_wait._resolve_settings` established (#1946). A
    client absent from ``clients.yaml``, or a lane it does not declare, falls
    through to the global value. Re-read from disk on every invocation, so the
    kill switch needs no worker restart — the point of having one on a guard
    that can refuse work.
    """
    enabled: bool = getattr(load_orchestrator_config(), toggle)
    lane_cfg = find_lane_config(client, lane)
    if lane_cfg is None:
        return enabled
    override: bool | None = getattr(lane_cfg, toggle)
    return enabled if override is None else override
