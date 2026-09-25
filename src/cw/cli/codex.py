"""``cw codex run`` — subprocess entry point for the codex review stage.

RFC 0014 S1 / ADR-0018: thin CLI wiring over ``cw.codex_driver``'s testable
core. ``--stage impl`` is not implemented yet (#1550) and exits non-zero
naming that ticket rather than silently no-op'ing.
"""

from __future__ import annotations

import click

from cw.cli._base import handle_errors, main
from cw.codex_driver import (
    _IMPL_STAGE_NOT_IMPLEMENTED,
    STAGE_IMPL,
    STAGE_REVIEW,
    run_codex_review_stage,
)
from cw.exceptions import CwError


@main.group(name="codex")
def codex_group() -> None:
    """Codex executor subprocess commands."""


@codex_group.command(name="run")
@click.argument("ticket_id")
@click.option(
    "--stage",
    type=click.Choice([STAGE_REVIEW, STAGE_IMPL]),
    required=True,
    help="Pipeline stage to run.",
)
@click.option(
    "--session-id",
    required=True,
    help="cw Session id this run completes.",
)
@click.option(
    "--wall-clock-budget-seconds",
    type=int,
    default=None,
    help="Shared wall-clock budget for the review + fix loop.",
)
@handle_errors
def codex_run(
    ticket_id: str,
    stage: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
) -> None:
    """Run a codex pipeline stage for TICKET_ID as a detached subprocess."""
    if stage == STAGE_IMPL:
        raise CwError(_IMPL_STAGE_NOT_IMPLEMENTED)
    run_codex_review_stage(
        ticket_id=ticket_id,
        session_id=session_id,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
    )
