"""Shared stdin/file JSON helpers for Claude Code hook handlers (#940).

Both the Stop hook (``cw signal-stop``, ``cli/sessions.py``) and the
PreToolUse guard (``cw guard-cwd``, ``cli/guard.py``) read a JSON hook
payload from stdin, and both may then load ``.claude/cw-context.json`` from
a ``cwd`` the payload names. Extracted here so a fix to either read path
(a new stdin/JSON edge case, a cw-context.json shape change) can't drift
between the two — previously each hook reimplemented this independently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


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
