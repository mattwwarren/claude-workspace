"""Wrapper around Claude that signals cw on exit.

Used as the pane command in the multiplexer workspace so ``cw`` can
detect when Claude exits and transition the session to IDLE or COMPLETED.

Two modes:

- **Interactive** — no ``--print`` in args. The wrapper passes stdin/stdout
  through unchanged and signals IDLE on exit (legacy behavior).
- **Headless** (``--print`` in args) — the wrapper captures stdout (tee'd
  to the parent's stdout so terminal observers still see it), parses for
  the ``<<<AUTO_DEV_RESULT…>>>`` sentinel block on exit, and signals
  SESSION_COMPLETED when a result is found. See issue #99 for why we do
  this here instead of from reconcile.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult, parse_stdout
from cw.config import events_dir, load_state, save_state
from cw.events import record_event as record_orchestrator_event
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    SessionStatus,
)
from cw.notify import fire_push_notification

if TYPE_CHECKING:
    from cw.models import CwState, Session

_log = logging.getLogger(__name__)

# Sliding-window cap for captured stdout in headless mode. The sentinel block
# lives at the tail of stdout, so we keep the last N bytes and let earlier
# noise fall off. 1 MiB comfortably holds any realistic auto-dev run.
_TAIL_CAPTURE_BYTES = 1_048_576

# Number of trailing stdout lines to store as breadcrumbs when routing to
# signal_needs_attention. Enough to capture the final state summary without
# bloating the session record.
_NEEDS_ATTENTION_BREADCRUMB_LINES = 20


def _idle_signal_path(client: str, purpose: str) -> Path:
    """Path to the idle signal file for a (client, purpose) pair."""
    return events_dir() / f"{client}__{purpose}.idle"


def _detect_claude_session_id(workspace_path: str) -> str | None:
    """Detect the Claude session ID from the most recently modified session file.

    Claude stores sessions at ``~/.claude/projects/<encoded-path>/<uuid>.jsonl``
    where the path encoding replaces ``/`` with ``-`` (e.g. ``/home/foo/bar``
    becomes ``-home-foo-bar``).
    """
    encoded = workspace_path.replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / encoded
    if not project_dir.is_dir():
        return None
    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0].stem


def _is_headless(args: tuple[str, ...]) -> bool:
    """True when ``--print`` appears in the args passed to claude."""
    return "--print" in args or "-p" in args


def _write_passthrough(chunk: bytes) -> None:
    """Tee a chunk to fd 1 (parent stdout). Best-effort — errors are swallowed.

    fd 1 may be closed or redirected to a non-writable target. Capture
    still completes; only the user-visible tee is degraded.
    """
    with contextlib.suppress(OSError):
        os.write(1, chunk)


def _run_claude_streaming(
    args: list[str],
    *,
    max_capture_bytes: int = _TAIL_CAPTURE_BYTES,
) -> tuple[int, str]:
    """Run claude with stdout streamed to parent and captured into a bounded buffer.

    Returns ``(returncode, captured_text)``. The captured text is the last
    ``max_capture_bytes`` of stdout, decoded with replacement. stderr is
    inherited from the parent so terminal observers see errors live.
    """
    buf = bytearray()
    proc = subprocess.Popen(
        ["claude", *args],
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stdout is not None  # noqa: S101 - guaranteed by PIPE above
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            _write_passthrough(chunk)
            buf.extend(chunk)
            if len(buf) > max_capture_bytes:
                del buf[: len(buf) - max_capture_bytes]
    finally:
        returncode = proc.wait()
    return returncode, buf.decode("utf-8", errors="replace")


def _is_paused_for_user_input(result: AutoDevResult) -> bool:
    """Return True when the result indicates the session is waiting for human input.

    Covers three cases:
    - status is ``ambiguities_pending_resolution`` or ``premises_pending_verification``
      (planning halted, structured questions posted).
    - status is ``blocked`` AND at least one ``next_actions`` entry starts with a
      user-directed prefix (``user_resolve_``, ``user_decide_``, ``user_verify_``).
    """
    if result.status in (
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    ):
        return True
    if result.status == "blocked":
        user_prefixes = ("user_resolve_", "user_decide_", "user_verify_")
        return any(action.startswith(user_prefixes) for action in result.next_actions)
    return False


def signal_needs_attention(
    client: str,
    purpose: str,
    *,
    breadcrumbs: str,
    session_id: str | None,
    claude_session_id: str | None,
) -> None:
    """Transition session to COMPLETED and emit SESSION_NEEDS_ATTENTION.

    Mirrors ``signal_completed`` but for sessions that paused waiting for
    operator input rather than completing work normally. Idempotent — a
    no-op when the session is already COMPLETED.
    """
    state = load_state()
    session = _resolve_session(client, purpose, session_id=session_id, state=state)
    if session is None:
        _log.debug(
            "signal_needs_attention: no session found for client=%s purpose=%s id=%s",
            client,
            purpose,
            session_id,
        )
        return
    if session.status == SessionStatus.COMPLETED:
        _log.debug(
            "signal_needs_attention: session %s already COMPLETED — no-op",
            session.id,
        )
        return

    now = datetime.now(UTC)
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    session.last_result = {"breadcrumbs": breadcrumbs, "needs_attention": True}
    if claude_session_id:
        session.claude_session_id = claude_session_id
    save_state(state)

    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": client,
        "ticket_id": None,
        "claude_session_id": claude_session_id,
        "paused_status": None,
        "breadcrumbs": breadcrumbs,
        "crashed": False,
    }
    record_orchestrator_event(OrchestratorEventType.SESSION_NEEDS_ATTENTION, payload)
    fire_push_notification(session.name, client)


def run_claude_wrapper(extra_args: tuple[str, ...]) -> None:
    """Run Claude and signal cw on exit.

    Reads ``CW_CLIENT`` and ``CW_PURPOSE`` from the environment. If either
    is missing, runs Claude once and exits (no signaling).

    In headless mode (``--print`` in args), captures stdout and parses for
    the AUTO_DEV_RESULT sentinel block. If parsed, signals
    SESSION_COMPLETED; otherwise falls back to SESSION_IDLED.
    """
    client = os.environ.get("CW_CLIENT")
    purpose = os.environ.get("CW_PURPOSE")
    session_id_env = os.environ.get("CW_SESSION_ID")

    claude_args = list(extra_args)
    workspace_path = str(Path.cwd())

    if _is_headless(extra_args):
        returncode, captured = _run_claude_streaming(claude_args)
    else:
        result = subprocess.run(["claude", *claude_args], check=False)
        returncode = result.returncode
        captured = ""

    if not client or not purpose:
        sys.exit(returncode)

    claude_session_id = _detect_claude_session_id(workspace_path)

    if captured and returncode == 0:
        parsed = parse_stdout(captured)
        if isinstance(parsed, AutoDevResult):
            if _is_paused_for_user_input(parsed):
                breadcrumbs = "\n".join(
                    captured.splitlines()[-_NEEDS_ATTENTION_BREADCRUMB_LINES:]
                )
                signal_needs_attention(
                    client,
                    purpose,
                    breadcrumbs=breadcrumbs,
                    session_id=session_id_env,
                    claude_session_id=claude_session_id,
                )
                return
            signal_completed(
                client,
                purpose,
                result=parsed,
                session_id=session_id_env,
                claude_session_id=claude_session_id,
            )
            return

        # headless + returncode==0 + no valid sentinel → signal_needs_attention
        if _is_headless(extra_args):
            breadcrumbs = "\n".join(
                captured.splitlines()[-_NEEDS_ATTENTION_BREADCRUMB_LINES:]
            )
            signal_needs_attention(
                client,
                purpose,
                breadcrumbs=breadcrumbs,
                session_id=session_id_env,
                claude_session_id=claude_session_id,
            )
            return

    signal_idle(
        client,
        purpose,
        exit_code=returncode,
        claude_session_id=claude_session_id,
        session_id=session_id_env,
    )


def _resolve_session(
    client: str,
    purpose: str,
    *,
    session_id: str | None,
    state: CwState,
) -> Session | None:
    """Look up the session by explicit ID first, then by (client, purpose)."""
    if session_id:
        for s in state.sessions:
            if s.id == session_id:
                return s
    return state.find_session(client, purpose)


def signal_idle(
    client: str,
    purpose: str,
    *,
    exit_code: int = 0,
    claude_session_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Transition the session to IDLE and write an event signal file."""
    state = load_state()
    session = _resolve_session(client, purpose, session_id=session_id, state=state)
    if session is None or session.status != SessionStatus.ACTIVE:
        return

    session.status = SessionStatus.IDLE
    session.idle_at = datetime.now(UTC)
    if claude_session_id:
        session.claude_session_id = claude_session_id
    save_state(state)

    events_dir().mkdir(parents=True, exist_ok=True)
    signal_file = _idle_signal_path(client, purpose)
    payload: dict[str, object] = {
        "session_id": session.id,
        "client": client,
        "purpose": purpose,
        "exit_code": exit_code,
    }
    if claude_session_id:
        payload["claude_session_id"] = claude_session_id
    signal_file.write_text(json.dumps(payload))

    record_event(
        client,
        HistoryEvent(
            event_type=EventType.SESSION_IDLED,
            client=client,
            session_id=session.id,
            session_name=session.name,
            purpose=purpose,
            metadata={"exit_code": str(exit_code)},
        ),
    )


def signal_completed(
    client: str,
    purpose: str,
    *,
    result: AutoDevResult,
    session_id: str | None = None,
    claude_session_id: str | None = None,
) -> None:
    """Transition the session to COMPLETED and emit SESSION_COMPLETED.

    Idempotent: a no-op when the session is already COMPLETED (e.g. when
    reconcile got there first via the phantom-pane path). Persists the
    parsed ``result`` to ``session.last_result`` so the daemon's
    consume_completed_sessions can route by status.
    """
    state = load_state()
    session = _resolve_session(client, purpose, session_id=session_id, state=state)
    if session is None:
        _log.debug(
            "signal_completed: no session found for client=%s purpose=%s id=%s",
            client,
            purpose,
            session_id,
        )
        return
    if session.status == SessionStatus.COMPLETED:
        _log.debug(
            "signal_completed: session %s already COMPLETED — no-op",
            session.id,
        )
        return

    now = datetime.now(UTC)
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    session.last_result = result.model_dump(mode="json")
    if claude_session_id:
        session.claude_session_id = claude_session_id
    save_state(state)

    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": client,
        "crashed": False,
        "status": result.status,
        "ticket_id": result.ticket_id,
    }
    record_orchestrator_event(OrchestratorEventType.SESSION_COMPLETED, payload)
