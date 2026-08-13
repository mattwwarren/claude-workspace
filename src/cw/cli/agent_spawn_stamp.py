"""The ``cw agent-spawn-pre`` / ``cw agent-spawn-post`` hook handlers (#1646).

Claude Code invokes these around every subagent-spawning tool call in a
dispatched worker (wired via ``settings.local.json`` in
:data:`cw.spawn._HOOK_SETTINGS_TEMPLATE`, matched on
:data:`cw.spawn._AGENT_TOOL_MATCHER`). ``agent-spawn-pre`` increments
``agent_spawn_stamp.unresolved_count`` in ``<cwd>/.claude/cw-context.json``
before the spawn starts; ``agent-spawn-post`` decrements it when the spawn
returns.

The point is what happens when the pair does *not* balance. A worker that dies
— or pauses forever — with a sub-agent spawn still in flight leaves the count
above zero, on disk, in its own worktree. That is durable evidence no
transcript scrape can fabricate, and ``cw.reconcile`` reads it to distinguish
"this surface vanished" (generic ``phantom_surface``) from "this surface died
mid-spawn, so committed work may sit behind a verification tail that never
ran". Both the explicit-pause case and the silent-dropout case produce the
identical on-disk signature, because this layer can observe neither prose nor
tool results — only whether its own Post fired.

Fail-open throughout, mirroring ``cw guard-cwd``: unreadable stdin, a missing
or malformed context, a contended lock, or any unexpected error all yield a
silent exit 0. Unlike the guard these never block a tool call under any
circumstances — refusing a subagent spawn is never the right answer, and a
missed stamp costs only disposition precision on a crash that may not happen.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.cli._base import main
from cw.cli._hook_io import _read_cw_context, _read_hook_stdin_json
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOCK_SUFFIX = ".lock"
# Bounded, non-blocking lock acquisition. A plain blocking ``LOCK_EX`` would be
# wrong here in a way the dev_queue_lock precedent is not: PreToolUse runs
# synchronously inside the live worker's own turn, so blocking on contention
# hangs the worker itself rather than stalling a background dispatch tick.
# Exhausting the budget fails open (skip the stamp) instead.
_LOCK_TIMEOUT_SECS_DEFAULT = 0.5
_LOCK_RETRY_INTERVAL_SECS = 0.01


@contextlib.contextmanager
def _context_lock(context_path: Path) -> Iterator[bool]:
    """Hold a per-worktree lock around *context_path*; yield whether acquired.

    Scoped to ``<worktree>/.claude/cw-context.json.lock`` rather than the
    process-wide ``dev_queue_lock`` — the contention this serialises is between
    two hooks of the same worker, and nothing else should ever wait on it.

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


def _unresolved_count(context: dict[str, object]) -> int:
    """Return the stamp's current count, treating every odd shape as 0.

    A pre-v5 context has no stamp key at all, and a hand-edited one can hold
    anything. ``bool`` is excluded explicitly because it is an ``int`` subclass
    in Python, so ``True`` would otherwise read as a live count of 1.
    """
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return 0
    count = stamp.get(AGENT_SPAWN_UNRESOLVED_COUNT_KEY)
    if isinstance(count, bool) or not isinstance(count, int):
        return 0
    return count


def _last_stamped_at(context: dict[str, object]) -> str | None:
    """Return the stamp's existing ``last_stamped_at``, or None."""
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return None
    value = stamp.get(AGENT_SPAWN_LAST_STAMPED_AT_KEY)
    return value if isinstance(value, str) else None


def _hook_cwd() -> str | None:
    """Return the hook payload's ``cwd`` string, or None on any read failure."""
    payload = _read_hook_stdin_json()
    cwd_value = payload.get("cwd") if payload is not None else None
    return cwd_value if isinstance(cwd_value, str) and cwd_value else None


def _adjust_unresolved_count(delta: int) -> None:
    """Apply *delta* to the unresolved-spawn counter for the hook's worktree.

    Read-modify-write under the per-worktree lock, floored at zero: a Post with
    no matching prior Pre (a reused worktree, a hook wired mid-flight) must not
    drive the counter negative, which would swallow the *next* real crash.

    ``last_stamped_at`` advances only on increment — it answers "when did the
    oldest unresolved spawn begin", which is what an operator reading a parked
    row wants; refreshing it on decrement would erase that.
    """
    cwd_value = _hook_cwd()
    if cwd_value is None:
        return
    context_path = Path(cwd_value) / HOOK_CONTEXT_RELATIVE_PATH
    if not context_path.is_file():
        return
    with _context_lock(context_path) as acquired:
        if not acquired:
            return
        context = _read_cw_context(cwd_value)
        if context is None:
            return
        new_count = max(0, _unresolved_count(context) + delta)
        context[AGENT_SPAWN_STAMP_KEY] = {
            AGENT_SPAWN_UNRESOLVED_COUNT_KEY: new_count,
            AGENT_SPAWN_LAST_STAMPED_AT_KEY: (
                datetime.now(UTC).isoformat()
                if delta > 0
                else _last_stamped_at(context)
            ),
        }
        atomic_write_text(context_path, json.dumps(context, indent=2) + "\n")


@main.command(name="agent-spawn-pre")
def agent_spawn_pre() -> None:
    """Mark a subagent spawn as unresolved before the tool call runs.

    Reads the PreToolUse hook JSON from stdin and increments the counter in
    ``<cwd>/.claude/cw-context.json``. Always exits 0 — see module docstring.
    """
    try:
        _adjust_unresolved_count(1)
    except Exception:  # noqa: BLE001 — a hook must never crash; fail open.
        return


@main.command(name="agent-spawn-post")
def agent_spawn_post() -> None:
    """Clear one unresolved subagent spawn after the tool call returns.

    Reads the PostToolUse hook JSON from stdin and decrements the counter
    (floored at 0). Always exits 0 — see module docstring.
    """
    try:
        _adjust_unresolved_count(-1)
    except Exception:  # noqa: BLE001 — a hook must never crash; fail open.
        return
