"""``cw review check-voided`` — the Claude path's voided-findings hop (#1814).

``cw review check-voided <path>`` runs between consolidate and adjudicate: it
suppresses findings a prior pass's operator decision already settled, and
renders the durable record of those decisions back out for posting to the
ticket. It is the Claude-native half of a mechanism the codex backend reaches
through ``cw.codex_review`` instead — same library function, same outcome, no
coordinating session required on that side.

Split out of :mod:`cw.cli.review.commands` (#2319), which would otherwise have
grown past the module-size ceiling. The request/response envelopes
(``_CheckVoidedInput`` / ``_CheckVoidedOutput``) and ``_utc_now_iso`` stay
there: the envelopes annotate ``ReviewVerdict`` at runtime for pydantic under
that module's existing ``TC001`` per-file entry, and ``settle`` shares the
clock helper.
"""

from __future__ import annotations

from pathlib import Path

import click

from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors
from cw.cli.review._group import _parse_payload_or_exit, review
from cw.cli.review.commands import (
    _CheckVoidedInput,
    _CheckVoidedOutput,
    _utc_now_iso,
)
from cw.review_adjudication import (
    VoidedFinding,
    apply_voided_suppression,
    parse_voided_findings_block,
    render_voided_findings_block,
)


def _stamp_voided_at(entry: VoidedFinding) -> VoidedFinding:
    """Fill a blank ``voided_at`` with now, leaving a supplied one alone.

    The coordinating session supplies the judgment; the CLI supplies the
    clock. Re-stamping an entry that already carries a date would rewrite
    history on every idempotent re-post.
    """
    if entry.voided_at.strip():
        return entry
    return entry.model_copy(update={"voided_at": _utc_now_iso()})


@review.command(name="check-voided")
@click.argument("path")
@click.option(
    "--voided-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also render the merged voided-findings record to this path, as a "
        "postable '## Voided Review Findings' ticket comment. Nothing is "
        "written when there is no void to record."
    ),
)
@handle_errors
def review_check_voided(path: str, voided_findings_out: Path | None) -> None:
    """Suppress findings an operator already voided on a prior pass (#1814).

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the
    ReviewVerdict from `cw review consolidate`>, "ticket_id": "<id>",
    "comment_bodies": ["<live-fetched ticket comment>", ...],
    "new_voided_entries": [{"severity": ..., "file": ..., "summary": ...,
    "evidence": ..., "operator_comment_id": ..., "operator_comment_excerpt":
    ..., "original_rationale": ...}]}.

    A finding is suppressed only when its content anchor — severity, file,
    summary, and evidence — matches a recorded void exactly. File and line
    position are deliberately NOT the identity: a voided finding whose code
    moved still matches, and a genuinely new finding at the voided one's old
    line never does.

    Each suppression stamps `disposition="rejected"`, drops the finding from
    `must_fix`/`blocking`, and emits one `review.finding_voided` event
    correlated to `ticket_id`.

    On success: exits 0, prints {"verdict": ..., "adjudications": [...]} to
    stdout. Append the adjudications verbatim to your ADJUDICATIONS array.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _CheckVoidedInput)
    merged = [
        *parse_voided_findings_block(parsed.comment_bodies),
        *(_stamp_voided_at(entry) for entry in parsed.new_voided_entries),
    ]
    verdict, adjudications = apply_voided_suppression(
        parsed.verdict, merged, ticket_id=parsed.ticket_id
    )

    if voided_findings_out is not None:
        rendered = render_voided_findings_block(merged)
        # "" means there is nothing to record — omit the artifact entirely
        # rather than leave an empty one behind, same rule as
        # --deferred-findings-out.
        if rendered:
            voided_findings_out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(voided_findings_out, rendered)

    output = _CheckVoidedOutput(verdict=verdict, adjudications=adjudications)
    click.echo(output.model_dump_json(indent=2))
