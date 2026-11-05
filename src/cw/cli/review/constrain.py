"""``cw review constrain`` -- record a binding operator constraint (#2964).

The constraint text is appended to the ticket's cw dev-queue row
(:func:`cw.dev_queue.operator_constraints.record_operator_constraint`), which
the codex review prompt and the codex fix loop read on every tracker,
independent of any tracker comment, provenance marker or operator login. The
row is the source of truth; the block printed on stdout is a human-readable
mirror for the orchestrator to post with its tracker's own tool.

Steps, in the order that matters:

1. Refuse inside a headless dispatch worker: authority is where the command
   runs, not a login comparison.
2. Print the stale-dispatch-loop ``warning:``/``note:`` line (#2729): an older
   ``cw dev-queue serve`` loop rewrites the queue without the new list and
   erases recorded constraints.
3. Read the file (or stdin) as UTF-8, resolve the client, then record the
   entry under one bounded lock, event-first.
4. Print the confirmation, parse preview and row notes on stderr, then the
   mirror body on stdout.

Every failure to record the row exits non-zero and prints no mirror, on every
tracker: a mirror with no row entry behind it is exactly the channel the
codex lane drops. Constraints are append-only; there is no edit, supersede or
revoke path (retraction is tracked in #2968).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw import operator_identity
from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.cli.dev_queue._loop_warning import refuse_if_dispatch_loop_unsafe
from cw.cli.review._group import review
from cw.codex_fix_loop.constraints import constraint_rules
from cw.config import load_effective_config
from cw.dev_queue import TERMINAL_ROW_STATUSES, resolve_client
from cw.dev_queue.operator_constraints import (
    MAX_TOTAL_CONSTRAINT_BYTES,
    record_operator_constraint,
)
from cw.exceptions import CwError
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.models.operator_constraint import (
    OPERATOR_CONSTRAIN_COMMAND,
    format_recorded_at,
)

if TYPE_CHECKING:
    from cw.dev_queue.operator_constraints import OperatorConstraintRecord

_STDIN = "-"
_STDIN_LABEL = "<stdin>"
_CONSTRAIN_IN_WORKER_MSG = (
    "Refusing to record an operator constraint from inside a dispatch worker "
    "(.claude/cw-context.json reports headless). A constraint is binding input "
    "to the codex review and its fix loop, so a worker must not constrain its "
    "own reviewer. Hand the text to the orchestrator session, which runs "
    f"`{OPERATOR_CONSTRAIN_COMMAND}` outside the worker."
)
_UNRESOLVED_ACTOR_WARNING = (
    "WARNING: could not resolve your GitHub identity (gh api user failed); the "
    "constraint is recorded with actor 'unresolved' and the audit event carries "
    "actor_resolved: false. Authority comes from running this in the "
    "orchestrator session, not from the login."
)
_TERMINAL_ROW_NOTE = (
    "Note: ticket '{ticket}' only has a terminal row ({status}). A constraint "
    "recorded on it is lost if the ticket is re-enqueued (`cw dev-queue add` "
    "creates a new row and nothing is carried forward): re-run this constrain "
    "on the new row."
)
_RUNNING_ROW_NOTE = (
    "Note: ticket '{ticket}' has a RUNNING row. A codex review or fix loop "
    "already in progress loads constraints once per run and will not see this "
    "one until its next run."
)
_NO_PROHIBITION_NOTE = (
    "Note: no sentence matched a prohibition pattern (`do not add`, "
    "`don't introduce`, `must not create`, `never add`, `no new`, ...), so the "
    "fix-loop guard has nothing to enforce for this constraint; the text is "
    "still delivered verbatim to the codex review prompt and the fix prompt."
)


def _read_constraint_text(path: str) -> str:
    """The UTF-8 text at *path* (``-`` is stdin); a failure is a ``CwError``."""
    label = _STDIN_LABEL if path == _STDIN else path
    try:
        raw = (
            click.get_binary_stream("stdin").read()
            if path == _STDIN
            else Path(path).read_bytes()
        )
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = (
            f"Cannot read constraints file {label}: it is not valid UTF-8"
            f" ({exc.reason} at byte {exc.start}). Nothing was recorded."
        )
        raise CwError(msg) from exc
    except OSError as exc:
        msg = (
            f"Cannot read constraints file {label}: could not read it"
            f" ({exc.__class__.__name__}: {exc}). Nothing was recorded."
        )
        raise CwError(msg) from exc


def _row_write_failed(ticket: str, exc: OSError) -> str:
    """The refusal for a queue write that failed after the audit event."""
    return (
        f"Cannot record a constraint for ticket '{ticket}': writing the"
        f" dev-queue failed ({exc.__class__.__name__}: {exc}). The"
        f" {OrchestratorEventType.OPERATOR_CONSTRAINT_RECORDED.value} audit"
        " event was already written but no"
        " constraint is on the row; re-run constrain."
    )


def _parse_preview(text: str) -> str:
    """What the fix-loop guard will enforce for *text*."""
    rules = constraint_rules(text)
    if not rules:
        return _NO_PROHIBITION_NOTE
    tokens = sorted({t for rule in rules for t in rule.tokens})
    kinds = sorted({k.value for rule in rules for k in rule.kinds})
    return (
        f"Parsed {len(rules)} prohibition sentence(s): forbidden tokens:"
        f" {', '.join(tokens) or 'none'}; forbidden kinds:"
        f" {', '.join(kinds) or 'none'}. The fix loop parks a cycle that"
        " introduces one of them."
    )


def _echo_notes(record: OperatorConstraintRecord) -> None:
    """The confirmation, actor warning, parse preview and row notes (stderr)."""
    ticket, client = record["ticket_id"], record["client"]
    if record["duplicate"]:
        click.echo(
            f"Note: identical to constraint {record['round']} already recorded on"
            f" ticket '{ticket}'; nothing new recorded.",
            err=True,
        )
    else:
        click.echo(
            f"Recorded constraint {record['round']} for {ticket} ({client}) by"
            f" {record['actor']} ({record['total_bytes']} of"
            f" {MAX_TOTAL_CONSTRAINT_BYTES} bytes used). Constraints are"
            " append-only; retraction is tracked in #2968.",
            err=True,
        )
        if not record["actor_resolved"]:
            click.echo(_UNRESOLVED_ACTOR_WARNING, err=True)
    click.echo(_parse_preview(record["entry"].text), err=True)
    status = record["row_status"]
    if status in TERMINAL_ROW_STATUSES:
        click.echo(
            _TERMINAL_ROW_NOTE.format(ticket=ticket, status=status.value), err=True
        )
    elif status is QueueItemStatus.RUNNING:
        click.echo(_RUNNING_ROW_NOTE.format(ticket=ticket), err=True)


def _mirror_body(record: OperatorConstraintRecord) -> str:
    """The human-readable mirror comment, agent provenance marker last."""
    entry = record["entry"]
    return (
        f"### Binding operator constraint {entry.round} (recorded via"
        f" `{OPERATOR_CONSTRAIN_COMMAND}`)\n\n"
        f"{entry.text}\n\n"
        f"_Recorded on the cw dev-queue row by {entry.actor} at"
        f" {format_recorded_at(entry.recorded_at)}. The codex review and fix"
        " loop read the row, not this comment; this comment is a"
        " human-readable mirror._\n\n"
        f"{AGENT_COMMENT_MARKER}"
    )


@review.command(name="constrain")
@click.argument("ticket")
@click.option(
    "--file",
    "constraint_file",
    required=True,
    type=str,
    help=(
        "Path to a UTF-8 file holding the constraint text, or '-' for stdin."
        " Recorded verbatim (surrounding whitespace stripped) as one"
        " append-only entry."
    ),
)
@click.option(
    "--client",
    "-c",
    default=None,
    help=(
        "Client that owns the ticket's dev-queue row. Defaults to the client the"
        " ticket's prefix maps to in linear_prefix_map; required when the prefix"
        " does not resolve (GitHub issue numbers carry none)."
    ),
)
@handle_errors
def review_constrain(ticket: str, constraint_file: str, client: str | None) -> None:
    """Record binding operator constraints on the cw dev-queue row and print a
    mirror comment (#2964).

    Run it from the orchestrator session; it is refused only inside a
    headless dispatch worker. The text in --file is appended to the row's
    operator_constraints as one entry. The row is the source of truth: the
    codex review prompt and the codex fix loop read it on every tracker,
    independent of any tracker comment, provenance marker or login, and the
    fix loop parks a cycle that introduces a mechanism a "do not add `x`"
    sentence forbids.

    Constraints are append-only: there is no edit, supersede or revoke here
    (retraction is tracked in #2968). Re-running with the text of an existing
    entry records nothing new and reprints its mirror.

    The block printed on stdout is the human-readable mirror to post with
    your tracker's own tool; posting it is optional and carries no authority.
    The command fails, and prints no mirror, on every tracker when the row
    cannot be recorded. Check what is recorded with `cw dev-queue tasks --json`.

    Restart `cw dev-queue serve` after upgrading: an older dispatch loop
    rewrites the queue without the list and erases recorded constraints. A
    `warning:` line prints on stderr, and never blocks the write, when the
    running loop is stale.
    """
    refuse_inside_dispatch_worker(_CONSTRAIN_IN_WORKER_MSG)
    refuse_if_dispatch_loop_unsafe()
    text = _read_constraint_text(constraint_file)
    resolved = resolve_client(ticket, load_effective_config(), client)
    actor = operator_identity.cached_gh_login()
    try:
        # bounded=True (#2501): an operator command; a timeout records nothing.
        record = record_operator_constraint(
            ticket, resolved, text, actor=actor, bounded=True
        )
    except OSError as exc:
        raise CwError(_row_write_failed(ticket, exc)) from exc
    _echo_notes(record)
    click.echo(_mirror_body(record))
