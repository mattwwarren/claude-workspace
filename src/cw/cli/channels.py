"""MCP channel server commands (``pr-channel`` and ``queue-channel`` groups)."""

from __future__ import annotations

import click

from cw.cli._base import main


@main.group(name="pr-channel")
def pr_channel() -> None:
    """PR channel server: push MCP notifications to subscribed Claude sessions."""


@pr_channel.command(name="proxy")
@click.option("--client-id", default=None, help="Unique client ID for cursor tracking.")
def pr_channel_proxy(client_id: str | None) -> None:
    """Start the MCP stdio proxy for cw-pr-events (add to .mcp.json)."""
    from cw.cw_pr_events_channel import run_proxy

    run_proxy(client_id=client_id)


@pr_channel.command(name="serve")
@click.option("--port", default=8788, type=int, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option(
    "--allow-unsigned",
    is_flag=True,
    default=False,
    help=(
        "Accept unsigned /pr-event requests when CW_PR_EVENTS_HMAC_SECRET is "
        "unset (#1127). Off by default: without a secret configured, "
        "unsigned requests are rejected with 401. Only pass this if you "
        "understand the blast radius of an unauthenticated, internet-facing "
        "endpoint (see docs/dispatch-runbook.md)."
    ),
)
def pr_channel_serve(port: int, host: str, allow_unsigned: bool) -> None:
    """Start the cw-pr-events MCP channel server.

    Defaults mirror ``cw.cw_pr_events_server.DEFAULT_HOST`` / ``DEFAULT_PORT`` —
    kept inline here so the click decorators don't trigger an eager import of
    ``starlette`` (lives in the ``[mcp]`` optional-deps extra).
    """
    from cw.cw_pr_events_server import serve as _serve

    _serve(host=host, port=port, allow_unsigned=allow_unsigned)


@main.group(name="queue-channel")
def queue_channel() -> None:
    """Queue channel server: push MCP notifications to subscribed Claude sessions."""


@queue_channel.command(name="proxy")
@click.option("--client-id", default=None, help="Unique client ID for cursor tracking.")
def queue_channel_proxy(client_id: str | None) -> None:
    """Start the MCP stdio proxy for cw-queue-events (add to .mcp.json)."""
    from cw.cw_queue_events_channel import run_proxy

    run_proxy(client_id=client_id)


@queue_channel.command(name="serve")
@click.option("--port", default=8789, type=int, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
def queue_channel_serve(port: int, host: str) -> None:
    """Start the cw-queue-events MCP channel server.

    Defaults mirror ``cw.cw_queue_events_server.DEFAULT_HOST`` / ``DEFAULT_PORT`` —
    kept inline here so the click decorators don't trigger an eager import of
    ``starlette`` (lives in the ``[mcp]`` optional-deps extra).
    """
    from cw.cw_queue_events_server import serve as _serve

    _serve(host=host, port=port)


@main.group(name="operator-channel")
def operator_channel() -> None:
    """Operator channel: push MCP notifications filtered for operator attention."""


@operator_channel.command(name="proxy")
@click.option("--client-id", default=None, help="Unique client ID for cursor tracking.")
def operator_channel_proxy(client_id: str | None) -> None:
    """Start the MCP stdio proxy for cw-operator (add to .mcp.json)."""
    from cw.cw_operator_events_channel import run_proxy

    run_proxy(client_id=client_id)
