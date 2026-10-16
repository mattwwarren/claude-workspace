"""Dev-queue ``resolve`` command: record a plan park's answers on the row (#2702).

The operator's decisions are appended to the row's ``operator_resolutions``
under one bounded lock (:func:`cw.dev_queue.record_operator_resolution`), then
the row is requeued by composing ``dev_queue_requeue`` through
``ctx.invoke``, so its settle, routed-orphan close, live-session guard,
``TICKET_REQUEUED`` event and row echo are inherited rather than re-built. The
two steps take the non-reentrant dev-queue lock separately, so a refused
requeue leaves the resolution recorded and says so.

The relay block printed last is an optional mirror for the tracker; the row is
what the plan stage reads.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.codex_fix_loop.constraints import RESOLUTIONS_MARKER
from cw.config import load_orchestrator_config
from cw.dev_queue import record_operator_resolution, resolve_client
from cw.dev_queue.requeue import post_tracker_comment
from cw.exceptions import CwError
from cw.gh import AGENT_COMMENT_MARKER, post_issue_comment

from ._group import dev_queue
from ._settle import settle_lagging_row
from .approve import _raise_if_settled_into_human_gate
from .crud import dev_queue_requeue

if TYPE_CHECKING:
    from cw.dev_queue.operator_resolutions import OperatorResolutionRecord

_RESOLVE_IN_WORKER_MSG = (
    "Refusing to record an operator resolution from inside a dispatch worker "
    "(.claude/cw-context.json reports headless). A resolution is binding input "
    "to the plan stage, so a worker must not answer its own park. Run "
    "`cw dev-queue resolve` from the orchestrator session."
)
# How much of a bound plan-draft fingerprint the confirmation shows.
_FINGERPRINT_DISPLAY_LEN = 12


def _read_decisions(path: Path) -> str:
    """*path*'s text; a decode or read failure is a ``CwError`` naming it."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        msg = (
            f"Cannot read decisions file {path}: it is not valid UTF-8"
            f" ({exc.reason} at byte {exc.start}). Nothing was recorded."
        )
        raise CwError(msg) from exc
    except OSError as exc:
        msg = (
            f"Cannot read decisions file {path}: could not read it"
            f" ({exc.__class__.__name__}: {exc}). Nothing was recorded."
        )
        raise CwError(msg) from exc


def _approval_text(record: OperatorResolutionRecord) -> str:
    """The confirmation's approval sentence, or ``""`` when not approving."""
    if not record["approved"]:
        return ""
    fingerprint = record["plan_approved_fingerprint"]
    if fingerprint is None:
        return (
            " Plan approval stamped (unbound: the parked session recorded no"
            " plan_draft_fingerprint, so the approval is not tied to a draft)."
        )
    return (
        " Plan approval stamped, bound to draft"
        f" {fingerprint[:_FINGERPRINT_DISPLAY_LEN]}."
    )


def _echo_record(record: OperatorResolutionRecord) -> None:
    """Print the confirmation, the #2311 drift warning and the #968 note."""
    ticket, client, number = record["ticket_id"], record["client"], record["round"]
    if record["duplicate"]:
        line = (
            f"Resolution round {number} for {ticket} ({client}) is already"
            " recorded; identical to the latest entry, nothing new recorded."
        )
    else:
        actor = record["actor"] or "<unresolved operator>"
        line = f"Recorded resolution round {number} for {ticket} ({client}) by {actor}."
    click.echo(line + _approval_text(record))
    if record["body_drift_warning"]:
        click.echo(record["body_drift_warning"], err=True)
    if record["plan_reviewed"] is False:
        click.echo(
            "Note: the plan is not yet quality-reviewed (#968); the requeued plan"
            " stage runs the review before the approval can take effect."
        )


def _recorded_note(record: OperatorResolutionRecord) -> str:
    """What a failed requeue leaves behind, and the command to finish it."""
    ticket, client, number = record["ticket_id"], record["client"], record["round"]
    requeue = f"cw dev-queue requeue {shlex.quote(ticket)} -c {shlex.quote(client)}"
    status = record["status"]
    if not record["approved"]:
        return (
            f"Resolution round {number} is recorded, but the requeue failed. The"
            f" row is still parked ({status}). Run `{requeue}` once the cause"
            " above is cleared; re-running `resolve` with the same --file"
            " records nothing new."
        )
    fingerprint = record["plan_approved_fingerprint"]
    bound = (
        f"bound to draft {fingerprint[:_FINGERPRINT_DISPLAY_LEN]}"
        if fingerprint is not None
        else "unbound"
    )
    return (
        f"Resolution round {number} is recorded and plan approval is stamped"
        f" ({bound}), but the requeue failed: the approval is stamped and the"
        f" row is still parked ({status}). Run `{requeue}` once the cause above"
        " is cleared."
    )


def _requeue(ctx: click.Context, record: OperatorResolutionRecord) -> None:
    """Requeue the row at its stage through ``dev_queue_requeue``.

    Lane and priority are untouched (``requeue_ticket`` never writes them).
    Every failure keeps its exit status and adds the recorded/still-parked
    note: a ``ClickException`` (or ``CwError``) gets it appended to its
    message, and an ``Exit``/``SystemExit`` with a non-zero code prints it to
    stderr before propagating.
    """
    note = _recorded_note(record)
    try:
        ctx.invoke(
            dev_queue_requeue,
            ticket_id=record["ticket_id"],
            client=record["client"],
            stage_override=None,
            regress=False,
            from_cancelled=False,
            from_failed=False,
            from_completed=False,
            reset_attempts=False,
        )
    except click.ClickException as exc:
        msg = f"{exc.format_message()}\n{note}"
        raise click.ClickException(msg) from exc
    except CwError as exc:
        msg = f"{exc}\n{note}"
        raise click.ClickException(msg) from exc
    except click.exceptions.Exit as exc:
        if exc.exit_code != 0:
            click.echo(note, err=True)
        raise
    except SystemExit as exc:
        if exc.code not in (0, None):
            click.echo(note, err=True)
        raise


def _relay_body(record: OperatorResolutionRecord, text: str) -> str:
    """The relay comment body, without the agent provenance marker."""
    return (
        f"### Operator resolutions (round {record['round']}, recorded via cw)\n\n"
        f"{text}\n\n{RESOLUTIONS_MARKER}"
    )


def _emit_relay_block(record: OperatorResolutionRecord, text: str) -> None:
    """Print the ready-to-post relay block, agent marker last."""
    click.echo("")
    click.echo("Relay block (post as a comment on the ticket; optional):")
    click.echo(f"{_relay_body(record, text)}\n\n{AGENT_COMMENT_MARKER}")


def _post_relay_block(record: OperatorResolutionRecord, text: str) -> None:
    """``--post``: mirror the relay block to a GitHub ticket via gh.

    Skipped for a pure duplicate (the retry after a refused requeue must not
    post a second marker-bearing comment) and on a known non-GitHub tracker.
    ``post_issue_comment`` appends the agent marker itself. A failed post
    prints to stderr and never changes the exit status: the row, not the
    comment, is what the plan stage reads.
    """
    ticket, client = record["ticket_id"], record["client"]
    if record["duplicate"] and not record["approved"]:
        click.echo(
            "Skipped --post: identical to the latest recorded entry (already"
            " mirrored or not needed)."
        )
        return
    tracker, result = post_tracker_comment(
        ticket,
        client,
        _relay_body(record, text),
        post=post_issue_comment,
    )
    if tracker is not None:
        click.echo(
            f"--post: tracker is {tracker!r}, not GitHub — the relay block is"
            f" not posted to {ticket} ({client}). The resolutions are recorded"
            " on the dev-queue row, which the plan stage reads."
        )
        return
    if result is not None and result.returncode == 0:
        click.echo(f"--post: posted the relay block to {ticket} ({client}).")
        return
    click.echo(
        f"--post: failed to post the relay block to {ticket} ({client}) — see the"
        " gh error above. The resolution is recorded on the row regardless.",
        err=True,
    )


@dev_queue.command(name="resolve")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@click.option(
    "--file",
    "decisions_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help=(
        "Path to a UTF-8 markdown file holding the operator's decisions for the"
        " open plan park. Recorded as the next operator_resolutions entry,"
        " surrounding whitespace trimmed; at most 64 KiB."
    ),
)
@click.option(
    "--approve",
    is_flag=True,
    default=False,
    help=(
        "Also stamp plan approval on the row, bound to the parked draft's"
        " fingerprint, through the same gate as `cw dev-queue approve`. Only"
        " for a row already parked at plan_pending_approval (or another"
        " approval gate): a row parked on open ambiguities is refused, so"
        " record the resolutions without --approve, let the revised draft"
        " re-park, then run `cw dev-queue approve`. A parked session with no"
        " recorded fingerprint stamps an unbound approval. The plan stage"
        " keeps the approval only if the revised draft's fingerprint is"
        " unchanged; otherwise it revokes it and asks again."
    ),
)
@click.option(
    "--post",
    is_flag=True,
    default=False,
    help=(
        "Also post the relay block as a comment on the ticket via gh"
        " (GitHub-tracked or unresolved-tracker clients only; a known"
        " non-GitHub tracker prints a note and posts nothing, and a failed"
        " post prints to stderr and exits 0). The post is an audit mirror:"
        " the ticket proceeds identically whether or not it is posted."
    ),
)
@click.pass_context
@handle_errors
def dev_queue_resolve(
    ctx: click.Context,
    ticket_id: str,
    client: str | None,
    decisions_file: Path,
    approve: bool,
    post: bool,
) -> None:
    """Record the operator's answers to a plan park on the row, then requeue it.

    TICKET must be BLOCKED_ON_USER at the plan stage. The decisions in --file
    are appended to the row's operator_resolutions, an append-only record
    that survives every requeue and is never part of the plan draft or its
    fingerprint. The next plan-stage spawn receives them in
    .claude/cw-context.json (queue_metadata.operator_resolutions) as binding
    resolutions, with the same authority as a
    <!-- auto-dev-preflight-resolutions --> comment, and the row is requeued
    at the same stage with its lane and priority unchanged. The tracker is
    neither read nor written unless --post is passed.

    Re-running with a file identical to the latest entry records nothing new
    (no duplicate entry, event or queue save) and goes on to the requeue.

    Prints the requeued row, then a ready-to-post relay block: the decisions,
    the <!-- auto-dev-preflight-resolutions --> marker and the
    <!-- cw-agent-authored --> provenance marker, for the orchestrator to
    post to its tracker with its own tool. Posting it is optional.

    --approve also stamps plan approval, but only through the gate
    `cw dev-queue approve` applies: the row must be at an approval gate, the
    plan-reviewed check (#968) and the body-drift check (#2311) run, and the
    stamp is operator-initiated. The plan stage keeps that approval only if
    the revised draft's fingerprint is unchanged.

    Refused inside a dispatch worker (the check `cw review settle` applies).

    Restart `cw dev-queue serve` before using this command: a dispatch loop
    older than the release that ships `resolve` rewrites the queue without
    the ledger and erases recorded resolutions. Detection of a stale loop is
    tracked in #2729.
    """
    refuse_inside_dispatch_worker(_RESOLVE_IN_WORKER_MSG)
    resolved = resolve_client(ticket_id, load_orchestrator_config(), client)
    outcome = settle_lagging_row(ticket_id, resolved)
    if approve:
        # #2692 R8, as `approve`: one blind invocation must not approve a row
        # settle just routed into a human-kept gate.
        _raise_if_settled_into_human_gate(outcome)
    text = _read_decisions(decisions_file).strip()
    # bounded=True (#2501): operator command. Settle may first route the row
    # under its OWN bounded lock; a timeout here records nothing.
    record = record_operator_resolution(
        ticket_id,
        resolved,
        text,
        approve=approve,
        operator_initiated=True,
        bounded=True,
    )
    _echo_record(record)
    try:
        _requeue(ctx, record)
    finally:
        # The resolution is recorded either way, so the mirror is valid even
        # when the requeue was refused.
        _emit_relay_block(record, text)
        if post:
            _post_relay_block(record, text)
