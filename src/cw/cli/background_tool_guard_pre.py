"""The ``cw background-tool-guard-pre`` PreToolUse hook handler (#2303).

Wired to two matchers in :func:`cw.spawn._build_hook_settings` — as the third
command on the ``"Bash"`` entry and as the sole command on a ``"Monitor"``
entry — and backed by one classifier,
:func:`cw.cli._background_tool_policy.classify_background_tool`, which
branches on ``tool_name``. Exits ``2`` with the refusal reason on stderr for a
backgrounded Bash call or a Monitor call in a headless worker, and records a
``guard.background_tool_refused`` event; exits ``0`` on everything else,
including any unexpected error.
"""

from __future__ import annotations

from cw.cli._background_tool_policy import (
    _RefusalDecision,
    classify_background_tool,
    enforce,
)
from cw.cli._base import main
from cw.cli._hook_io import _read_hook_stdin_json
from cw.events import record_event
from cw.models import OrchestratorEventType


def _record_refusal(decision: _RefusalDecision) -> None:
    """Append the durable bus record for a refusal; never raise.

    Isolated in its own try/except, mirroring
    :func:`cw.cli.guard_busy_wait._record_block`: a failure to *record* a
    refusal must never suppress the refusal itself.
    """
    payload: dict[str, object] = {
        "tool_name": decision.tool_name,
        "client": decision.client,
        "lane": decision.lane,
        "ticket_id": decision.ticket_id,
    }
    if decision.agent_id is not None:
        payload["agent_id"] = decision.agent_id
    try:
        record_event(
            OrchestratorEventType.GUARD_BACKGROUND_TOOL_REFUSED,
            payload,
            correlation_id=decision.session_id,
        )
    except Exception:  # noqa: BLE001 — a failed record must not undo a refusal.
        return


@main.command(name="background-tool-guard-pre")
def background_tool_guard_pre() -> None:
    """Refuse a backgrounded Bash call or a Monitor call in a headless worker.

    Reads the PreToolUse hook JSON from stdin. Exits 2 on a refusal; exits 0
    (no-op) on everything else, including any unexpected error — the hook
    must never crash. See :mod:`cw.cli._background_tool_policy`.
    """
    try:
        payload = _read_hook_stdin_json()
        decision = classify_background_tool(payload)
    except Exception:  # noqa: BLE001 — a hook must never crash; fail open.
        return
    if decision is None:
        return
    _record_refusal(decision)
    enforce(decision.reason)
