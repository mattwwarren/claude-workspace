"""Dev-queue task inspection + repo refresh: tasks, refresh-all, attention helpers.

``_count_needs_attn`` / ``_needs_attn_by_client`` also back the aggregate
``dev-queue status`` view (imported by ``status`` — a deliberate cross-submodule
dependency).
"""

from __future__ import annotations

import json

import click

from cw.auto_dev_result import is_known_blocker_reason
from cw.cli._base import handle_errors
from cw.config import load_clients
from cw.dev_queue import load_dev_queue, task_attention_state
from cw.exceptions import MissingWorkspaceError, WorktreeError
from cw.models import QueueItemStatus, TicketTask
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


def _reason_cell(blocked_reason: str | None) -> str:
    """Render the REASON column, flagging an unregistered reason (#2097).

    A ``?`` **prefix** rather than a suffix so the flag survives the column's
    20-char truncation -- a reason long enough to be cut is exactly the one an
    operator is most likely to mistake for a documented routing code. Advisory
    only: ``blocker.reason`` stays an open enum and the value is still shown
    verbatim (see ``cw.auto_dev_result.is_known_blocker_reason``).
    """
    if not blocked_reason:
        return "—"
    if is_known_blocker_reason(blocked_reason):
        return blocked_reason[:20]
    return f"?{blocked_reason}"[:20]


def _print_tasks_human(tasks: list[TicketTask]) -> None:
    if not tasks:
        click.echo("No tasks found.")
        return
    headers = [
        "TICKET_ID",
        "CLIENT",
        "STATUS",
        "SESSION_ID",
        "ATTEMPTS",
        "LANE",
        "SCOPE_HINT",
        "COMPUTED_SCOPE_TIER",
        "STAGE",
        "DISPOSITION",
        "REASON",
        "PR",
        "ATTENTION",
        "STALE_GATE",
    ]
    col_widths = [12, 16, 16, 12, 8, 12, 12, 20, 10, 20, 20, 10, 18, 10]
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for t in tasks:
        attention = task_attention_state(t) or "—"
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:16],
            (t.session_id or "-")[:12],
            str(t.attempts)[:8],
            t.lane[:12],
            (t.scope_hint or "—")[:12],
            (t.computed_scope_tier or "—")[:20],
            t.stage.value[:10],
            (t.disposition or "—")[:20],
            _reason_cell(t.blocked_reason),
            (t.pr_url or "—")[:10],
            attention[:18],
            ("yes" if t.stale_gate_detected_at else "—")[:10],
        ]
        click.echo("  ".join(f"{v:<{w}}" for v, w in zip(row, col_widths, strict=True)))


@dev_queue.command(name="tasks")
@click.option("--ticket", "-t", default=None, help="Filter by ticket id.")
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
    ticket: str | None,
    status: str | None,
    client: str | None,
    output_json: bool,
) -> None:
    """List dev-queue tasks with typed field output.

    Programmatic inspection view. For the human aggregate summary use dev-queue status.

    REASON is the row's blocker reason. A leading ``?`` marks a reason that is
    not in cw's known-reason registry and does not declare itself freeform with
    an ``x_`` prefix (#2097) — the value is still shown verbatim, the flag just
    says cw does not recognise it as a documented routing code.
    """
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
        _print_tasks_human(tasks)


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
