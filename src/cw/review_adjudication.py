"""Claude-native review adjudication seam (#1805).

``cw review consolidate`` leaves every surviving :class:`AcceptedFinding` at
``disposition="fixed"`` — an optimistic default, correct at the moment it is
written because nothing has been adjudicated yet. The Codex fix-loop adapter
overwrites it from its own re-review. The Claude-native ``/auto-dev-review``
pipeline had no equivalent overwrite: Checkpoint 3a's FIX / REJECT / DEFER
adjudication happened entirely in LLM prose, and ``.cw/deferred-findings.md``
was hand-authored from that same prose — two independently typed records of
one judgment, only one of which was accurate.

This module is that missing seam. :func:`apply_adjudication` stamps the
dispositions from a structured :class:`Adjudication` list and recomputes the
gate-feeding fields from the stamped result;
:func:`render_deferred_findings_md` renders the *same* list into the
``.cw/deferred-findings.md`` artifact, so the two records cannot disagree.
:func:`verify_fixed_dispositions` then downgrades any ``"fixed"`` claim the
fix-cycle diff does not substantiate. The judgment step itself is untouched —
only its serialization becomes mechanical.

**Why this is a Claude-native-only seam, and why it must not be unified with
the Codex path (#1805 R2 — the durable principle lives here rather than in a
new ADR).** :func:`cw.codex_fix_loop._survivors_only_verdict` also stamps
``disposition`` and recomputes ``blocking``, but its ``"deferred"`` means the
opposite of this module's: there it means "the fix loop capped out, this
MUST_FIX is still genuinely unresolved" (so ``blocking`` must stay ``True``
for a deferred survivor, which is exactly why that function computes
``blocking`` from the open-finding set rather than from dispositions). Here
``"deferred"`` means "the coordinating session deliberately decided this may
ship un-fixed" — so a deferred finding correctly stops blocking. The two
shapes must NOT be unified by generalizing one to cover the other. If a
second consumer ever needs *this* seam (not merely the ``Disposition`` /
:class:`AcceptedFinding` types, which are shared already), that is the trigger
to write an ADR; until then this docstring is the record of why they differ.

**The one deliberate exception (#1814).** :class:`VoidedFinding` /
:func:`apply_voided_suppression` and their helpers ARE shared by both
backends. That is not a breach of the rule above but the carve-out it already
names: they reuse only the already-declared-shared ``Disposition`` /
:class:`AcceptedFinding` types, never :func:`apply_adjudication` itself and
never the ``"defer"`` outcome whose two meanings are the actual reason the
seams must stay apart. Suppression produces exactly one outcome — ``"reject"``
— whose meaning is identical on both paths, which is why one implementation
can serve both. See ADR-0015 for the durable invariant.

Public surface: :class:`Adjudication`, :data:`AdjudicationOutcome`,
:func:`apply_adjudication`, :func:`matched_adjudications`,
:func:`verify_fixed_dispositions`, :func:`render_deferred_findings_md`,
:class:`VoidedFinding`, :func:`find_voided_matches`,
:func:`apply_voided_suppression`, :func:`render_voided_findings_block`,
:func:`parse_voided_findings_block`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, field_validator, model_validator

from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.review_findings import (
    AcceptedFinding,
    Disposition,
    Severity,
    _line_reference_valid,
    derive_review_counts,
)

if TYPE_CHECKING:
    from cw.review_findings import CapturedDiff, Finding, ReviewVerdict

_log = logging.getLogger(__name__)

AdjudicationOutcome = Literal["fix", "reject", "defer"]

# The three genuine, recorded post-adjudication decisions. Every other
# disposition an accepted finding can end at is an absence of decision.
_OUTCOME_DISPOSITIONS: dict[AdjudicationOutcome, Disposition] = {
    "fix": "fixed",
    "reject": "rejected",
    "defer": "deferred",
}

_MUST_FIX = "MUST_FIX"

#: ``disposition_detail`` for a finding no adjudication entry covered.
NO_ENTRY_DETAIL = "no adjudication entry recorded for this finding"

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


class Adjudication(BaseModel):
    """One coordinating-session decision about one accepted finding.

    ``severity``/``file``/``line_start``/``line_end`` are the identity fields
    matched against :attr:`AcceptedFinding.finding` (see
    :func:`_location_key`). ``reviewers`` is deliberately NOT among them: it is
    the post-dedup merged role list, so it carries no disambiguating power
    beyond what the location key already has.

    ``evidence`` is a same-location tiebreak, not part of the identity key —
    the fragility this ticket exists to fix is precisely verbatim
    evidence-string and off-by-one line mismatches (#1792, #1738, #1715,
    #1743), so requiring an exact evidence match to identify a finding
    reproduces the bug. It still disambiguates the rare case of two distinct
    findings at one location.

    ``summary`` is render-only: :func:`render_deferred_findings_md` fills the
    documented ``"<summary>"`` slot from it, so rendering needs no second
    lookup against the verdict.
    """

    severity: Severity
    file: str
    line_start: int | None = None
    line_end: int | None = None
    evidence: str = ""
    summary: str = ""
    outcome: AdjudicationOutcome
    rationale: str = ""

    @model_validator(mode="after")
    def _check_rationale_recorded(self) -> Adjudication:
        # "Record the rationale" is the adjudication instruction's own rule for
        # REJECT/DEFER (auto-dev-review.md Checkpoint 3a) — a blank one is a
        # contract violation, not a silently-accepted value. A FIX needs none:
        # the fix itself is the record.
        if self.outcome != "fix" and not self.rationale.strip():
            msg = (
                f"an adjudication with outcome={self.outcome!r} must record a "
                "non-empty rationale"
            )
            raise ValueError(msg)
        return self


def _location_key(
    severity: str, file: str, line_start: int | None, line_end: int | None
) -> tuple[str, str, int, int]:
    """The coarse 4-field identity key shared by findings and adjudications.

    Deliberately the ``_dedup_key`` prefix minus ``evidence`` (which is a
    tiebreak here, not identity). ``None`` endpoints map to ``-1``, same as
    :func:`cw.review_findings._dedup_key`.
    """
    return (
        severity,
        file,
        line_start if line_start is not None else -1,
        line_end if line_end is not None else -1,
    )


def _entry_key(entry: Adjudication) -> tuple[str, str, int, int]:
    return _location_key(entry.severity, entry.file, entry.line_start, entry.line_end)


def _finding_key(finding: Finding) -> tuple[str, str, int, int]:
    return _location_key(
        finding.severity, finding.file, finding.line_start, finding.line_end
    )


def _candidates_for_entry(
    entry: Adjudication, accepted: list[AcceptedFinding]
) -> list[int]:
    """Indices into *accepted* whose finding shares *entry*'s location key.

    Matching runs from the entry's side, not the finding's: the collision that
    actually needs handling is "two accepted findings share one entry's
    location", and only an entry-first pass can see that both could equally
    claim the entry.
    """
    key = _entry_key(entry)
    return [i for i, af in enumerate(accepted) if _finding_key(af.finding) == key]


def _resolve_entry(
    entry: Adjudication, accepted: list[AcceptedFinding], candidates: list[int]
) -> int | None:
    """Pick the single accepted finding *entry* decides, or ``None``.

    Zero candidates → nothing to decide. One → unambiguous. Two or more → the
    ``evidence`` tiebreak, which must narrow to exactly one; if it narrows to
    zero (the entry quotes neither) or leaves several (identical evidence),
    the entry is genuinely ambiguous and is left unconsumed rather than
    guessed at — guessing is what stamps the wrong finding.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    tied = [i for i in candidates if accepted[i].finding.evidence == entry.evidence]
    return tied[0] if len(tied) == 1 else None


def _match_adjudications(
    accepted: list[AcceptedFinding], adjudications: list[Adjudication]
) -> tuple[dict[int, Adjudication], list[Adjudication]]:
    """Map accepted-finding indices to the entries that decide them.

    Returns ``(matched, unconsumed)``. Entries are walked in payload order and
    an index already claimed by an earlier entry is never re-claimed
    (first-entry-wins), so a duplicate-entry payload degrades to "the later
    entry matched nothing" rather than raising or silently re-stamping.
    """
    matched: dict[int, Adjudication] = {}
    unconsumed: list[Adjudication] = []
    for entry in adjudications:
        index = _resolve_entry(entry, accepted, _candidates_for_entry(entry, accepted))
        if index is None or index in matched:
            unconsumed.append(entry)
            continue
        matched[index] = entry
    return matched, unconsumed


def matched_adjudications(
    accepted: list[AcceptedFinding], adjudications: list[Adjudication]
) -> list[Adjudication]:
    """The subset of *adjudications* that :func:`apply_adjudication` applies.

    Callers rendering ``.cw/deferred-findings.md`` (or any other artifact
    derived from the adjudication payload) must filter through this first —
    passing the raw, unfiltered *adjudications* list to
    :func:`render_deferred_findings_md` would let an entry that matched no
    accepted finding (stale anchor, an ambiguous same-location collision, a
    shadowed duplicate — the exact cases counted in
    ``ReviewVerdict.unmatched_adjudication_count``) still appear in the
    rendered markdown as if it had been applied, even though the verdict
    itself never recorded that decision. That reintroduces this ticket's own
    "two records disagree" failure one layer down, inside the fix meant to
    remove it.
    """
    matched, _ = _match_adjudications(accepted, adjudications)
    return list(matched.values())


def _stamp(accepted: AcceptedFinding, entry: Adjudication | None) -> AcceptedFinding:
    """Return *accepted* carrying *entry*'s decision (or the no-decision state)."""
    if entry is None:
        return accepted.model_copy(
            update={"disposition": "dropped", "disposition_detail": NO_ENTRY_DETAIL}
        )
    return accepted.model_copy(
        update={
            "disposition": _OUTCOME_DISPOSITIONS[entry.outcome],
            "disposition_detail": entry.rationale,
        }
    )


def apply_adjudication(
    verdict: ReviewVerdict, adjudications: list[Adjudication]
) -> ReviewVerdict:
    """Stamp *verdict*'s accepted findings from *adjudications* and recompute.

    Recomputing is not optional garnish: a MUST_FIX the session REJECTed still
    reads ``blocking=True`` if only ``disposition`` is stamped, which is the
    same class of lie this ticket exists to remove. The rule, derived from
    ``consolidate_verdict``'s own generic rule rather than copied from
    ``_survivors_only_verdict`` (see the module docstring for why the Codex
    path's inverted ``"deferred"`` semantics do not transfer):

    - ``"fixed"``/``"rejected"``/``"deferred"`` are all genuine recorded
      decisions post-adjudication — all three stop blocking.
    - ``"dropped"`` — nobody decided — is the ONLY disposition that still
      counts toward ``must_fix``/``blocking``, erring toward gate failure over
      silent pass-through.

    ``review.deferred`` is recomputed; ``must_fix_initial``, ``should_fix``,
    ``fix_cycles_used``, ``agents_run`` and ``had_real_commit`` are preserved
    verbatim. Those are the frozen cycle-0 baseline (``auto-dev-review.md``
    Checkpoint 3a step 4 freezes them, and ``codex_fix_loop._finalize_review``
    keeps the same split) — recomputing them from a disposition-stamped list
    would silently corrupt them.

    Never raises on a per-item mismatch (only pydantic's own ``ValidationError``
    on structurally malformed input can): an unmatched entry is logged and
    counted in ``unmatched_adjudication_count`` instead.
    """
    matched, unconsumed = _match_adjudications(verdict.accepted, adjudications)
    stamped = [_stamp(af, matched.get(i)) for i, af in enumerate(verdict.accepted)]

    for entry in unconsumed:
        # Mirrors review_findings' silently-dropped-content logging convention
        # (see the stripped-escalation warning there): a mechanical alteration
        # nobody asked for is always announced.
        _log.warning(
            "auto-dev: adjudication entry did not match any accepted finding "
            "(severity=%s, file=%s, line=%s-%s, outcome=%s)",
            entry.severity,
            entry.file,
            entry.line_start,
            entry.line_end,
            entry.outcome,
        )

    must_fix = [
        af.finding
        for af in stamped
        if af.finding.severity == _MUST_FIX and af.disposition == "dropped"
    ]
    # Borrow derive_review_counts's own deferred-counting rule instead of
    # duplicating its (severity, disposition) filter here — only `.deferred`
    # is used; must_fix_initial/should_fix/agents_run from that call are
    # discarded, since those three stay pinned at the frozen cycle-0 baseline
    # (see the docstring above), never recomputed from `stamped`.
    deferred = derive_review_counts(stamped).deferred
    return verdict.model_copy(
        update={
            "accepted": stamped,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
            "review": verdict.review.model_copy(update={"deferred": deferred}),
            "unmatched_adjudication_count": len(unconsumed),
        }
    )


class VoidedFinding(BaseModel):
    """One finding an operator settled as REJECTed, recorded durably (#1814).

    Minted exactly once, by the Claude coordinating session, at the moment
    Checkpoint 3a step 4c correlates a specific operator comment to a specific
    finding — that correlation needs LLM judgment over free-text prose, so a
    codex-only lane can consult existing voids but never create new ones.
    Persisted as a JSON sentinel inside a ticket comment (see
    :func:`render_voided_findings_block`) rather than a ``.cw/`` file, because
    ``.cw/`` state does not survive the worktree teardown of the very
    regress/redispatch cycle this record exists to survive.

    ``severity``/``file``/``summary``/``evidence`` are the identity fields. The
    absence of ``line_start``/``line_end`` is the design, not an omission:
    matching on position is what a re-derived finding's drifted line anchor
    (#1715) would break, and what an unrelated new finding at the same line
    would falsely satisfy. See :func:`_voided_fingerprint` and ADR-0015.

    The remaining fields are provenance, carried so an operator reading a
    suppression can find the settling comment without re-fetching the thread.
    ``voided_at`` may arrive blank from a producer that has no clock handy
    (``cw review check-voided`` stamps it); it is never part of identity.
    """

    severity: Severity
    file: str
    summary: str
    evidence: str
    operator_comment_id: str
    operator_comment_excerpt: str = ""
    voided_at: str = ""
    original_rationale: str = ""

    @field_validator("file", "summary", "evidence", "operator_comment_id")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        # Mirrors Finding._reject_blank: a blank identity field would make the
        # fingerprint match findings it has no business matching, which is the
        # one failure mode this ticket's zero-false-positive stance forbids.
        if not v.strip():
            msg = f"voided-finding field must be non-empty, non-whitespace (got {v!r})"
            raise ValueError(msg)
        return v


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


def _fix_is_substantiated(finding: Finding, fix_diff: CapturedDiff) -> bool:
    """True iff *fix_diff* actually touches *finding*'s cited location.

    A file-level finding (both endpoints ``None``) needs only the file to
    appear in the diff. A line-anchored one goes through
    ``_line_reference_valid`` — the same tolerance-aware gate consolidation
    uses, so a fix landing a line or two off its cited anchor is not called a
    non-fix. ``worktree=None`` deliberately disables the #1743 enclosing-def
    fallback: "some line inside the enclosing function changed" is evidence a
    finding is *anchorable*, not evidence it was *fixed*.
    """
    if finding.line_start is None and finding.line_end is None:
        return finding.file in fix_diff.files
    return _line_reference_valid(fix_diff, finding, worktree=None)


def verify_fixed_dispositions(
    verdict: ReviewVerdict, fix_diff: CapturedDiff
) -> ReviewVerdict:
    """Downgrade every ``"fixed"`` claim *fix_diff* does not substantiate.

    A ``"fixed"`` disposition whose cited file/line the fix-cycle diff never
    touched becomes ``"dropped"`` with the reason in ``disposition_detail``.
    Any other disposition is passed through untouched — a deferred or rejected
    finding was never claimed to be fixed, so checking it against the fix diff
    could only produce a false downgrade.

    Record-only by design (#1805 R1): this never triggers a new fix cycle, and
    deliberately does NOT recompute ``blocking``/``must_fix`` the way
    :func:`apply_adjudication` does. The downgrade is a statement about the
    accuracy of the record, not a new gate; the caller
    (``auto-dev-review.md`` Step 3b) surfaces it in ``friction_highlights`` so
    a human sees it.
    """
    accepted: list[AcceptedFinding] = []
    for af in verdict.accepted:
        if af.disposition != "fixed" or _fix_is_substantiated(af.finding, fix_diff):
            accepted.append(af)
            continue
        _log.warning(
            "auto-dev: downgraded 'fixed' disposition — fix-cycle diff does "
            "not touch cited location (file=%s, line=%s)",
            af.finding.file,
            af.finding.line_start,
        )
        accepted.append(
            af.model_copy(
                update={
                    "disposition": "dropped",
                    "disposition_detail": (
                        "fixed disposition claimed but fix-cycle diff does not "
                        f"touch {af.finding.file}:{af.finding.line_start}"
                    ),
                }
            )
        )
    return verdict.model_copy(update={"accepted": accepted})


def render_deferred_findings_md(adjudications: list[Adjudication]) -> str:
    """Render *adjudications* into the ``.cw/deferred-findings.md`` artifact.

    Reproduces the block documented in ``.claude/commands/auto-dev-review.md``
    byte-for-byte — Stage 4 Step 4d copies it into the PR body verbatim and the
    PR-hygiene sweep greps the ``DEFERRED-REVIEW-FINDINGS`` sentinels
    literally, so the shape is a contract, not a style choice.

    Returns ``""`` when there is nothing to record (every finding was fixed):
    the documented rule is "omit the file entirely", so the caller skips the
    write rather than leaving an empty artifact behind.
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
        rows = "\n".join(
            f'- {a.file} — "{a.summary}" — {a.rationale}' for a in rejected
        )
        sections.append(f"{_REJECTED_MD_HEADING}\n{rows}")
    if deferred:
        entries = "\n".join(
            f"- severity: {a.severity}\n"
            f'  summary: "{a.summary}"\n'
            f"  file: {a.file}\n"
            f'  rationale: "{a.rationale}"'
            for a in deferred
        )
        sections.append(
            f"<!-- {_DEFERRED_SENTINEL}\n{entries}\n{_DEFERRED_SENTINEL} -->"
        )
    return "\n\n".join(sections) + "\n"
