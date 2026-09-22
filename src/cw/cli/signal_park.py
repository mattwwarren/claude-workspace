"""The ``cw signal-park`` park-comment stamp command (GitHub #2135).

The PRODUCER half of the Stop-hook abandoned-exit park. A headless worker runs
it once, from its session worktree root, after its park comment has posted and
immediately before it emits its exit sentinel; the CONSUMER is
``cw signal-stop`` (``cli/stop_hook.py``), which reads the marker back on the
next Stop and may park the dev-queue row when no sentinel landed.

Best-effort and fail-open throughout, mirroring ``cw agent-spawn-pre`` and
``cw guard-cwd``: an unresolvable current directory, no readable context, no
string ids, no RUNNING row, an unreadable dev queue, or a contended context
lock each print one reason line to stderr, write nothing, and exit 0. A worker
MUST be able to ignore this call entirely -- including an unknown-command error
from a ``cw`` that predates it -- and still emit the sentinel it was already
going to emit. The whole feature is worth less than one wrongly-diverted exit,
so every failure here costs at most a Stop-hook deferral, which is what
happened before #2135 anyway.

It emits no event and writes no audit row: the marker is fallback evidence for
exactly one Stop-hook decision, and the parked row already emits
``SESSION_NEEDS_ATTENTION`` carrying the disposition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click

from cw.cli._base import main
from cw.cli._hook_io import _read_cw_context, _write_cw_context_locked
from cw.models import PARK_COMMENT_MARKER_KEY, ParkCommentMarker
from cw.reconcile import find_running_task_for_session

__all__ = ["signal_park"]

# The five reasons a stamp can fail, verbatim. Six failure CASES map onto
# five strings: ``find_running_task_for_session`` (shared with the Stop hook)
# returns None for both an absent row and an unreadable dev queue, and the
# unreadable-queue case is already distinguished by its own WARNING on stderr,
# so a tri-state on a shared helper would buy one diagnostic line.
_NO_CWD = "could not resolve the current directory"
_NO_CONTEXT = "no readable .claude/cw-context.json in the current directory"
_NO_IDS = "cw-context.json carries no string session_id and ticket_id"
_NO_ROW = "no RUNNING dev-queue row for this session (or the dev queue is unreadable)"
_NO_WRITE = "could not write cw-context.json (locked, missing or malformed)"


def _record_park_marker(cwd_value: str) -> ParkCommentMarker | str:
    """Stamp the marker for *cwd_value*'s session; a ``str`` is the failure reason.

    Every id in the marker comes from cw's own state -- the context file and
    the RUNNING dev-queue row -- never from the caller, which is why the
    command takes no arguments: a stage typed into a stage doc could otherwise
    drift from the row the Stop hook compares against.
    """
    context = _read_cw_context(cwd_value)
    if context is None:
        return _NO_CONTEXT
    session_id = context.get("session_id")
    ticket_id = context.get("ticket_id")
    if (
        not isinstance(session_id, str)
        or not isinstance(ticket_id, str)
        or not session_id
        or not ticket_id
    ):
        return _NO_IDS
    task = find_running_task_for_session(ticket_id, session_id)
    if task is None:
        return _NO_ROW
    marker = ParkCommentMarker(
        ticket_id=ticket_id,
        stage=task.stage,
        session_id=session_id,
        posted_at=datetime.now(UTC),
    )
    payload = marker.model_dump(mode="json")
    written = _write_cw_context_locked(
        cwd_value,
        lambda ctx: {**ctx, PARK_COMMENT_MARKER_KEY: payload},
    )
    return marker if written else _NO_WRITE


@main.command(name="signal-park")
def signal_park() -> None:
    """Record the worker's park comment for the Stop hook (#2135).

    Run once from the worktree root, after the park comment has posted and
    immediately before emitting the exit sentinel. Writes ``park_comment_marker``
    (ticket id, the RUNNING row's stage, cw session id, UTC ``posted_at``) into
    ``.claude/cw-context.json``. The marker is the worker's own recorded claim
    that it posted its park comment and is taking that exit -- not an observation
    by cw that anything was posted. If the turn then ends without
    the sentinel landing, ``cw signal-stop`` may park the row as
    ``stopped_without_sentinel`` (only when ``park_on_abandoned_exit_enabled`` is
    armed for its lane); otherwise it defers exactly as before.

    Best-effort and fail-open: it takes no arguments, exits 0 on every failure it
    can foresee, and on any failure prints ``park marker NOT recorded: <reason>``
    to stderr and writes nothing. Callers must ignore any failure (including a
    non-zero exit or an unknown-command error from an older ``cw``) and still emit
    their sentinel unchanged. A worker that dies between deciding its exit and
    this call leaves no marker, and the Stop hook defers (accepted limitation).
    Run it from the directory holding ``.claude/cw-context.json``; from a
    subdirectory it finds no context and fails open.
    """
    # ``Path.cwd()`` raises when the worktree directory has been removed out
    # from under the process; ``guard_cwd`` wraps its body for the same reason.
    # Only this call is guarded, so an unrelated ``OSError`` is never reported
    # under the cwd reason string.
    try:
        cwd_value = str(Path.cwd())
    except OSError:
        outcome: ParkCommentMarker | str = _NO_CWD
    else:
        outcome = _record_park_marker(cwd_value)
    if isinstance(outcome, str):
        click.echo(
            f"park marker NOT recorded: {outcome}; the Stop hook will defer",
            err=True,
        )
        return
    click.echo(
        f"park marker recorded (ticket {outcome.ticket_id},"
        f" stage {outcome.stage.value})"
    )
