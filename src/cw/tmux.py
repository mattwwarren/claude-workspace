"""tmux multiplexer adapter.

Wraps the ``tmux`` CLI via :mod:`subprocess`. A workspace maps to a tmux
session, a surface to a tmux pane. Implements the
:class:`cw.cmux.MultiplexerAdapter` protocol (``spawn``, ``close``,
``identify``, ``list_surfaces``) alongside the cmux backend.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from cw.exceptions import CwError

# Pane reference format returned by ``tmux split-window -P -F ...``.
_PANE_FORMAT = "#{session_name}:#{window_index}.#{pane_index}"

# Pane reference format that also captures the foreground command name.
# Used by :meth:`TmuxAdapter.list_live_surface_commands` to detect zombie
# panes whose claude process has exited (pane is back at the shell prompt).
_PANE_FORMAT_WITH_COMMAND = (
    "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}"
)

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
        """Return the set of live tmux pane refs across all sessions.

        Empty set when the tmux server is not running — callers in
        reconciliation rely on this invariant to avoid false positives.
        """
        result = self._run(
            ["list-panes", "-a", "-F", _PANE_FORMAT],
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return set()
        return {line for line in result.stdout.strip().splitlines() if line}

    def capture_surface(self, surface_ref: str, lines: int, scrollback: int) -> str:
        """Return last *lines* lines of worker output for *surface_ref*.

        Uses ``tmux capture-pane`` looking back at most *scrollback* lines.
        Raises :exc:`cw.exceptions.CwError` when the pane is not found or
        the tmux server is not running.
        """
        result = self._run(
            ["capture-pane", "-t", surface_ref, "-p", "-S", f"-{scrollback}"],
            check=False,
        )
        if result.returncode != 0:
            msg = f"Surface '{surface_ref}' not found or tmux server is not running."
            raise CwError(msg)
        content = result.stdout
        all_lines = content.splitlines()
        if len(all_lines) > lines:
            return "\n".join(all_lines[-lines:])
        return content.rstrip("\n")

    def list_live_surface_commands(self) -> dict[str, str]:
        """Return mapping of pane ref to foreground command name.

        Single ``tmux list-panes`` call with a format string capturing
        both the pane ref and ``pane_current_command``. Returns an empty
        dict when the tmux server is not running or the call fails —
        same all-or-nothing semantics as :meth:`list_surfaces`. The
        reconciler treats an empty return as "command info unavailable,
        skip the zombie filter" — fail-open, no false-positive reaping.
        """
        result = self._run(
            ["list-panes", "-a", "-F", _PANE_FORMAT_WITH_COMMAND],
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return {}
        commands: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                commands[parts[0]] = parts[1].strip()
        return commands
