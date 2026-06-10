"""Shared utility helpers for cw internal modules.

Keep this module free of imports from other ``cw.*`` modules to avoid
circular dependencies — imported by :mod:`cw.cli` and :mod:`cw.reconcile`,
so those modules load without circular dependencies.
"""

from __future__ import annotations

from pathlib import Path


def _tail_lines(content: str, n: int) -> str:
    """Return the last *n* lines of *content*, preserving no trailing newline."""
    all_lines = content.splitlines()
    if len(all_lines) > n:
        return "\n".join(all_lines[-n:])
    return content.rstrip("\n")


def claude_project_dir(path: str | Path) -> Path:
    """Return the Claude Code project directory for *path*.

    Claude Code encodes a project's working directory into a flat directory
    name under ``~/.claude/projects/`` by replacing every ``/`` **and** every
    ``.`` with ``-``.  For example ``/home/u/.cw/wt/abc`` becomes
    ``-home-u--cw-wt-abc`` (double dash for the dot-prefixed ``.cw``
    segment).

    Using only ``.replace("/", "-")`` — which was the original single-replace
    — produces ``-home-u-.cw-wt-abc``, a path that does not exist on disk,
    causing all transcript-based liveness checks to return ``False`` and the
    idle watchdog to falsely reap active sessions whose worktrees live under a
    dotted directory such as ``~/.cw/``.  See GitHub issue #463.
    """
    encoded = str(path).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded
