"""Typed model group for the executor-neutral finding contract (#1237).

Every type alias, model-only constant, and Pydantic/TypedDict class the
:mod:`cw.review_findings` package exposes lives here — the schema layer, with
no validation, dedup, or consolidation logic of its own. The sibling
submodules import from this one; nothing here imports from them, which is what
keeps the package's dependency direction acyclic.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator, model_validator

from cw.auto_dev_result import Review

# "DEBT" (#1837) is the non-blocking severity for a real problem the reviewer
# found on code this diff did not cause: it is tracked in the verdict's debt
# ledger for later filing rather than handed to the fix loop. Like
# NIT/PRINCIPLE it is excluded from every gate-feeding aggregate by the
# existing `severity in {MUST_FIX, SHOULD_FIX}` filters, with no extra code.
Severity = Literal["MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"]
# "dropped" (#1805) is the terminal state for an accepted finding that nobody
# actually decided the fate of: no matching adjudication entry, or a "fixed"
# claim the fix-cycle diff does not substantiate. It is stamped exclusively by
# ``cw.review_adjudication`` — ``consolidate_verdict`` never produces it.
#
# "operator_actionable" (#1817) is a genuine recorded decision like the first
# three: the session accepted the finding but its remedy lies outside this
# diff (a follow-up ticket that was never filed, an absent artifact), so the
# fix loop structurally cannot act on it and it is posted to the tracker as an
# operator checklist item instead. Also stamped exclusively by
# ``cw.review_adjudication``.
Disposition = Literal["fixed", "rejected", "deferred", "dropped", "operator_actionable"]
ReviewerHealthStatus = Literal["ok", "degraded", "failed"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
# The filesystem-capability mode reviewers actually ran under (#1709); see
# ReviewVerdict.capability_mode.
CapabilityMode = Literal["capable", "degraded"]
# Where a reviewer role's agent specification was actually resolved from
# (#1773): the repo-local ``.claude/agents/`` copy, the operator's global
# ``~/.claude/agents/`` fallback, or nowhere at all. See AgentSpecStatus.
AgentSpecSource = Literal["repo", "global", "none"]
# Where a DebtRecord stands with the issue tracker (#1837). "NEEDS_FILING" is
# the only value this ticket ever produces; the other two exist for the
# follow-up that actually files the tickets (#1838).
TrackingDisposition = Literal["FILED", "ALREADY_TRACKED", "NEEDS_FILING"]
# The fingerprint-normalizer version stamped on every DebtRecord (#1837).
# Defined here, next to the model that owns the field, rather than in
# cw.review_debt (which imports it) -- a single source of truth so a future
# version bump can't update one site and silently miss the other.
FINGERPRINT_VERSION: Literal["FINGERPRINT_V1"] = "FINGERPRINT_V1"
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

    ``no_diff_anchor`` (#1817) marks the finding whose remedy lies outside the
    diff entirely — an acceptance criterion demanding a follow-up ticket that
    was never filed, a required artifact that does not exist anywhere. This is
    NOT #1632's ``"unanchored"`` case (a real path on disk that this diff
    simply does not touch): there is no file to point at at all, so no
    tree-existence check could ever resolve it. Reviewers previously expressed
    it by inventing a fake ``file`` value, which :func:`_classify_finding` then
    mechanically rejected as ``"unknown_file"`` — the silent-drop this marker
    exists to close. When it is set, ``file`` MUST be the fixed literal
    ``"N/A"``: :attr:`file` stays required and non-blank so the field remains
    queryable, and a per-reviewer freeform value would defeat that.
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
    no_diff_anchor: bool = False
    # #1837: the two fields a reviewer uses to argue a finding on code the
    # latest fix cycle did NOT touch still belongs in this fix loop.
    # ``transitive_impact_evidence`` is a verbatim quote from the delta
    # demonstrating the causal link; ``release_critical_exception`` is the
    # rationale for invoking the release-critical carve-out (the string itself
    # IS the required justification — non-blank means "invoked"). Both blank
    # by default, so a reviewer that has never heard of them degrades to the
    # pre-#1837 shape: the finding is treated as treadmill and downgraded to
    # tracked debt rather than blocking the loop.
    transitive_impact_evidence: str = ""
    release_critical_exception: str = ""

    @field_validator("no_diff_anchor", mode="before")
    @classmethod
    def _null_no_diff_anchor_to_default(cls, v: bool | None) -> bool:
        # Why: an OpenAI strict-mode schema (#1364) wraps every previously-
        # optional field as nullable rather than omittable, so codex may send
        # an explicit `null` for a field it left at its default. Normalize
        # before pydantic's type coercion instead of widening
        # `no_diff_anchor`'s Python-level type to `bool | None`. Mirrors the
        # existing `_null_detail_to_default` / `_null_findings_to_default`
        # on ReviewerFindingsDocument — #1817 added the field without the
        # matching normalizer, so every codex-produced finding with
        # `no_diff_anchor: null` failed model_validate as a schema_mismatch.
        return False if v is None else v

    @field_validator("file", "summary", "consequence", "suggested_fix", "evidence")
    @classmethod
    def _reject_blank(cls, v: str) -> str:
        if _is_blank(v):
            msg = f"finding string field must be non-empty, non-whitespace (got {v!r})"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _check_no_diff_anchor_has_no_line_anchor(self) -> Finding:
        # A claimed line position is meaningless when the finding itself
        # asserts there is no diff to anchor it to — fail fast on the producer
        # mistake rather than carrying a contradiction into classification.
        if self.no_diff_anchor and (
            self.line_start is not None or self.line_end is not None
        ):
            msg = (
                "a finding with no_diff_anchor=True must not claim a line "
                f"anchor (got line_start={self.line_start}, "
                f"line_end={self.line_end})"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_no_diff_anchor_file_is_na(self) -> Finding:
        # The docstring above documents "N/A" as the required literal — a
        # per-reviewer freeform value here is the exact "unknown_file"
        # silent-drop this marker exists to close (#1817 review, 2026-08-11).
        if self.no_diff_anchor and self.file != "N/A":
            msg = (
                "a finding with no_diff_anchor=True must have "
                f'file="N/A" (got file={self.file!r})'
            )
            raise ValueError(msg)
        return self

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
    A ``degraded`` or ``failed`` reviewer must also state its reason in
    ``detail`` — a blank reason on either status is a contract violation
    (#1806), not a silently-accepted value.
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

    @model_validator(mode="after")
    def _check_degraded_or_failed_has_reason(self) -> ReviewerFindingsDocument:
        if self.status in ("degraded", "failed") and _is_blank(self.detail):
            msg = (
                f"a reviewer with status={self.status!r} must state its "
                "reason in `detail` — an empty or missing reason on a "
                "degraded or failed verdict is a contract violation, not a "
                "silently-accepted value (#1806)"
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

    ``"dropped"`` (#1805) and ``"operator_actionable"`` (#1817) are never
    stamped here — only :mod:`cw.review_adjudication` produces them, the first
    for a finding no adjudication decision covered (or a ``"fixed"`` claim the
    fix-cycle diff does not substantiate), the second for an accepted MUST_FIX
    whose remedy lies outside this diff. :func:`consolidate_verdict`'s own
    contract is unchanged: optimistic default in, adapter overwrites it later.

    ``disposition_detail`` is the free-text "why" paired with the closed
    ``disposition`` enum, mirroring :attr:`RejectedFinding.detail`'s pairing
    with its own ``reason`` Literal. Blank for the default disposition; the
    adjudication rationale (reject/defer) or the downgrade explanation
    (dropped) otherwise.
    """

    finding: Finding
    reviewers: list[str]
    disposition: Disposition = "fixed"
    disposition_detail: str = ""


class ReviewerRunMetrics(TypedDict, total=False):
    """Transient kwargs bag of one reviewer run's executor telemetry (#1710).

    NOT a persisted structure and NOT a field on any model: a ``TypedDict``
    erases to a plain ``dict`` at runtime, and the only thing this shape is
    ever used for is ``ReviewerRunRecord(**metrics)``. The persisted home for
    every value here is :class:`ReviewerRunRecord`'s own fields — there is
    deliberately no parallel metrics structure hanging off
    :class:`ReviewVerdict` (R3, #1710).

    ``total=False`` so a producer may omit any key; every corresponding
    ``ReviewerRunRecord`` field is defaulted, so a partial bag splats cleanly.
    """

    thread_id: str | None
    effective_model: str | None
    duration_seconds: float | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    terminal_event: str | None
    tool_call_counts: dict[str, int]
    had_command_evidence: bool
    unexpected_tool_attempts: list[str]


class ReviewerRunRecord(BaseModel):
    """Terminal-health record for one reviewer agent that ran (or failed).

    ``detail`` mirrors :attr:`ReviewerFindingsDocument.detail` (#1775) --
    it is copied verbatim from the source document by :func:`consolidate_verdict`
    so a degraded reviewer's stated reason survives synthesis into the
    persisted verdict instead of being silently dropped.

    Everything below ``finding_count`` is executor audit telemetry (#1710),
    populated from the codex ``--json`` event stream where one is available and
    left at its default everywhere else (the Claude-native review path, a role
    whose stream was malformed, a run that never emitted one). It is purely
    observational: no health, blocking, or gate decision reads any of it.
    """

    reviewer_role: str
    status: ReviewerHealthStatus
    detail: str = ""
    finding_count: int
    # Executor-native run identifier (codex ``thread.started.thread_id``).
    thread_id: str | None = None
    # Always None today: no codex-cli 0.147.0 event carries a model field.
    effective_model: str | None = None
    duration_seconds: float | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Type of the last recognized stream event; anything other than
    # "turn.completed"/"turn.failed" means the stream was cut off or absent.
    terminal_event: str | None = None
    tool_call_counts: dict[str, int] = Field(default_factory=dict)
    had_command_evidence: bool = False
    unexpected_tool_attempts: list[str] = Field(default_factory=list)


class AgentSpecStatus(BaseModel):
    """How one reviewer role's agent specification resolved (#1773).

    ``empty`` is the single "this role ran unspecified" signal: it is True
    whenever the finally-selected text was blank, whatever ``source`` says.
    ``empty_repo_file`` is an independent fact about the repo-tracked copy —
    True when ``.claude/agents/<role>.md`` existed but was blank, including
    the case where the global fallback then recovered a usable spec
    (``source="global", empty=False, empty_repo_file=True``).
    """

    role: str
    source: AgentSpecSource
    empty: bool
    empty_repo_file: bool = False


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


class DebtRecord(BaseModel):
    """One tracked debt item carried on a verdict (#1837).

    Produced by :mod:`cw.review_debt` from an accepted DEBT-severity finding,
    or from a MUST_FIX the fix loop's admission gate refused (a treadmill
    finding on code the latest fix cycle never touched). Either way the
    finding is recorded rather than acted on, so the loop can converge without
    the problem going unremembered.

    ``fingerprint`` is the ``(file, normalized_summary)`` pair
    :func:`cw.review_debt.fingerprint_v1` produces, stored alongside the
    ``fingerprint_version`` that generated it so a later normalizer revision
    can tell which records it can still compare against.

    ``rule_id`` and ``symbol`` are always blank today — :class:`Finding` has no
    source field for either — and exist so a future producer that does know
    them has a typed home instead of a schema change.
    """

    fingerprint: tuple[str, str]
    fingerprint_version: Literal["FINGERPRINT_V1"] = FINGERPRINT_VERSION
    rule_id: str = ""
    file: str
    symbol: str = ""
    evidence: str
    summary: str
    suggested_follow_up: str
    discovery_sha: str
    tracking_disposition: TrackingDisposition = "NEEDS_FILING"
    reviewer_role: str = ""


class ReviewVerdict(BaseModel):
    """Consolidated review outcome across all reviewers (#1108 artifact).

    ``blocking``/``must_fix``/``reviewed_sha`` are the exact 3 keys #1108
    requires; the rest is the executor-neutral superset.

    ``capability_mode``/``capability_reason`` record which filesystem-capability
    mode the reviewers actually ran under (#1709) — ``"capable"`` or
    ``"degraded"``, with the classified reason on the degraded branch. Both stay
    ``None`` for executors that have no such concept (LocalExecutor) rather than
    defaulting to a mode nobody probed. Purely recorded: nothing in
    ``consolidate_verdict`` or the health derivation reads them.

    ``is_terminal_snapshot`` (#1763) means "this snapshot represents the
    terminal disposition of its review pass, not one superseded by a later
    fix-loop cycle over the same session". :func:`consolidate_verdict` never
    sets it False — a freshly consolidated verdict IS its pass's outcome.
    Only a caller persisting a known-non-final intermediate artifact (the
    codex fix loop's per-cycle ``cycleN-review-verdict.json`` snapshots) stamps
    it False, then re-stamps exactly one file True once the loop's true exit
    disposition is known. It exists so an operator reading a snapshot off disk
    can tell whether its ``rejected_must_fix`` is the one backing the reported
    ``Blocker.details`` (#1729).

    ``unmatched_adjudication_count`` (#1805) is written solely by
    :func:`cw.review_adjudication.apply_adjudication`: the number of
    adjudication entries that matched no accepted finding (stale anchor,
    ambiguous same-location collision, shadowed duplicate). Like
    ``capability_mode``, it is purely recorded here — nothing in this module
    reads it back; the approval gate surfaces it so a silently-degraded
    adjudication is visible rather than invisible.
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
    # #1714: the subset of ``rejected`` whose raw payload claimed MUST_FIX
    # severity. Deliberately independent of ``blocking``/``must_fix``, which
    # are computed from ACCEPTED findings only -- see
    # :func:`_select_rejected_must_fix` for why this is a second signal rather
    # than a widening of the first.
    rejected_must_fix: list[RejectedFinding] = Field(default_factory=list)
    capability_mode: CapabilityMode | None = None
    capability_reason: str | None = None
    # #1773: one record per selected reviewer role describing where its agent
    # specification resolved from. Default-empty (not Optional) like
    # ``agents_run``: "no records" is the honest shape for a verdict built by a
    # path that never resolved specs, and the renderer treats it as "say
    # nothing" rather than "everything was fine".
    agent_spec_status: list[AgentSpecStatus] = Field(default_factory=list)
    # #1763: see the class docstring — True unless a fix-loop caller explicitly
    # marks this persisted snapshot as a superseded intermediate cycle.
    is_terminal_snapshot: bool = True
    # #1805: adjudication entries that matched no accepted finding. Additive
    # and default-0 (the `rejected_must_fix`/`stripped_escalations` precedent):
    # `consolidate_verdict` never touches it, only `apply_adjudication` does.
    unmatched_adjudication_count: int = 0
    # #1837: the head this pass's diff was taken FROM, set only on a fix-loop
    # re-review (cycle N reviews `previous_reviewed_sha..reviewed_sha`, not
    # the whole PR). `None` means the pass reviewed the full branch diff.
    # Purely recorded, same convention as `capability_mode` above.
    previous_reviewed_sha: str | None = None
    # #1837: findings recorded rather than acted on — accepted DEBT-severity
    # findings plus MUST_FIX findings the fix loop's admission gate refused.
    # Deduplicated by fingerprint before it is stamped, so the renderer needs
    # no already-seen bookkeeping of its own.
    debt: list[DebtRecord] = Field(default_factory=list)


class CapturedDiff(BaseModel):
    """A captured diff: full text plus per-file line-level detail.

    ``text`` is the full unified diff (context, removed, and added lines) used
    for verbatim escalation-quote matching. ``files`` maps each changed file
    path to the list of changed (added) line numbers used for line-reference
    validation. ``file_diffs`` maps each file to its raw per-file hunk text
    (``+``/``-``/context lines intact), used for prompt inlining AND as the
    file-level evidence-validation fallback (``_evidence_in_claimed_lines``)
    for findings with no line anchor. ``file_line_text`` maps each file to a
    ``{line_number: content}`` map for exactly the added lines (same line-number
    domain as ``files[file]``) — the substrate for true line-position evidence
    validation of finding evidence.

    ``file_window_text`` (#1738) is a second ``{line_number: content}`` map per
    file, alongside ``file_line_text`` rather than replacing it: it ALSO
    includes unchanged context-line content at its real new-file line number
    (only a removed line, which has no new-file position, is excluded). It
    exists solely as the widened substrate for ``_evidence_in_claimed_lines``'s
    windowed branch (via ``_resolve_hunk_window``/``_nearest_hunk_line``) — it
    must never feed ``_line_reference_valid``/``_nearest_added_line`` (the
    anchor-*validity* gate) or ``files``, both of which stay added-line-only
    on purpose.
    """

    text: str
    files: dict[str, list[int]] = Field(default_factory=dict)
    file_diffs: dict[str, str] = Field(default_factory=dict)
    file_line_text: dict[str, dict[int, str]] = Field(default_factory=dict)
    file_window_text: dict[str, dict[int, str]] = Field(default_factory=dict)
