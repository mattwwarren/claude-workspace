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
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.models import HOOK_CONTEXT_RELATIVE_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_LOCK_SUFFIX = ".lock"
# Bounded, non-blocking lock acquisition. A plain blocking ``LOCK_EX`` would be
# wrong here in a way the dev_queue_lock precedent is not: both callers
# (PreToolUse, Stop) run synchronously inside the live worker's own turn, so
# blocking on contention hangs the worker itself rather than stalling a
# background dispatch tick. Exhausting the budget fails open (skip the write)
# instead.
_LOCK_TIMEOUT_SECS_DEFAULT = 0.5
_LOCK_RETRY_INTERVAL_SECS = 0.01


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
    context_path = Path(cwd) / ".claude" / "cw-context.json"
    if not context_path.is_file():
        return None
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return context if isinstance(context, dict) else None
