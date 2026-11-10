"""``cw dev-queue record-fix-handoff`` (#2651).

Run by the review stage's fix loop (Step 3b) once it has written the fix-agent
prompt. It records the handoff on the ticket's RUNNING row through
:func:`cw.dev_queue.fix_handoff.record_pending_fix_dispatch`. Ticket, client
and session id come from the nearest ``.claude/cw-context.json``, never from
the environment or the command line, so a model- or operator-typed identity
cannot reach the row.

Exit codes: 0 when the handoff is recorded (a row/context session mismatch
only warns); 1 when nothing was recorded; 2 for a usage error.
"""

from __future__ import annotations

from pathlib import Path

import click

from cw.cli._base import handle_errors
from cw.cli._hook_io import _context_str, find_cw_context_failopen
from cw.cli.dev_queue._group import dev_queue
from cw.dev_queue.fix_handoff import record_pending_fix_dispatch
from cw.exceptions import CwError

_CONTEXT_KEYS = ("session_id", "ticket_id", "client")


def _refuse(reason: str) -> CwError:
    msg = f"Cannot record the fix-agent handoff: {reason}. Nothing was recorded."
    return CwError(msg)


def _context_fields(cwd: Path) -> tuple[str, str, str]:
    """``(session_id, ticket_id, client)`` from the nearest context, or refuse."""
    context = find_cw_context_failopen(cwd)
    if context is None:
        reason = f"no .claude/cw-context.json found from {cwd} upward"
        raise _refuse(reason)
    values: dict[str, str] = {}
    for key in _CONTEXT_KEYS:
        value = _context_str(context, key)
        if value is None:
            reason = f"the nearest .claude/cw-context.json has no non-empty '{key}'"
            raise _refuse(reason)
        values[key] = value
    return values["session_id"], values["ticket_id"], values["client"]


def _read_prompt(prompt_file: str) -> str:
    """The prompt file's UTF-8 text; refuse when unreadable, not UTF-8 or blank."""
    try:
        raw = Path(prompt_file).read_bytes()
    except OSError as exc:
        reason = (
            f"cannot read prompt file {prompt_file} ({exc.__class__.__name__}: {exc})"
        )
        raise _refuse(reason) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        reason = (
            f"prompt file {prompt_file} is not valid UTF-8"
            f" ({exc.reason} at byte {exc.start})"
        )
        raise _refuse(reason) from exc
    if not text.strip():
        reason = f"prompt file {prompt_file} is empty"
        raise _refuse(reason)
    return text


@dev_queue.command(name="record-fix-handoff")
@click.option(
    "--cycle",
    required=True,
    type=click.IntRange(min=1),
    help="Current fix-loop cycle (1 or higher), recorded on the handoff.",
)
@click.option(
    "--prompt-file",
    "prompt_file",
    required=True,
    type=str,
    help="Path to the fix-agent prompt, normally .cw/fix-agent-prompt.md.",
)
@handle_errors
def record_fix_handoff_command(cycle: int, prompt_file: str) -> None:
    """Record the review fix-agent handoff on the ticket's row (#2651).

    Run by the review stage's fix loop (Step 3b) once it has written the
    fix-agent prompt. The ticket, client and session id come from the nearest
    ``.claude/cw-context.json``, searched from the working directory upward,
    never from the environment; the session id recorded is the one dispatch
    stamped on the row. The row must be RUNNING. The next reconcile tick
    dispatches the fix agent from the recorded prompt, replacing any handoff
    already on the row. Nothing is recorded when the context lacks a session
    id, ticket or client, the prompt file is unreadable, not UTF-8 or blank, or
    no RUNNING row matches. A row whose session id differs from the context's
    is recorded with a warning on stderr. Exit codes: 0 when the handoff is
    recorded; 1 when nothing was recorded; 2 for a usage error such as --cycle
    below 1.
    """
    session_id, ticket, client = _context_fields(Path.cwd())
    text = _read_prompt(prompt_file)
    try:
        # bounded=True (#2501): the review worker runs this in the foreground;
        # a wedged queue lock must fail it, not hang it.
        row_sid = record_pending_fix_dispatch(
            ticket,
            client,
            prompt=text,
            cycle=cycle,
            requested_by_session_id=session_id,
            bounded=True,
        )
    except OSError as exc:
        msg = (
            f"Cannot record the fix-agent handoff for ticket '{ticket}': writing"
            f" the dev-queue failed ({exc.__class__.__name__}: {exc}). Re-run the"
            " command; recording the same cycle again overwrites the same handoff."
        )
        raise CwError(msg) from exc
    if row_sid is not None and row_sid != session_id:
        click.echo(
            f"warning: row session_id '{row_sid}' != cw-context session_id"
            f" '{session_id}'; fix_dispatch's unresolved-ref park will skip this row",
            err=True,
        )
    click.echo(
        f"Recorded fix-agent handoff for ticket '{ticket}' ({client}): cycle"
        f" {cycle}, session {session_id}.",
        err=True,
    )
