"""The ``.cw/deferred-findings.md`` artifact: render, parse, merge (#1805/#1840).

Round-trips an adjudication list through the ``DEFERRED-REVIEW-FINDINGS``
markdown sentinel Stage 4 copies verbatim into the PR body. Unlike its sibling
:func:`~cw.review_adjudication._voided.parse_voided_findings_block`, the parser
here fails CLOSED — it reads this command's own durable output, so silently
dropping content it cannot read is the very "prior records lost" failure #1840
exists to remove.

Split out of the single ``review_adjudication.py`` module (#2011); import these
names from :mod:`cw.review_adjudication`, not from this private submodule.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cw.review_adjudication._models import Adjudication

if TYPE_CHECKING:
    from cw.review_findings import Severity

_DEFERRED_MD_TITLE = "# Deferred Review Findings"
# One line in the rendered artifact; split here only to stay inside the
# 88-column lint limit.
_DEFERRED_MD_PROVENANCE = (
    "<!-- written by Stage 3 (auto-dev-review.md), consumed by "
    "Stage 4 Step 4d (auto-dev-finalize.md) -->"
)
_DEFERRED_MD_HEADING = "## Review adjudication"
_REJECTED_MD_HEADING = "Rejected (intentional / documented tradeoff):"
_DEFERRED_SENTINEL = "DEFERRED-REVIEW-FINDINGS"

#: Severity a ``reject`` entry parsed back out of the artifact carries (#1840).
#:
#: The documented "Rejected (intentional / documented tradeoff):" bullet
#: records file, summary and rationale only — severity was never part of that
#: line shape, so there is nothing to recover on parse and nothing to render
#: back out. A fixed placeholder is used rather than a guess: it is inert by
#: construction (rendering ignores a reject entry's severity, and
#: :func:`merge_deferred_adjudications`'s callers normalize both sides of the
#: merge onto it), and ``SHOULD_FIX`` cannot escalate a gate the way a
#: speculative ``MUST_FIX`` could if one ever did read it.
REJECTED_ENTRY_SEVERITY: Severity = "SHOULD_FIX"

#: The lines the artifact carries that are structure, not entries. Recognized
#: explicitly so :func:`parse_deferred_findings_md` can fail closed on
#: everything else instead of silently skipping content it does not understand.
_DEFERRED_MD_STRUCTURAL_LINES = frozenset(
    {
        _DEFERRED_MD_TITLE,
        _DEFERRED_MD_PROVENANCE,
        _DEFERRED_MD_HEADING,
        _REJECTED_MD_HEADING,
    }
)

#: One rejected bullet. The ``[round N, <date>]`` prefix is an OPTIONAL group,
#: so this single pattern matches both the stamped shape and the pre-#1840
#: shape (#1840's legacy-tolerance decision) — a partially-present prefix
#: matches neither and falls through to a hard error.
_REJECTED_LINE_RE = re.compile(
    r"- (?:\[round (?P<round>\d+), (?P<recorded_at>[^\]]+)\] )?"
    r'(?P<file>.+?) — "(?P<summary>.*)" — (?P<rationale>.+)'
)

#: One deferred sentinel-block entry. The trailing ``round:``/``recorded_at:``
#: pair is one optional group covering BOTH lines: present together, or absent
#: together. A lone ``round:`` line satisfies neither shape, which is the
#: intended hard error rather than a silently half-read entry.
_DEFERRED_ENTRY_RE = re.compile(
    r"- severity: (?P<severity>\S+)\n"
    r'  summary: "(?P<summary>.*)"\n'
    r"  file: (?P<file>.+)\n"
    r'  rationale: "(?P<rationale>.*)"'
    r"(?:\n  round: (?P<round>\d+)\n  recorded_at: (?P<recorded_at>\S+))?"
)

#: Splits a sentinel body into per-entry chunks on each entry's opening line.
_DEFERRED_ENTRY_SPLIT_RE = re.compile(r"\n(?=- severity: )")

#: The deferred sentinel block's body, mirroring :data:`_VOIDED_BLOCK_RE`'s
#: shape (the sibling extraction this parser is modelled on — its shape only,
#: never its fail-open degrade contract).
_DEFERRED_BLOCK_RE = re.compile(
    rf"<!--\s*{_DEFERRED_SENTINEL}\s*(?P<body>.*?)\s*{_DEFERRED_SENTINEL}\s*-->",
    re.DOTALL,
)


def render_deferred_findings_md(adjudications: list[Adjudication]) -> str:
    """Render *adjudications* into the ``.cw/deferred-findings.md`` artifact.

    Reproduces the block documented in ``.claude/commands/auto-dev-review.md``
    byte-for-byte — Stage 4 Step 4d copies it into the PR body verbatim and the
    PR-hygiene sweep greps the ``DEFERRED-REVIEW-FINDINGS`` sentinels
    literally, so the shape is a contract, not a style choice.

    Returns ``""`` when there is nothing to record (every finding was fixed):
    the documented rule is "omit the file entirely", so the caller skips the
    write rather than leaving an empty artifact behind.

    ``operator_action`` entries (#1817) fall through untouched by design — an
    operator-actionable finding is posted directly to the ticket at
    Checkpoint 3a under its own header, not deferred to PR-body time, so
    rendering it here would duplicate it onto a surface whose whole purpose
    (Step H3's merge-time ticket sweep) it has already bypassed.
    """
    rejected = [a for a in adjudications if a.outcome == "reject"]
    deferred = [a for a in adjudications if a.outcome == "defer"]
    if not rejected and not deferred:
        return ""

    sections = [
        f"{_DEFERRED_MD_TITLE}\n{_DEFERRED_MD_PROVENANCE}",
        _DEFERRED_MD_HEADING,
    ]
    if rejected:
        rows = "\n".join(_render_rejected_row(a) for a in rejected)
        sections.append(f"{_REJECTED_MD_HEADING}\n{rows}")
    if deferred:
        entries = "\n".join(_render_deferred_entry(a) for a in deferred)
        sections.append(
            f"<!-- {_DEFERRED_SENTINEL}\n{entries}\n{_DEFERRED_SENTINEL} -->"
        )
    return "\n\n".join(sections) + "\n"


def _render_rejected_row(entry: Adjudication) -> str:
    """One "Rejected (…)" bullet, round/date-prefixed only when stamped.

    An unstamped entry (``round is None`` — legacy, or never through the CLI
    write path) renders in the bare pre-#1840 shape, so re-rendering a parsed
    legacy entry reproduces it byte-for-byte instead of fabricating fields it
    never carried.
    """
    stamp = f"[round {entry.round}, {entry.recorded_at}] " if _is_stamped(entry) else ""
    return f'- {stamp}{entry.file} — "{entry.summary}" — {entry.rationale}'


def _render_deferred_entry(entry: Adjudication) -> str:
    """One sentinel-block entry, with trailing round/date lines when stamped.

    Same rule as :func:`_render_rejected_row`: unstamped renders bare.
    """
    stamp = (
        f"\n  round: {entry.round}\n  recorded_at: {entry.recorded_at}"
        if _is_stamped(entry)
        else ""
    )
    return (
        f"- severity: {entry.severity}\n"
        f'  summary: "{entry.summary}"\n'
        f"  file: {entry.file}\n"
        f'  rationale: "{entry.rationale}"{stamp}'
    )


def _is_stamped(entry: Adjudication) -> bool:
    """True when the CLI recorded round/date context on *entry*."""
    return entry.round is not None


def _adjudication_from_rejected_line(line: str) -> Adjudication:
    """One ``Adjudication`` from a rejected bullet, or raise ``ValueError``."""
    match = _REJECTED_LINE_RE.fullmatch(line)
    if match is None:
        msg = f"unrecognized deferred-findings rejected line: {line!r}"
        raise ValueError(msg)
    raw_round = match.group("round")
    return Adjudication(
        severity=REJECTED_ENTRY_SEVERITY,
        file=match.group("file"),
        summary=match.group("summary"),
        outcome="reject",
        rationale=match.group("rationale"),
        round=int(raw_round) if raw_round is not None else None,
        recorded_at=match.group("recorded_at") or "",
    )


def _parse_rejected_lines(text: str) -> list[Adjudication]:
    """Every rejected bullet in *text* (the artifact minus its blocks).

    Fails closed: any non-blank line that is neither a recognized structural
    line nor a well-formed bullet raises, rather than being skipped. Silently
    ignoring content we cannot read is how a merge-then-single-write artifact
    loses the very records it exists to preserve.
    """
    entries: list[Adjudication] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line in _DEFERRED_MD_STRUCTURAL_LINES:
            continue
        entries.append(_adjudication_from_rejected_line(line))
    return entries


def _parse_deferred_entries(body: str) -> list[Adjudication]:
    """Every entry in one ``DEFERRED-REVIEW-FINDINGS`` sentinel body."""
    entries: list[Adjudication] = []
    for chunk in _DEFERRED_ENTRY_SPLIT_RE.split(body.strip("\n")):
        match = _DEFERRED_ENTRY_RE.fullmatch(chunk.rstrip("\n"))
        if match is None:
            msg = f"unrecognized deferred-findings entry: {chunk!r}"
            raise ValueError(msg)
        raw_round = match.group("round")
        # model_validate, not the constructor: `severity` comes off the text
        # as a plain str, so pydantic is what turns an unrecognized value into
        # the ValueError this parser's fail-closed contract promises.
        entries.append(
            Adjudication.model_validate(
                {
                    "severity": match.group("severity"),
                    "file": match.group("file"),
                    "summary": match.group("summary"),
                    "outcome": "defer",
                    "rationale": match.group("rationale"),
                    "round": int(raw_round) if raw_round is not None else None,
                    "recorded_at": match.group("recorded_at") or "",
                }
            )
        )
    return entries


def parse_deferred_findings_md(text: str) -> list[Adjudication]:
    """Read a rendered ``.cw/deferred-findings.md`` back into entries (#1840).

    The inverse of :func:`render_deferred_findings_md`, so a second
    ``cw review adjudicate --deferred-findings-out`` call in one Stage 3 pass
    can merge into the prior call's record instead of replacing it.

    **Fails closed, deliberately unlike its sibling
    :func:`parse_voided_findings_block`.** That parser degrades a malformed
    block to ``[]`` because it reads third-party ticket-comment prose, where a
    missed suppression merely re-surfaces a finding an operator can act on.
    This one reads *this command's own durable output*: silently dropping prior
    records on a self-parse failure is precisely the "prior content lost"
    failure #1840 exists to remove, shape-shifted. Content matching neither the
    stamped shape nor the documented pre-#1840 shape raises ``ValueError``,
    naming the offending line or entry.

    Empty or whitespace-only *text* is NOT an error — it means "first call,
    nothing to merge" and returns ``[]``. (An absent file is the caller's
    business; there is no path to read.)

    Rejected entries come back with :data:`REJECTED_ENTRY_SEVERITY` and no
    line anchor or evidence: the rendered shape never recorded those, so a
    round-trip is lossy in exactly the fields the artifact does not carry.
    Entries are returned rejected-first, then deferred — the same order
    :func:`render_deferred_findings_md` writes them in, so parse → render is
    byte-stable.
    """
    if not text.strip():
        return []
    if _DEFERRED_MD_TITLE not in text:
        msg = (
            "deferred-findings content carries no "
            f"{_DEFERRED_MD_TITLE!r} title — refusing to guess at its shape "
            "or overwrite it"
        )
        raise ValueError(msg)

    deferred: list[Adjudication] = []
    outside: list[str] = []
    cursor = 0
    for match in _DEFERRED_BLOCK_RE.finditer(text):
        outside.append(text[cursor : match.start()])
        deferred.extend(_parse_deferred_entries(match.group("body")))
        cursor = match.end()
    outside.append(text[cursor:])
    return [*_parse_rejected_lines("".join(outside)), *deferred]


def _deferred_merge_fingerprint(
    entry: Adjudication,
) -> tuple[str, str, int, int, str, str, str, str]:
    """The content identity :func:`merge_deferred_adjudications` dedupes on.

    A genuinely third, wider field set — NOT a reuse of :func:`_entry_key`
    (4 fields, location-only, used to match an entry against a finding) or
    :func:`_entry_fingerprint` (4 fields, ``VoidedFinding``'s content anchor).
    Both of those deliberately exclude ``outcome``; this key REQUIRES it,
    because #1840's binding decision is that a genuine outcome flip at one
    location (REJECT on an earlier round, DEFER on a later one) accumulates as
    two records rather than collapsing to one. ``rationale`` rides alongside
    for the same reason ``evidence`` disambiguates the coarser key: a
    same-outcome re-adjudication with a different rationale is new
    information, not noise.

    ``evidence``/``summary`` compare verbatim — deliberately not through
    :func:`_normalize_finding_text`. That normalization exists to match a
    suppression across independently-typed prose; this key's job is exact
    re-adjudication detection inside one artifact's own output.

    ``None`` line endpoints map to ``-1``, the same convention as
    :func:`_location_key`, so the tuple stays plainly hashable.
    """
    return (
        entry.severity,
        entry.file,
        entry.line_start if entry.line_start is not None else -1,
        entry.line_end if entry.line_end is not None else -1,
        entry.evidence,
        entry.summary,
        entry.outcome,
        entry.rationale,
    )


def merge_deferred_adjudications(
    prior: list[Adjudication], new: list[Adjudication]
) -> list[Adjudication]:
    """*prior* then *new*, deduped against *prior* by content fingerprint (#1840).

    A *new* entry is dropped only when it fingerprint-matches something
    already in *prior* — the "identical re-adjudication collapses" case. An
    already-recorded decision keeps its original round/date context (or its
    absence, for a legacy entry) rather than being re-stamped with today's.
    ``round``/``recorded_at`` are excluded from the fingerprint precisely so
    a re-adjudication of the same content across rounds collapses.

    *new* entries are never deduped against each other. The CLI's
    artifact-shape projection (:func:`Adjudication.round`'s stamping call
    site) nulls ``line_start``/``line_end``/``evidence`` before this runs —
    fields ``prior`` can never carry either, since the rendered artifact
    never records them — so the fingerprint has to live in that reduced
    field space to let a genuine cross-round re-adjudication match at all.
    But within one round, every entry in *new* comes from a distinct
    ``AcceptedFinding`` at a distinct location; two of them sharing that
    reduced fingerprint (e.g. identical templated summary/rationale text at
    different lines) are two different findings, not a duplicate.
    Fingerprint-deduping *new* against itself would silently drop one of
    them from the audit trail — exactly the failure #1840 exists to
    eliminate, reintroduced one layer down.
    """
    seen = {_deferred_merge_fingerprint(entry) for entry in prior}
    merged = list(prior)
    for entry in new:
        if _deferred_merge_fingerprint(entry) in seen:
            continue
        merged.append(entry)
    return merged
