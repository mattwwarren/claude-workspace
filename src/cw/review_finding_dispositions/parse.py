"""The disposition marker: render it, parse it, and build a ledger for it.

:func:`render_finding_disposition_block` and
:func:`parse_finding_disposition_block` are the two directions of the
``## Review Finding Dispositions`` ticket-comment marker, and
:func:`build_finding_disposition_ledger` is the producer-side twin ``cw review
settle`` mints a marker through. Split out of the flat
``review_finding_dispositions.py`` (#2498).

Round 4 of #2210 closed the layer underneath the provenance checks: a record
was recognised by SHAPE alone, so a model-authored finding summary carrying a
well-formed sentinel block minted a suppression nobody authored, and the
provenance checks were the only thing standing in front of it. Two independent
layers now sit in front of them — the renderer escapes marker syntax out of
every piece of untrusted text it interpolates
(:func:`cw.review_markers.neutralise_marker_syntax`), and
:data:`_DISPOSITION_BLOCK_RE` honours a block only at the structural POSITION
:func:`render_finding_disposition_block` emits it at. Position, not just
shape, is what makes a record.

Imports nothing from ``cw`` at module scope beyond :mod:`cw.review_markers`
and this package's own submodules (see the package docstring's "Import
discipline" section).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from cw.review_finding_dispositions._constants import _LOGGER_NAME
from cw.review_finding_dispositions.model import FindingDisposition, _disposition_key
from cw.review_finding_dispositions.provenance import (
    _provenance_gaps,
    merge_finding_dispositions,
    partition_enforceable_dispositions,
)
from cw.review_markers import DISPOSITION_SENTINEL

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.review_markers import RefusedDisposition

_log = logging.getLogger(_LOGGER_NAME)

#: Ticket-comment header the ledger block renders under, and the sentinel that
#: is the actual contract. Mirrors ``_VOIDED_MD_TITLE``/``_VOIDED_SENTINEL``
#: (#1814) — a JSON payload inside an HTML comment, mechanically parsed. NOT
#: ``auto-dev-preflight-resolutions``' free-prose grammar, which would need an
#: LLM to read and would reintroduce the fragility class #1805 removed.
_DISPOSITION_MD_TITLE = "## Review Finding Dispositions"
#: Bump when the sentinel's on-the-wire shape changes in a way a reader must
#: branch on, following ``_VOIDED_SCHEMA_VERSION``'s convention. #2210's
#: ``actor``/``reviewed_sha``/``summary`` fields deliberately did NOT bump it:
#: they are optional and defaulted, the model ignores unknown keys, and no
#: reader branches on their presence — a v1 marker and a v1 queue row written
#: either side of that change load identically. Same reasoning applies to
#: ``DEV_QUEUE_SCHEMA_VERSION``, which ``TicketTask.finding_dispositions``
#: rides on: the migration ladder fills defaults for absent TicketTask FIELDS,
#: and this is a nested model gaining defaulted ones.
_DISPOSITION_SCHEMA_VERSION = 1

#: A disposition record is recognised by POSITION as well as by shape (#2210
#: round 4). A comment carries a record only when it IS the marker: the
#: ``## Review Finding Dispositions`` title opens the body, and the sentinel
#: block follows it with nothing but whitespace between. That is byte for byte
#: what :func:`render_finding_disposition_block` produces and what ``cw review
#: settle --out`` hands the operator to post, including through
#: :func:`cw.gh.post_issue_comment`, which APPENDS its provenance marker and so
#: never displaces the title.
#:
#: The layer this adds is independent of the provenance checks below, and the
#: hole it closes is not hypothetical. The pipeline renders model-authored text
#: — a finding summary, a file path, quoted evidence — into the very comments
#: this parser reads on the next round, so a shape-only reader could not tell a
#: record an operator minted from one a REVIEWER wrote into its own finding
#: text. Rendered finding text is never at this position: it sits under the
#: verdict comment's own ``## Codex Review Verdict`` title, many lines in. The
#: renderer separately escapes the sentinel and the comment delimiters out of
#: every untrusted span (:func:`cw.review_markers.neutralise_marker_syntax`),
#: so an injection has to defeat both layers; the cost of this one is a single
#: anchored regex.
#:
#: ``\A`` (not ``^``): a title on some later line of a longer body does not
#: qualify, which is the whole point — ``re.MULTILINE`` would hand the
#: injection exactly the foothold this removes. One marker per comment; the
#: ledger's additive union across COMMENTS is unchanged and is how an operator
#: settles more findings later.
_DISPOSITION_BLOCK_RE = re.compile(
    r"\A[ \t\r\n]*"
    + re.escape(_DISPOSITION_MD_TITLE)
    + r"[ \t\r]*\n\s*"
    + rf"<!--\s*{DISPOSITION_SENTINEL}\s*(?P<body>.*?)"
    + rf"\s*{DISPOSITION_SENTINEL}\s*-->",
    re.DOTALL,
)


def render_finding_disposition_block(ledger: dict[str, FindingDisposition]) -> str:
    """Render *ledger* as the postable ``## Review Finding Dispositions`` comment.

    Embeds its own markdown header so the caller posts the returned text as-is,
    and carries the payload as JSON inside the HTML comment — both mirroring
    :func:`cw.review_adjudication.render_voided_findings_block`, and for the
    same reason: this record is read back by the codex backend, which has no
    LLM to interpret prose, so it must round-trip through ``json.loads`` while
    staying human-readable.

    Returns ``""`` for an empty ledger — nothing to record means no comment.
    """
    if not ledger:
        return ""
    payload = {
        "schema_version": _DISPOSITION_SCHEMA_VERSION,
        "dispositions": {
            key: entry.model_dump(mode="json") for key, entry in sorted(ledger.items())
        },
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"{_DISPOSITION_MD_TITLE}\n\n"
        f"<!-- {DISPOSITION_SENTINEL}\n{body}\n{DISPOSITION_SENTINEL} -->\n"
    )


def _parse_one_disposition_block(body: str) -> dict[str, FindingDisposition]:
    """Parse one sentinel body, degrading a malformed block to ``{}``."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _log.warning("auto-dev: ignoring malformed %s block", DISPOSITION_SENTINEL)
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("dispositions")
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, FindingDisposition] = {}
    for key, item in raw.items():
        try:
            entries[str(key)] = FindingDisposition.model_validate(item)
        except ValueError:
            _log.warning("auto-dev: ignoring malformed %s entry", DISPOSITION_SENTINEL)
    return entries


def parse_finding_disposition_block(
    comment_bodies: list[str],
) -> tuple[dict[str, FindingDisposition], list[RefusedDisposition]]:
    """Union every disposition sentinel across *comment_bodies*.

    Returns ``(enforceable, refused)``. Only a record that passes provenance
    reaches ``enforceable`` (#2210 round 3: validate first, write second — this
    is what the ledger is written FROM, so an invalid record must not be in
    it). Every record that fails is reported in ``refused`` instead, once per
    key, in key order, so the caller can log it and put it on the review
    output rather than losing it silently. Validation happens BEFORE the
    cross-comment fold, so a later invalid comment can never displace an
    earlier valid one for the same key.

    Fail-open throughout — a missing, truncated, or malformed block yields
    nothing and never raises, and one bad block never discards a good sibling.
    Same degrade contract, and same justification, as
    :func:`cw.review_adjudication.parse_voided_findings_block`: a review that
    could not read the ledger is strictly better than no review, and the missed
    suppression surfaces as the finding re-appearing, which an operator can act
    on.

    A key recorded validly in more than one comment resolves through
    :func:`merge_finding_dispositions` (newest ``recorded_at`` wins), which is
    the #1654 marker-supersession convention: an operator who changes their
    mind re-posts the marker rather than editing history.

    **Position is part of the grammar** (#2210 round 4): a body is read for a
    record only when it IS the marker — see :data:`_DISPOSITION_BLOCK_RE`. A
    sentinel block sitting inside rendered finding text, inside a fenced
    payload, or anywhere below other content is not a record and is not
    reported as a refusal either: nothing tried to settle anything, so there is
    no operator-facing refusal to raise.
    """
    merged: dict[str, FindingDisposition] = {}
    refused: dict[str, RefusedDisposition] = {}
    for body in comment_bodies:
        match = _DISPOSITION_BLOCK_RE.match(body)
        if match is None:
            continue
        enforceable, rejected = partition_enforceable_dispositions(
            _parse_one_disposition_block(match.group("body"))
        )
        merged = merge_finding_dispositions(merged, enforceable)
        for record in rejected:
            refused.setdefault(record.key, record)
    return merged, sorted(refused.values(), key=lambda record: record.key)


def build_finding_disposition_ledger(
    entries: Iterable[tuple[str, str, FindingDisposition]],
) -> dict[str, FindingDisposition]:
    """Fold ``(file, summary, entry)`` triples into a keyed ledger (#2210).

    The producer-side twin of :func:`parse_finding_disposition_block`, and the
    seam ``cw review settle`` builds its postable marker through — which is
    what finally gives :func:`render_finding_disposition_block` a production
    caller. Keying goes through :func:`_disposition_key`, so a marker minted
    here and a finding re-derived later cannot disagree about identity.

    Raises ``ValueError`` for an un-keyable file rather than silently dropping
    the entry: an operator asking to settle a finding and getting a marker that
    quietly omits it is the worst of both outcomes. It raises for an entry that
    fails provenance for the same reason: :func:`merge_finding_dispositions`
    would refuse to write it, and ``cw review settle`` always supplies the full
    set, so a refusal here is a bug to surface, not a record to drop.
    Duplicates fold through :func:`merge_finding_dispositions`, so the newest
    ``recorded_at`` wins.
    """
    ledger: dict[str, FindingDisposition] = {}
    for file, summary, entry in entries:
        key = _disposition_key(file, summary)
        if key is None:
            msg = f"cannot record a disposition for file={file!r}: no path to key on"
            raise ValueError(msg)
        gaps = _provenance_gaps(key, entry)
        if gaps:
            msg = (
                f"cannot record a disposition for file={file!r}: it fails "
                f"provenance (missing {', '.join(gaps)})"
            )
            raise ValueError(msg)
        ledger = merge_finding_dispositions(ledger, {key: entry})
    return ledger
