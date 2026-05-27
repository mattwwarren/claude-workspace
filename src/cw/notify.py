"""Push notification helper for sessions that need operator attention."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

_PEON_TIMEOUT = 5  # seconds
_HOOK_EVENT_NAME = "Notification"
_NOTIFICATION_TYPE = "input.required"

_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError)


@lru_cache(maxsize=1)
def _peon_sh_path() -> Path | None:
    """Locate peon.sh in the standard install location."""
    p = Path.home() / ".claude" / "hooks" / "peon-ping" / "peon.sh"
    return p if p.is_file() else None


def _fire_push_notification_sync(session_name: str, client: str, cwd: str = "") -> None:
    """Blocking implementation — called in a daemon thread by fire_push_notification."""
    payload = json.dumps(
        {
            "hook_event_name": _HOOK_EVENT_NAME,
            "notification_type": _NOTIFICATION_TYPE,
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


def fire_push_notification(session_name: str, client: str, *, cwd: str = "") -> None:
    """Fire a push notification for a session that needs operator attention.

    Runs in a daemon thread — non-blocking. Calls peon-ping (if installed) then
    tries notify-send as a Linux fallback. All failures are swallowed.
    """
    threading.Thread(
        target=_fire_push_notification_sync,
        args=(session_name, client, cwd),
        daemon=True,
    ).start()
