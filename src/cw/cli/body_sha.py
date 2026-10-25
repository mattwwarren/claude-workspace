"""The ``cw body-sha`` command (#2761).

Prints :func:`cw.plan_fingerprint.compute_body_sha` of a ticket body, so a
plan-stage worker persists the exact ``body_sha`` that ``cw dev-queue
approve``'s drift check recomputes instead of hashing the body by hand. Two
mutually exclusive inputs: a GitHub TICKET fetched through
``fetch_issue_body``, or ``--body-file PATH`` (``-`` for stdin) holding a body
the caller fetched itself -- the Linear path.

Fails closed on every error (exit 1, nothing on stdout): unlike approve's
advisory check, a worker must never persist a fabricated or empty hash.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cw.cli._base import handle_errors, main
from cw.config import get_client
from cw.dev_queue.lifecycle import _tracker_allows_github_fetch
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_body
from cw.plan_fingerprint import compute_body_sha
from cw.worktree import _git_dir

__all__ = ["body_sha"]

_STDIN_PATH = "-"


def _resolve_body(
    ticket: str | None, client_name: str | None, body_file: str | None
) -> str:
    """Validate the inputs and return the body they name.

    Exactly one of TICKET or ``--body-file`` is required, and ``-c`` must
    accompany TICKET.
    """
    if ticket is None:
        if body_file is None:
            msg = "give a TICKET (with -c/--client) or --body-file PATH"
            raise click.ClickException(msg)
        return _read_body_file(body_file)
    if body_file is not None:
        msg = "give a TICKET or --body-file, not both"
        raise click.ClickException(msg)
    if client_name is None:
        msg = "a TICKET needs -c/--client to resolve its repo and tracker"
        raise click.ClickException(msg)
    return _fetch_ticket_body(ticket, client_name)


def _read_body_file(path: str) -> str:
    """Read *path* (``-`` = stdin) as bytes and decode UTF-8, untranslated.

    Bytes, not text mode, so a CRLF body is hashed exactly as written.
    """
    try:
        if path == _STDIN_PATH:
            raw = sys.stdin.buffer.read()
        else:
            raw = Path(path).read_bytes()
    except OSError as exc:
        msg = f"cannot read --body-file {path}: {exc}"
        raise click.ClickException(msg) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"--body-file {path} is not valid UTF-8: {exc}"
        raise click.ClickException(msg) from exc


def _fetch_ticket_body(ticket: str, client_name: str) -> str:
    """Fetch *ticket*'s raw body from GitHub for *client_name*.

    Refuses a client whose tracker is positively non-GitHub
    (``fetch_issue_body`` is ``gh``-only) and a failed fetch.
    """
    cfg = get_client(client_name)
    if not _tracker_allows_github_fetch(cfg):
        msg = (
            f"client {client_name!r} does not track tickets on GitHub; write the"
            " body to a file and run `cw body-sha --body-file PATH`"
        )
        raise click.ClickException(msg)
    ticket_id = ticket.lstrip("#")
    body = fetch_issue_body(ticket_id, FETCH_COMMENTS_TIMEOUT, cwd=_git_dir(cfg))
    if body is None:
        msg = (
            f"could not fetch the body of ticket {ticket_id} via gh; no body_sha"
            " was produced"
        )
        raise click.ClickException(msg)
    return body


@main.command(
    "body-sha",
    help=(
        "Print the body_sha of a ticket body: `cw body-sha TICKET -c CLIENT` "
        "fetches it from GitHub, `cw body-sha --body-file PATH` (- for stdin) "
        "reads it from a file."
    ),
)
@click.argument("ticket", required=False)
@click.option(
    "--client",
    "-c",
    "client_name",
    default=None,
    help="Client whose repo and tracker TICKET is fetched from.",
)
@click.option(
    "--body-file",
    "body_file",
    type=click.Path(path_type=str),
    default=None,
    help="Hash the body in this file instead of fetching (- reads stdin).",
)
@handle_errors
def body_sha(
    ticket: str | None, client_name: str | None, body_file: str | None
) -> None:
    """Print compute_body_sha of TICKET's body, or of --body-file's contents.

    The value is what `cw dev-queue approve` recomputes for its ticket-body
    drift check; persist it verbatim. Exits 1 with nothing on stdout when the
    body cannot be obtained.
    """
    body = _resolve_body(ticket, client_name, body_file)
    click.echo(compute_body_sha(body))
