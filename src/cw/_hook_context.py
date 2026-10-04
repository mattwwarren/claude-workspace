"""Locked read/write primitives for ``<worktree>/.claude/cw-context.json``.

Moved out of ``cw.cli._hook_io`` (#2458) so a module below ``cw.cli`` --
``cw.result``, whose ``cw result emit`` both reads the context and stamps
``staged_emit_result`` into it -- can share the one lock discipline without
importing upward into ``cw.cli`` (``cw.cli`` imports ``cw.result``, not the
other way round). Depends only on ``cw.atomic`` and ``cw.models``.

``_context_lock``/``_write_cw_context_locked`` (#1947) are the
lock-then-read-then-mutate-then-atomic-write discipline every writer of this
file shares: ``cw agent-spawn-pre``'s stamp, ``cw signal-stop``'s
``background_tasks`` snapshot, ``cw guard-busy-wait``, ``cw signal-park`` and
``cw result emit`` all mutate the same file from independent processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path
from typing import TYPE_CHECKING

from cw._flock import try_flock_until
from cw.atomic import atomic_write_text
from cw.models import HOOK_CONTEXT_RELATIVE_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_LOCK_SUFFIX = ".lock"
# Bounded, non-blocking lock acquisition. A plain blocking ``LOCK_EX`` would be
# wrong here in a way the dev_queue_lock precedent is not: the hook callers
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
    Only genuine contention exhausts the budget (:func:`cw._flock.try_flock_until`);
    any other ``OSError`` from ``flock`` propagates to the caller, which
    :func:`_write_cw_context_locked` already treats as fail-open.
    """
    lock_path = context_path.with_name(context_path.name + _LOCK_SUFFIX)
    with lock_path.open("w") as handle:
        acquired = try_flock_until(
            handle,
            timeout_s=_LOCK_TIMEOUT_SECS_DEFAULT,
            poll_interval_s=_LOCK_RETRY_INTERVAL_SECS,
        )
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

    Best-effort: a missing context file, lock-acquisition exhaustion, an
    unreadable/malformed context, or any unexpected error while
    building/writing the new payload all yield a silent ``False`` — never
    raises. A hook write path must never crash or block the tool call / turn
    boundary it's wrapping (#1646, #1947).
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
