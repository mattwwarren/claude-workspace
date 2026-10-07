"""The #2135 abandoned-exit park for a sentinel-less headless Stop.

The cost-ordered preconditions (RUNNING row, park flag, the worker's own
``park_comment_marker``) and the transcript's negative evidence that decide
whether a Stop with no sentinel parks its ticket's row. Ships dark: with the
park flag off it defers exactly as before #2135. Imports nothing from its
siblings. Split out of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw._util import claude_project_dir
from cw.cli._sentinels import _sentinel_frame_after
from cw.models import read_park_comment_marker
from cw.reconcile import (
    _route_stopped_without_sentinel,
    find_running_task_for_session,
    park_gate_open,
)

if TYPE_CHECKING:
    from cw.models import ParkCommentMarker, Session, TicketTask


def _armed_running_task(session: Session, ticket_id: str) -> TicketTask | None:
    """The RUNNING row the #2135 park may fire for, or ``None``.

    The Stop hook fires at **every** main-agent turn boundary, so the park's
    evidence -- and above all the transcript walk whose cost grows with session
    length -- must not be read on every turn, and neither should a config file.
    The preconditions are therefore ordered by cost:

    1. headless DAEMON session -- already established, since ``signal_stop``
       returns before this path otherwise. ``background_tasks`` is empty on
       this path too, with one narrow exception since #2458: a Stop with
       pending background work reaches here only when the session holds a
       staged emit_cli result that failed reconstruction and the transcript
       carries no sentinel either;
    2. the RUNNING dev-queue row this session owns (one lock-free
       ``dev_queue.json`` read). No row, or a row that is not RUNNING, means
       there is nothing to park;
    3. the park flag for that row (:func:`park_gate_open`): the master switch
       plus the per-lane / per-ticket resolution. Memoized per process and
       fail-closed -- an unreadable config, an unknown client, an undeclared
       lane or an absent lane entry all read as disabled, and none of them
       raises out of the hook.

    Returns the row rather than a bool because the caller needs its ``stage``:
    that is the one field of the marker checked against a source independent of
    the marker's own file. With the park disabled -- the shipped default -- a
    sentinel-less Stop costs the row lookup and at most one config resolution,
    and behaves exactly as the pre-#2135 unconditional defer.
    """
    task = find_running_task_for_session(ticket_id, session.id)
    return task if task is not None and park_gate_open(task) else None


def _sentinel_frame_follows_marker(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
    marker: ParkCommentMarker,
) -> bool:
    """Whether a sentinel frame appears after *marker* was stamped (#2135).

    True suppresses the park. Both candidate transcripts are read -- the hook's
    ``cwd`` project dir and the session's recorded ``worktree_path`` project dir
    (issue #799, for an EnterWorktree-shifted cwd). That is the same pair
    ``_parse_headless_sentinel`` searches, but not the same stopping rule: it
    falls back to the second location whenever the first yields no *sentinel*,
    so a first transcript that exists yet carries no frame must not end this
    search either. A frame is a hit wherever it lands.

    Returns True on the first frame hit or read failure, and False only once
    every transcript that exists has been read clean. A missing Claude session
    id, or no transcript in either location, also returns True: without the
    transcript a late frame cannot be ruled out, and the cost of being wrong
    that way is a silent non-park rather than a park that hides a real blocker
    reason behind the wrong disposition.
    """
    if not isinstance(claude_session_id, str) or not claude_session_id:
        return True
    search_dirs = [cwd_value]
    if session.worktree_path is not None:
        search_dirs.append(str(session.worktree_path))
    found_transcript = False
    # ``dict.fromkeys`` drops a repeated directory (the common case: the hook's
    # cwd IS the worktree) so the hot path never reads the same file twice.
    for search_dir in dict.fromkeys(search_dirs):
        transcript_path = claude_project_dir(search_dir) / f"{claude_session_id}.jsonl"
        if not transcript_path.is_file():
            continue
        found_transcript = True
        if _sentinel_frame_after(transcript_path, marker.posted_at):
            return True
    return not found_transcript


def _park_if_abandoned(
    session: Session,
    context: dict[str, object],
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object,
) -> bool:
    """Park the ticket's row when this Stop looks like an abandoned exit (#2135).

    Returns True when the park was attempted -- every precondition held and
    ``_route_stopped_without_sentinel`` ran, which pages on its own -- so the
    caller does not page ``sentinel_unroutable`` a second time (#2458). False
    on every fail-closed early return.

    Called only after the sentinel parse has already returned ``None``. The
    park ships **dark**: with ``park_on_abandoned_exit_enabled`` false (the
    default) :func:`_armed_running_task` returns ``None`` before the marker is
    read or any transcript is opened, and the caller defers exactly as it did
    before #2135.

    The evidence is the worker's own ``park_comment_marker``, written by ``cw
    signal-park`` after its park comment posted -- a RECORDED CLAIM by the
    producer, never an observation by cw that a tracker comment exists. It must
    cover this session id, this ticket id and the RUNNING row's stage; a
    malformed marker is an absent marker.

    The transcript is then consulted for NEGATIVE evidence only
    (:func:`_sentinel_frame_follows_marker`): a frame marker at or after the
    stamp suppresses the park, because a truncated, unpaired or placeholder
    frame parses to ``None`` and would otherwise be stamped
    ``stopped_without_sentinel``, hiding the worker's real blocker reason. It
    can never cause a park, and any read or parse trouble counts as a frame.

    Order (cheapest first): ticket id, RUNNING row, park flag, marker (an
    in-memory lookup in the context dict the hook already parsed), transcript.
    Fail-closed at every step -- each returns without parking, and none raises.

    Accepted limitation: a worker that dies between deciding its exit and
    running the stamp leaves no marker, and this defers exactly as it did
    before #2135.
    """
    if not isinstance(ticket_id_value, str) or not ticket_id_value:
        return False
    task = _armed_running_task(session, ticket_id_value)
    if task is None:
        return False
    marker = read_park_comment_marker(context)
    if marker is None or not marker.covers(
        session_id=session.id, ticket_id=ticket_id_value, stage=task.stage
    ):
        return False
    if _sentinel_frame_follows_marker(session, cwd_value, claude_session_id, marker):
        return False
    _route_stopped_without_sentinel(ticket_id_value, session)
    return True
