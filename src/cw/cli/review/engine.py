"""The ``cw review engine`` group and its ``validate`` command (#3036).

``validate FILE`` runs one engine summary through the bounded loader
(``cw.review_engine_summary_io``). Exit 0 prints one stdout line naming the
schema version, verdict and coverage; any rejection exits 1 with
``<reason>: <field.path or ->: <message>`` lines on stderr, at most
``MAX_VALIDATE_ERROR_LINES`` of them plus a count of the lines omitted. Exit 2
stays click's usage error.

Read-only: it emits no event and writes nothing. It reads no client or queue
state, takes no stdin (``-`` is an ordinary path) and has no ``--json``. It
renders its own stderr format, so it does not use ``handle_errors``.
"""

from __future__ import annotations

from pathlib import Path

import click

from cw.cli.review._group import review
from cw.review_engine_summary_io import EngineSummaryError, load_review_engine_summary

MAX_VALIDATE_ERROR_LINES = 20

_VALIDATE_HELP = (
    "Validate an engine summary JSON file against the cw-owned schema. "
    "Exits 0 when valid, 1 on any rejection."
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
