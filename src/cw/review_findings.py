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
# The six reasons a finding can be rejected outright (used by
# :attr:`RejectedFinding.reason` and :func:`_classify_finding`'s return type).
# Split from the escalation-strip reason (R6): a stripped escalation is a
# survived finding whose escalation evidence failed the diff check, not a
# rejected finding, so the two never share a Literal.
#
# "unanchored" is special (#1632): _classify_finding returns it as a plain
# discriminator like every other value here, but validate_reviewer_document
# routes it to `accepted` rather than constructing a RejectedFinding — in
# normal operation no RejectedFinding.reason is ever "unanchored". It exists
# in this Literal only so the discriminator type is honest about every value
# _classify_finding can return.
RejectedFindingReason = Literal[
    "invalid_severity",
    "missing_evidence",
    "evidence_not_in_diff",
    "unknown_file",
    "invalid_line_reference",
    "unanchored",
]
# The sole reason an escalation is stripped, kept as its own single-value
# Literal so :attr:`StrippedEscalation.reason` cannot accidentally carry a
# core rejection reason.
EscalationStripReason = Literal["escalation_evidence_not_in_diff"]

# Valid severity strings, derived from the Literal so the two never drift.
# Used by the defensive severity check in validate_reviewer_document — findings
# may arrive via model_construct (executor adapters, untrusted payloads) that
# bypasses Pydantic's Literal enforcement, so validate re-checks it.
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))

_ESCALATION_STRIP_REASON: EscalationStripReason = "escalation_evidence_not_in_diff"

# Schema version for the ReviewVerdict artifact (#1108/R6), following the
# AutoDevResult.schema_version convention (auto_dev_result.py). Bump when the
# verdict's on-disk shape changes in a way a reader must branch on.
_REVIEW_VERDICT_SCHEMA_VERSION: Literal[1] = 1


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

    @field_validator("detail", mode="before")
    @classmethod
    def _null_detail_to_default(cls, v: str | None) -> str:
        # Why: an OpenAI strict-mode schema (#1364) wraps every previously-
        # optional field as nullable rather than omittable, so codex may send
        # an explicit `null` for a field it left at its default. Normalize
        # before pydantic's type coercion instead of widening `detail`'s
        # Python-level type to `str | None`.
        return "" if v is None else v

    @field_validator("findings", mode="before")
    @classmethod
    def _null_findings_to_default(cls, v: list[Any] | None) -> list[Any]:
        return [] if v is None else v

    @model_validator(mode="after")
    def _check_failed_has_no_findings(self) -> ReviewerFindingsDocument:
        if self.status == "failed" and self.findings:
            msg = "a reviewer with status='failed' must not carry findings"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_ok_empty_findings_has_justification(self) -> ReviewerFindingsDocument:
        if self.status == "ok" and not self.findings and _is_blank(self.detail):
            msg = (
                "a reviewer with status='ok' and no findings must state what "
                "it checked in `detail` (or use status='degraded' if a "
                "required check could not be performed)"
            )
            raise ValueError(msg)
        return self


class RejectedFinding(BaseModel):
    """A finding that failed validation, with its raw payload preserved."""

    raw: dict[str, Any]
    reviewer_role: str
    reason: RejectedFindingReason
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
    reason: EscalationStripReason = _ESCALATION_STRIP_REASON


class ReviewVerdict(BaseModel):
    """Consolidated review outcome across all reviewers (#1108 artifact).

    ``blocking``/``must_fix``/``reviewed_sha`` are the exact 3 keys #1108
    requires; the rest is the executor-neutral superset.
    """

    schema_version: Literal[1] = _REVIEW_VERDICT_SCHEMA_VERSION
    blocking: bool
    must_fix: list[Finding]
    reviewed_sha: str
    accepted: list[AcceptedFinding] = Field(default_factory=list)
    rejected: list[RejectedFinding] = Field(default_factory=list)
    agents_run: list[ReviewerRunRecord] = Field(default_factory=list)
    review: Review
    stripped_escalations: list[StrippedEscalation] = Field(default_factory=list)


class CapturedDiff(BaseModel):
    """A captured diff: full text plus per-file line-level detail.

    ``text`` is the full unified diff (context, removed, and added lines) used
    for verbatim escalation-quote matching. ``files`` maps each changed file
    path to the list of changed (added) line numbers used for line-reference
    validation. ``file_diffs`` maps each file to its raw per-file hunk text
    (``+``/``-``/context lines intact), used only for prompt inlining, never for
    evidence validation. ``file_line_text`` maps each file to a
    ``{line_number: content}`` map for exactly the added lines (same line-number
    domain as ``files[file]``) — the substrate for true line-position evidence
    validation of finding evidence.
    """

    text: str
    files: dict[str, list[int]] = Field(default_factory=dict)
    file_diffs: dict[str, str] = Field(default_factory=dict)
    file_line_text: dict[str, dict[int, str]] = Field(default_factory=dict)


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


def _file_in_repo_tree(worktree: Path, file: str) -> bool:
    """True iff *file* resolves to a real file under *worktree*.

    Guards against a hallucinated/adversarial ``Finding.file`` escaping the
    worktree via an absolute path (pathlib's ``/`` operator silently
    discards the left operand when the right is absolute) or a ``../``
    traversal — the joined-and-resolved candidate must stay under the
    resolved worktree root. Proves the *path* is real; callers must not
    treat this as evidence the *quote* is real (R2, #1632).
    """
    try:
        root = worktree.resolve()
        candidate = (worktree / file).resolve()
    except OSError:
        return False
    return candidate.is_relative_to(root) and candidate.is_file()


def _line_reference_valid(diff: CapturedDiff, finding: Finding) -> bool:
    """Return True iff *finding*'s line references are in the diff.

    A file-level finding (both endpoints ``None``) is exempt — it has no line
    anchor to check.
    """
    for line in (finding.line_start, finding.line_end):
        if line is not None and not _line_in_diff(diff, finding.file, line):
            return False
    return True


def _evidence_in_claimed_lines(
    diff: CapturedDiff,
    file: str,
    text: str,
    line_start: int | None,
    line_end: int | None,
) -> bool:
    """True iff *text* appears within the finding's claimed line window for *file*.

    A file-level finding (both endpoints ``None``) has no line anchor — falls
    back to file-level matching against that file's full hunk text
    (``file_diffs``), the same fallback ``_line_reference_valid`` already grants
    file-level findings today.

    When a line window is claimed, *text* must appear within the joined content
    of exactly those added lines (``file_line_text``). A quote whose true origin
    is a removed/context line — which has no new-file line number — is correctly
    rejected here rather than accepted via a whole-file or whole-diff fallback.
    """
    if line_start is not None and line_end is not None:
        start, end = line_start, line_end
    elif line_start is not None:
        start = end = line_start
    elif line_end is not None:
        start = end = line_end
    else:
        return text in diff.file_diffs.get(file, "")
    line_text = diff.file_line_text.get(file, {})
    window = "\n".join(line_text[n] for n in range(start, end + 1) if n in line_text)
    return text in window


def _classify_finding(
    finding: Finding,
    diff: CapturedDiff,
    changed: frozenset[str],
    worktree: Path | None = None,
) -> RejectedFindingReason | None:
    """Return the rejection reason for *finding*, or ``None`` if it passes.

    Check order (first failure wins): severity → evidence-present →
    file-known → line-in-range → evidence-in-claimed-lines. The
    ``invalid_line_reference`` check MUST run before the evidence check so that
    ``_evidence_in_claimed_lines`` only builds a window from confirmed-real
    changed lines — a bogus line reference (e.g. ``line_start=999``) is reported
    as ``invalid_line_reference``, not misclassified as ``evidence_not_in_diff``
    via an empty window. The escalation-quote check is NOT here — it runs only
    after a finding passes all of these.

    When *finding*'s file is not in the diff at all, ``worktree`` (when given)
    is consulted as a fallback: a file that genuinely exists in the repo tree
    is "unanchored" rather than "unknown_file" (#1632) — evidence proven only
    by tree-existence, not diff-containment, so the finding is routed to
    adjudication (see :func:`validate_reviewer_document`) instead of being
    silently discarded. ``worktree=None`` (no caller opted in) or a failed
    tree check both fall back to today's ``"unknown_file"`` behavior, and
    neither ``_line_reference_valid`` nor ``_evidence_in_claimed_lines`` ever
    runs for an unanchored finding — there is no diff-line window to check it
    against.
    """
    if finding.severity not in _VALID_SEVERITIES:
        return "invalid_severity"
    if _is_blank(finding.evidence):
        return "missing_evidence"
    if finding.file not in changed:
        if worktree is not None and _file_in_repo_tree(worktree, finding.file):
            return "unanchored"
        return "unknown_file"
    if not _line_reference_valid(diff, finding):
        return "invalid_line_reference"
    if not _evidence_in_claimed_lines(
        diff, finding.file, finding.evidence, finding.line_start, finding.line_end
    ):
        return "evidence_not_in_diff"
    return None


def validate_reviewer_document(
    doc: ReviewerFindingsDocument, diff: CapturedDiff, *, worktree: Path | None = None
) -> tuple[list[Finding], list[RejectedFinding], list[StrippedEscalation]]:
    """Validate one reviewer's findings against *diff*.

    Returns ``(accepted, rejected, stripped)``. A finding that passes all core
    checks but carries an escalation whose ``evidence_quote`` is not in the
    diff is accepted with its escalation nulled, and a
    :class:`StrippedEscalation` is recorded (with a WARNING log). A finding
    that is itself rejected never reaches the escalation-quote check.

    ``worktree`` (default ``None``) opts into the #1632 unanchored-finding
    relaxation: when a finding's ``file`` is not part of *diff* but resolves
    to a real file under ``worktree``, :func:`_classify_finding` returns
    ``"unanchored"`` instead of ``"unknown_file"`` — this function then routes
    it into ``accepted`` (with an INFO log) rather than constructing a
    :class:`RejectedFinding`, so it reaches adjudication instead of being
    silently discarded. Tree-existence proves only that the *path* is real,
    not that the finding's evidence quote is — an unanchored finding's
    escalation (if any) still goes through the ordinary diff-based
    evidence_quote check below, same as any other accepted finding.
    """
    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []
    stripped: list[StrippedEscalation] = []
    changed = _changed_files(diff)

    for index, finding in enumerate(doc.findings):
        reason = _classify_finding(finding, diff, changed, worktree)
        if reason is not None and reason != "unanchored":
            rejected.append(
                RejectedFinding(
                    raw=finding.model_dump(),
                    reviewer_role=doc.reviewer_role,
                    reason=reason,
                )
            )
            continue
        if reason == "unanchored":
            _log.info(
                "auto-dev: routed unanchored finding to adjudication (file "
                "exists in repo tree, not diff-anchored; evidence quote not "
                "verified) (reviewer_role=%s, finding_index=%d, file=%s)",
                doc.reviewer_role,
                index,
                finding.file,
            )
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
    ``NIT``/``PRINCIPLE`` findings never contribute to any of the three
    gate-feeding aggregates, regardless of disposition — ``deferred`` is
    filtered to ``severity in {MUST_FIX, SHOULD_FIX}`` first, same as the
    other two.
    """
    deferred = sum(
        1
        for af in findings
        if af.disposition == "deferred"
        and af.finding.severity in ("MUST_FIX", "SHOULD_FIX")
    )
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
    worktree: Path | None = None,
    failed_reviewers: list[ReviewerRunFailure] | None = None,
    fix_cycles_used: int = 0,
) -> ReviewVerdict:
    """Consolidate every reviewer's document into a single :class:`ReviewVerdict`.

    ``worktree`` (default ``None``) is threaded into
    :func:`validate_reviewer_document` for every document — see that
    function's docstring for the #1632 unanchored-finding relaxation it
    controls.

    ``failed_reviewers`` (defaulting to an empty list — never a mutable default)
    each contribute one ``status="failed"`` / ``finding_count=0``
    :class:`ReviewerRunRecord`, appended after the parsed documents' records —
    they are still RECORDED in ``verdict.agents_run`` for audit purposes.
    ``stripped_escalations`` is the concatenation of every document's strip list
    in document order. ``blocking`` is True iff at least one accepted,
    non-deferred MUST_FIX finding exists.

    ``review.agents_run`` (the int count, distinct from the
    ``verdict.agents_run`` list above) counts only roles that actually
    produced a document — ``len(documents)`` — excluding every failure/skip
    regardless of reason. A role that never invoked codex exec (a
    budget-exhausted skip) or that invoked it but produced no usable document
    (timeout/error/unparseable) contributed nothing to this review pass and
    must not inflate the count the ``auto_approve_clean_review`` gate recipe
    treats as a required-non-zero signal (standing binding decision, #1236).

    ``fix_cycles_used`` (default ``0``) is threaded into the internal
    :func:`derive_review_counts` call so a caller running this per fix-loop
    cycle can stamp each intermediate ``Review`` with the cycle it belongs to.
    A single call still CANNOT produce a correct ``fix_cycles_used`` /
    ``must_fix_initial`` / ``deferred`` combination across a multi-pass loop
    (``must_fix_initial`` needs cycle 0's pre-defer snapshot, ``deferred`` needs
    the loop's cross-cycle survivor set) — a fix-loop adapter reconstructs the
    terminal ``Review`` itself (see ``cw.codex_fix_loop._finalize_review``);
    this parameter only carries the per-cycle count for that adapter's own
    intermediate verdicts (#1392).
    """
    failures = failed_reviewers if failed_reviewers is not None else []
    candidates: list[tuple[str, Finding]] = []
    all_rejected: list[RejectedFinding] = []
    all_stripped: list[StrippedEscalation] = []
    run_records: list[ReviewerRunRecord] = []

    for doc in documents:
        accepted, rejected, stripped = validate_reviewer_document(
            doc, diff, worktree=worktree
        )
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
    review = derive_review_counts(
        accepted_findings, fix_cycles_used=fix_cycles_used, agents_run=len(documents)
    )
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
