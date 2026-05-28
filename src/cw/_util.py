"""Shared utility helpers for cw internal modules.

Keep this module free of imports from other ``cw.*`` modules to avoid
circular dependencies — it is imported by both :mod:`cw.cmux` and
:mod:`cw.tmux`.
"""

from __future__ import annotations


def _tail_lines(content: str, n: int) -> str:
    """Return the last *n* lines of *content*, preserving no trailing newline."""
    all_lines = content.splitlines()
    if len(all_lines) > n:
        return "\n".join(all_lines[-n:])
    return content.rstrip("\n")
