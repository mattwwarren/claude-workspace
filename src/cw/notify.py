"""Push notification helper for sessions that need operator attention."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

_PEON_TIMEOUT = 5  # seconds

_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError)


def _peon_sh_path() -> Path | None:
    """Locate peon.sh in the standard install location."""
    p = Path.home() / ".claude" / "hooks" / "peon-ping" / "peon.sh"
    return p if p.is_file() else None


def fire_push_notification(session_name: str, client: str, *, cwd: str = "") -> None:
    """Fire a push notification for a session that needs operator attention.

    Calls peon-ping (if installed) then tries notify-send as a Linux fallback.
    All failures are swallowed — notification is best-effort.
    """
    payload = json.dumps(
        {
            "hook_event_name": "Notification",
            "notification_type": "input.required",
            "session_name": session_name,
            "client": client,
            "cwd": cwd,
        }
    )
    peon = _peon_sh_path()
    if peon is not None:
        with contextlib.suppress(*_SUBPROCESS_ERRORS):
            subprocess.run(
                ["bash", str(peon)],
                input=payload,
                text=True,
                timeout=_PEON_TIMEOUT,
                capture_output=True,
                check=False,
            )

    with contextlib.suppress(*_SUBPROCESS_ERRORS):
        subprocess.run(
            [
                "notify-send",
                "cw: session needs attention",
                f"{client}: {session_name}",
            ],
            timeout=_PEON_TIMEOUT,
            capture_output=True,
            check=False,
        )
