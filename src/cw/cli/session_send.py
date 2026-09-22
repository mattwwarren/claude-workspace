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

from getpass import getuser
from pathlib import Path

import click

from cw import session_inbox
from cw.cli._base import handle_errors
from cw.cli.session_inspect import _resolve_session, session_group
from cw.events import record_event
from cw.models import TERMINAL_SESSION_STATUSES, OrchestratorEventType
from cw.session_resume_trigger import get_resume_trigger_adapter


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

    session = _resolve_session(session_ref)
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
    click.echo(f"Message {queued.id} queued for session {session.id}.")

    result = get_resume_trigger_adapter().trigger(session, queued)
    if result.delivered:
        click.echo(f"Session resumed: {result.reason}")
    else:
        click.echo(f"Warning: not delivered yet — {result.reason}", err=True)
