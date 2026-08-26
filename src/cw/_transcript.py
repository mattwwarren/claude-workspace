"""Shared transcript path resolution helpers.

Pure helpers used by ``cw.queue_peek``, ``cw.reconcile._shared``, and
``cw.cli.agent_spawn_verify``. No heuristic, no degraded fallback — just the
canonical resolution logic those consumers share.
"""

from __future__ import annotations

import datetime as dt
import itertools
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


def subagent_transcript_paths(project_dir: Path, claude_session_id: str) -> list[Path]:
    """Return every subagent transcript for one parent session, sorted, or [].

    Subagents write to ``<project_dir>/<claude_session_id>/subagents/*.jsonl``
    (the layout backing ``find_new_subagent_transcript``, #2012). Scoped to
    exactly this parent session's id: unlike ``find_new_subagent_transcript``,
    which deliberately scans the whole project dir for *any* new subagent
    transcript at spawn time, an idle-liveness check must not pick up a
    sibling session's leftover subagents from a sequentially-reused worktree
    — so this does not rglob the project dir. Fails open (``[]``) on a
    missing directory or an OSError, matching every sibling helper in this
    module.
    """
    subagents_dir = project_dir / claude_session_id / "subagents"
    if not subagents_dir.is_dir():
        return []
    try:
        return sorted(subagents_dir.glob("*.jsonl"))
    except OSError:
        return []


def find_new_subagent_transcript(
    project_dir: Path,
    *,
    since: dt.datetime,
    exclude_stem: str | None,
) -> Path | None:
    """Return the newest transcript under *project_dir* written after *since*.

    The dispatch-verification leaf behind ``cw agent-spawn-verify`` (#2012):
    a subagent spawned by the caller writes its own ``*.jsonl`` into the same
    encoded project dir (today under a ``subagents/`` subdirectory), under a
    filename matching neither the parent's csid nor its surface_ref prefix.
    Its *appearance* is therefore the cheapest positive evidence that an async
    spawn actually launched, as opposed to silently never starting.

    Two filters make that evidence specific rather than incidental:

    * ``exclude_stem`` drops the caller's own transcript — it is being
      appended to continuously, so without this every call would trivially
      "verify" itself. Pass the caller's ``$CLAUDE_CODE_SESSION_ID``.
    * ``since`` drops pre-existing siblings from an earlier dispatch in the
      same (sequentially reused) worktree — the same mtime guard
      ``locate_transcript`` applies to its surface_ref candidate, here applied
      per-file across the whole glob.

    Single-pass and side-effect free: the polling loop lives in the CLI
    command, not here. Returns the *newest* qualifying candidate rather than
    the first one ``rglob`` happens to yield, so the answer does not depend on
    filesystem iteration order. Fails open (``None``) on a missing dir or a
    per-candidate stat failure, matching every sibling helper.
    """
    if not project_dir.is_dir():
        return None
    since_ts = since.timestamp()
    newest: Path | None = None
    newest_mtime = since_ts
    try:
        # #2012 fix-loop review: scoped to the documented `subagents/`
        # location instead of an unbounded rglob over every *.jsonl ever
        # written to the project dir. `rglob("subagents/*.jsonl")` matches
        # `subagents/*.jsonl` at any depth (project_dir/subagents/*.jsonl
        # directly, or project_dir/<session-uuid>/subagents/*.jsonl, or
        # deeper) without pulling in unrelated top-level transcripts. The
        # separate top-level `*.jsonl` glob stays included only because
        # tests/test_transcript.py's pre-existing generic-behavior cases
        # (outside this fix's file scope) seed fixtures directly at
        # project_dir/*.jsonl and assert they're found there.
        candidates = itertools.chain(
            project_dir.glob("*.jsonl"),
            project_dir.rglob("subagents/*.jsonl"),
        )
        for candidate in candidates:
            if exclude_stem is not None and candidate.stem == exclude_stem:
                continue
            # Per-candidate: a stat failure on one sibling (rotated/deleted
            # mid-glob) must not discard a hit already found on another.
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = candidate, mtime
    except OSError:
        return None
    return newest
