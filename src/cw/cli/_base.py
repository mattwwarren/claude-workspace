"""Shared CLI framework helpers and the root ``main`` group.

Holds the pieces every command submodule needs: the ``main`` Click group all
commands register onto, the ``handle_errors`` boundary decorator, shell
completion callbacks, logging configuration, and small formatting helpers.
Command submodules import ``main`` (and these helpers) from here and attach
their commands via decorators; :mod:`cw.cli` imports the submodules to trigger
registration.
"""

from __future__ import annotations

import functools
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click
from click.shell_completion import CompletionItem

from cw import __version__
from cw.config import get_client, load_clients, load_state
from cw.dispatch import (
    FRESHNESS_MAIN_BEHIND,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
)
from cw.exceptions import CwError
from cw.models import ClientConfig, SessionStatus

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper


def _complete_client(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete client names from config."""
    return [
        CompletionItem(name) for name in load_clients() if name.startswith(incomplete)
    ]


def _complete_session(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete session names from backgrounded sessions."""
    state = load_state()
    return [
        CompletionItem(s.name)
        for s in state.sessions
        if s.name.startswith(incomplete) and s.status != SessionStatus.COMPLETED
    ]


_LOG_FORMAT = "%(levelname)s %(name)s %(message)s"

_VERBOSE_DEBUG_THRESHOLD = 2


def _configure_logging(verbose: int) -> None:
    """Install a single stderr logging handler for the CLI process.

    Uses ``basicConfig`` *without* ``force`` on purpose: when a test harness
    (pytest) has already attached handlers to the root logger, ``basicConfig``
    is a no-op, so the harness keeps full control of log capture. In a real
    ``cw`` process the root logger starts empty, so a stderr handler is
    installed once at the requested level. ``-v`` -> INFO, ``-vv`` -> DEBUG.
    """
    if verbose >= _VERBOSE_DEBUG_THRESHOLD:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, stream=sys.stderr, format=_LOG_FORMAT)


@click.group()
@click.version_option(version=__version__, prog_name="cw")
@click.option(
    "-v",
    "--verbose",
    count=True,
    default=0,
    help="Increase log verbosity (-v INFO, -vv DEBUG).",
)
def main(verbose: int) -> None:
    """Claude Workspace - multi-session orchestrator for Claude Code."""
    _configure_logging(verbose)


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < _SECONDS_PER_MINUTE:
        return "just now"
    if seconds < _SECONDS_PER_HOUR:
        m = seconds // _SECONDS_PER_MINUTE
        return f"{m}m ago"
    if seconds < _SECONDS_PER_DAY:
        h = seconds // _SECONDS_PER_HOUR
        return f"{h}h ago"
    d = seconds // _SECONDS_PER_DAY
    return f"{d}d ago"


def _resolve_client(client_name: str | None) -> ClientConfig:
    """Resolve --client to a ClientConfig, defaulting to the first configured client."""
    clients = load_clients()
    if client_name:
        return get_client(client_name)
    if not clients:
        msg = "No clients configured. Add one to ~/.config/cw/clients.yaml."
        raise CwError(msg)
    return next(iter(clients.values()))


def _emit_freshness_subline(
    client_name: str,
    tick_freshness_detail: str | None,
    tick_blocked_branch: str | None,
    n_pending: int,
) -> None:
    """Print a freshness-block subline under a stale tick entry."""
    if tick_freshness_detail == FRESHNESS_NON_MAIN_HEAD:
        try:
            cc = get_client(client_name)
            default_br: str = cc.default_branch
            ws_path: str = str(cc.workspace_path)
        except CwError:
            default_br = "main"
            ws_path = client_name
        branch_str = tick_blocked_branch or "(detached)"
        click.echo(
            f"  ⚠ base checkout HEAD on '{branch_str}'"
            f" (not {default_br})"
            f" — {n_pending} pending blocked."
            f" Fix: git -C {ws_path} checkout {default_br}"
        )
    elif tick_freshness_detail == FRESHNESS_MAIN_BEHIND:
        click.echo(f"  ⚠ {client_name}: main behind origin — auto-ff pending/failed")
    elif tick_freshness_detail == FRESHNESS_MAIN_DIRTY_CHECKOUT:
        click.echo(
            f"  ⚠ {client_name}: main checkout dirty — commit or stash changes,"
            " then auto-ff will retry"
        )
    elif tick_freshness_detail == FRESHNESS_MAIN_DETACHED:
        try:
            cc = get_client(client_name)
            default_br = cc.default_branch
            ws_path = str(cc.workspace_path)
        except CwError:
            default_br = "main"
            ws_path = client_name
        click.echo(
            f"  ⚠ {client_name}: main checkout has a detached HEAD —"
            f" run: git -C {ws_path} checkout {default_br}"
        )
    elif tick_freshness_detail == FRESHNESS_MAIN_DIVERGED:
        click.echo(
            f"  ⚠ {client_name}: main diverged from origin —"
            " reconcile with: git -C <workspace> pull --rebase"
        )
