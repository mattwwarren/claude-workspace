"""``cw dev-queue usage-limit clear`` -- lift a usage-limit window early (#3547).

Order (ADR-0019): unlocked reads build the plan; nothing to clear or
``--dry-run`` stops there. Then the operator login is resolved and, unless
``--force``, the account is probed, both with no lock held. Only then does
:func:`cw.usage_limit_window.apply_usage_limit_clear` take its two STATE
locks in sequence, after recording the audit event first (a failed event write
refuses the clear and changes nothing). A probe ERROR fails closed: the
clear refuses and names ``--force``; the probe runs once, with no retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.cli._base import _complete_client, handle_errors
from cw.config import get_client
from cw.exceptions import CwError
from cw.operator_identity import resolve_actor_or_empty
from cw.usage_limit_probe import ProbeVerdict, probe_account_usage_limit
from cw.usage_limit_window import (
    apply_usage_limit_clear,
    display_instant,
    plan_usage_limit_clear,
)

from ._group import dev_queue

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.usage_limit_window import ClearPlan, ClearResult

_PROBE_SKIPPED = "skipped"


def _dry_run_lines(plan: ClearPlan) -> list[str]:
    """The ``--dry-run`` report; the release and note lines only when rows qualify."""
    if plan.window_until is None:
        lines = [f"dry run: no active usage-limit window for {plan.client}"]
    else:
        lines = [
            f"dry run: would clear usage-limit window for {plan.client}"
            f" (until {display_instant(plan.window_until)})"
        ]
    lines += [
        f"dry run: would release {row.ticket_id}: next_eligible_at"
        f" {display_instant(row.stamp)} -> {display_instant(row.restore_to)}"
        for row in plan.rows
    ]
    if plan.rows:
        lines.append(
            "dry run: note: next_eligible_at is shared by usage-limit, spawn-error"
            " and worktree waits; every future stamp on the client's pending rows"
            " is released"
        )
    lines.append("dry run: no changes made")
    return lines


def _summary_lines(result: ClearResult) -> list[str]:
    """What the clear changed."""
    if result.old_until is None:
        lines = [
            f"no active usage-limit window for {result.client}; clear mark recorded"
        ]
    else:
        lines = [
            f"cleared usage-limit window for {result.client}"
            f" (was until {display_instant(result.old_until)})"
        ]
    if result.rows_released:
        lines.append(
            f"released {len(result.rows_released)} row(s):"
            f" {', '.join(result.rows_released)}"
        )
    lines.append(
        "a running dispatch loop drops the window on its next tick; no restart needed"
    )
    return lines


def _probe_verdict(client_cfg: ClientConfig, *, force: bool) -> str:
    """Run the account probe unless *force*; refuse on LIMITED or ERROR."""
    if force:
        return _PROBE_SKIPPED
    result = probe_account_usage_limit(client_cfg.worker_model)
    if result.verdict is ProbeVerdict.LIMITED:
        msg = (
            f"refusing to clear {client_cfg.name}: the account probe reports the"
            f" usage limit is still in effect ({result.detail}). Re-run with"
            " --force to clear anyway."
        )
        raise CwError(msg)
    if result.verdict is ProbeVerdict.ERROR:
        msg = (
            f"refusing to clear {client_cfg.name}: the account probe could not"
            f" determine the account state ({result.detail}). Re-run with --force"
            " to clear without probing."
        )
        raise CwError(msg)
    return result.verdict.value


def _echo(lines: list[str]) -> None:
    for line in lines:
        click.echo(line)


@dev_queue.group(
    name="usage-limit",
    help="Inspect and clear per-client usage-limit back-off windows.",
)
def usage_limit() -> None:
    """Group for the usage-limit window commands."""


@usage_limit.command(
    name="clear",
    help=(
        "Lift a client's usage-limit back-off window early, after the account"
        " limit was reset by hand."
    ),
)
@click.option(
    "--client",
    "-c",
    required=True,
    shell_complete=_complete_client,
    help="Client whose window to clear.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be cleared; change nothing and run no probe.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip the account probe and clear even if the account may still be limited.",
)
@handle_errors
def usage_limit_clear(client: str, dry_run: bool, force: bool) -> None:
    client_cfg = get_client(client)
    plan = plan_usage_limit_clear(client)
    if plan.is_empty:
        click.echo(
            f"nothing to clear for {client}: no active usage-limit window and no"
            " waiting rows"
        )
        return
    if dry_run:
        _echo(_dry_run_lines(plan))
        return
    actor = resolve_actor_or_empty(client_cfg, context=f"usage-limit clear {client}")
    verdict = _probe_verdict(client_cfg, force=force)
    result = apply_usage_limit_clear(
        plan, actor=actor, forced=force, probe_verdict=verdict
    )
    _echo(_summary_lines(result))
