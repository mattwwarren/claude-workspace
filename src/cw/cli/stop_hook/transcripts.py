"""Transcript-candidate discovery shared by the Stop hook's park and continuation.

Both the #2135 abandoned-exit park and the #2348 auto-continuation locate the
session's Claude transcript the same way: the hook's ``cwd`` project dir and the
session's recorded ``worktree_path`` project dir (issue #799, for an
EnterWorktree-shifted cwd), de-duplicated. One definition here keeps the two
from drifting (#3623). Path-only: callers keep their own existence checks and
scan decisions. Imports nothing from its siblings. Logs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import Session


def _transcript_candidates(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
    claude_project_dir: Callable[[str], Path],
) -> tuple[Path, ...]:
    """Return the distinct transcript paths to consider, cwd first, then worktree.

    ``()`` for a non-str or empty *claude_session_id*. *claude_project_dir* is
    passed in, not imported, so each caller resolves it through its own module
    global (the Stop-hook tests patch it in the namespace the reader uses).
    A repeated directory (the common case: the hook's cwd IS the worktree) is
    resolved once, so the hot path never reads the same file twice. Does not
    touch the filesystem.
    """
    if not isinstance(claude_session_id, str) or not claude_session_id:
        return ()
    search_dirs: list[str] = [cwd_value]
    if session.worktree_path is not None:
        search_dirs.append(str(session.worktree_path))
    return tuple(
        claude_project_dir(search_dir) / f"{claude_session_id}.jsonl"
        for search_dir in dict.fromkeys(search_dirs)
    )
