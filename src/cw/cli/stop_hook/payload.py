"""Stop-hook stdin payload and ``cw-context.json`` resolution.

The first step of every ``cw signal-stop`` fire: read the hook JSON from
stdin, then the worktree's ``cw-context.json``, and return ``None`` (a silent
no-op) on any missing or ill-typed field. Imports nothing from its siblings.
Split out of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

from cw._hook_context import _read_cw_context
from cw.cli._hook_io import _read_hook_stdin_json


def _read_stop_hook_payload() -> tuple[dict[str, object], str] | None:
    """Read the Stop-hook JSON from stdin and extract its ``cwd``.

    Returns ``(hook_payload, cwd_value)`` when stdin holds a JSON object with a
    string ``cwd``, else ``None``. Best-effort: every failure mode (unreadable
    stdin, empty body, malformed JSON, missing cwd) is a silent no-op.
    """
    hook_payload = _read_hook_stdin_json()
    if hook_payload is None:
        return None
    cwd_value = hook_payload.get("cwd")
    if not isinstance(cwd_value, str):
        return None
    return hook_payload, cwd_value


def _resolve_signal_stop_context() -> (
    tuple[dict[str, object], dict[str, object], str, str] | None
):
    """Read and validate the Stop-hook payload + cw-context.json.

    Returns ``(hook_payload, context, cwd_value, cw_session_id)`` when every
    required field is present and well-typed, else ``None`` (silent no-op so
    hook execution never blocks claude from exiting). See :func:`signal_stop`.
    """
    payload = _read_stop_hook_payload()
    if payload is None:
        return None
    hook_payload, cwd_value = payload

    context = _read_cw_context(cwd_value)
    if context is None:
        return None

    cw_session_id = context.get("session_id")
    if not isinstance(cw_session_id, str):
        return None

    return hook_payload, context, cwd_value, cw_session_id
