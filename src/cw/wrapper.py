"""Wrapper around Claude that signals cw on exit.

Used as the pane command in Zellij layouts so ``cw`` can detect when
Claude exits and transition the session to IDLE.  After signaling IDLE,
the wrapper waits for a trigger file from the daemon before launching
Claude again.  If no trigger arrives within the timeout, the wrapper
exits and the pane returns to a shell prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click

from cw.config import EVENTS_DIR, load_state, save_state
from cw.history import EventType, HistoryEvent, record_event
from cw.models import SessionStatus

if TYPE_CHECKING:
    from pathlib import Path

# How long the wrapper waits for a trigger file before exiting (seconds).
_TRIGGER_WAIT_TIMEOUT_S = 300  # 5 minutes
_TRIGGER_POLL_INTERVAL_S = 0.5


def _idle_signal_path(client: str, purpose: str) -> Path:
    """Path to the idle signal file for a (client, purpose) pair."""
    return EVENTS_DIR / f"{client}__{purpose}.idle"


def _trigger_path(client: str, purpose: str) -> Path:
    """Path to the trigger file the daemon writes."""
    return EVENTS_DIR / f"{client}__{purpose}.trigger"


def run_claude_wrapper(extra_args: tuple[str, ...]) -> None:
    """Run Claude, signal IDLE on exit, then wait for a daemon trigger.

    Reads ``CW_CLIENT`` and ``CW_PURPOSE`` from the environment.  If
    either is missing, runs Claude once and exits (no IDLE signaling).
    """
    client = os.environ.get("CW_CLIENT")
    purpose = os.environ.get("CW_PURPOSE")

    claude_args = list(extra_args)
    result = subprocess.run(["claude", *claude_args], check=False)

    if not client or not purpose:
        sys.exit(result.returncode)

    signal_idle(client, purpose, exit_code=result.returncode)

    # Wait for daemon trigger to launch Claude again.
    trigger = _trigger_path(client, purpose)
    next_args = _wait_for_trigger(trigger)

    while next_args is not None:
        click.echo(f"Trigger received — launching Claude ({client}/{purpose})")
        result = subprocess.run(["claude", *next_args], check=False)
        signal_idle(client, purpose, exit_code=result.returncode)
        next_args = _wait_for_trigger(trigger)

    click.echo(f"No trigger after {_TRIGGER_WAIT_TIMEOUT_S}s — wrapper exiting.")


def signal_idle(
    client: str,
    purpose: str,
    *,
    exit_code: int = 0,
) -> None:
    """Transition the session to IDLE and write an event signal file."""
    state = load_state()
    session = state.find_session(client, purpose)
    if session is None or session.status != SessionStatus.ACTIVE:
        return

    session.status = SessionStatus.IDLE
    session.idle_at = datetime.now(UTC)
    save_state(state)

    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    signal_file = _idle_signal_path(client, purpose)
    payload = {
        "session_id": session.id,
        "client": client,
        "purpose": purpose,
        "exit_code": exit_code,
    }
    signal_file.write_text(json.dumps(payload))

    record_event(client, HistoryEvent(
        event_type=EventType.SESSION_IDLED,
        client=client,
        session_id=session.id,
        session_name=session.name,
        purpose=purpose,
        metadata={"exit_code": str(exit_code)},
    ))


def _wait_for_trigger(
    trigger_path: Path,
    timeout: float = _TRIGGER_WAIT_TIMEOUT_S,
    poll_interval: float = _TRIGGER_POLL_INTERVAL_S,
) -> list[str] | None:
    """Poll for a trigger file and return the claude args, or None on timeout."""
    elapsed = 0.0
    while elapsed < timeout:
        if trigger_path.exists():
            try:
                data = json.loads(trigger_path.read_text())
                trigger_path.unlink(missing_ok=True)
                result: list[str] = data.get("claude_args", [])
                return result
            except (json.JSONDecodeError, OSError):
                trigger_path.unlink(missing_ok=True)
                return []
        time.sleep(poll_interval)
        elapsed += poll_interval
    return None
