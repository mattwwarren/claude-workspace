"""The ``cw guard-cwd`` PreToolUse hook handler (#940 R9a).

Claude Code invokes this before every Bash tool call in a dispatched worker
(wired via ``settings.local.json`` in :data:`cw.spawn._HOOK_SETTINGS_TEMPLATE`).
It reads the hook JSON from stdin, loads ``<cwd>/.claude/cw-context.json``, and
exits ``2`` (block) only when the resolved ``cwd`` equals the resolved
``workspace_path`` — the operator's main checkout, forbidden for any git
mutation from a worker (the #925/#766 isolation breach).

Every other outcome is a best-effort no-op (exit ``0``): a distinct worktree, a
missing / malformed context, an absent ``workspace_path``, or unreadable stdin.
A broken hook that crashed or blocked spuriously would wedge every Bash call in
every worker, which is strictly worse than not guarding — so this must never
raise uncaught.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from cw.cli._base import main

# PreToolUse contract: exit 2 blocks the tool call and feeds stderr back to the
# agent; any other code (0 here) allows it. Distinct from
# check_not_main_checkout.py's 1=block/2=error convention — that is a manual
# guard script, not a Claude Code hook.
_GUARD_CWD_BLOCK_EXIT = 2


def _read_hook_cwd() -> str | None:
    """Return the hook payload's ``cwd`` string, or None on any read failure.

    Best-effort: unreadable stdin, an empty body, malformed JSON, a non-object
    payload, or a missing/non-string ``cwd`` all yield None (silent no-op).
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
    cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd_value if isinstance(cwd_value, str) else None


def _guard_cwd_blocks() -> bool:
    """Return True iff the hook cwd resolves to the context's workspace_path."""
    cwd_value = _read_hook_cwd()
    if cwd_value is None:
        return False

    context_path = Path(cwd_value) / ".claude" / "cw-context.json"
    if not context_path.is_file():
        return False
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(context, dict):
        return False

    workspace_raw = context.get("workspace_path")
    if not isinstance(workspace_raw, str) or not workspace_raw:
        return False

    return Path(cwd_value).resolve() == Path(workspace_raw).resolve()


@main.command(name="guard-cwd")
def guard_cwd() -> None:
    """Block a Bash tool call when the worker cwd is the operator main checkout.

    Reads the PreToolUse hook JSON from stdin and consults
    ``<cwd>/.claude/cw-context.json``. Exits 2 (block) only on a confirmed
    main-checkout match; exits 0 (no-op) on everything else, including any
    unexpected error — the hook must never crash. See module docstring.
    """
    try:
        blocked = _guard_cwd_blocks()
    except Exception:  # noqa: BLE001 — a hook must never crash; fail open.
        return
    if blocked:
        click.echo(
            "BLOCKED (#940): git/Bash rejected — cwd is the operator main"
            " checkout (workspace_path in cw-context.json). A dispatch worker"
            " must only mutate its own worktree.",
            err=True,
        )
        sys.exit(_GUARD_CWD_BLOCK_EXIT)
