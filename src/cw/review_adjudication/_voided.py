"""Voided-finding suppression, shared by both review backends (#1814).

The one seam in this package both the Claude-native and the Codex path consult
(see the package docstring's "one deliberate exception" paragraph, and
ADR-0015): a finding an operator already settled as REJECTed is suppressed on
every subsequent pass, matched by content fingerprint rather than position, and
the void itself round-trips through a JSON sentinel inside a ticket comment.

Split out of the single ``review_adjudication.py`` module (#2011); import these
names from :mod:`cw.review_adjudication`, not from this private submodule.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication._models import (
    _MUST_FIX,
    Adjudication,
    VoidedFinding,
)

if TYPE_CHECKING:
    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

#: Ticket-comment header the voided-findings block renders under (#1814).
#: Fixed text, mirroring ``## Blocking Review Findings`` (#1815) — the header
#: is decorative and ignored on parse; the sentinel below is the contract.
_VOIDED_MD_TITLE = "## Voided Review Findings"
_VOIDED_SENTINEL = "VOIDED-REVIEW-FINDINGS"
#: Bump when the sentinel's on-the-wire shape changes in a way a reader must
#: branch on, following ``ReviewVerdict.schema_version``'s convention (#1108).
_VOIDED_SCHEMA_VERSION = 1
_VOIDED_BLOCK_RE = re.compile(
    rf"<!--\s*{_VOIDED_SENTINEL}\s*(?P<body>.*?)\s*{_VOIDED_SENTINEL}\s*-->",
    re.DOTALL,
)

#: ``disposition_detail`` / ``Adjudication.rationale`` for a suppressed
#: finding. Deterministic so two passes over the same void produce the same
#: text — a rationale that drifts between passes is a second record that can
#: disagree with the first.
_VOIDED_RATIONALE = (
    "suppressed: matches finding voided by operator comment "
    "{operator_comment_id} ({voided_at}) — original rejection: "
    "{original_rationale}"
)


def _normalize_finding_text(text: str) -> str:
    """Collapse *text*'s whitespace, leaving every other character alone.

    Deliberately NOT :func:`cw.review_findings._normalize_diff_text`, which
    strips a leading ``+``/``-`` per line. That is right for diff text and
    wrong for a ``summary``, where a leading ``-`` is an ordinary English
    character and stripping it would silently equate two different sentences.
    """
    return " ".join(text.split())


def _voided_fingerprint(
    severity: str, file: str, summary: str, evidence: str
) -> tuple[str, str, str, str]:
    """The content-anchored identity of a finding, for void matching (#1814).

    Deliberately NOT :func:`cw.review_findings._dedup_key`, which is
    positional ``(severity, file, line_start, line_end, evidence)``. Position
    is exactly what this identity must exclude, in both directions:

    - a voided finding whose code moved re-derives at a different line, and
      must still match (the #1715 code-motion case);
    - a genuinely new finding at the voided one's old line must NOT match,
      because sharing a location says nothing about being the same defect.

    ``evidence`` and ``summary`` both go through :func:`_normalize_finding_text`
    (whitespace collapse only) — NOT ``_normalize_diff_text``, whose leading
    ``+``/``-`` stripping is unsafe here for the same reason
    :func:`_normalize_finding_text`'s own docstring gives for ``summary``:
    reviewed ``evidence`` is arbitrary source text (Markdown, YAML, diffs of
    diffs), not exclusively diff-hunk lines, so a leading ``-``/``+`` can be
    real content rather than a diff marker. Stripping it risked collapsing two
    genuinely different findings onto one fingerprint — exactly the false
    positive this ticket's zero-tolerance decision forbids. ``file`` and
    ``severity`` are compared verbatim — a void is scoped to the file and
    severity it was granted for.

    Anchor-based expiry falls out of this for free: rewrite the code and the
    re-derived ``evidence`` differs, so the fingerprint stops matching and the
    void lapses. There is deliberately no TTL to keep in sync with it.
    """
    return (
        severity,
        file,
        _normalize_finding_text(summary),
        _normalize_finding_text(evidence),
    )


def _entry_fingerprint(entry: VoidedFinding) -> tuple[str, str, str, str]:
    return _voided_fingerprint(
        entry.severity, entry.file, entry.summary, entry.evidence
    )


def find_voided_matches(
    accepted: list[AcceptedFinding], voided: list[VoidedFinding]
) -> dict[int, VoidedFinding]:
    """Map indices into *accepted* to the void that suppresses each one.

    An index appears at most once (first matching void wins); a void may cover
    several accepted findings, which is correct — two reviewers reporting the
    same defect dedup to one entry, but a re-review can legitimately surface it
    at two locations.
    """
    if not voided:
        return {}
    by_fingerprint: dict[tuple[str, str, str, str], VoidedFinding] = {}
    for entry in voided:
        by_fingerprint.setdefault(_entry_fingerprint(entry), entry)
    matches: dict[int, VoidedFinding] = {}
    for index, af in enumerate(accepted):
        finding = af.finding
        key = _voided_fingerprint(
            finding.severity, finding.file, finding.summary, finding.evidence
        )
        matched = by_fingerprint.get(key)
        if matched is not None:
            matches[index] = matched
    return matches


def _voided_rationale(entry: VoidedFinding) -> str:
    """The one rationale text a suppression produces, on both backends."""
    return _VOIDED_RATIONALE.format(
        operator_comment_id=entry.operator_comment_id,
        voided_at=entry.voided_at or "date not recorded",
        original_rationale=entry.original_rationale or "not recorded",
    )


def _voided_adjudication(
    accepted: AcceptedFinding, entry: VoidedFinding
) -> Adjudication:
    """The ``reject`` entry a suppression contributes to ``ADJUDICATIONS``.

    Identity fields are copied off the matched FINDING, not off *entry* — the
    void carries no line anchor by design, and ``apply_adjudication`` matches
    on ``_location_key``. Sourcing them from the void would produce an entry
    that never matches the finding it decided, i.e. an ``unmatched`` count and
    a ``"dropped"`` stamp on the very finding the operator settled.
    """
    finding = accepted.finding
    return Adjudication(
        severity=finding.severity,
        file=finding.file,
        line_start=finding.line_start,
        line_end=finding.line_end,
        evidence=finding.evidence,
        summary=finding.summary,
        outcome="reject",
        rationale=_voided_rationale(entry),
    )


def apply_voided_suppression(
    verdict: ReviewVerdict, voided: list[VoidedFinding], *, ticket_id: str
) -> tuple[ReviewVerdict, list[Adjudication]]:
    """Suppress every accepted finding an operator already voided (#1814).

    The one seam both backends consult. A matched finding is stamped
    ``disposition="rejected"`` with the void's rationale in
    ``disposition_detail`` and leaves ``must_fix``/``blocking``; everything
    else passes through byte-identically. Returns the suppressed verdict plus
    one ``outcome="reject"`` :class:`Adjudication` per suppression — which the
    Claude path appends verbatim to its own ``ADJUDICATIONS`` array, and the
    codex path (which has no such array) simply discards, having already got
    the same outcome recorded on the shared ``AcceptedFinding`` substrate.

    **Emits one ``review.finding_voided`` event per suppression, inline.** That
    is deliberate coupling, not a layering slip: suppression is the only way a
    finding stops blocking without anything in *this* pass deciding so, and the
    event is its only local record. Splitting emission into a separate call the
    caller must remember would make the audit trail optional for exactly the
    act that most needs it (ADR-0015, invariant 3).

    ``must_fix``/``blocking`` are recomputed from the stamped set the same way
    ``consolidate_verdict`` derives them. ``must_fix_initial``, ``should_fix``,
    ``agents_run`` and ``review.deferred`` are preserved verbatim — they are
    the frozen cycle-0 baseline, and a suppression is not a fix, so the
    originally-found counts must keep saying what was originally found.
    """
    matches = find_voided_matches(verdict.accepted, voided)
    if not matches:
        return verdict, []

    stamped: list[AcceptedFinding] = []
    adjudications: list[Adjudication] = []
    for index, af in enumerate(verdict.accepted):
        entry = matches.get(index)
        if entry is None:
            stamped.append(af)
            continue
        adjudications.append(_voided_adjudication(af, entry))
        stamped.append(
            af.model_copy(
                update={
                    "disposition": "rejected",
                    "disposition_detail": _voided_rationale(entry),
                }
            )
        )
        _log.info(
            "auto-dev: suppressed re-derived finding voided by operator "
            "(ticket=%s, severity=%s, file=%s, comment=%s)",
            ticket_id,
            af.finding.severity,
            af.finding.file,
            entry.operator_comment_id,
        )
        record_event(
            OrchestratorEventType.REVIEW_FINDING_VOIDED,
            payload={
                "file": af.finding.file,
                "severity": af.finding.severity,
                "summary": af.finding.summary,
                "operator_comment_id": entry.operator_comment_id,
                "voided_at": entry.voided_at,
                "original_rationale": entry.original_rationale,
            },
            correlation_id=ticket_id,
        )

    # Only "fixed" (nothing decided yet this pass) and the "rejected" just
    # stamped above are reachable here: this runs before any adjudication or
    # fix-loop stamping, so no other disposition exists to reason about.
    must_fix = [
        af.finding
        for index, af in enumerate(stamped)
        if af.finding.severity == _MUST_FIX and index not in matches
    ]
    return (
        verdict.model_copy(
            update={
                "accepted": stamped,
                "must_fix": must_fix,
                "blocking": bool(must_fix),
            }
        ),
        adjudications,
    )


def _dedup_voided(entries: list[VoidedFinding]) -> list[VoidedFinding]:
    """*entries* in first-seen order, one per content fingerprint.

    An idempotent re-post of the same comment, or a session that re-appends a
    void it already recorded, must not grow the record — and two entries with
    the same fingerprint are by construction the same suppression.
    """
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[VoidedFinding] = []
    for entry in entries:
        key = _entry_fingerprint(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def render_voided_findings_block(voided: list[VoidedFinding]) -> str:
    """Render *voided* as the postable ``## Voided Review Findings`` comment.

    Embeds its own markdown header, mirroring
    :func:`render_deferred_findings_md`'s ``_DEFERRED_MD_TITLE`` precedent, so
    the caller posts the returned text as-is with no separate composition step.

    The payload is JSON inside the HTML comment — a deliberate deviation from
    ``DEFERRED-REVIEW-FINDINGS``'s bullet list. That format has never needed a
    Python parser (only ``auto-dev.md``'s prose Step H3 reads it); this one is
    read back by the codex backend, which has no LLM to interpret prose, so it
    must round-trip through ``json.loads`` while staying human-readable.

    Returns ``""`` for an empty list — nothing to record means no comment,
    same rule as :func:`render_deferred_findings_md`.
    """
    unique = _dedup_voided(voided)
    if not unique:
        return ""
    payload = {
        "schema_version": _VOIDED_SCHEMA_VERSION,
        "voided": [entry.model_dump(mode="json") for entry in unique],
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"{_VOIDED_MD_TITLE}\n\n"
        f"<!-- {_VOIDED_SENTINEL}\n{body}\n{_VOIDED_SENTINEL} -->\n"
    )


def _parse_one_voided_block(body: str) -> list[VoidedFinding]:
    """Parse one sentinel body, degrading a malformed block to ``[]``."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _log.warning("auto-dev: ignoring malformed %s block", _VOIDED_SENTINEL)
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("voided")
    if not isinstance(raw, list):
        return []
    entries: list[VoidedFinding] = []
    for item in raw:
        try:
            entries.append(VoidedFinding.model_validate(item))
        except ValueError:
            _log.warning("auto-dev: ignoring malformed %s entry", _VOIDED_SENTINEL)
    return entries


def parse_voided_findings_block(comment_bodies: list[str]) -> list[VoidedFinding]:
    """Union every voided-findings sentinel across *comment_bodies*.

    Fail-open throughout — a missing, truncated, or malformed block yields
    nothing and never raises, and one bad block never discards a good sibling.
    This mirrors ``_load_operator_comments``'s degrade contract for the same
    reason: a review that could not read a void is strictly better than no
    review, and the missed suppression surfaces as the finding re-appearing,
    which an operator can act on.
    """
    entries: list[VoidedFinding] = []
    for body in comment_bodies:
        for match in _VOIDED_BLOCK_RE.finditer(body):
            entries.extend(_parse_one_voided_block(match.group("body")))
    return _dedup_voided(entries)
