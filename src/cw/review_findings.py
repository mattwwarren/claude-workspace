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
  :class:`ReviewerRunMetrics`, :class:`ReviewerRunFailure`,
  :class:`AgentSpecStatus`, :class:`StrippedEscalation`,
  :class:`ReviewVerdict`, :class:`CapturedDiff`.
- Functions: :func:`validate_reviewer_document`, :func:`dedupe_findings`,
  :func:`derive_review_counts`, :func:`consolidate_verdict`,
  :func:`write_review_verdict`.
"""

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING, Any, Literal, TypedDict, get_args

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
# The filesystem-capability mode reviewers actually ran under (#1709); see
# ReviewVerdict.capability_mode.
CapabilityMode = Literal["capable", "degraded"]
# Where a reviewer role's agent specification was actually resolved from
# (#1773): the repo-local ``.claude/agents/`` copy, the operator's global
# ``~/.claude/agents/`` fallback, or nowhere at all. See AgentSpecStatus.
AgentSpecSource = Literal["repo", "global", "none"]
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

# Reviewer-supplied line anchors observed drifting off the true added line by
# one to three lines in fleet review runs (#1715) — usually a stale line
# number from a prior diff revision or an off-by-one miscount, with otherwise
# correct evidence text. Fixed module constant, not derived from hunk/file
# size (see _nearest_added_line).
_LINE_ANCHOR_TOLERANCE: int = 3  # lines

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


def _normalize_diff_text(text: str) -> str:
    """Strip one leading diff marker and surrounding whitespace per line.

    Lets an evidence-vs-diff substring comparison match regardless of which
    side (or neither) carries a stray ``+``/``-`` prefix: a reviewer's evidence
    quote copied straight from a rendered diff view carries the marker even
    though it isn't part of the real source line, while the file-level
    fallback (``file_diffs``) always carries one because it stores raw
    per-file hunk text. Only a single leading marker character is stripped per
    line — line count and order are preserved (no blank-line collapsing), so
    this cannot merge or reorder content (#1715).
    """
    normalized_lines = []
    for raw_line in text.split("\n"):
        stripped_marker = raw_line[1:] if raw_line[:1] in ("+", "-") else raw_line
        normalized_lines.append(stripped_marker.strip())
    return "\n".join(normalized_lines)


def _nearest_added_line(
    diff: CapturedDiff, file: str, line: int, tolerance: int = _LINE_ANCHOR_TOLERANCE
) -> int | None:
    """Return the added line of *file* nearest *line*, within *tolerance*.

    An exact hit (*line* itself is a changed line) short-circuits via
    :func:`_line_in_diff` without scanning candidates. Otherwise the nearest
    candidate in ``diff.files.get(file, [])`` within *tolerance* lines wins;
    ties break by lowest distance, then lowest line number. Returns ``None``
    when no added line is within tolerance, including when *file* has none at
    all (#1715).
    """
    if _line_in_diff(diff, file, line):
        return line
    best: int | None = None
    best_distance = tolerance + 1
    for candidate in diff.files.get(file, []):
        distance = abs(candidate - line)
        if distance > tolerance:
            continue
        if (
            best is None
            or distance < best_distance
            or (distance == best_distance and candidate < best)
        ):
            best, best_distance = candidate, distance
    return best


def _nearest_hunk_line(
    diff: CapturedDiff, file: str, line: int, tolerance: int = _LINE_ANCHOR_TOLERANCE
) -> int | None:
    """Return the hunk-covered line of *file* nearest *line*, within *tolerance*.

    Sibling of :func:`_nearest_added_line`, deliberately kept separate rather
    than repurposing it (#1738): candidates are drawn from
    ``diff.file_window_text.get(file, {})`` — every context OR added line the
    diff covers, not only added lines — so this must never be substituted for
    :func:`_nearest_added_line` at :func:`_line_reference_valid`'s
    anchor-validity call site. Same exact-hit short-circuit and tie-break
    rules as :func:`_nearest_added_line`.
    """
    if line in diff.file_window_text.get(file, {}):
        return line
    best: int | None = None
    best_distance = tolerance + 1
    for candidate in diff.file_window_text.get(file, {}):
        distance = abs(candidate - line)
        if distance > tolerance:
            continue
        if (
            best is None
            or distance < best_distance
            or (distance == best_distance and candidate < best)
        ):
            best, best_distance = candidate, distance
    return best


def _enclosing_def_span(source: str, line: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` line span of the innermost function or
    class in *source* enclosing *line*, or ``None`` if none does.

    Walks every :class:`ast.FunctionDef`/:class:`ast.AsyncFunctionDef`/
    :class:`ast.ClassDef` node and picks the smallest span containing *line*,
    so a nested function's span wins over its enclosing function's. A
    decorated function's span starts at the ``def``/``class`` line itself
    (Python's ``lineno`` for such nodes, since 3.8, excludes the decorator
    lines) — a line that only touches a decorator has no enclosing span.
    *source* that fails to parse (a syntax error, or ``ast.parse``'s
    ``ValueError`` on embedded null bytes) returns ``None`` rather than
    raising — the caller (:func:`_anchor_in_enclosing_def`) treats "can't
    determine a span" the same as "no span" (#1743).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end = node.end_lineno
        if end is None or not (node.lineno <= line <= end):
            continue
        if best is None or (end - node.lineno) < (best[1] - best[0]):
            best = (node.lineno, end)
    return best


def _anchor_in_enclosing_def(
    diff: CapturedDiff, worktree: Path, file: str, line: int
) -> bool:
    """True iff *line*'s enclosing def/class span in *file* contains a
    changed line of *diff*.

    Reads ``(worktree / file).read_text()`` and resolves *line*'s span via
    :func:`_enclosing_def_span`; a missing/unreadable file or a line with no
    enclosing definition both return ``False``. Gives a structural finding
    (too-long function, too-many-params, does-two-things) anchored on the
    enclosing ``def``/``class`` line — which is itself rarely a changed line —
    a legitimate way to anchor: the span, not the single def line, is checked
    against the diff's changed lines (#1743).
    """
    try:
        source = (worktree / file).read_text()
    except (OSError, UnicodeDecodeError):
        return False
    span = _enclosing_def_span(source, line)
    if span is None:
        return False
    start, end = span
    return any(start <= changed <= end for changed in diff.files.get(file, []))


def _line_reference_valid(
    diff: CapturedDiff, finding: Finding, worktree: Path | None = None
) -> bool:
    """Return True iff *finding*'s line references resolve to a changed line.

    A file-level finding (both endpoints ``None``) is exempt — it has no line
    anchor to check. A near-miss anchor within ``_LINE_ANCHOR_TOLERANCE`` lines
    of a real changed line resolves via :func:`_nearest_added_line` rather than
    requiring an exact match (#1715).

    ``worktree`` (default ``None``) opts into the #1743 enclosing-def
    fallback: when an endpoint is not itself near a changed line, and
    ``worktree`` is given, :func:`_anchor_in_enclosing_def` is tried before
    giving up on that endpoint — this is what lets a structural finding
    anchored on a function/class's ``def`` line survive even though that line
    is rarely itself changed, as long as some changed line falls inside the
    definition's span. ``worktree=None`` (no caller opted in) disables the
    fallback entirely, matching today's behavior byte-for-byte — the same
    opt-in shape as #1632's ``_classify_unanchored_file``.
    """
    for line in (finding.line_start, finding.line_end):
        if line is None:
            continue
        if _nearest_added_line(diff, finding.file, line) is not None:
            continue
        if worktree is not None and _anchor_in_enclosing_def(
            diff, worktree, finding.file, line
        ):
            _log.info(
                "auto-dev: rescued finding anchored on enclosing def/class "
                "span not itself a changed line (file=%s, line=%d)",
                finding.file,
                line,
            )
            continue
        return False
    return True


def _resolve_line_window(
    diff: CapturedDiff, file: str, line_start: int | None, line_end: int | None
) -> tuple[int, int] | None:
    """Resolve a claimed (``line_start``, ``line_end``) pair to real added lines.

    Callers must not pass both endpoints ``None`` (that's the file-level case,
    handled separately). A set endpoint resolves via :func:`_nearest_added_line`;
    an unset endpoint mirrors the other before resolving, matching the
    single-line-claim behavior ``_evidence_in_claimed_lines`` has always had.
    Returns the resolved ``(start, end)`` in ascending order, or ``None`` if
    either endpoint fails to resolve within tolerance (#1715).
    """
    raw_start = line_start if line_start is not None else line_end
    raw_end = line_end if line_end is not None else line_start
    if raw_start is None or raw_end is None:
        msg = "_resolve_line_window requires at least one endpoint set"
        raise ValueError(msg)
    resolved_start = _nearest_added_line(diff, file, raw_start)
    resolved_end = _nearest_added_line(diff, file, raw_end)
    if resolved_start is None or resolved_end is None:
        return None
    return min(resolved_start, resolved_end), max(resolved_start, resolved_end)


def _resolve_hunk_window(
    diff: CapturedDiff, file: str, line_start: int | None, line_end: int | None
) -> tuple[int, int] | None:
    """Resolve a claimed (``line_start``, ``line_end``) pair to hunk-covered lines.

    Mirror of :func:`_resolve_line_window`, calling :func:`_nearest_hunk_line`
    (candidates: every context OR added line, via ``file_window_text``)
    instead of :func:`_nearest_added_line` on both endpoints (#1738). Wired
    into exactly one call site — :func:`_evidence_in_claimed_lines`'s windowed
    branch — so the widened recall only affects evidence *matching*, never the
    anchor-*validity* gate (:func:`_line_reference_valid`) or the persisted
    anchor an accepted finding is snapped onto (:func:`_resolved_finding`,
    which keeps calling this function's unchanged sibling).
    """
    raw_start = line_start if line_start is not None else line_end
    raw_end = line_end if line_end is not None else line_start
    if raw_start is None or raw_end is None:
        msg = "_resolve_hunk_window requires at least one endpoint set"
        raise ValueError(msg)
    resolved_start = _nearest_hunk_line(diff, file, raw_start)
    resolved_end = _nearest_hunk_line(diff, file, raw_end)
    if resolved_start is None or resolved_end is None:
        return None
    return min(resolved_start, resolved_end), max(resolved_start, resolved_end)


def _reconcile_evidence_window(
    candidates: dict[int, str],
    text: str,
    start: int,
    end: int,
    tolerance: int = _LINE_ANCHOR_TOLERANCE,
) -> tuple[int, int] | None:
    """Reconcile a declared (start, end) window against *text*'s own content
    when the window itself is too short/long by a few lines (#1792,
    producer-side variant of #1715/#1738/#1743).

    Two-phase, deliberately asymmetric in strictness:

    1. The declared ``(start, end)`` window unchanged (widen=0,0) is tried
       first, using the exact pre-#1792 rule: a gap-tolerant join of
       whichever of ``range(start, end + 1)`` are present in *candidates*
       (a missing line is silently skipped, never synthesized), then a
       plain substring check against *text*. This reproduces pre-#1792
       behavior byte-for-byte — required so every already-passing
       #1236/#1715/#1738 case is untouched, including ones where the
       evidence is a short fragment inside a wider, near-line-tolerance-
       widened window that itself has gaps.
    2. Only when that fails does this grow the start backward and/or the
       end forward by up to *tolerance* lines each. A widened candidate
       counts ONLY when every line in its range is present in *candidates*
       (no gaps — never synthesizes a line the diff doesn't contain) AND
       its joined content EXACTLY equals *text* (both normalized) — not
       merely contains it. Exact equality (stricter than phase 1's
       substring check) is load-bearing: a substring check here would let
       widening accidentally absorb a different, genuinely-real-but-
       unrelated adjacent line into the window purely because it happens
       to contain the evidence text as a substring once joined (regression
       guard: a 1-line quote actually anchored on the line right after a
       claimed single-line window, with the declared line prepended as
       unrelated "noise", must stay rejected — #1236 R6's claimed-window
       boundary). Phase 2 is what actually *widens* the window; phase 1
       never does, so it can never be fooled by an adjacent real line the
       way an unconstrained substring-under-widening search would be.

    ``candidates`` is a ``{line: content}`` map — callers pass either the
    narrow added-lines-only substrate for anchor persistence or the wider
    hunk-context substrate for evidence matching, never both. Returns the
    smallest matching widened window (by span, then lowest start) so a
    repaired anchor is the tightest defensible span. ``None`` when neither
    phase finds a match — a genuinely-absent evidence string is unaffected
    by this function at any offset (#1714's false-accept guard is
    preserved: this only ever repairs a length mismatch on real, fully and
    exactly present content, never accepts fabricated or unrelated
    content).
    """
    target = _normalize_diff_text(text)
    base_joined = "\n".join(
        candidates[n] for n in range(start, end + 1) if n in candidates
    )
    if target in _normalize_diff_text(base_joined):
        return (start, end)

    best: tuple[int, int] | None = None
    for widen_start in range(tolerance + 1):
        for widen_end in range(tolerance + 1):
            if widen_start == 0 and widen_end == 0:
                continue  # already tried above (phase 1)
            candidate_start = start - widen_start
            candidate_end = end + widen_end
            if candidate_start > candidate_end:
                continue
            line_range = range(candidate_start, candidate_end + 1)
            if not all(n in candidates for n in line_range):
                continue
            joined = "\n".join(candidates[n] for n in line_range)
            if _normalize_diff_text(joined) != target:
                continue
            span = candidate_end - candidate_start
            if (
                best is None
                or span < (best[1] - best[0])
                or (span == (best[1] - best[0]) and candidate_start < best[0])
            ):
                best = (candidate_start, candidate_end)
    return best


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

    When a line window is claimed, :func:`_resolve_hunk_window` snaps both
    endpoints (the same near-line tolerance ``_line_reference_valid`` applies,
    but over the wider ``file_window_text`` candidate set — context lines
    included) and orders them ascending; :func:`_reconcile_evidence_window`
    then checks *text* against that window's content and, if the window
    itself is a few lines short/long of *text*'s own true span, against a
    widened window within ``_LINE_ANCHOR_TOLERANCE`` lines (#1792) — an
    endpoint that fails to resolve, or a widened search that finds no
    matching window, makes the whole check fail. Both sides of every
    substring comparison are routed through :func:`_normalize_diff_text` so a
    stray ``+``/``-`` diff marker on either the evidence or the diff-derived
    text cannot break an otherwise-genuine match (#1715).

    A quote whose true origin is a REMOVED line — which has no new-file line
    number at all — is correctly rejected here (#1738: a context line, unlike
    a removed line, does have a real new-file position and IS captured in
    ``file_window_text``, so it is no longer excluded the way this docstring
    used to claim).
    """
    if line_start is None and line_end is None:
        return _normalize_diff_text(text) in _normalize_diff_text(
            diff.file_diffs.get(file, "")
        )
    resolved = _resolve_hunk_window(diff, file, line_start, line_end)
    if resolved is None:
        return False
    start, end = resolved
    window_text = diff.file_window_text.get(file, {})
    return _reconcile_evidence_window(window_text, text, start, end) is not None


def _resolved_finding(diff: CapturedDiff, finding: Finding) -> Finding:
    """Return *finding* with its line anchors snapped onto the resolved window.

    A file-level finding (both endpoints ``None``) passes through unchanged.
    Any other finding reaching this point has already passed
    ``_line_reference_valid``/``_evidence_in_claimed_lines``, so
    :func:`_resolve_line_window` is expected to succeed — persisting the
    resolved anchor (rather than the reviewer's raw, possibly
    ``_LINE_ANCHOR_TOLERANCE``-lines-off claim) keeps downstream consumers of
    an accepted finding (the verdict comment, the fix-loop prompt) pointed at
    the real changed line. The unlikely resolution-failure case (e.g. an
    unanchored finding whose file isn't in the diff at all) returns *finding*
    unchanged rather than raising (#1715).

    After resolution, :func:`_reconcile_evidence_window` is tried against the
    narrow ``file_line_text`` (added-lines-only) substrate — deliberately NOT
    ``file_window_text`` — so a persisted anchor can be repaired to better
    match its own evidence's true span (#1792) without ever snapping onto a
    context line: this preserves the same #1738 invariant that keeps a
    persisted anchor pointed at real added-line content even when the
    (separately, more permissively matched) evidence-quote check in
    :func:`_evidence_in_claimed_lines` spans further via the wider
    ``file_window_text`` substrate. A reconciliation that finds no better
    match (or whose added-only substrate simply cannot reach the evidence's
    true span) leaves the anchor at its #1715 near-line-tolerance resolution,
    unchanged.
    """
    if finding.line_start is None and finding.line_end is None:
        return finding
    resolved = _resolve_line_window(
        diff, finding.file, finding.line_start, finding.line_end
    )
    if resolved is None:
        return finding
    start, end = resolved
    candidates = diff.file_line_text.get(finding.file, {})
    reconciled = _reconcile_evidence_window(candidates, finding.evidence, start, end)
    if reconciled is not None and reconciled != (start, end):
        _log.info(
            "auto-dev: repaired finding's declared line window to match its "
            "own evidence span (file=%s, declared=%d-%d, repaired=%d-%d)",
            finding.file,
            start,
            end,
            reconciled[0],
            reconciled[1],
        )
        start, end = reconciled
    updates: dict[str, int | None] = {}
    if finding.line_start is not None:
        updates["line_start"] = start
    if finding.line_end is not None:
        updates["line_end"] = end
    return finding.model_copy(update=updates)


def _classify_unanchored_file(
    file: str, worktree: Path | None
) -> Literal["unanchored", "unknown_file"]:
    """Classify a finding's file once it's known not to be a diff key (#1632).

    Split out of :func:`_classify_finding` to keep that function's return
    count under the ``PLR0911`` ceiling. ``worktree=None`` (no caller opted
    in) or a failed tree-existence check both return ``"unknown_file"`` —
    today's behavior, byte-identical.
    """
    if worktree is not None and _file_in_repo_tree(worktree, file):
        return "unanchored"
    return "unknown_file"


def _evidence_window_discrepancy_detail(finding: Finding) -> str:
    """Build a diagnosable ``RejectedFinding.detail`` for an
    ``evidence_not_in_diff`` rejection (#1792 AC4).

    For a line-anchored finding, only called once it has already passed
    ``_line_reference_valid`` (its endpoints DO resolve to real diff lines) —
    the rejection is about the *span*, not the anchor, so this reports the
    evidence's own line count against the declared window rather than
    re-deriving anchor validity. The no-anchor branch below instead covers a
    file-level finding (``line_start``/``line_end`` both ``None``), which has
    no endpoints for ``_line_reference_valid`` to have validated in the first
    place. Takes only *finding* (no ``diff`` — the message is derived
    entirely from the finding's own declared/evidence shape, not from diff
    content).
    """
    evidence_lines = finding.evidence.count("\n") + 1
    if finding.line_start is None and finding.line_end is None:
        return (
            f"evidence is {evidence_lines} line(s); finding has no line "
            "anchor (file-level fallback) and the evidence text was not "
            "found anywhere in the file's diff"
        )
    declared_lines = (
        (finding.line_end or finding.line_start or 0)
        - (finding.line_start or finding.line_end or 0)
        + 1
    )
    return (
        f"evidence is {evidence_lines} line(s) long but the declared range "
        f"line_start={finding.line_start}, line_end={finding.line_end} spans "
        f"{declared_lines} line(s); no window within ±{_LINE_ANCHOR_TOLERANCE} "
        "lines of the declared range contains the evidence text verbatim"
    )


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
    changed lines (possibly snapped within tolerance) — a bogus line reference
    (e.g. ``line_start=999``) is reported
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

    ``worktree`` also threads into :func:`_line_reference_valid` as the
    #1743 enclosing-def fallback: a structural finding anchored on a
    function/class's ``def`` line, which is itself rarely a changed line,
    can now survive this check when a changed line falls inside that
    definition's span. This can turn a previously ``invalid_line_reference``
    case into ``evidence_not_in_diff`` at the very next check below —
    intentional; #1743 owns the anchor-resolution axis, #1738 owns
    evidence-quote matching.
    """
    if finding.severity not in _VALID_SEVERITIES:
        return "invalid_severity"
    if _is_blank(finding.evidence):
        return "missing_evidence"
    if finding.file not in changed:
        return _classify_unanchored_file(finding.file, worktree)
    if not _line_reference_valid(diff, finding, worktree):
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
                    detail=(
                        _evidence_window_discrepancy_detail(finding)
                        if reason == "evidence_not_in_diff"
                        else ""
                    ),
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
        resolved_finding = _resolved_finding(diff, finding)
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
            accepted.append(resolved_finding.model_copy(update={"escalation": None}))
        else:
            accepted.append(resolved_finding)

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


def _select_rejected_must_fix(rejected: list[RejectedFinding]) -> list[RejectedFinding]:
    """Select the MUST_FIX-severity members of *rejected* (#1714).

    Keyed on the finding's own claimed SEVERITY, never on an enumerated set of
    :data:`RejectedFindingReason` values — so a reason added later is covered by
    construction rather than by remembering to extend a set here. ``raw`` is the
    pre-validation ``Finding.model_dump()``, read defensively via ``.get()``
    because a rejected payload is by definition one that failed validation.

    Why this is a SECOND signal and not a widening of ``blocking``: a
    mechanically-rejected finding was dropped precisely because its file/line/
    evidence anchor could not be trusted, so it must never be handed to the
    autofix loop (which gates on ``ReviewVerdict.blocking``; see
    ``cw.codex_fix_loop``). It still must not vanish silently, though — a
    MUST_FIX nobody ever evaluated on its merits is not a clean review. So the
    two travel separately: ``blocking`` drives autofix, this drives an operator
    park (``cw.codex_review._verdict``).
    """
    return [rf for rf in rejected if rf.raw.get("severity") == "MUST_FIX"]


def consolidate_verdict(
    documents: list[ReviewerFindingsDocument],
    diff: CapturedDiff,
    reviewed_sha: str,
    *,
    worktree: Path | None = None,
    failed_reviewers: list[ReviewerRunFailure] | None = None,
    fix_cycles_used: int = 0,
    metrics_by_role: dict[str, ReviewerRunMetrics] | None = None,
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

    ``rejected_must_fix`` (#1714) is the MUST_FIX-severity subset of
    ``rejected`` — see :func:`_select_rejected_must_fix`. It is computed
    independently of ``blocking``/``must_fix``, which continue to read accepted
    findings only: a mechanically-rejected finding must block the pipeline for
    an operator without ever entering the autofix loop.

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

    ``metrics_by_role`` (default ``None`` → ``{}``) supplies per-role executor
    audit telemetry, splatted onto the matching :class:`ReviewerRunRecord` for
    documents and failures alike; a role with no entry keeps every telemetry
    field at its default. Defaulting to ``None`` is what lets the Claude-native
    caller (``cw.cli.review``), which never has codex metrics, stay unchanged —
    and it is purely additive: nothing here reads the values back (#1710).

    Each parsed document's ``detail`` (#1775) is copied verbatim onto its
    :class:`ReviewerRunRecord`, unconditionally of ``status`` — so a degraded
    role's stated reason (or an ``ok`` role's justification) reaches
    ``verdict.agents_run`` and, from there, the persisted artifact
    (:func:`write_review_verdict`). A role recorded only via
    ``failed_reviewers`` has no document and so no ``detail`` to copy; its
    record keeps the ``""`` default.
    """
    failures = failed_reviewers if failed_reviewers is not None else []
    metrics = metrics_by_role if metrics_by_role is not None else {}
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
                detail=doc.detail,
                **metrics.get(doc.reviewer_role, {}),
            )
        )

    run_records.extend(
        # ReviewerRunFailure has no `detail` concept (it never parsed into a
        # document), so this entry's `detail` stays at its "" default (#1775).
        ReviewerRunRecord(
            reviewer_role=failure.role,
            status="failed",
            finding_count=0,
            **metrics.get(failure.role, {}),
        )
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
        rejected_must_fix=_select_rejected_must_fix(all_rejected),
    )


def write_review_verdict(verdict: ReviewVerdict, path: Path) -> None:
    """Atomically write *verdict* to *path* as JSON (#1108 artifact).

    Full-replace semantics via :func:`cw.atomic.atomic_write_text`. Not wired to
    any executor/CLI call site in this ticket.
    """
    atomic_write_text(path, verdict.model_dump_json(indent=2))
