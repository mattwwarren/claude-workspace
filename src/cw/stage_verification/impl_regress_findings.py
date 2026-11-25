"""The REVIEW-to-IMPL regress findings rule (#3694, RFC 0019).

When a row regresses from REVIEW (or FINALIZE) back to IMPL, the verdict the
REVIEW pass wrote to the worktree's ``.claude/review-verdict.json`` holds the
MUST_FIX findings the implementation must address. This module owns the one
stateless rule that decides whether those findings are delivered to the IMPL
agent. Every consumer calls :func:`regress_review_findings`: the codex prompt
section, the codex ``existing_branch_work`` exemption, the claude-native
``queue_metadata.regress_review_findings`` key and the requeue echo.

A claude-native blocking-mode park saves no verdict, so the blocking-findings
comment it posted is the source there: ``cw post-comment`` saves its body to
``.cw/review-park-findings.json`` (#3762, :mod:`cw.review_park_findings`), and
that saved record takes precedence over the verdict readers.

The rule reads only the row's stage facts and the artifacts. It never reads
``regressed_into_stage``, ``blocked_reason``, ``pr_url`` or HEAD, so it holds
on every IMPL retry until the next REVIEW pass rewrites or clears them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from cw.context_queue_metadata import REGRESS_REVIEW_FINDINGS_KEY
from cw.dev_queue.must_fix_override import read_owned_verdict
from cw.models import Stage
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES
from cw.models.tasks.schema import REGRESSED_OWN_BRANCH_HIGH_WATER
from cw.review_findings import (
    REVIEW_VERDICT_JSON_RELATIVE_PATH,
    has_held_findings,
    held_findings,
)
from cw.review_park_findings import read_park_findings
from cw.stage_verification.impl_regress_ledger import read_held_round_verdict
from cw.stage_verification.posted_text import redact_and_cap_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_findings import Finding, ReviewVerdict

#: Why the rule withheld delivery. ``artifact_missing`` covers an absent,
#: unreadable or invalid verdict; ``not_owned`` covers a foreign verdict, a
#: saved park-findings record for another ticket (#3762) and a ledger round
#: recorded for another head; ``not_blocking`` covers a verdict
#: with no deliverable finding: no blocking ``must_fix`` and no held
#: evidence-gap finding, a clean-mode ledger none of whose listed rounds holds a
#: held finding, or one whose listed round holds an unsettled decision contest
#: (the contest withholds every gap finding until ``cw review settle``).
WithheldReason = Literal[
    "review_feedback_present",
    "override_present",
    "artifact_missing",
    "not_owned",
    "not_blocking",
]

REVIEW_FEEDBACK_PRESENT: Final[WithheldReason] = "review_feedback_present"
OVERRIDE_PRESENT: Final[WithheldReason] = "override_present"
ARTIFACT_MISSING: Final[WithheldReason] = "artifact_missing"
NOT_OWNED: Final[WithheldReason] = "not_owned"
NOT_BLOCKING: Final[WithheldReason] = "not_blocking"

_CAP_BYTES: Final = MAX_REVIEW_REPORT_BYTES


@dataclass(frozen=True)
class RegressFindings:
    """Delivered MUST_FIX text plus the REVIEW pass's pinned head."""

    reviewed_sha: str
    text: str

    def wire(self) -> dict[str, str]:
        """The ``queue_metadata.regress_review_findings`` wire shape."""
        return {"reviewed_sha": self.reviewed_sha, "text": self.text}


@dataclass(frozen=True)
class RegressOutcome:
    """Delivered findings, or the reason they were withheld. Exactly one is set."""

    findings: RegressFindings | None
    reason: WithheldReason | None

    @property
    def friction(self) -> str:
        """The one-line friction record for this outcome."""
        if self.findings is not None:
            return "regress_review_findings: delivered"
        return f"regress_review_findings: withheld ({self.reason})"


def _render(must_fix: list[Finding]) -> str:
    """Render MUST_FIX entries (severity, file, summary, evidence) under the cap.

    Lines are not cut at the 120-character posted-text default: the cap is the
    64 KiB report bound. The truncation marker always ends the text when cut.
    """
    blocks = [
        f"{index}. [{finding.severity}] {finding.file}\n"
        f"   summary: {finding.summary}\n"
        f"   evidence: {finding.evidence}"
        + (
            f"\n   evidence gap (not verified by the reviewer): {finding.evidence_gap}"
            if finding.evidence_gap and finding.evidence_gap.strip()
            else ""
        )
        for index, finding in enumerate(must_fix, start=1)
    ]
    return redact_and_cap_bytes("\n\n".join(blocks), cap_bytes=_CAP_BYTES)


def _applies(task: TicketTask) -> bool:
    """Whether the rule is in scope: an IMPL row whose high water is REVIEW/FINALIZE."""
    return (
        task.stage == Stage.IMPL
        and task.stage_high_water in REGRESSED_OWN_BRANCH_HIGH_WATER
    )


class _Loaded(NamedTuple):
    """The verdict the rule reads, its refusal when absent, and its source."""

    verdict: ReviewVerdict | None
    reason: WithheldReason | None
    from_ledger: bool


def _load_verdict(task: TicketTask, worktree: Path) -> _Loaded:
    """Read the codex envelope; fall back to the clean-mode ledger when it is absent."""
    read = read_owned_verdict(
        worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH,
        task.ticket_id,
        require_blocking=False,
    )
    if read.verdict is not None:
        return _Loaded(read.verdict, None, from_ledger=False)
    if read.refusal != ARTIFACT_MISSING:
        return _Loaded(None, _refusal_reason(read.refusal), from_ledger=False)
    ledger = read_held_round_verdict(worktree, task.ticket_id)
    if ledger.verdict is not None:
        return _Loaded(ledger.verdict, None, from_ledger=True)
    return _Loaded(None, _refusal_reason(ledger.refusal), from_ledger=True)


def _load_parked(task: TicketTask, worktree: Path) -> RegressOutcome | None:
    """The saved claude-native park findings (#3762), or ``None`` to fall through.

    A readable, owned record is delivered (re-redacted and capped on the way
    out); a record stamped for another ticket is ``not_owned`` with no
    fall-through to the verdict readers. Any other state, including an absent,
    unreadable or blank record, falls through to the verdict checks.
    """
    read = read_park_findings(worktree, task.ticket_id)
    if read.findings is not None:
        text = redact_and_cap_bytes(read.findings.text, cap_bytes=_CAP_BYTES)
        return RegressOutcome(RegressFindings(read.findings.reviewed_sha, text), None)
    if read.refusal == NOT_OWNED:
        return RegressOutcome(None, NOT_OWNED)
    return None


def _deliverable(verdict: ReviewVerdict, *, with_must_fix: bool) -> list[Finding]:
    """The findings the rule may deliver from one verdict, in delivery order.

    A decision contest anywhere in the verdict withholds the evidence-gap holds
    beside it (R5); ``must_fix`` survivors still deliver. Duplicates cannot
    occur: held findings sit in ``accepted`` and are excluded from ``must_fix``.
    """
    must_fix = list(verdict.must_fix) if with_must_fix and verdict.blocking else []
    if has_held_findings(verdict, "decision_contest"):
        return must_fix
    return [*must_fix, *(af.finding for af in held_findings(verdict, "evidence_gap"))]


def _operator_guard(task: TicketTask) -> WithheldReason | None:
    """The reason an operator-supplied direction already covers this IMPL run."""
    if task.review_feedback is not None:
        return REVIEW_FEEDBACK_PRESENT
    if task.must_fix_override is not None:
        return OVERRIDE_PRESENT
    return None


def regress_review_findings(task: TicketTask, worktree: Path) -> RegressOutcome | None:
    """Decide whether the REVIEW verdict's findings reach this IMPL run.

    ``None`` means the rule does not apply (not an IMPL row, or the high water
    is not REVIEW or FINALIZE): nothing is read, printed or recorded.
    """
    if not _applies(task):
        return None
    guard = _operator_guard(task)
    if guard is not None:
        return RegressOutcome(None, guard)
    parked = _load_parked(task, worktree)
    if parked is not None:
        return parked
    loaded = _load_verdict(task, worktree)
    if loaded.verdict is None:
        return RegressOutcome(None, loaded.reason)
    deliverable = _deliverable(loaded.verdict, with_must_fix=not loaded.from_ledger)
    if not deliverable:
        return RegressOutcome(None, NOT_BLOCKING)
    findings = RegressFindings(
        reviewed_sha=loaded.verdict.reviewed_sha,
        text=_render(deliverable),
    )
    return RegressOutcome(findings, None)


def _refusal_reason(refusal: str | None) -> WithheldReason:
    """Map a :func:`read_owned_verdict` refusal onto the closed vocabulary."""
    if refusal == NOT_OWNED:
        return NOT_OWNED
    if refusal == NOT_BLOCKING:
        return NOT_BLOCKING
    return ARTIFACT_MISSING


def with_regress_findings(
    metadata: dict[str, object], task: TicketTask, worktree: Path
) -> dict[str, object]:
    """Return *metadata* with ``regress_review_findings`` set (null when withheld)."""
    outcome = regress_review_findings(task, worktree)
    findings = outcome.findings if outcome is not None else None
    value: dict[str, str] | None = findings.wire() if findings is not None else None
    return {**metadata, REGRESS_REVIEW_FINDINGS_KEY: value}
