"""Session transcript location, timestamps and record iteration.

Resolves a session's Claude transcript (``_locate_session_transcript`` and
its project-dir helpers), derives its effective / widened activity timestamp
and age, and iterates its parsed records (all, assistant-only, and
queue-operation notifications). Imports ``_constants``. Split out of the flat
``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw._transcript import locate_transcript
from cw._util import (
    _last_content_entry_timestamp,
    _parse_transcript_record,
    _TranscriptRecord,
    claude_project_dir,
)
from cw.reconcile._shared._constants import (
    _LOGGER_NAME,
    _QUEUE_OPERATION_RECORD_TYPE,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cw.models import Session

_log = logging.getLogger(_LOGGER_NAME)


class _TranscriptRecordIterator:
    """Forward transcript iterator with an explicit incomplete-scan marker."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None
        self._done = False
        self.scan_complete = True

    def __iter__(self) -> _TranscriptRecordIterator:
        return self

    def __next__(self) -> _TranscriptRecord:
        if self._done:
            raise StopIteration
        if self._handle is None:
            try:
                self._handle = self._path.open(encoding="utf-8", errors="replace")
            except OSError:
                self.scan_complete = False
                self._done = True
                raise StopIteration from None
        while True:
            try:
                line = next(self._handle)
            except StopIteration:
                self._handle.close()
                self._done = True
                raise
            except OSError:
                self.scan_complete = False
                self._handle.close()
                self._done = True
                raise StopIteration from None
            try:
                record = _parse_transcript_record(line)
            except json.JSONDecodeError:
                self.scan_complete = False
                continue
            if record is not None:
                return record


def _iter_transcript_records(path: Path) -> _TranscriptRecordIterator:
    """Yield parsed transcript records while exposing whether EOF was reached."""
    return _TranscriptRecordIterator(path)


def _iter_assistant_records(path: Path) -> Iterator[tuple[datetime | None, str]]:
    """Yield ``(timestamp, text)`` for each assistant record in a jsonl transcript."""
    for record in _iter_transcript_records(path):
        if (
            record.record_type == "assistant"
            and record.content_bearing
            and record.text is not None
        ):
            yield record.timestamp, record.text


def _iter_notification_records(path: Path) -> Iterator[str]:
    """Yield text from task-notification records in a jsonl transcript (#1923).

    Unlike :func:`_iter_assistant_records`, which requires ``type ==
    "assistant"`` and a list-shaped ``message.content``, this yields text
    from two different record shapes -- both lifted verbatim from a live
    capture (dev-1751 impl worker, session
    286032f7-47ee-4985-a45d-e7a946aa1d9d): (a) ``type == "user"`` records
    whose ``message.content`` is a bare string, and (b) ``type ==
    "queue-operation"`` records whose top-level ``content`` is a bare
    string. Swallows ``OSError`` like ``_iter_assistant_records``; yields
    nothing on any read error.
    """
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = record.get("type")
                if record_type == "user":
                    message = record.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            yield content
                elif record_type == _QUEUE_OPERATION_RECORD_TYPE:
                    content = record.get("content")
                    if isinstance(content, str):
                        yield content
    except OSError:
        return


def _session_project_dir(session: Session) -> Path | None:
    """Return the Claude project dir for *session*, or None if worktree path unset."""
    worktree = session.worktree_path
    if worktree is None:
        return None
    return claude_project_dir(worktree)


def _newest_surface_ref_transcript(project_dir: Path, session: Session) -> Path | None:
    """Return the newest ``<surface_ref>*.jsonl`` newer than session start, else None.

    Thin wrapper — delegates to ``locate_transcript`` for surface_ref-only
    resolution. Caller guarantees ``surface_ref`` is set.
    """
    return locate_transcript(
        project_dir=project_dir,
        claude_session_id=None,
        surface_ref=session.surface_ref,
        started_at=session.started_at,
    )


def _locate_session_transcript(session: Session) -> Path | None:
    """Return the session's transcript path, or None if not locatable.

    Thin wrapper — unpacks the Session and delegates to ``locate_transcript``.
    Resolution order:
    1. ``claude_session_id`` set and ``<project_dir>/<csid>.jsonl`` exists →
       return that path directly (mtime guard not needed; csid is exact).
    2. ``surface_ref`` set → newest ``<project_dir>/<surface_ref>*.jsonl``
       with mtime strictly after ``session.started_at``, else None
       (reused-worktree stale-transcript guard, #358/#372).
    3. No project_dir, or neither identifier set → None.
    """
    return locate_transcript(
        project_dir=_session_project_dir(session),
        claude_session_id=session.claude_session_id,
        surface_ref=session.surface_ref,
        started_at=session.started_at,
    )


def _csid_from_transcript(session: Session) -> str | None:
    """Return claude_session_id from the transcript filename, or None.

    Thin wrapper around :func:`_locate_session_transcript`.  The transcript
    is named ``<project_dir>/<full-csid>.jsonl`` where
    ``full-csid[:8] == surface_ref``; the stem is therefore the full csid.
    """
    path = _locate_session_transcript(session)
    if path is None:
        return None
    csid = path.stem
    _log.debug(
        "Resolved claude_session_id=%s for session %s via transcript fallback",
        csid,
        session.id,
    )
    return csid


def _effective_transcript_timestamp(transcript: Path) -> datetime:
    """Return the timestamp to use for liveness checks on *transcript*.

    Prefers the timestamp of the last *content-bearing* transcript entry
    (:func:`cw._util._last_content_entry_timestamp`) over the file's mtime,
    so a trailing metadata-only write (e.g. an ``ai-title`` record) does not
    falsely resurrect liveness or understate idle age (GitHub #1076). Falls
    back to mtime, unchanged from prior behavior, when no content entry has
    a parseable timestamp. Raises ``OSError`` if the file cannot be stat'd —
    callers are expected to catch it, matching existing fail-open behavior.
    """
    content_ts = _last_content_entry_timestamp(transcript)
    if content_ts is not None:
        return content_ts
    return datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)


def _project_transcripts_latest_timestamp(session: Session) -> datetime | None:
    """Return the max effective timestamp across ALL transcripts in the project dir.

    Widens the single-file csid/surface_ref resolution (#1283): a subagent's own
    transcript in the same project dir has a filename that matches neither the
    csid nor the ``<surface_ref>*`` prefix, so :func:`_locate_session_transcript`
    is blind to it even while it carries fresh activity — leaving a worker that
    is mid-subagent-delegation for >``TRANSCRIPT_LIVENESS_WINDOW_SECONDS`` (its
    own tail quiet) to look dead. Globs every ``*.jsonl`` under the project dir
    and returns the max :func:`_effective_transcript_timestamp` across files
    whose mtime is strictly after ``session.started_at`` — the same reused-
    worktree stale-transcript guard (#358/#372) that ``locate_transcript``
    applies to the surface_ref candidate, here applied per-file across the whole
    glob so a leftover prior-session transcript never counts. Fails open
    (``None``) on missing dir / ``OSError``, matching every sibling helper.

    Why no filename filter beyond the mtime guard: a worktree's project dir is
    reused sequentially per-ticket, never shared concurrently across unrelated
    tickets, so ``mtime > started_at`` alone bounds the glob to this session's
    lineage.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return None
    max_ts: datetime | None = None
    for candidate in project_dir.rglob("*.jsonl"):
        # Per-candidate: a stat/read failure on one sibling (deleted/rotated
        # mid-glob) must not discard max_ts already found from other, valid
        # siblings -- only that one candidate is skipped.
        try:
            mtime = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if mtime <= session.started_at:
                continue
            ts = _effective_transcript_timestamp(candidate)
        except OSError:
            continue
        if max_ts is None or ts > max_ts:
            max_ts = ts
    return max_ts


def _widened_transcript_timestamp(session: Session) -> datetime | None:
    """Return the freshest liveness timestamp for *session*, or None (#1283).

    Takes the max of the single-file csid/surface_ref resolution
    (:func:`_locate_session_transcript` + :func:`_effective_transcript_timestamp`)
    and :func:`_project_transcripts_latest_timestamp` (the sibling-transcript
    glob). Monotonic widening — never reports a session as *more* stale than the
    registered transcript alone would. The two sources are computed
    independently: a stat failure on the registered transcript alone does not
    prevent the sibling-glob fallback from being consulted, so the widened
    signal fix (b) exists to add is never discarded by an unrelated primary-file
    error. Fails open (``None``) if both sources are unavailable.
    """
    best_ts: datetime | None = None
    transcript = _locate_session_transcript(session)
    if transcript is not None:
        try:
            best_ts = _effective_transcript_timestamp(transcript)
        except OSError:
            best_ts = None
    project_ts = _project_transcripts_latest_timestamp(session)
    if project_ts is not None and (best_ts is None or project_ts > best_ts):
        best_ts = project_ts
    return best_ts


def _transcript_recently_active(
    session: Session,
    now: datetime,
    *,
    window_seconds: int = TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
) -> bool:
    """Return True if the transcript shows activity within *window_seconds* ago.

    Uses :func:`_widened_transcript_timestamp` — the max of the precise
    per-session lookup (surface_ref-prefix glob, #541) and any fresher sibling
    subagent transcript in the same project dir (#1283).  Returns False —
    permitting the watchdog to proceed — when no transcript is found
    (pre-first-write or path unavailable).  See GitHub #340.
    """
    try:
        ts = _widened_transcript_timestamp(session)
        if ts is None:
            return False
        return (now - ts).total_seconds() < window_seconds
    except OSError:
        return False


def _transcript_age_seconds(
    session: Session,
    now: datetime,
) -> float | None:
    """Return seconds since the session's transcript last showed activity, or None.

    Returns None when no transcript file can be located.  Uses
    :func:`_widened_transcript_timestamp` — the max of the precise per-session
    lookup (surface_ref-prefix glob, #541) and any fresher sibling subagent
    transcript in the same project dir (#1283).
    """
    try:
        ts = _widened_transcript_timestamp(session)
        if ts is None:
            return None
        return (now - ts).total_seconds()
    except OSError:
        return None
