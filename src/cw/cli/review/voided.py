"""``cw review check-voided`` — the Claude path's voided-findings hop (#1814).

``cw review check-voided <path>`` runs between consolidate and adjudicate: it
suppresses findings a prior pass's operator decision already settled, and
renders the durable record of those decisions back out for posting to the
ticket. It is the Claude-native half of a mechanism the codex backend reaches
through ``cw.codex_review`` instead — same library function, same outcome, no
coordinating session required on that side.

#2319: a ``new_voided_entries`` entry whose content anchor matches no accepted
finding is almost always a mis-copied anchor. Recorded silently, it would void
nothing, and the finding it was meant to settle would re-appear on the next
pass with no explanation. So the command checks the NEW entries (never the
prior voids in ``comment_bodies``, which legitimately stop matching once their
code is fixed), counts the distinct unmatched anchors in
``verdict.unmatched_voided_count``, warns about each on stderr, and exits 1
after printing the full JSON — unless ``--allow-unmatched-voided`` is given.
That check is CLI-local: ``apply_voided_suppression`` is shared with the codex
backend and stays unchanged.

Split out of :mod:`cw.cli.review.commands` (#2319), which would otherwise have
grown past the module-size ceiling. The request/response envelopes
(``_CheckVoidedInput`` / ``_CheckVoidedOutput``) and ``_utc_now_iso`` stay
there: the envelopes annotate ``ReviewVerdict`` at runtime for pydantic under
that module's existing ``TC001`` per-file entry, and ``settle`` shares the
clock helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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
    find_voided_matches,
    parse_voided_findings_block,
    render_voided_findings_block,
)
from cw.review_adjudication._voided import _entry_fingerprint

if TYPE_CHECKING:
    from cw.review_findings import ReviewVerdict

_ALLOW_UNMATCHED_VOIDED_HELP = (
    "Exit 0 even when a new_voided_entries entry matches no accepted finding "
    "in this verdict (the default is to print the full JSON and then exit 1). "
    "Each unmatched entry is still warned about on stderr and counted in "
    "verdict.unmatched_voided_count, and is also written to "
    "--voided-findings-out. Use only to deliberately void a finding this "
    "verdict does not carry."
)
_VOIDED_FINDINGS_OUT_HELP = (
    "Also render the merged voided-findings record to this path, as a "
    "postable '## Voided Review Findings' ticket comment. Nothing is written "
    "when there is no void to record. When an unmatched new entry makes the "
    "command exit 1, the record is still written first and holds the prior "
    "voids plus only the new entries that matched."
)
#: One stderr line per distinct unmatched anchor. ``summary`` is rendered with
#: ``!r`` so a multi-line summary stays on one line.
_UNMATCHED_WARNING = (
    "warning: new_voided_entries entry matched no accepted finding: "
    "severity={severity} file={file} summary={summary!r}"
)
_UNMATCHED_REFUSED = (
    "error: unmatched new_voided_entries: {n}; exiting 1. The verdict JSON on "
    "stdout is complete. Copy severity, file, summary and evidence verbatim "
    "from the finding and re-run, or pass --allow-unmatched-voided to accept "
    "them."
)
_UNMATCHED_ALLOWED = (
    "note: unmatched new_voided_entries: {n}; continuing because "
    "--allow-unmatched-voided was passed."
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


def _partition_new_entries(
    verdict: ReviewVerdict, entries: list[VoidedFinding]
) -> tuple[list[VoidedFinding], list[VoidedFinding]]:
    """Split *entries* into ``(matched, unmatched)`` against *verdict* (#2319).

    An entry matches when :func:`find_voided_matches` maps at least one
    accepted finding to it — the one matcher suppression itself uses, so the
    two can never disagree about what matched. ``matched`` keeps every
    matching entry in order, duplicates included, because it feeds the record.
    ``unmatched`` is deduped by the matcher's own content fingerprint in
    first-seen order, so its length is the number of distinct unmatched
    anchors and two whitespace variants of one anchor warn once.
    """
    matched: list[VoidedFinding] = []
    unmatched: list[VoidedFinding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        if find_voided_matches(verdict.accepted, [entry]):
            matched.append(entry)
            continue
        key = _entry_fingerprint(entry)
        if key not in seen:
            seen.add(key)
            unmatched.append(entry)
    return matched, unmatched


def _warn_unmatched(unmatched: list[VoidedFinding], *, allowed: bool) -> None:
    """One stderr warning per unmatched anchor, then the outcome trailer."""
    for entry in unmatched:
        click.echo(
            _UNMATCHED_WARNING.format(
                severity=entry.severity, file=entry.file, summary=entry.summary
            ),
            err=True,
        )
    trailer = _UNMATCHED_ALLOWED if allowed else _UNMATCHED_REFUSED
    click.echo(trailer.format(n=len(unmatched)), err=True)


def _write_voided_record(path: Path, entries: list[VoidedFinding]) -> None:
    """Render *entries* as the postable voided-findings record at *path*."""
    rendered = render_voided_findings_block(entries)
    # "" means there is nothing to record — omit the artifact entirely
    # rather than leave an empty one behind, same rule as
    # --deferred-findings-out.
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)


@review.command(name="check-voided")
@click.argument("path")
@click.option(
    "--voided-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=_VOIDED_FINDINGS_OUT_HELP,
)
@click.option(
    "--allow-unmatched-voided",
    is_flag=True,
    default=False,
    help=_ALLOW_UNMATCHED_VOIDED_HELP,
)
@handle_errors
def review_check_voided(
    path: str, voided_findings_out: Path | None, allow_unmatched_voided: bool
) -> None:
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

    Only `new_voided_entries` are checked for a match: a prior pass's void in
    `comment_bodies` that matches nothing is normal (the code it described was
    fixed or rewritten). A new entry whose content anchor matches no accepted
    finding in this verdict is almost always a mis-copied anchor that would
    record a void suppressing nothing. It is warned about on stderr (one line
    per distinct anchor), counted in the printed
    `verdict.unmatched_voided_count`, and makes the command exit 1 AFTER the
    full JSON is printed, unless --allow-unmatched-voided is given, which
    keeps exit 0 and the warnings. Suppressions for entries that DID match
    still apply, and their `review.finding_voided` events still fire, before
    that exit. With --voided-findings-out the record is written before the
    exit: on the refused exit it holds the prior voids plus only the new
    entries that matched; with --allow-unmatched-voided it holds the
    unmatched ones too.

    On success: exits 0, prints {"verdict": ..., "adjudications": [...]} to
    stdout. Append the adjudications verbatim to your ADJUDICATIONS array.
    On an unmatched new entry (without --allow-unmatched-voided): exits 1 after
    printing the same JSON to stdout and the stderr lines above.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _CheckVoidedInput)
    prior = parse_voided_findings_block(parsed.comment_bodies)
    new_entries = [_stamp_voided_at(entry) for entry in parsed.new_voided_entries]
    matched, unmatched = _partition_new_entries(parsed.verdict, new_entries)
    suppressed, adjudications = apply_voided_suppression(
        parsed.verdict, [*prior, *new_entries], ticket_id=parsed.ticket_id
    )
    # Stamped AFTER suppression so it covers both of its return paths, and
    # recomputed rather than carried: the count describes this call's entries.
    verdict = suppressed.model_copy(update={"unmatched_voided_count": len(unmatched)})
    refuse = bool(unmatched) and not allow_unmatched_voided

    if voided_findings_out is not None:
        # Built by partition, never by filtering the merged list on
        # fingerprint: a prior void sharing an unmatched new entry's anchor
        # is a durable record and must survive the refused run.
        recorded = matched if refuse else new_entries
        _write_voided_record(voided_findings_out, [*prior, *recorded])

    output = _CheckVoidedOutput(verdict=verdict, adjudications=adjudications)
    click.echo(output.model_dump_json(indent=2))
    if unmatched:
        _warn_unmatched(unmatched, allowed=not refuse)
    if refuse:
        raise click.exceptions.Exit(1)
