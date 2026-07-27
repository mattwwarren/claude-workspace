"""Dev-queue task inspection + repo refresh: tasks, refresh-all, attention helpers.

``_count_needs_attn`` / ``_needs_attn_by_client`` also back the aggregate
``dev-queue status`` view (imported by ``status`` — a deliberate cross-submodule
dependency).
"""

from __future__ import annotations

import json

import click

from cw.cli._base import handle_errors
from cw.config import load_clients
from cw.dev_queue import load_dev_queue
from cw.exceptions import MissingWorkspaceError, WorktreeError
from cw.models import QueueItemStatus, TicketTask
from cw.worktree import fast_forward_main

from ._group import dev_queue


def _task_to_dict(task: TicketTask) -> dict[str, object]:
    return {
        "ticket_id": task.ticket_id,
        "client": task.client,
        "status": task.status.value,
        "session_id": task.session_id,
        "attempts": task.attempts,
        "priority": task.priority,
        "lane": task.lane,
        "created_at": task.created_at.isoformat(),
        "total_cost_usd": task.total_cost_usd,
        "worktree_path": str(task.worktree_path) if task.worktree_path else None,
        "disposition": task.disposition,
        "blocked_reason": task.blocked_reason,
        "pr_url": task.pr_url,
        "pr_state": (
            task.pr_state.model_dump(mode="json") if task.pr_state is not None else None
        ),
        "signoff": task.signoff,
        "last_blocked_result": task.last_blocked_result,
    }


def _task_attention_state(task: TicketTask) -> str | None:
    """The task's hydrated PR attention_state, or None if not hydrated/clean."""
    return task.pr_state.attention_state if task.pr_state is not None else None


def _count_needs_attn(tasks: list[TicketTask]) -> int:
    """Count tasks whose hydrated PR state carries a non-null attention_state."""
    return sum(1 for t in tasks if _task_attention_state(t) is not None)


def _needs_attn_by_client(tasks: list[TicketTask]) -> dict[str, int]:
    """Map client -> count of tasks needing attention (non-null attention_state)."""
    counts: dict[str, int] = {}
    for t in tasks:
        if _task_attention_state(t) is not None:
            counts[t.client] = counts.get(t.client, 0) + 1
    return counts


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
        "DISPOSITION",
        "REASON",
        "PR",
        "ATTENTION",
    ]
    col_widths = [12, 16, 16, 12, 8, 12, 20, 20, 10, 18]
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for t in tasks:
        attention = _task_attention_state(t) or "—"
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:16],
            (t.session_id or "-")[:12],
            str(t.attempts)[:8],
            t.lane[:12],
            (t.disposition or "—")[:20],
            (t.blocked_reason or "—")[:20],
            (t.pr_url or "—")[:10],
            attention[:18],
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
