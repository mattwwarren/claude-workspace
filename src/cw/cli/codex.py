"""``cw codex`` — codex executor subprocess and migration commands.

``cw codex run``: RFC 0014 S1 / ADR-0018, thin CLI wiring over
``cw.codex_driver``'s testable core. ``--stage impl`` is not implemented yet
(#1550) and exits non-zero naming that ticket rather than silently
no-op'ing.

``cw codex migrate-legacy``: RFC 0014 B1 (#2389), thin CLI wiring over
``cw.codex_legacy_recovery``.
"""

from __future__ import annotations

import json

import click

from cw.cli._base import handle_errors, main
from cw.codex_driver import (
    _IMPL_STAGE_NOT_IMPLEMENTED,
    STAGE_IMPL,
    STAGE_REVIEW,
    run_codex_review_stage,
)
from cw.codex_legacy_recovery import (
    format_report_json,
    format_report_text,
    preflight_codex_legacy_recovery,
    run_codex_legacy_recovery,
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


@codex_group.command(name="migrate-legacy")
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Output report as JSON."
)
@click.option(
    "--client",
    "client_name",
    default=None,
    help="Recover this client (required when multiple clients exist).",
)
@click.option(
    "--preflight",
    is_flag=True,
    default=False,
    help="Report the selected scope and candidates without changing state.",
)
@handle_errors
def codex_migrate_legacy(
    as_json: bool, client_name: str | None, preflight: bool
) -> None:
    """Recover live pre-RFC-0014 codex sessions for one client, once.

    Scans each legacy codex session's worktree for a live writer, then
    requeues or parks its task through the codex harvest gate. Safe to run
    while serve is up. Exits 1 while any session is unresolved; re-run once
    the causes are fixed. A completed run makes every later run a no-op.
    See docs/dispatch-runbook.md.
    """
    if preflight:
        scope, candidates = preflight_codex_legacy_recovery(client=client_name)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "status": "preflight",
                        "scope": scope or "all-clients",
                        "candidates": candidates,
                    },
                    indent=2,
                )
            )
        else:
            click.echo(
                "codex legacy recovery preflight: "
                f"scope={scope or 'all-clients'}, candidates={candidates}; "
                "no changes applied"
            )
        return
    report = run_codex_legacy_recovery(client=client_name)
    click.echo(format_report_json(report) if as_json else format_report_text(report))
    raise click.exceptions.Exit(0 if report.ok else 1)
