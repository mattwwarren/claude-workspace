"""Freshness-block sublines printed under a stale dispatch tick.

Shared by ``cw status`` (:mod:`cw.cli.sessions`) and ``cw dev-queue status``
(:mod:`cw.cli.dev_queue.status`). Lives outside :mod:`cw.cli._base` because
the ``FRESHNESS_*`` constants come from :mod:`cw.dispatch`, and ``_base`` is
imported on every ``cw`` invocation (#2934).
"""

from __future__ import annotations

import click

from cw.config import get_client
from cw.dispatch import (
    FRESHNESS_MAIN_BEHIND,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
)
from cw.dispatch.gating.freshness import _FRESHNESS_FAILURE_CODES
from cw.exceptions import CwError


def _resolve_default_branch_and_workspace(client_name: str) -> tuple[str, str]:
    """Look up a client's default branch and workspace path, with a bare fallback."""
    try:
        cc = get_client(client_name)
        return cc.default_branch, str(cc.workspace_path)
    except CwError:
        return "main", client_name


def _emit_freshness_subline(
    client_name: str,
    tick_freshness_detail: str | None,
    tick_blocked_branch: str | None,
    n_pending: int,
    *,
    is_blocked: bool = True,
) -> None:
    """Print a freshness-block subline under a stale tick entry."""
    failure = _freshness_failure_parts(tick_freshness_detail)
    if failure is not None:
        reason, message = failure
        context = (
            "pending blocked; ticket skipped" if is_blocked else "dispatch proceeded"
        )
        detail = f": {message}" if message else ""
        description = (
            "auto-fast-forward failed" if is_blocked else "freshness check failed"
        )
        click.echo(f"  ⚠ {client_name}: {description} ({reason}) — {context}{detail}")
    elif tick_freshness_detail == FRESHNESS_NON_MAIN_HEAD:
        default_br, ws_path = _resolve_default_branch_and_workspace(client_name)
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
        default_br, ws_path = _resolve_default_branch_and_workspace(client_name)
        click.echo(
            f"  ⚠ {client_name}: main checkout has a detached HEAD —"
            f" run: git -C {ws_path} checkout {default_br}"
        )
    elif tick_freshness_detail == FRESHNESS_MAIN_DIVERGED:
        click.echo(
            f"  ⚠ {client_name}: main diverged from origin —"
            " reconcile with: git -C <workspace> pull --rebase"
        )


def _freshness_failure_parts(detail: str | None) -> tuple[str, str] | None:
    """Return the stable reason and message from a structured detail string."""
    if detail is None:
        return None
    reason, separator, message = detail.partition(": ")
    if reason not in _FRESHNESS_FAILURE_CODES:
        return None
    return reason, message if separator else ""


def _is_freshness_failure_detail(detail: str | None) -> bool:
    """Return whether *detail* contains a structured freshness warning."""
    return _freshness_failure_parts(detail) is not None
