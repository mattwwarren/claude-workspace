"""Executor-neutral structured finding contract (#1237).

A schema + validation/dedup/aggregation library for code-review findings that
is neutral to the executor that produced them (Claude, Codex, or any future
backend). The model group here is the typed home for what `.claude/commands/
review.md` describes in prose: per-reviewer findings, the ESCALATIONS protocol
(§8 of that file, lines ~246-253), evidence-quote validation against the diff
(Step 5, line ~294), dedup/consolidation across reviewers (Step 5.9), and the
`.claude/review-verdict.json` artifact (#1108).

Ticket lineage: #1237 (this contract) builds on the sentinel contract in
`docs/headless-contract.md` / `auto_dev_result.py` (#1194) and feeds the
verdict artifact consumed downstream (#1108). Nothing here is wired to an
executor or CLI call site yet — this is the greenfield library the adapters
will build on.

Public surface:

- Models: :class:`Finding`, :class:`EscalationMetadata`,
  :class:`ReviewerFindingsDocument`, :class:`RejectedFinding`,
  :class:`AcceptedFinding`, :class:`ReviewerRunRecord`,
  :class:`ReviewerRunFailure`, :class:`StrippedEscalation`,
  :class:`ReviewVerdict`, :class:`CapturedDiff`.
- Functions: :func:`validate_reviewer_document`, :func:`dedupe_findings`,
  :func:`derive_review_counts`, :func:`consolidate_verdict`,
  :func:`write_review_verdict`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from cw.atomic import atomic_write_text
from cw.auto_dev_result import Review

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

Severity = Literal["MUST_FIX", "SHOULD_FIX", "NIT", "PRINCIPLE"]
Disposition = Literal["fixed", "rejected", "deferred"]
ReviewerHealthStatus = Literal["ok", "degraded", "failed"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
# The last value is populated only on :attr:`StrippedEscalation.reason`, never
# on :attr:`RejectedFinding.reason` — a stripped escalation is a survived
# finding whose escalation evidence failed the diff check, not a rejected
# finding (see :func:`validate_reviewer_document`).
RejectionReason = Literal[
    "invalid_severity",
    "missing_evidence",
    "evidence_not_in_diff",
    "unknown_file",
    "invalid_line_reference",
    "escalation_evidence_not_in_diff",
]

# Valid severity strings, derived from the Literal so the two never drift.
# Used by the defensive severity check in validate_reviewer_document — findings
# may arrive via model_construct (executor adapters, untrusted payloads) that
# bypasses Pydantic's Literal enforcement, so validate re-checks it.
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))

_ESCALATION_STRIP_REASON: Literal["escalation_evidence_not_in_diff"] = (
    "escalation_evidence_not_in_diff"
)


def _is_blank(s: str) -> bool:
    """Return True iff *s* is empty or whitespace-only.

    Mirrors ``cw.auto_dev_result._is_blank`` intentionally rather than importing
    that module-private helper — the two contracts stay decoupled.
    """
    return not s.strip()


class EscalationMetadata(BaseModel):
    """A structured cross-reviewer escalation (review.md ESCALATIONS block).

    ``target_reviewer`` maps to the prose ``to:`` field; ``evidence_quote`` to
    ``evidence:``. The narrative ``reason:`` field is deliberately not carried
    here — it is already covered by :attr:`Finding.summary`/`consequence`.
    """

    target_reviewer: str
    evidence_quote: str

    @field_validator("target_reviewer", "evidence_quote")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        if _is_blank(v):
            msg = f"escalation field must be non-empty, non-whitespace (got {v!r})"
            raise ValueError(msg)
        return v


class Finding(BaseModel):
    """A single code-review finding, executor-neutral.

    ``line_start``/``line_end`` are ``None`` for a file-level finding (no line
    anchor); when both are set, ``line_end >= line_start`` is enforced.
    """

    severity: Severity
    file: str
    line_start: int | None = None
    line_end: int | None = None
    summary: str
    consequence: str
    suggested_fix: str
    evidence: str
    confidence: Confidence
    escalation: EscalationMetadata | None = None

    @field_validator("file", "summary", "consequence", "suggested_fix", "evidence")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        if _is_blank(v):
            msg = f"finding string field must be non-empty, non-whitespace (got {v!r})"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _check_line_range(self) -> Finding:
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            msg = (
                f"line_end ({self.line_end}) must be >= line_start ({self.line_start})"
            )
            raise ValueError(msg)
        return self


class ReviewerFindingsDocument(BaseModel):
    """One reviewer's output: a health status plus its findings.

    A ``failed`` reviewer produced no usable findings, so a non-empty
    ``findings`` list under ``status="failed"`` is a contradiction and rejected.
    """

    reviewer_role: str
    status: ReviewerHealthStatus
    detail: str = ""
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_failed_has_no_findings(self) -> ReviewerFindingsDocument:
        if self.status == "failed" and self.findings:
            msg = "a reviewer with status='failed' must not carry findings"
            raise ValueError(msg)
        return self


class RejectedFinding(BaseModel):
    """A finding that failed validation, with its raw payload preserved."""

    raw: dict[str, Any]
    reviewer_role: str
    reason: RejectionReason
    detail: str = ""


class AcceptedFinding(BaseModel):
    """A validated finding, merged across the reviewers that reported it.

    ``disposition`` defaults to ``"fixed"``: this library performs no fix loop,
    so a survivor of validation/dedup carries the optimistic terminal
    disposition. Downstream consumers (a fix-loop adapter) overwrite it, and
    :func:`derive_review_counts` treats ``"deferred"`` specially.
    """

    finding: Finding
    reviewers: list[str]
    disposition: Disposition = "fixed"


class ReviewerRunRecord(BaseModel):
    """Terminal-health record for one reviewer agent that ran (or failed)."""

    reviewer_role: str
    status: ReviewerHealthStatus
    finding_count: int


class ReviewerRunFailure(BaseModel):
    """A reviewer that failed to produce a document at all.

    ``reason`` is an open string (not a closed Literal) — failure modes are
    executor-specific and evolve independently of this contract. It is
    deliberately not projected onto :class:`ReviewerRunRecord`.
    """

    role: str
    reason: str


class StrippedEscalation(BaseModel):
    """Record of an escalation dropped because its evidence quote was not in
    the diff — the finding itself survives, only its escalation is nulled.

    ``reviewer_role`` + ``finding_index`` together identify the source finding:
    ``finding_index`` (0-based within that reviewer's ``findings`` list) is only
    stable within one document, and :attr:`ReviewVerdict.stripped_escalations`
    aggregates across every reviewer's document.
    """

    reviewer_role: str
    finding_index: int
    target_reviewer: str
    reason: RejectionReason = _ESCALATION_STRIP_REASON


class ReviewVerdict(BaseModel):
    """Consolidated review outcome across all reviewers (#1108 artifact).

    ``blocking``/``must_fix``/``reviewed_sha`` are the exact 3 keys #1108
    requires; the rest is the executor-neutral superset.
    """

    blocking: bool
    must_fix: list[Finding]
    reviewed_sha: str
    accepted: list[AcceptedFinding] = Field(default_factory=list)
    rejected: list[RejectedFinding] = Field(default_factory=list)
    agents_run: list[ReviewerRunRecord] = Field(default_factory=list)
    review: Review
    stripped_escalations: list[StrippedEscalation] = Field(default_factory=list)


class CapturedDiff(BaseModel):
    """A captured diff: full text plus per-file changed line numbers.

    ``text`` is the full unified diff (context, removed, and added lines) used
    for verbatim evidence-quote matching. ``files`` maps each changed file path
    to the list of changed line numbers used for line-reference validation.
    """

    text: str
    files: dict[str, list[int]] = Field(default_factory=dict)


def _changed_files(diff: CapturedDiff) -> frozenset[str]:
    """Return the set of file paths touched by *diff*."""
    return frozenset(diff.files)


def _line_in_diff(diff: CapturedDiff, file: str, line: int) -> bool:
    """Return True iff *line* is a changed line of *file* in *diff*."""
    return line in diff.files.get(file, [])


def _substring_in_diff(diff: CapturedDiff, text: str) -> bool:
    """Return True iff *text* appears verbatim anywhere in the diff text.

    Matches against the FULL diff text (context and removed lines included),
    not only ``+``-prefixed added lines — same rule for finding evidence and
    escalation evidence quotes.
    """
    return text in diff.text


def _line_reference_valid(diff: CapturedDiff, finding: Finding) -> bool:
    """Return True iff *finding*'s line references are in the diff.

    A file-level finding (both endpoints ``None``) is exempt — it has no line
    anchor to check.
    """
    for line in (finding.line_start, finding.line_end):
        if line is not None and not _line_in_diff(diff, finding.file, line):
            return False
    return True


def _classify_finding(
    finding: Finding, diff: CapturedDiff, changed: frozenset[str]
) -> RejectionReason | None:
    """Return the rejection reason for *finding*, or ``None`` if it passes.

    Check order (first failure wins): severity → evidence-present →
    evidence-in-diff → file-known → line-in-range. The escalation-quote check
    is NOT here — it runs only after a finding passes all five of these.
    """
    if finding.severity not in _VALID_SEVERITIES:
        return "invalid_severity"
    if _is_blank(finding.evidence):
        return "missing_evidence"
    if not _substring_in_diff(diff, finding.evidence):
        return "evidence_not_in_diff"
    if finding.file not in changed:
        return "unknown_file"
    if not _line_reference_valid(diff, finding):
        return "invalid_line_reference"
    return None


def validate_reviewer_document(
    doc: ReviewerFindingsDocument, diff: CapturedDiff
) -> tuple[list[Finding], list[RejectedFinding], list[StrippedEscalation]]:
    """Validate one reviewer's findings against *diff*.

    Returns ``(accepted, rejected, stripped)``. A finding that passes all core
    checks but carries an escalation whose ``evidence_quote`` is not in the
    diff is accepted with its escalation nulled, and a
    :class:`StrippedEscalation` is recorded (with a WARNING log). A finding
    that is itself rejected never reaches the escalation-quote check.
    """
    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []
    stripped: list[StrippedEscalation] = []
    changed = _changed_files(diff)

    for index, finding in enumerate(doc.findings):
        reason = _classify_finding(finding, diff, changed)
        if reason is not None:
            rejected.append(
                RejectedFinding(
                    raw=finding.model_dump(),
                    reviewer_role=doc.reviewer_role,
                    reason=reason,
                )
            )
            continue
        escalation = finding.escalation
        if escalation is not None and not _substring_in_diff(
            diff, escalation.evidence_quote
        ):
            # Log the dropped escalation before appending, mirroring
            # auto_dev_result._filter_empty_pending_items' convention of always
            # logging silently-dropped payload content.
            _log.warning(
                "auto-dev: stripped escalation with evidence_quote not found in "
                "diff (reviewer_role=%s, finding_index=%d, target_reviewer=%s)",
                doc.reviewer_role,
                index,
                escalation.target_reviewer,
            )
            stripped.append(
                StrippedEscalation(
                    reviewer_role=doc.reviewer_role,
                    finding_index=index,
                    target_reviewer=escalation.target_reviewer,
                    reason=_ESCALATION_STRIP_REASON,
                )
            )
            accepted.append(finding.model_copy(update={"escalation": None}))
        else:
            accepted.append(finding)

    return accepted, rejected, stripped


def _dedup_key(finding: Finding) -> tuple[str, str, int, int, str]:
    """Stable dedup key. ``None`` line endpoints map to ``-1`` for sorting."""
    return (
        finding.severity,
        finding.file,
        finding.line_start if finding.line_start is not None else -1,
        finding.line_end if finding.line_end is not None else -1,
        finding.evidence,
    )


def _pick_representative(members: list[tuple[str, Finding]]) -> Finding:
    """Choose the surviving Finding for a dedup group.

    Prefer the source with a non-null escalation when exactly one side has it
    set; otherwise tie-break deterministically by lowest-sorting reviewer_role.
    """
    escalated = [(role, f) for role, f in members if f.escalation is not None]
    if len(escalated) == 1:
        return escalated[0][1]
    return min(members, key=lambda rf: rf[0])[1]


def dedupe_findings(candidates: list[tuple[str, Finding]]) -> list[AcceptedFinding]:
    """Merge ``(reviewer_role, Finding)`` candidates into accepted findings.

    Findings sharing ``(severity, file, line_start, line_end, evidence)`` merge
    into one :class:`AcceptedFinding` whose ``reviewers`` lists every reporting
    role (sorted). Output order is deterministic (by dedup key).
    """
    groups: dict[tuple[str, str, int, int, str], list[tuple[str, Finding]]] = {}
    for role, finding in candidates:
        groups.setdefault(_dedup_key(finding), []).append((role, finding))

    merged: list[AcceptedFinding] = []
    for key in sorted(groups):
        members = groups[key]
        reviewers = sorted({role for role, _ in members})
        merged.append(
            AcceptedFinding(finding=_pick_representative(members), reviewers=reviewers)
        )
    return merged


def derive_review_counts(
    findings: list[AcceptedFinding],
    *,
    fix_cycles_used: int = 0,
    agents_run: int = 0,
) -> Review:
    """Aggregate accepted findings into a :class:`Review` count block.

    A ``deferred`` finding is counted in ``deferred`` and excluded from
    ``must_fix_initial``/``should_fix`` (both severities behave the same way).
    """
    deferred = sum(1 for af in findings if af.disposition == "deferred")
    must_fix_initial = sum(
        1
        for af in findings
        if af.finding.severity == "MUST_FIX" and af.disposition != "deferred"
    )
    should_fix = sum(
        1
        for af in findings
        if af.finding.severity == "SHOULD_FIX" and af.disposition != "deferred"
    )
    return Review(
        must_fix_initial=must_fix_initial,
        should_fix=should_fix,
        fix_cycles_used=fix_cycles_used,
        deferred=deferred,
        agents_run=agents_run,
    )


def consolidate_verdict(
    documents: list[ReviewerFindingsDocument],
    diff: CapturedDiff,
    reviewed_sha: str,
    *,
    failed_reviewers: list[ReviewerRunFailure] | None = None,
) -> ReviewVerdict:
    """Consolidate every reviewer's document into a single :class:`ReviewVerdict`.

    ``failed_reviewers`` (defaulting to an empty list — never a mutable default)
    each contribute one ``status="failed"`` / ``finding_count=0``
    :class:`ReviewerRunRecord`, appended after the parsed documents' records.
    ``stripped_escalations`` is the concatenation of every document's strip list
    in document order. ``blocking`` is True iff at least one accepted,
    non-deferred MUST_FIX finding exists.
    """
    failures = failed_reviewers if failed_reviewers is not None else []
    candidates: list[tuple[str, Finding]] = []
    all_rejected: list[RejectedFinding] = []
    all_stripped: list[StrippedEscalation] = []
    run_records: list[ReviewerRunRecord] = []

    for doc in documents:
        accepted, rejected, stripped = validate_reviewer_document(doc, diff)
        candidates.extend((doc.reviewer_role, f) for f in accepted)
        all_rejected.extend(rejected)
        all_stripped.extend(stripped)
        run_records.append(
            ReviewerRunRecord(
                reviewer_role=doc.reviewer_role,
                status=doc.status,
                finding_count=len(accepted),
            )
        )

    run_records.extend(
        ReviewerRunRecord(reviewer_role=failure.role, status="failed", finding_count=0)
        for failure in failures
    )

    accepted_findings = dedupe_findings(candidates)
    review = derive_review_counts(accepted_findings, agents_run=len(run_records))
    must_fix = [
        af.finding
        for af in accepted_findings
        if af.finding.severity == "MUST_FIX" and af.disposition != "deferred"
    ]
    return ReviewVerdict(
        blocking=bool(must_fix),
        must_fix=must_fix,
        reviewed_sha=reviewed_sha,
        accepted=accepted_findings,
        rejected=all_rejected,
        agents_run=run_records,
        review=review,
        stripped_escalations=all_stripped,
    )


def write_review_verdict(verdict: ReviewVerdict, path: Path) -> None:
    """Atomically write *verdict* to *path* as JSON (#1108 artifact).

    Full-replace semantics via :func:`cw.atomic.atomic_write_text`. Not wired to
    any executor/CLI call site in this ticket.
    """
    atomic_write_text(path, verdict.model_dump_json(indent=2))
