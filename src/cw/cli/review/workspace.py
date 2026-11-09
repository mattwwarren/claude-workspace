"""Deterministic bookkeeping for the Claude-native review workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from cw.cli._base import handle_errors
from cw.cli.review._group import review
from cw.exceptions import CwError
from cw.review_findings import ReviewerRunFailure
from cw.review_workspace import (
    WorkspaceResult,
    finish_workspace,
    initialize_workspace,
    reconcile_workspace,
    record_document,
    record_failure,
    recover_workspace,
)

if TYPE_CHECKING:
    from typing import TextIO


@review.group(name="workspace")
def workspace() -> None:
    """Initialize, reconcile, and persist a review workspace."""


def _emit(result: WorkspaceResult | None) -> None:
    if result is None:
        click.echo(json.dumps({"recovered": True}))
        return
    click.echo(
        json.dumps(
            {
                "action": result.action,
                "ticket": result.ticket,
                "round_id": result.round_id,
                "pending_roles": list(result.pending_roles),
                "completed_roles": list(result.completed_roles),
                "failed_reviewers": [
                    failure.model_dump(mode="json")
                    for failure in result.failed_reviewers
                ],
            },
            indent=2,
        )
    )


@workspace.command(name="init")
@click.option("--ticket", required=True)
@click.option("--round-id", required=True)
@click.option("--role", "roles", multiple=True, required=True)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def init_workspace(
    ticket: str, round_id: str, roles: tuple[str, ...], worktree: Path
) -> None:
    """Initialize a new round or resume its persisted roster."""
    _emit(initialize_workspace(worktree, ticket=ticket, round_id=round_id, roles=roles))


@workspace.command(name="reconcile")
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def reconcile(worktree: Path) -> None:
    """Reconcile role states with findings artifacts and failures."""
    _emit(reconcile_workspace(worktree))


@workspace.command(name="record-document")
@click.argument("role")
@click.argument("document", type=click.File("r"))
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def record_document_command(role: str, document: TextIO, worktree: Path) -> None:
    """Validate and persist a reviewer document from FILE or stdin (-)."""
    content = document.read()
    _refuse_engine_findings(content)
    _emit(record_document(worktree, role=role, content=content))


_ENGINE_SOURCE_REFUSAL = (
    "record-document refuses engine-sourced findings (source: engine); only "
    "`cw review engine record` may record a repo review engine round (#3039)"
)


def _refuse_engine_findings(content: str) -> None:
    """Refuse a document carrying an engine finding. Malformed JSON passes through.

    A malformed document is left to ``record_document`` to reject with its own
    message, so this guard never changes that error.
    """
    try:
        payload = json.loads(content)
    except ValueError:
        return
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        return
    if any(
        isinstance(item, dict) and item.get("source") == "engine" for item in findings
    ):
        raise CwError(_ENGINE_SOURCE_REFUSAL)


@workspace.command(name="record-failure")
@click.argument("role")
@click.option("--reason", required=True)
@click.option("--detail", default="")
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def record_failure_command(role: str, reason: str, detail: str, worktree: Path) -> None:
    """Persist a failed reviewer return and its friction note."""
    _emit(
        record_failure(
            worktree,
            role=role,
            failure=ReviewerRunFailure(role=role, reason=reason, detail=detail),
        )
    )


@workspace.command(name="finish")
@click.option("--status", type=click.Choice(["completed", "failed"]), required=True)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def finish(status: Literal["completed", "failed"], worktree: Path) -> None:
    """Mark the round terminal after all reviewers have returned."""
    _emit(finish_workspace(worktree, status=status))


@workspace.command(name="recover")
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@handle_errors
def recover(worktree: Path) -> None:
    """Recover an interrupted archive or staging promotion."""
    recover_workspace(worktree)
    _emit(None)
