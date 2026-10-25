"""Dev-queue task inspection + repo refresh: tasks, refresh-all, attention helpers.

``_count_needs_attn`` / ``_needs_attn_by_client`` also back the aggregate
``dev-queue status`` view (imported by ``status`` — a deliberate cross-submodule
dependency).
"""

from __future__ import annotations

import json

import click
import yaml

from cw.auto_dev_result import is_known_blocker_reason
from cw.cli._base import handle_errors, print_fixed_width_table
from cw.config import load_clients, load_state
from cw.dev_queue import (
    load_dev_queue,
    session_attention_state,
    task_attention_state,
)
from cw.dev_queue.next_commands import (
    HUMAN_GATE_CELL_LABEL,
    compact_command,
    is_human_gate_command,
    next_commands,
)
from cw.exceptions import CwError, MissingWorkspaceError, WorktreeError
from cw.models import QueueItemStatus, Session, TicketTask
from cw.reconcile._shared import resolve_attempt_ceiling
from cw.ticket_view import ViewSources, next_command_facts
from cw.worktree import fast_forward_main

from ._group import dev_queue


def _task_to_dict(task: TicketTask) -> dict[str, object]:
    """Full TicketTask field set for `tasks --json` (GitHub #1618)."""
    return task.model_dump(mode="json")


def _count_needs_attn(tasks: list[TicketTask]) -> int:
    """Count tasks whose hydrated PR state carries a non-null attention_state."""
    return sum(1 for t in tasks if task_attention_state(t) is not None)


def _needs_attn_by_client(tasks: list[TicketTask]) -> dict[str, int]:
    """Map client -> count of tasks needing attention (non-null attention_state)."""
    counts: dict[str, int] = {}
    for t in tasks:
        if task_attention_state(t) is not None:
            counts[t.client] = counts.get(t.client, 0) + 1
    return counts


def _reason_cell(blocked_reason: str | None, advisory_note: str | None) -> str:
    """Render the REASON column, flagging an unregistered reason (#2097).

    A ``?`` **prefix** rather than a suffix so the flag survives the column's
    20-char truncation -- a reason long enough to be cut is exactly the one an
    operator is most likely to mistake for a documented routing code. Advisory
    only: ``blocker.reason`` stays an open enum and the value is still shown
    verbatim (see ``cw.auto_dev_result.is_known_blocker_reason``).

    ``blocked_reason`` always wins (#1762). It is stamped only on terminal
    transitions, so it and ``advisory_note`` -- which describes a *non*-terminal
    RUNNING row -- should never be set at once; if a stale note survives anyway,
    the row's actual park reason is the more important of the two. The note is
    rendered verbatim: its own ``?`` marker is baked into the stored token, not
    added here.
    """
    if blocked_reason:
        if is_known_blocker_reason(blocked_reason):
            return blocked_reason[:20]
        return f"?{blocked_reason}"[:20]
    if advisory_note:
        return advisory_note[:20]
    return "—"


def _sessions_by_id() -> dict[str, Session]:
    """Every persisted session keyed by id, for the ATTENTION cell (#2153).

    One read-only ``load_state()``; an unreadable state file yields ``{}``
    so the table falls back to PR state instead of failing (mirrors
    ``status._unowned_ceiling_sessions``).
    """
    try:
        state = load_state()
    except (OSError, ValueError, CwError):
        return {}
    return {s.id: s for s in state.sessions}


def _attention_cell(task: TicketTask, sessions_by_id: dict[str, Session]) -> str:
    """Dead-session page state first, then the hydrated PR state, else ``—``."""
    session = sessions_by_id.get(task.session_id) if task.session_id else None
    return session_attention_state(task, session) or task_attention_state(task) or "—"


def _unproductive_cell(task: TicketTask, sources: ViewSources) -> str:
    """``unproductive/ceiling`` (#2695): ``none`` when disabled, ``?`` unreadable."""
    try:
        ceiling = resolve_attempt_ceiling(
            sources.effective_client(task.client), task, sources.orchestrator_config
        )
    except (CwError, OSError, ValueError, yaml.YAMLError):
        return f"{task.unproductive_attempts}/?"
    shown = "none" if ceiling is None else str(ceiling)
    return f"{task.unproductive_attempts}/{shown}"


def _next_cell(
    task: TicketTask, sessions_by_id: dict[str, Session], sources: ViewSources
) -> str:
    """The row's first legal command, compacted; labelled for a human gate.

    The opt-in and pipeline position are resolved as ``show`` resolves them
    (``next_command_facts``): an unreadable config counts as armed (it can
    only drop the ``requeue --stage finalize`` jump, #2692 R5) and as an
    interior stage (the pre-#2760 cell; ``approve`` fails on it anyway).
    """
    session = sessions_by_id.get(task.session_id) if task.session_id else None
    facts = next_command_facts(task, sources)
    first = next_commands(
        task,
        session=session,
        operator_opt_in=facts.operator_opt_in,
        pipeline_position=facts.position,
    )[0]
    cell = compact_command(task, first.command)
    if is_human_gate_command(task, first):
        return f"{HUMAN_GATE_CELL_LABEL}{cell}"
    return cell


def _print_tasks_human(
    tasks: list[TicketTask],
    sessions_by_id: dict[str, Session],
    sources: ViewSources,
) -> None:
    if not tasks:
        click.echo("No tasks found.")
        return
    headers = [
        "TICKET_ID",
        "CLIENT",
        "STATUS",
        "SESSION_ID",
        "ATTEMPTS",
        "UNPRODUCTIVE",
        "LANE",
        "SCOPE_HINT",
        "COMPUTED_SCOPE_TIER",
        "STAGE",
        "DISPOSITION",
        "REASON",
        "PR",
        "ATTENTION",
        "STALE_GATE",
        "NEXT",
    ]
    col_widths = [12, 16, 16, 12, 8, 12, 12, 12, 20, 10, 20, 20, 10, 18, 10, 44]
    rows: list[list[str]] = []
    for t in tasks:
        attention = _attention_cell(t, sessions_by_id)
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:16],
            (t.session_id or "-")[:12],
            str(t.attempts)[:8],
            _unproductive_cell(t, sources)[:12],
            t.lane[:12],
            (t.scope_hint or "—")[:12],
            (t.computed_scope_tier or "—")[:20],
            t.stage.value[:10],
            (t.disposition or "—")[:20],
            _reason_cell(t.blocked_reason, t.advisory_note),
            (t.pr_url or "—")[:10],
            attention[:18],
            ("yes" if t.stale_gate_detected_at else "—")[:10],
            # Never truncated: the column fits the longest first command.
            _next_cell(t, sessions_by_id, sources),
        ]
        rows.append(row)
    print_fixed_width_table(headers, col_widths, rows)


@dev_queue.command(name="tasks")
@click.argument("ticket_arg", required=False, default=None, metavar="[TICKET]")
@click.option(
    "--ticket",
    "-t",
    default=None,
    help="Filter by ticket id (same as the optional TICKET argument).",
)
@click.option(
    "--status",
    "-s",
    default=None,
    type=click.Choice([s.value for s in QueueItemStatus]),
    help="Filter by task status.",
)
@click.option("--client", "-c", default=None, help="Filter by client name.")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON array.")
@handle_errors
def dev_queue_tasks(
    ticket_arg: str | None,
    ticket: str | None,
    status: str | None,
    client: str | None,
    output_json: bool,
) -> None:
    """List dev-queue tasks with typed field output.

    TICKET may be given positionally or via -t/--ticket (not both).

    Programmatic inspection view. For the human aggregate summary use dev-queue status.

    REASON is the row's blocker reason, or — on a row that is not parked — its
    advisory note (#1762), e.g. ``?session_mismatch`` for a RUNNING row whose
    session_id no longer resolves to a live session. A leading ``?`` marks a
    blocker reason that is not in cw's known-reason registry and does not
    declare itself freeform with an ``x_`` prefix (#2097) — the value is still
    shown verbatim, the flag just says cw does not recognise it as a documented
    routing code.

    UNPRODUCTIVE is ``unproductive_attempts/ceiling`` (#2695): the counter the
    attempt ceiling compares, ``none`` when the lane disables the ceiling and
    ``?`` when it cannot be resolved. ATTEMPTS is the raw count, which the
    ceiling never reads.

    NEXT is the row's first legal command (``cw dev-queue show`` lists them
    all), without the ``cw dev-queue`` prefix and the row's own ticket and
    client. A gate the human opted into reads ``HUMAN GATE: approve``, or
    ``HUMAN GATE: requeue --stage <stage>`` when ``approve`` cannot take the
    gate: the operator's command, not a step for an agent (#2692, #2760).
    """
    if ticket_arg is not None and ticket is not None:
        msg = "Pass the ticket either positionally or via -t/--ticket, not both."
        raise click.UsageError(msg)
    ticket = ticket_arg if ticket_arg is not None else ticket

    queue = load_dev_queue()
    tasks: list[TicketTask] = queue.tasks

    if ticket is not None:
        tasks = [t for t in tasks if t.ticket_id == ticket]

    if status is not None:
        target_status = QueueItemStatus(status)
        tasks = [t for t in tasks if t.status == target_status]

    if client is not None:
        tasks = [t for t in tasks if t.client == client]

    if output_json:
        click.echo(json.dumps([_task_to_dict(t) for t in tasks]))
    else:
        _print_tasks_human(tasks, _sessions_by_id(), ViewSources())


@dev_queue.command(name="refresh-all")
@handle_errors
def dev_queue_refresh_all() -> None:
    """Fast-forward main on every configured client repo.

    Runs ``git pull --ff-only origin <default_branch>`` for each client.
    Does NOT emit events — absence of ``ticket.needs_sync`` on the next
    dispatch tick confirms the refresh succeeded.
    """
    clients = load_clients()
    had_error = False
    for client in clients.values():
        try:
            before, after = fast_forward_main(client, ignore_untracked=True)
            if before == after:
                click.echo(f"{client.name}: already up to date ({before[:8]})")
            else:
                click.echo(f"{client.name}: updated {before[:8]}..{after[:8]}")
        except MissingWorkspaceError as exc:
            click.echo(f"{client.name}: SKIP — {exc}", err=True)
            # missing workspace is config-hygiene; does not contribute to had_error
        except WorktreeError as exc:
            click.echo(f"{client.name}: ERROR — {exc}", err=True)
            had_error = True
    if had_error:
        raise click.exceptions.Exit(1)
