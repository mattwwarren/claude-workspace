"""``cw session send`` -- queue an operator message for a session (#2212).

Attaches to the existing ``session`` group defined in
:mod:`cw.cli.session_inspect`, the way :mod:`cw.cli.session_prune` does,
rather than living inside it: that module's docstring scopes it to
inspection, and ``send`` is a mutation.

Exit-code contract: **exit 0 whenever the message was durably queued**, even
when the resume trigger declines or fails. Queuing, not delivery, is this
command's reliability bar -- and until #2255 lands a gate refusal is the
common case for a live session, so treating it as an error would make the
command look broken on every such attempt. Exit 1 is reserved for failures
where nothing was queued at all: unknown session, terminal session, bad
flags, inbox write failure.
"""

from __future__ import annotations

import logging
from getpass import getuser
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw import session_inbox
from cw.cli._base import handle_errors
from cw.cli.session_inspect import session_group
from cw.config import load_state
from cw.events import record_event
from cw.models import TERMINAL_SESSION_STATUSES, OrchestratorEventType
from cw.session_resume_trigger import get_resume_trigger_adapter
from cw.session_retention import find_session_by_id

if TYPE_CHECKING:
    from cw.models import CwState, Session

logger = logging.getLogger(__name__)


def _resolve_body(message: str | None, message_file: Path | None) -> str:
    """Return the message body, or raise a UsageError on a bad flag pair."""
    if message is not None and message_file is not None:
        msg = "Pass exactly one of --message or --message-file, not both."
        raise click.UsageError(msg)
    if message is not None:
        body = message
    elif message_file is not None:
        body = message_file.read_text()
    else:
        msg = "Pass exactly one of --message or --message-file."
        raise click.UsageError(msg)
    body = body.strip()
    if not body:
        msg = "Message body is empty."
        raise click.UsageError(msg)
    return body


def _ambiguous_matches(state: CwState, prefix: str) -> list[Session]:
    """Return every session an id-or-claude_session_id prefix resolves to.

    ``find_session_by_id`` -> ``_find_by_prefix``
    (``session_retention.py:173-189``) returns only the *first* match, with
    no ambiguity check -- fine for its existing read/spawn-resolution
    consumers, but ``send`` is the first *mutating* one, and a colliding
    prefix would silently deliver to whichever session sorts first. Mirrors
    ``_find_by_prefix``'s own two-pass, id-then-claude_session_id field
    priority so ambiguity is judged within the same field that would decide
    the match, but returns every hit instead of stopping at one.

    Deliberately reimplemented here rather than extending the shared
    helper: ``_find_by_prefix`` has consumers outside this ticket's approved
    blast radius (``session_inspect.py`` show/list/wait/result, ``cw.spawn``,
    ``fix_agent``'s parent resolution) and changing its first-match
    semantics under them is its own ticket, not this one (GitHub #2212
    review). Scoped to the hot ``sessions.json`` state only -- unlike
    ``find_session_by_id`` this does not fall back to scanning archives,
    since a mutating command's targets are live sessions, not archived ones.

    Takes an already-loaded *state* rather than reloading, so the caller can
    reuse the same snapshot for both this check and the actual resolution
    below (``find_session_by_id(prefix, state=state)``) instead of reading
    ``sessions.json`` twice and judging ambiguity and identity against two
    different points in time (#2212 review round 4, finding 3).
    """
    id_matches = [s for s in state.sessions if s.id.startswith(prefix)]
    if id_matches:
        return id_matches
    return [
        s
        for s in state.sessions
        if s.claude_session_id and s.claude_session_id.startswith(prefix)
    ]


@session_group.command(name="send")
@click.argument("session_ref")
@click.option("--message", "-m", default=None, help="Message body, inline.")
@click.option(
    "--message-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the message body from this file instead of --message.",
)
@click.option(
    "--author",
    default=None,
    help="Audit-trail author. Defaults to the local OS username.",
)
@handle_errors
def session_send(
    session_ref: str,
    message: str | None,
    message_file: Path | None,
    author: str | None,
) -> None:
    """Queue a message for one session and wake it if it is paused.

    SESSION_REF is a short session id or claude_session_id prefix.

    The message is appended to the session's durable inbox first, then a
    resume is attempted. A session that is genuinely live and mid-task cannot
    be woken yet (deferred to #2255): the message still queues, and the
    command still exits 0, with the reason printed as a warning.
    """
    body = _resolve_body(message, message_file)

    # Single load, reused for both the ambiguity check and the actual
    # resolution below -- two separate reads previously judged ambiguity
    # and identity against two different snapshots, a genuine TOCTOU window
    # (#2212 review round 4, finding 3).
    state = load_state()
    candidates = _ambiguous_matches(state, session_ref)
    if len(candidates) > 1:
        ids = ", ".join(sorted(s.id for s in candidates))
        click.echo(
            f"Ambiguous session reference {session_ref!r} matches: {ids}."
            " Use a longer prefix.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    # find_session_by_id's archive fallback still applies on a hot-state
    # miss, so a prefix matching only an archived terminal session still
    # gets the friendlier "will never read an inbox message" message below
    # instead of a generic "not found" (#2212 review round 4, finding 3
    # caveat).
    session = find_session_by_id(session_ref, state=state)
    if session is None:
        click.echo(f"Session not found: {session_ref}", err=True)
        raise click.exceptions.Exit(1)
    if session.status in TERMINAL_SESSION_STATUSES:
        click.echo(
            f"Session {session.name} is {session.status.value}; it will never"
            " read an inbox message.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    queued = session_inbox.append_message(
        session.id, author=author or getuser(), body=body
    )
    # The message is already durably queued at this point (exit-code contract
    # above): a failure recording the observability event must not turn a
    # successfully queued message into a command failure (#2212 review
    # finding 4). handle_errors only translates CwError, so an OSError from
    # the event write would otherwise propagate uncaught.
    try:
        record_event(
            OrchestratorEventType.SESSION_MESSAGE_SENT,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "message_id": queued.id,
                "author": queued.author,
            },
        )
    except OSError as exc:
        logger.warning(
            "session send: failed to record SESSION_MESSAGE_SENT event for"
            " message %s: %s",
            queued.id,
            exc,
        )
    click.echo(f"Message {queued.id} queued for session {session.id}.")

    result = get_resume_trigger_adapter().trigger(session, queued)
    if result.delivered:
        click.echo(f"Session resumed: {result.reason}")
    else:
        click.echo(f"Warning: not delivered yet — {result.reason}", err=True)
