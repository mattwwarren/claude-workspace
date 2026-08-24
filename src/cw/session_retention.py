"""Retention/archival for the hot ``sessions.json`` state file (#1983).

``load_state()`` is the hottest read path in cw: every command, every
dispatch tick, and every reconcile pass parses and validates the whole file.
Its cost is linear in the number of persisted sessions, and terminal
(COMPLETED/TIMED_OUT) sessions accumulate forever — so the file grows without
bound even though almost none of that history is ever read again.

This module archives terminal sessions older than ``_SESSION_RETENTION_DAYS``
out of ``sessions.json`` into dated cold files (``sessions.<YYYY-MM-DD>.json``,
plain JSONL), mirroring ``cw.events.prune_events``'s archive-then-truncate
shape. Reachable from the CLI as ``cw session prune``.

Paths are resolved exclusively through the ``cw.config`` accessors
(``state_dir``/``load_state``/``save_state``/``sessions_lock``), never by
importing ``STATE_DIR``/``STATE_FILE`` directly — see the accessor convention
documented in ``cw.config``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

from cw.config import load_state, save_state, sessions_lock, state_dir
from cw.dev_queue import load_dev_queue
from cw.models import TERMINAL_SESSION_STATUSES, Session
from cw.reconcile import ticket_id_for_session

if TYPE_CHECKING:
    from pathlib import Path

# Age (days) past which a terminal session is eligible for archival out of the
# hot sessions.json. 30 days comfortably outlasts any live dispatch episode
# while still bounding the file; sessions that are still referenced by a live
# dev-queue row are exempt regardless of age (see prune_sessions). #1983.
_SESSION_RETENTION_DAYS = 30


class SessionPruneResult(BaseModel):
    """Outcome of a :func:`prune_sessions` call."""

    archived_count: int
    deleted_count: int
    archive_path: str | None
    kept_count: int


def _archive_path_for_today() -> Path:
    """Return today's session archive path: ``sessions.<YYYY-MM-DD>.json``.

    Same zero-padded ISO-8601 date convention as
    ``cw.events._archive_path_for_today``, which makes the filenames sort
    lexicographically == chronologically (relied on by
    :func:`find_session_by_id`).
    """
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    return state_dir() / f"sessions.{date_str}.json"


def _archive_files_newest_first() -> list[Path]:
    """Return existing session archive files, newest date first.

    ``sessions.json`` itself never matches the glob (it has no date segment).
    """
    return sorted(state_dir().glob("sessions.*.json"), reverse=True)


def archive_file_count() -> int:
    """Return how many dated session archive files exist.

    Exposed for ``cw session list --status completed|timed_out``, which warns
    that archived sessions are deliberately not merged into its output.
    """
    return len(_archive_files_newest_first())


def _is_prunable(session: Session, *, cutoff: datetime) -> bool:
    """True when *session* is terminal and its age anchor precedes *cutoff*.

    The age anchor is ``completed_at or started_at`` — the same idiom
    ``spawn._collect_prior_attempts_summary`` sorts by. A TIMED_OUT session
    reaped without a completed_at stamp therefore still ages off its
    started_at rather than being pinned in the hot file forever.
    """
    if session.status not in TERMINAL_SESSION_STATUSES:
        return False
    anchor = session.completed_at or session.started_at
    return anchor < cutoff


def _append_to_archive(sessions: list[Session]) -> Path:
    """Append *sessions* as JSONL to today's archive file and return its path."""
    path = _archive_path_for_today()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Why: this append is not atomic with the sessions.json rewrite in
    # prune_sessions, which calls this before save_state(). A crash between
    # the two leaves the archived sessions still present in sessions.json
    # too, so a retried prune re-appends them (duplication, not loss).
    # Accepted for the same reason as the events archive (cw.events.
    # prune_events): the archive is a best-effort cold copy, not a source of
    # truth, and this is an operator-invoked, non-hot-path command.
    with path.open("a") as f:
        for session in sessions:
            f.write(session.model_dump_json() + "\n")
    return path


def prune_sessions(
    *,
    before: datetime | None = None,
    archive: bool = True,
) -> SessionPruneResult:
    """Archive terminal sessions older than *before* out of ``sessions.json``.

    Only COMPLETED/TIMED_OUT sessions are eligible; live sessions are kept
    regardless of age. A terminal session is **also** kept regardless of age
    when its ``(client, ticket_id)`` still has any row in the dev queue —
    plain presence, not filtered by that row's status. That exemption is what
    keeps ``spawn._collect_prior_attempts_summary`` complete without teaching
    it to read archives (see the comment above that function).

    Args:
        before: Prune terminal sessions whose ``completed_at or started_at``
            precedes this. Defaults to ``now - _SESSION_RETENTION_DAYS``.
        archive: When True (default), append pruned sessions to
            ``sessions.<YYYY-MM-DD>.json`` before dropping them. When False,
            discard them outright.

    Returns:
        A :class:`SessionPruneResult` describing what happened.
    """
    # Why not mutate_state: needs to return which sessions were pruned
    # (archive write + result counts) and must consult the dev-queue
    # mid-mutation; mutate_state's callback-only Callable[[CwState], None]
    # signature can do neither.
    with sessions_lock():
        state = load_state()
        queue = load_dev_queue()
        live_keys = {(t.client, t.ticket_id) for t in queue.tasks}
        cutoff = before or (datetime.now(UTC) - timedelta(days=_SESSION_RETENTION_DAYS))

        kept: list[Session] = []
        candidates: list[Session] = []
        for session in state.sessions:
            ticket = ticket_id_for_session(session.name)
            exempt = (session.client, ticket) in live_keys
            if _is_prunable(session, cutoff=cutoff) and not exempt:
                candidates.append(session)
            else:
                kept.append(session)

        archive_path: str | None = None
        if candidates and archive:
            archive_path = str(_append_to_archive(candidates))

        state.sessions = kept
        save_state(state)

    return SessionPruneResult(
        archived_count=len(candidates) if archive else 0,
        deleted_count=0 if archive else len(candidates),
        archive_path=archive_path,
        kept_count=len(kept),
    )


def _find_by_prefix(sessions: list[Session], prefix: str) -> Session | None:
    """Resolve *prefix* against *sessions*, id-matches before claude_session_id.

    Two full passes, not one interleaved pass: the old ``_resolve_session``
    (pre-#1983) checked every session's ``id`` first, and only fell back to
    ``claude_session_id`` if no ``id`` matched anywhere in the list — field
    priority decided a cross-field collision, not list position. A single
    interleaved pass would let list position decide instead, silently
    changing which session a colliding prefix resolves to (#1983 review F1).
    """
    for session in sessions:
        if session.id.startswith(prefix):
            return session
    for session in sessions:
        if session.claude_session_id and session.claude_session_id.startswith(prefix):
            return session
    return None


def find_session_by_id(prefix: str) -> Session | None:
    """Resolve *prefix* against sessions.json, then archives newest-first.

    Single-key, stop-on-first-hit point lookup — the one archive-reading
    consumer this module builds, for
    ``cw.cli.session_inspect._resolve_session``. Not a generic
    ``iter_all_sessions()`` primitive: deliberately not offered, because a
    full-scan helper would reintroduce the unbounded read this module exists
    to remove. Bounded: each archive file is read at most once per call, and
    the scan stops at the first match per collection (id-priority within
    that collection — see :func:`_find_by_prefix`).
    """
    state = load_state()
    hot_match = _find_by_prefix(state.sessions, prefix)
    if hot_match is not None:
        return hot_match

    for archive_path in _archive_files_newest_first():
        archived: list[Session] = [
            Session.model_validate(json.loads(line))
            for line in archive_path.read_text().splitlines()
            if line.strip()
        ]
        archive_match = _find_by_prefix(archived, prefix)
        if archive_match is not None:
            return archive_match
    return None
