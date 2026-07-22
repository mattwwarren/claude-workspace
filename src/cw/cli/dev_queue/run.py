"""Dispatch-driving commands: run, serve, plan."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.cli._base import _complete_client, handle_errors
from cw.config import get_client
from cw.dispatch import run_dispatch_loop
from cw.dispatch_serve import run_dispatch_serve
from cw.plan import run_planner

from ._group import dev_queue

if TYPE_CHECKING:
    from cw.native_daemon import NativeDaemonClient


@dev_queue.command(name="run")
@click.option(
    "--max-parallel",
    "-p",
    default=None,
    type=int,
    help="Override per-client concurrency cap.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single dispatch tick and exit.",
)
@click.option(
    "--use-plan",
    is_flag=True,
    default=False,
    help="Respect the persisted DispatchPlan ordering when claiming tasks.",
)
@click.option(
    "--parent",
    default=None,
    help=(
        "Orchestrator session ID. Spawned workers are linked back via "
        "parent_session_id + worker_session_ids."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress per-tick operator output (for cron/scripted use).",
)
@click.option(
    "--auto-ff/--no-auto-ff",
    "auto_ff",
    default=True,
    help="Disable automatic fast-forward of local main (legacy block-only behavior).",
)
@click.option(
    "--client",
    "-c",
    default=None,
    help="Dispatch only this client's queue.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Bypass the dispatch-loop singleton lock (#1362). Logs a WARNING;"
        " use only to override a wedged/foreign holder."
    ),
)
@handle_errors
def dev_queue_run(
    max_parallel: int | None,
    once: bool,
    use_plan: bool,
    parent: str | None,
    quiet: bool,
    auto_ff: bool,
    client: str | None,
    force: bool,
) -> None:
    """Run the dispatch loop, spawning sessions for pending tickets."""
    if client is not None:
        get_client(client)
    run_dispatch_loop(
        max_parallel=max_parallel,
        once=once,
        use_plan=use_plan,
        parent=parent,
        emit=None if quiet else click.echo,
        auto_ff=auto_ff,
        client=client,
        force=force,
    )


@dev_queue.command(name="serve")
@click.option(
    "--max-parallel",
    "-p",
    default=None,
    type=int,
    help="Override per-client concurrency cap.",
)
@click.option(
    "--use-plan",
    is_flag=True,
    default=False,
    help="Respect the persisted DispatchPlan ordering when claiming tasks.",
)
@click.option(
    "--parent",
    default=None,
    help=(
        "Orchestrator session ID. Spawned workers are linked back via "
        "parent_session_id + worker_session_ids."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress per-tick operator output (for cron/scripted use).",
)
@click.option(
    "--auto-ff/--no-auto-ff",
    "auto_ff",
    default=True,
    help="Disable automatic fast-forward of local main (legacy block-only behavior).",
)
@click.option(
    "--client",
    "-c",
    default=None,
    help="Dispatch only this client's queue.",
)
@click.option(
    "--max-restarts",
    "max_restarts",
    type=int,
    default=-1,
    show_default=True,
    help="Maximum number of restarts. -1 = unlimited.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Bypass the dispatch-loop singleton lock (#1362). Logs a WARNING;"
        " use only to override a wedged/foreign holder."
    ),
)
@handle_errors
def dev_queue_serve(
    max_parallel: int | None,
    use_plan: bool,
    parent: str | None,
    quiet: bool,
    auto_ff: bool,
    client: str | None,
    max_restarts: int,
    force: bool,
) -> None:
    """Run the dispatch loop with automatic restart on crash.

    Unlike ``run``, ``serve`` restarts the dispatch loop after crashes with
    exponential backoff. It exits cleanly on Ctrl-C or a normal (non-crash)
    return from the loop. Use this command in place of ``run`` when you want
    a self-healing long-running dispatch process.
    """
    if client is not None:
        get_client(client)
    run_dispatch_serve(
        max_parallel=max_parallel,
        use_plan=use_plan,
        parent=parent,
        emit=None if quiet else click.echo,
        auto_ff=auto_ff,
        client=client,
        max_restarts=max_restarts,
        force=force,
    )


_PLAN_DEFAULT_TIMEOUT = 300


def _run_plan_impl(
    *,
    client_name: str,
    timeout: int,
    client_filter: str | None,
    native_daemon: NativeDaemonClient | None = None,
) -> int:
    """Spawn the planner, persist the result, and report status.

    Separated from the Click command so tests can inject a fake native
    daemon client directly.  Returns 0 on success, 1 on validation/timeout
    failure.
    """
    client_config = get_client(client_name)
    result = run_planner(
        client=client_config,
        native_daemon=native_daemon,
        timeout_seconds=timeout,
        client_filter=client_filter,
    )
    plan = result.plan
    if plan is None:
        click.echo(
            f"Planner failed: {result.error} (queue order unchanged)",
            err=True,
        )
        return 1
    click.echo(f"Plan persisted: {len(plan.tasks)} tasks (session {result.session_id})")
    return 0


@dev_queue.command(name="plan")
@click.option(
    "--client",
    "-c",
    required=True,
    shell_complete=_complete_client,
    help="Client whose workspace will host the planner session.",
)
@click.option(
    "--timeout",
    type=int,
    default=_PLAN_DEFAULT_TIMEOUT,
    help="Seconds to wait for the planner JSON output (default: 300).",
)
@click.option(
    "--filter-client",
    default=None,
    help="Only include pending tickets for this client in the planner prompt.",
)
@handle_errors
def dev_queue_plan(client: str, timeout: int, filter_client: str | None) -> None:
    """Spawn /orchestrate-plan to produce a DispatchPlan for pending tickets.

    Runs a one-shot Claude session via cw spawn, waits for it to write a
    DispatchPlan JSON file, validates it, and persists it for use by
    ``cw dev-queue run --use-plan``.

    On validation failure or timeout, the dev queue is left unchanged.
    """
    exit_code = _run_plan_impl(
        client_name=client,
        timeout=timeout,
        client_filter=filter_client,
    )
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)
