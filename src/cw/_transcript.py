"""Shared transcript path resolution helper.

Pure helper used by both ``cw.queue_peek`` and ``cw.reconcile._shared``.
No heuristic, no degraded fallback — just the canonical resolution logic
that both consumers share.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def locate_transcript(
    *,
    project_dir: Path | None,
    claude_session_id: str | None,
    surface_ref: str | None,
    started_at: dt.datetime,
) -> Path | None:
    """Return session transcript path, or None.

    Pure resolution — no heuristic, no degraded fallback.
    Resolution order:
    1. csid exact: ``<project_dir>/<csid>.jsonl``
    2. surface_ref newest-only: newest ``<surface_ref>*.jsonl`` with mtime >
       started_at (reused-worktree stale-transcript guard, #358/#372).
    3. None — does NOT fall through from csid-miss to surface_ref.
    """
    if project_dir is None or not project_dir.is_dir():
        return None
    try:
        if claude_session_id is not None:
            path = project_dir / f"{claude_session_id}.jsonl"
            return path if path.is_file() else None
        if surface_ref is not None:
            candidates = sorted(
                project_dir.glob(f"{surface_ref}*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                return None
            newest = candidates[0]
            mtime = dt.datetime.fromtimestamp(newest.stat().st_mtime, tz=dt.UTC)
            return newest if mtime > started_at else None
    except OSError:
        return None
    return None
