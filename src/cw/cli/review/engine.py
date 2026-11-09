"""The ``cw review engine`` group (#3036, RFC 0017 I1).

``validate FILE`` runs one engine summary through the bounded loader
(``cw.review_engine_summary_io``). Exit 0 prints one stdout line naming the
schema version, verdict and coverage; any rejection exits 1 with
``<reason>: <field.path or ->: <message>`` lines on stderr, at most
``MAX_VALIDATE_ERROR_LINES`` of them plus a count of the lines omitted. Exit 2
stays click's usage error.

``preflight``, ``materialize``, ``record`` and ``allow`` (#3039) are the
commands the Claude-native review stage sequences. Each prints one JSON object
and maps its exit code as ``cw.review_engine.constants`` documents: 0 ok, 3 park
or a recorded round failure, 1 a fault.

``validate`` is read-only: it emits no event and writes nothing. It takes no
stdin (``-`` is an ordinary path) and has no ``--json``. It renders its own
stderr format, so it does not use ``handle_errors``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.cli.review._group import review
from cw.config import load_effective_config
from cw.dev_queue.crud import resolve_client
from cw.dev_queue.review_engine_allow import record_review_engine_allow
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.review_engine.constants import (
    ENGINE_ROLE,
    EXIT_PARK,
    EXIT_ROUND_FAILED,
    EngineRoundFailure,
)
from cw.review_engine.context import EngineContext, read_engine_context
from cw.review_engine.materialize import (
    MaterializeError,
    check_round_id,
    materialize_skill,
)
from cw.review_engine.preflight import run_preflight
from cw.review_engine.record import record_engine_round
from cw.review_engine.skill import SkillResolutionError
from cw.review_engine_summary_io import EngineSummaryError, load_review_engine_summary
from cw.review_findings import ReviewerRunFailure
from cw.review_workspace import _parse_marker, record_failure
from cw.review_workspace._rounds import _active_pair

MAX_VALIDATE_ERROR_LINES = 20

_VALIDATE_HELP = (
    "Validate an engine summary JSON file against the cw-owned schema. "
    "Exits 0 when valid, 1 on any rejection."
)
_ALLOW_IN_WORKER_MSG = (
    "cw review engine allow is operator-only: refused inside a dispatch worker"
)


@review.group(name="engine")
def engine() -> None:
    """Validate and inspect repo review engine summaries (RFC 0017).

    The summary schema is cw-owned; the repo's own review skill is the engine
    that writes it. ``validate`` is the only subcommand.
    """


def _render_issue_lines(error: EngineSummaryError) -> list[str]:
    """One stderr line per issue, capped, with an omitted-count trailer."""
    lines = [
        f"{issue.reason}: {issue.field_path or '-'}: {issue.message}"
        for issue in error.issues
    ]
    if len(lines) <= MAX_VALIDATE_ERROR_LINES:
        return lines
    omitted = len(lines) - MAX_VALIDATE_ERROR_LINES
    return [
        *lines[:MAX_VALIDATE_ERROR_LINES],
        f"... {omitted} more line(s) omitted",
    ]


@engine.command(name="validate", help=_VALIDATE_HELP)
@click.argument("file_path", metavar="FILE", type=click.Path(path_type=Path))
def validate(file_path: Path) -> None:
    try:
        summary = load_review_engine_summary(file_path)
    except EngineSummaryError as exc:
        for line in _render_issue_lines(exc):
            click.echo(line, err=True)
        raise click.exceptions.Exit(1) from exc
    click.echo(
        "review engine summary valid: "
        f"schema_version={summary.schema_version} "
        f"verdict={summary.verdict} coverage={summary.coverage}"
    )


def _context_path(worktree: Path, context_path: Path) -> Path:
    return context_path if context_path.is_absolute() else worktree / context_path


def _load_context(
    worktree: Path, context_path: Path, default_branch: str | None
) -> EngineContext:
    return read_engine_context(
        _context_path(worktree, context_path), default_branch=default_branch
    )


def _validate_engine_workspace(
    worktree: Path, *, context: EngineContext, round_id: str
) -> None:
    """Validate the engine's active round without reconciling or mutating it."""
    if context.ticket_id is None:
        message = "engine context has no ticket_id"
        raise click.ClickException(message)

    _directory, marker_path = _active_pair(worktree.resolve())
    marker = _parse_marker(marker_path)
    if marker.ticket != context.ticket_id:
        message = (
            "active review round belongs to ticket "
            f"{marker.ticket!r}, not engine context ticket {context.ticket_id!r}"
        )
        raise click.ClickException(message)
    if marker.round_id != round_id:
        message = (
            "active review round is "
            f"{marker.round_id!r}, not requested engine round {round_id!r}"
        )
        raise click.ClickException(message)
    if marker.status != "in_progress":
        message = f"active review round is not in progress: {marker.status!r}"
        raise click.ClickException(message)
    if marker.roles.get(ENGINE_ROLE) != "pending":
        message = f"reviewer role {ENGINE_ROLE!r} is not pending in the active round"
        raise click.ClickException(message)


_CONTEXT_OPTION_HELP = (
    "The worker context file, relative to --worktree unless absolute. "
    "Carries review.engine, the ticket and the client."
)


@engine.command(name="preflight")
@click.option(
    "--diff-base",
    required=True,
    help="The base commit the branch diffs from (the fork point).",
)
@click.option(
    "--context",
    "context_path",
    default=HOOK_CONTEXT_RELATIVE_PATH,
    show_default=True,
    type=click.Path(path_type=Path),
    help=_CONTEXT_OPTION_HELP,
)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@click.option(
    "--default-branch",
    default=None,
    help="Default branch to read the engine skill from (default: the snapshot).",
)
@handle_errors
def preflight_command(
    diff_base: str,
    context_path: Path,
    worktree: Path,
    default_branch: str | None,
) -> None:
    """Park or run the engine round: does the branch touch the engine skill?

    Exit 0 runs the round. Exit 3 parks it (review_engine_modified): the JSON
    lists the touched paths. Anything else exits 1.
    """
    context = _load_context(worktree, context_path, default_branch)
    result = run_preflight(worktree, diff_base=diff_base, context=context)
    click.echo(result.model_dump_json())
    if result.decision == "park":
        raise click.exceptions.Exit(EXIT_PARK)


@engine.command(name="materialize")
@click.option(
    "--round-id", required=True, help="The engine round id (one path segment)."
)
@click.option(
    "--expect-head", required=True, help="The HEAD sha the round is pinned to."
)
@click.option(
    "--context",
    "context_path",
    default=HOOK_CONTEXT_RELATIVE_PATH,
    show_default=True,
    type=click.Path(path_type=Path),
    help=_CONTEXT_OPTION_HELP,
)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@click.option(
    "--default-branch",
    default=None,
    help="Default branch to read the engine skill from (default: the snapshot).",
)
@handle_errors
def materialize_command(
    round_id: str,
    expect_head: str,
    context_path: Path,
    worktree: Path,
    default_branch: str | None,
) -> None:
    """Copy the default-branch engine skill into the round directory.

    Prints {skill_dir, skill_md, summary_path, args_line}. Exit 0 ok; exit 3
    records an engine_unresolvable round failure; exit 1 a precondition fault.
    """
    context = _load_context(worktree, context_path, default_branch)
    check_round_id(round_id)
    _validate_engine_workspace(worktree, context=context, round_id=round_id)
    try:
        materialized = materialize_skill(
            worktree, round_id=round_id, expect_head=expect_head, context=context
        )
    except (MaterializeError, SkillResolutionError) as exc:
        cause = getattr(exc, "cause", str(exc))
        record_failure(
            worktree,
            role=ENGINE_ROLE,
            failure=ReviewerRunFailure(
                role=ENGINE_ROLE, reason=str(EngineRoundFailure.ENGINE_UNRESOLVABLE)
            ),
        )
        click.echo(json.dumps({"failed": True, "cause": cause, "skill": context.skill}))
        raise click.exceptions.Exit(EXIT_ROUND_FAILED) from exc
    click.echo(
        json.dumps(
            {
                "skill_dir": str(materialized.skill_dir),
                "skill_md": str(materialized.skill_md),
                "summary_path": str(materialized.summary_path),
                "args_line": materialized.args_line,
            }
        )
    )


@engine.command(name="record")
@click.option(
    "--round-id", required=True, help="The engine round id (one path segment)."
)
@click.option(
    "--expect-head", required=True, help="The HEAD sha the round is pinned to."
)
@click.option(
    "--summary",
    "summary_path",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="The summary file. Default: the round's summary.json.",
)
@click.option(
    "--context",
    "context_path",
    default=HOOK_CONTEXT_RELATIVE_PATH,
    show_default=True,
    type=click.Path(path_type=Path),
    help=_CONTEXT_OPTION_HELP,
)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
)
@click.option(
    "--default-branch",
    default=None,
    help="Default branch to read the engine skill from (default: the snapshot).",
)
@handle_errors
def record_command(
    round_id: str,
    expect_head: str,
    summary_path: Path | None,
    context_path: Path,
    worktree: Path,
    default_branch: str | None,
) -> None:
    """Validate the engine summary and record the round's document.

    Prints the RecordResult JSON. Exit 0 recorded; exit 3 a failure recorded
    (the caller parks it as agent_block with retry_eligible); exit 1 a fault.
    """
    context = _load_context(worktree, context_path, default_branch)
    result = record_engine_round(
        worktree,
        round_id=round_id,
        expect_head=expect_head,
        summary_path=summary_path,
        skill=context.skill,
    )
    click.echo(json.dumps(asdict(result)))
    if result.result == "failed":
        raise click.exceptions.Exit(EXIT_ROUND_FAILED)


@engine.command(name="allow")
@click.option("--ticket", "ticket_id", required=True, help="The parked ticket.")
@click.option("--head-sha", required=True, help="The full 40-hex HEAD to allow.")
@click.option(
    "--reason", required=True, help="Why the engine-path change is acceptable."
)
@click.option(
    "--client", "-c", "client", default=None, help="Client the ticket belongs to."
)
@handle_errors
def allow_command(
    ticket_id: str, head_sha: str, reason: str, client: str | None
) -> None:
    """Allow one engine round on HEAD for a row parked review_engine_modified.

    Operator-only: refused inside a dispatch worker. Requeue the row afterwards
    with `cw dev-queue requeue`. A later commit needs a new allow.
    """
    refuse_inside_dispatch_worker(_ALLOW_IN_WORKER_MSG)
    resolved = resolve_client(ticket_id, load_effective_config(), client)
    recorded = record_review_engine_allow(
        ticket_id, resolved, head_sha, reason, bounded=True
    )
    click.echo(json.dumps(recorded))
