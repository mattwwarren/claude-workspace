"""The ``cw agent-spawn-pre`` hook handler (#1646, split by #1947).

Claude Code invokes this before every subagent-spawning tool call in a
dispatched worker (wired via ``settings.local.json`` in
:data:`cw.spawn._HOOK_SETTINGS_TEMPLATE`, matched on
:data:`cw.spawn._AGENT_TOOL_MATCHER`). It increments
``agent_spawn_stamp.unresolved_count`` in ``<cwd>/.claude/cw-context.json``
before the spawn starts.

#1646 originally paired this with a ``cw agent-spawn-post`` PostToolUse
handler that decremented the same counter when the spawn call returned. #1947
found that pairing hollow: replaying a live async ``Agent(isolation=
"worktree")`` spawn showed ``PostToolUse:Agent`` fires at launch-return (the
``Async agent launched successfully.`` tool_result), not subagent completion
— the harness's own turn accounting still reported the background agent
pending ~3.5s after the stamp had already balanced back to 0. The Post half
is removed; ``cw signal-stop`` (``cli/stop_hook.py``) now owns clearing/
snapshotting the counter, driven off the hook payload's own
``background_tasks`` list — a signal that reflects the harness's live
turn-accounting rather than a tool-call return that races ahead of it.

The point of what remains is still what happens when Pre fires but the
worker dies before Stop next runs with an empty ``background_tasks``: a
worker that dies — or pauses forever — with a sub-agent spawn still in flight
leaves the count above zero, on disk, in its own worktree. That is durable
evidence no transcript scrape can fabricate, and ``cw.reconcile`` reads it to
distinguish "this surface vanished" (generic ``phantom_surface``) from "this
surface died mid-spawn, so committed work may sit behind a verification tail
that never ran".

Fail-open throughout, mirroring ``cw guard-cwd``: unreadable stdin, a missing
or malformed context, a contended lock, or any unexpected error all yield a
silent exit 0. This never blocks a tool call under any circumstances —
refusing a subagent spawn is never the right answer, and a missed stamp costs
only disposition precision on a crash that may not happen.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.cli._base import main
from cw.cli._hook_io import (
    _LOCK_TIMEOUT_SECS_DEFAULT,
    _context_lock,
    _read_hook_stdin_json,
    _write_cw_context_locked,
)
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    extract_unresolved_spawn_count,
)

__all__ = [
    "_LOCK_TIMEOUT_SECS_DEFAULT",
    "_context_lock",
    "agent_spawn_pre",
]


def _unresolved_count(context: dict[str, object]) -> int:
    """Return the stamp's current count, treating every odd shape as 0.

    Thin wrapper over :func:`cw.models.extract_unresolved_spawn_count`, kept
    under this local name since call sites and tests already reference it —
    the validation logic itself lives in ``cw.models`` so this write-side
    reader and ``cw.reconcile._shared``'s read-side reader cannot drift onto
    different rules for the same on-disk shape (#1646 review finding).
    """
    return extract_unresolved_spawn_count(context)


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

    Read-modify-write via :func:`cw.cli._hook_io._write_cw_context_locked`,
    floored at zero: a caller with no matching prior Pre (a reused worktree, a
    hook wired mid-flight) must not drive the counter negative, which would
    swallow the *next* real crash. Only ever called with ``delta=1`` since
    #1947 removed the decrementing ``agent-spawn-post`` counterpart — kept
    general rather than hardcoded to +1 since the flooring/carry-forward
    logic below is delta-agnostic and the shape matches
    :func:`cw.cli._hook_io._write_cw_context_locked`'s mutate-fn contract
    either way.

    ``last_stamped_at`` advances only when the count increases — it answers
    "when did the oldest unresolved spawn begin", which is what an operator
    reading a parked row wants; refreshing it on a decrease would erase that.
    """
    cwd_value = _hook_cwd()
    if cwd_value is None:
        return

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        new_count = max(0, _unresolved_count(context) + delta)
        context[AGENT_SPAWN_STAMP_KEY] = {
            AGENT_SPAWN_UNRESOLVED_COUNT_KEY: new_count,
            AGENT_SPAWN_LAST_STAMPED_AT_KEY: (
                datetime.now(UTC).isoformat()
                if delta > 0
                else _last_stamped_at(context)
            ),
        }
        return context

    _write_cw_context_locked(cwd_value, _mutate)


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
