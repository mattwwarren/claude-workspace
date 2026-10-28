"""Session refs and transcript location for ``cw queue peek``.

Maps a dev-queue task to its worker's transcript: reads the session's lookup
fields from ``CW_STATE`` and searches ``CLAUDE_PROJECTS`` (or the worktree's
derived project dir) for the main ``/auto-dev`` transcript, plus the
mtime-based idle estimate used for blind rows.

``CW_STATE`` and ``CLAUDE_PROJECTS`` are read here, so tests patch them on this
module; the copies re-exported from ``cw.queue_peek`` are snapshots.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cw._transcript import locate_transcript
from cw._util import claude_project_dir
from cw.opencode_runner import OPENCODE_LOG_RELATIVE_PATH

if TYPE_CHECKING:
    from cw.models import TicketTask

# Patched by tests to redirect path lookups without touching the filesystem.
CLAUDE_PROJECTS: Path = Path.home() / ".claude/projects"
CW_STATE: Path = Path.home() / ".local/share/cw/sessions.json"
_EPOCH = dt.datetime.fromtimestamp(0, tz=dt.UTC)


def _load_session_refs(session_id: str | None) -> dict[str, Any]:
    """Load session lookup fields from CW_STATE for a cw session id.

    Returns a dict with ``claude_session_id``, ``surface_ref``,
    ``started_at``, ``worktree_path`` and ``local_liveness`` (all may be
    None) plus ``matched=True``, or an empty dict when session_id is absent
    or no match is found (``matched`` separates a null-id row from no row,
    #2417). Reads CW_STATE directly so tests can monkeypatch the path.
    """
    if not session_id or not CW_STATE.exists():
        return {}
    try:
        data = json.loads(CW_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    for sess in data.get("sessions", []):
        if sess.get("id") == session_id:
            return {
                "matched": True,
                "claude_session_id": sess.get("claude_session_id"),
                "surface_ref": sess.get("surface_ref"),
                "started_at": sess.get("started_at"),
                "worktree_path": sess.get("worktree_path"),
                "local_liveness": sess.get("local_liveness"),
            }
    return {}


def _is_known_idless_session(refs: dict[str, Any]) -> bool:
    """True iff *refs* is a matched Session row whose Claude ids are both null.

    No unscoped newest-JSONL or ticket-name heuristic may stand in for its
    transcript: it would surface an earlier stage's sentinel (#2417). An absent
    row (``refs == {}``) is unknown state, not this case.
    """
    return (
        bool(refs.get("matched"))
        and refs.get("claude_session_id") is None
        and refs.get("surface_ref") is None
    )


def load_claude_session_id(session_id: str | None) -> str | None:
    """Map a cw session id (8-char hex) to the full claude_session_id UUID.

    Reads CW_STATE directly so tests can monkeypatch the path without
    wiring through the full cw.config stack.
    """
    raw = _load_session_refs(session_id).get("claude_session_id")
    return str(raw) if raw is not None else None


def _parse_started_at(started_at_iso: str | None) -> dt.datetime:
    """Parse a started_at ISO string, returning _EPOCH on missing/invalid input."""
    if started_at_iso is None:
        return _EPOCH
    try:
        ts = dt.datetime.fromisoformat(started_at_iso)
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    except ValueError:
        return _EPOCH


def _find_transcript_in_project_dir(
    project_dir: Path,
    claude_session_id: str | None,
    surface_ref: str | None,
    started_at_iso: str | None,
) -> Path | None:
    """Find a transcript inside a known Claude project dir.

    Resolution order:
    1. csid set → ``<project_dir>/<csid>.jsonl`` (exact match via locate_transcript).
    2. csid absent or file missing → surface_ref newest-only with mtime >
       started_at (reused-worktree stale-transcript guard, via locate_transcript).
    3. Degraded fallback (both ids None) → newest ``*.jsonl`` in project_dir
       (best-effort when backfill hasn't fired yet; may include subagent files).

    Returns None when project_dir does not exist or no jsonl is found.
    """
    if not project_dir.is_dir():
        return None
    started_at = _parse_started_at(started_at_iso)
    # Layer 1: csid exact (locate_transcript does not fall through to surface_ref)
    if claude_session_id is not None:
        result = locate_transcript(
            project_dir=project_dir,
            claude_session_id=claude_session_id,
            surface_ref=None,
            started_at=started_at,
        )
        if result is not None:
            return result
    # Layer 2: surface_ref newest-only with stale guard
    if surface_ref is not None:
        return locate_transcript(
            project_dir=project_dir,
            claude_session_id=None,
            surface_ref=surface_ref,
            started_at=started_at,
        )
    # Layer 3: degraded fallback (both ids absent — backfill hasn't fired yet)
    try:
        all_jsonl = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return all_jsonl[0] if all_jsonl else None
    except OSError:
        return None


def _matching_project_dirs(ticket_id: str) -> list[Path]:
    """Return project dirs whose name ends with ``-{ticket_id}``."""
    if not CLAUDE_PROJECTS.exists():
        return []
    return [
        p
        for p in CLAUDE_PROJECTS.iterdir()
        if p.is_dir() and p.name.endswith(f"-{ticket_id}")
    ]


def _find_transcript_heuristic(ticket_id: str) -> Path | None:
    """Heuristic fallback: score by /auto-dev prefix, pick the most recent run."""
    candidates: list[tuple[Path, int, str]] = []  # (path, score, first_user_ts)
    for proj in _matching_project_dirs(ticket_id):
        for jsonl in proj.glob("*.jsonl"):
            first_user_ts = ""
            first_user_text = ""
            with jsonl.open() as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "user":
                        continue
                    first_user_ts = d.get("timestamp", "")
                    msg = d.get("message", {})
                    contents = msg.get("content", "")
                    if isinstance(contents, str):
                        first_user_text = contents
                    elif isinstance(contents, list):
                        for c in contents:
                            if isinstance(c, dict) and c.get("type") == "text":
                                first_user_text += c.get("text", "")
                    break
            score = 0 if f"/auto-dev {ticket_id}" in first_user_text else 1
            candidates.append((jsonl, score, first_user_ts))
    if not candidates:
        return None
    # Two-pass stable sort: latest timestamp first, then score ascending.
    # Result: score=0 (main session) beats score=1 (subagents); among score=0
    # candidates across multiple runs, the most recent run's transcript wins.
    candidates.sort(key=lambda c: c[2], reverse=True)  # latest timestamp first
    candidates.sort(key=lambda c: c[1])  # score 0 before 1 (stable)
    return candidates[0][0]


def _compute_jsonl_idle_min(t: TicketTask, now: dt.datetime) -> float | None:
    """Return minutes since the newest *.jsonl in identifiable project dirs, or None.

    Best-effort liveness for blind rows (no resolvable transcript). Scans
    the project dir derived from worktree_path (from task or CW_STATE), or
    falls back to heuristic-matched dirs. Returns round(elapsed, 1) or None.

    NOTE: mtime-based, not content-based — unlike parse_transcript()'s
    last_asst_ts (entry_type == "assistant" only), this does not filter out
    away_summary records, so a quiescent session's away_summary write can
    make this look more recent than the last real assistant turn. Accepted
    as best-effort because this branch only runs when no transcript could be
    resolved at all (see A2, GH #2028).
    """
    refs = _load_session_refs(t.session_id)
    effective_wt = t.worktree_path
    if effective_wt is None:
        raw_wt = refs.get("worktree_path")
        if raw_wt is not None:
            effective_wt = Path(str(raw_wt))

    project_dirs: list[Path] = []
    if effective_wt is not None:
        project_dirs.append(claude_project_dir(effective_wt))
    else:
        project_dirs.extend(_matching_project_dirs(str(t.ticket_id)))

    newest_mtime: float | None = None
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        try:
            for p in project_dir.glob("*.jsonl"):
                mtime = p.stat().st_mtime
                if newest_mtime is None or mtime > newest_mtime:
                    newest_mtime = mtime
        except OSError:
            continue

    if newest_mtime is None:
        return None
    elapsed_seconds = (
        now - dt.datetime.fromtimestamp(newest_mtime, tz=dt.UTC)
    ).total_seconds()
    return round(elapsed_seconds / 60.0, 1)


def find_transcript_for_ticket(
    ticket_id: str,
    session_id: str | None = None,
    worktree_path: Path | None = None,
) -> Path | None:
    """Locate the main /auto-dev transcript jsonl for a ticket.

    Uses ``claude_project_dir(worktree_path)`` to find the project dir when
    a worktree path is available — this resolves correctly for dispatch workers
    whose project dirs are named after the worktree path (e.g.
    ``-home-u--cw-wt-<hash>-dev-817``) rather than containing
    ``auto-dev-{ticket_id}`` in the name.

    The effective worktree path is resolved in priority order:
    1. Explicit ``worktree_path`` parameter (USER-origin sessions that stamp it).
    2. ``worktree_path`` from the Session in CW_STATE (DAEMON-origin sessions;
       dispatch writes worktree_path to the Session but not to the TicketTask).

    Within the project dir, resolution order is: (1) exact csid match, (2)
    surface_ref-prefix glob with mtime guard, (3) newest ``*.jsonl`` (degraded
    fallback when backfill hasn't fired yet for the session ids).

    Falls back to the legacy heuristic (name-based project dir search) when
    no worktree path is available or its project dir is not found on disk.

    opencode sessions: if the worktree contains ``.cw/opencode.log``, that
    path is returned directly — opencode sessions have no claude-jsonl
    transcript (#1671 R3).

    A matched Session with both Claude ids null returns None after the opencode
    check, skipping the degraded and legacy fallbacks (#2417); no Session row
    keeps the legacy behavior.
    """
    refs = _load_session_refs(session_id)

    # Prefer explicit arg; fall back to worktree_path from the Session in CW_STATE.
    # TicketTask.worktree_path is None for dispatch tasks (dispatch stamps
    # session_id but not worktree_path); the Session object carries the real path.
    effective_wt = worktree_path
    if effective_wt is None:
        raw_wt = refs.get("worktree_path")
        if raw_wt is not None:
            effective_wt = Path(str(raw_wt))

    # opencode sessions: check for .cw/opencode.log before searching claude
    # project dirs — an opencode session has no claude-jsonl transcript.
    if effective_wt is not None:
        opencode_log = effective_wt / OPENCODE_LOG_RELATIVE_PATH
        if opencode_log.exists():
            return opencode_log

    if _is_known_idless_session(refs):
        return None

    if effective_wt is not None:
        project_dir = claude_project_dir(effective_wt)
        transcript = _find_transcript_in_project_dir(
            project_dir,
            refs.get("claude_session_id"),
            refs.get("surface_ref"),
            refs.get("started_at"),
        )
        if transcript is not None:
            return transcript

    # Legacy path: search matching project dirs by name, then heuristic.
    claude_id = refs.get("claude_session_id")
    if claude_id:
        for proj in _matching_project_dirs(ticket_id):
            candidate = proj / f"{claude_id}.jsonl"
            if candidate.exists():
                return candidate
    return _find_transcript_heuristic(ticket_id)
