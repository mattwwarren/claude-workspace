"""tmux multiplexer adapter.

Wraps the ``tmux`` CLI via :mod:`subprocess`. A workspace maps to a tmux
session, a surface to a tmux pane. The same three-method protocol
(:class:`cw.cmux.MultiplexerAdapter`) that the cmux backend implements.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from cw.exceptions import CwError

# Pane reference format returned by ``tmux split-window -P -F ...``.
_PANE_FORMAT = "#{session_name}:#{window_index}.#{pane_index}"

# Mapping from the cw-level "surface" hint to tmux's split-window flag.
# "right" produces a horizontal split (new pane to the right); "bottom"
# produces a vertical split. Anything else falls back to horizontal.
_SPLIT_FLAG = {"right": "-h", "bottom": "-v"}


class TmuxAdapter:
    """Multiplexer adapter backed by the ``tmux`` command-line tool.

    Instantiation verifies that ``tmux`` is on PATH. The adapter shells
    out for each protocol call — no long-lived connection is held.
    """

    def __init__(self) -> None:
        if shutil.which("tmux") is None:
            msg = "tmux not found on PATH; install tmux or set CW_BACKEND=cmux"
            raise CwError(msg)

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a tmux subcommand and return the completed process."""
        return subprocess.run(
            ["tmux", *args],
            capture_output=capture,
            text=True,
            check=check,
        )

    def _ensure_session(self, workspace: str) -> None:
        """Create the tmux session for *workspace* if it does not exist."""
        result = self._run(["has-session", "-t", workspace], check=False)
        if result.returncode != 0:
            self._run(["new-session", "-d", "-s", workspace])

    def spawn(self, workspace: str, command: str, surface: str = "right") -> str:
        """Split a pane in *workspace* and run *command* inside it.

        Returns the tmux pane reference (``session:window.pane``).
        """
        self._ensure_session(workspace)
        split_flag = _SPLIT_FLAG.get(surface, "-h")
        result = self._run(
            [
                "split-window",
                split_flag,
                "-t",
                workspace,
                "-P",
                "-F",
                _PANE_FORMAT,
            ]
        )
        pane_ref = result.stdout.strip()
        if not pane_ref:
            msg = "tmux split-window returned empty pane reference"
            raise CwError(msg)
        # ``-l`` sends the command literally; ``Enter`` submits it.
        self._run(["send-keys", "-t", pane_ref, "-l", command])
        self._run(["send-keys", "-t", pane_ref, "Enter"])
        return pane_ref

    def close(self, surface_ref: str) -> None:
        """Kill the pane identified by *surface_ref*."""
        self._run(["kill-pane", "-t", surface_ref], check=False)

    def identify(self) -> dict[str, Any]:
        """Return the focused workspace/surface as seen by tmux.

        Uses ``tmux display-message`` with a JSON format string. If tmux
        is not attached to any session, returns an empty focus context.
        """
        result = self._run(
            [
                "display-message",
                "-p",
                '{"focused":{"workspace_id":"#S","surface_id":"#S:#I.#P"}}',
            ],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"focused": {}}
        try:
            parsed: dict[str, Any] = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"focused": {}}
        return parsed

    def list_surfaces(self) -> set[str]:
        """Return the set of live pane refs from tmux.

        Stub implementation — full reconciliation query added in Task 2.
        Returns empty set on any error so callers treat "tmux unreachable"
        as "no surfaces alive" rather than "all surfaces still alive".
        """
        return set()
