"""Shared CLI framework helpers and the root ``main`` group.

Holds the pieces every command submodule needs: the ``main`` Click group all
commands register onto, the ``handle_errors`` boundary decorator, shell
completion callbacks, logging configuration, and small formatting helpers.
Command submodules import ``main`` (and these helpers) from here and attach
their commands via decorators when imported. ``main`` is a
:class:`~cw.cli._lazy.LazyGroup`: it imports a command's submodule only when
that command is looked up, from the static map in :mod:`cw.cli._lazy`.

This module is imported on every ``cw`` invocation, so it keeps ``cw.config``
and ``cw.models`` (pydantic) out of its module-level imports; the helpers that
need them import them where they run (#2934).
"""

from __future__ import annotations

import functools
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import click
from click.shell_completion import CompletionItem

from cw import __version__
from cw._util import NO_CLIENTS_MSG, format_compact_age
from cw.cli._lazy import LAZY_COMMANDS, LazyGroup
from cw.exceptions import CwError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from cw.models import ClientConfig

logger = logging.getLogger(__name__)

# Shared by ``cw focus`` (writer) and ``cw statusline render`` (reader, #1644)
# — a single source of truth so the two never drift apart on the env var name
# that ties them together.
SESSION_ENV_VAR = "CLAUDE_CODE_SESSION_ID"


def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper


def print_fixed_width_table(
    headers: Sequence[str],
    col_widths: Sequence[int],
    rows: Iterable[Sequence[str]],
) -> None:
    """Echo a left-aligned fixed-width table: header, rule, then *rows*.

    Two spaces between columns, a rule as wide as the rendered header, and no
    truncation — every caller already truncates each cell to its own column's
    width, because what to cut and how to flag it is a per-column decision
    (see ``dev_queue.tasks._reason_cell``'s ``?`` prefix, which must survive
    the cut). Extracted (#2232) after ``cw dev-queue tasks`` and ``cw review
    dispositions`` carried byte-identical copies of the zip/format/echo
    triple.

    ``strict=True`` on both zips: a row or header that does not match
    ``col_widths`` is a programming error in the caller's column list, and a
    silently short row would print a misaligned table that reads as data.
    """
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo("  ".join(f"{v:<{w}}" for v, w in zip(row, col_widths, strict=True)))


def parse_iso_before(before: str) -> datetime:
    """Parse a --before value as an ISO 8601 timestamp. Raises CwError on failure.

    Shared by ``cw event prune`` (cli/queues.py) and ``cw session prune``
    (cli/session_prune.py) — extracted here (#1983) after both commands
    independently carried an identical private copy.
    """
    try:
        before_ts = datetime.fromisoformat(before)
    except ValueError as exc:
        msg = f"Cannot parse --before value '{before}' as ISO timestamp."
        raise CwError(msg) from exc
    if before_ts.tzinfo is None:
        click.echo(
            f"Warning: --before '{before}' has no timezone; assuming UTC",
            err=True,
        )
        before_ts = before_ts.replace(tzinfo=UTC)
    return before_ts


def _complete_client(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete client names from config."""
    from cw.config import load_clients

    return [
        CompletionItem(name) for name in load_clients() if name.startswith(incomplete)
    ]


def _complete_session(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete session names from backgrounded sessions."""
    from cw.config import load_state
    from cw.models import SessionStatus

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


@click.group(cls=LazyGroup, lazy_commands=LAZY_COMMANDS)
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


def _relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    delta = datetime.now(UTC) - dt
    if delta < timedelta(minutes=1):
        return "just now"
    return f"{format_compact_age(delta)} ago"


def _resolve_client(client_name: str | None) -> ClientConfig:
    """Resolve --client to a ClientConfig, defaulting to the first configured client.

    The default is announced on stderr (#3170): a silent first-client pick once
    scoped work to the wrong client without a trace.
    """
    from cw.config import get_client, load_clients

    clients = load_clients()
    if client_name:
        return get_client(client_name)
    if not clients:
        raise CwError(NO_CLIENTS_MSG)
    first = next(iter(clients.values()))
    click.echo(f"warning: no --client given; defaulting to {first.name}", err=True)
    return first


def _resolve_client_or_infer(client_name: str | None) -> ClientConfig:
    """Resolve --client, else infer the client from the working directory (#3170).

    An explicit name goes through :func:`cw.config.get_client`, whose error
    lists the configured clients. Without one, the resolved cwd is matched
    against every client's workspace, repo and worktree bases
    (:func:`cw.client_resolution.infer_client`): a single owner is used and
    announced (``client inferred from <cwd>: <name>`` on stdout, any nested-match
    warning on stderr); a tie or no match raises
    :class:`~cw.client_resolution.ClientInferenceError`. There is never a
    first-client fallback.

    The cwd's git toplevel is not a second candidate: it is always the cwd or
    an ancestor of it, so any client root containing it already contains the
    cwd, and it could never change the outcome.
    """
    from pathlib import Path

    from cw.client_resolution import infer_client
    from cw.config import get_client, load_clients

    if client_name:
        return get_client(client_name)
    clients = load_clients()
    cwd = Path.cwd().resolve()
    inferred = infer_client([cwd], clients)
    for warning in inferred.warnings:
        click.echo(warning, err=True)
    click.echo(f"client inferred from {cwd}: {inferred.name}")
    return clients[inferred.name]
