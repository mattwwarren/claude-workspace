"""The REVIEW-to-IMPL regress findings rule (#3694, RFC 0019).

When a row regresses from REVIEW (or FINALIZE) back to IMPL, the verdict the
REVIEW pass wrote to the worktree's ``.claude/review-verdict.json`` holds the
MUST_FIX findings the implementation must address. This module owns the one
stateless rule that decides whether those findings are delivered to the IMPL
agent. Every consumer calls :func:`regress_review_findings`: the codex prompt
section, the codex ``existing_branch_work`` exemption, the claude-native
``queue_metadata.regress_review_findings`` key and the requeue echo.

The rule reads only the row's stage facts and the artifact. It never reads
``regressed_into_stage``, ``blocked_reason``, ``pr_url`` or HEAD, so it holds
on every IMPL retry until the next REVIEW pass rewrites the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from cw.context_queue_metadata import REGRESS_REVIEW_FINDINGS_KEY
from cw.dev_queue.must_fix_override import read_owned_verdict
from cw.models import Stage
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES
from cw.models.tasks.schema import REGRESSED_OWN_BRANCH_HIGH_WATER
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH
from cw.stage_verification.posted_text import TRUNCATION_MARKER, redact_and_cap

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_findings import Finding

#: Why the rule withheld delivery. ``artifact_missing`` covers an absent,
#: unreadable or invalid verdict; ``not_blocking`` covers a non-blocking verdict
#: and a blocking one with nothing in ``must_fix``.
WithheldReason = Literal[
    "review_feedback_present",
    "override_present",
    "artifact_missing",
    "not_owned",
    "not_blocking",
]

_CAP_BYTES: Final = MAX_REVIEW_REPORT_BYTES
_MARKER_BYTES: Final = len(TRUNCATION_MARKER.encode("utf-8"))


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
        for index, finding in enumerate(must_fix, start=1)
    ]
    capped = redact_and_cap(
        "\n\n".join(blocks),
        max_line_chars=_CAP_BYTES,
        max_total_chars=_CAP_BYTES,
    )
    cut = capped.endswith(TRUNCATION_MARKER)
    body = capped.removesuffix(TRUNCATION_MARKER) if cut else capped
    raw = body.encode("utf-8")
    if not cut and len(raw) <= _CAP_BYTES:
        return body
    budget = _CAP_BYTES - _MARKER_BYTES
    return raw[:budget].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _applies(task: TicketTask) -> bool:
    """Whether the rule is in scope: an IMPL row whose high water is REVIEW/FINALIZE."""
    return (
        task.stage == Stage.IMPL
        and task.stage_high_water in REGRESSED_OWN_BRANCH_HIGH_WATER
    )


def regress_review_findings(task: TicketTask, worktree: Path) -> RegressOutcome | None:
    """Decide whether the REVIEW verdict's MUST_FIX findings reach this IMPL run.

    ``None`` means the rule does not apply (not an IMPL row, or the high water
    is not REVIEW or FINALIZE): nothing is read, printed or recorded.
    """
    if not _applies(task):
        return None
    if task.review_feedback is not None:
        return RegressOutcome(None, "review_feedback_present")
    if task.must_fix_override is not None:
        return RegressOutcome(None, "override_present")
    read = read_owned_verdict(
        worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH, task.ticket_id
    )
    if read.verdict is None:
        return RegressOutcome(None, _refusal_reason(read.refusal))
    verdict = read.verdict
    if not verdict.must_fix:
        return RegressOutcome(None, "not_blocking")
    findings = RegressFindings(
        reviewed_sha=verdict.reviewed_sha,
        text=_render(verdict.must_fix),
    )
    return RegressOutcome(findings, None)


def _refusal_reason(refusal: str | None) -> WithheldReason:
    """Map a :func:`read_owned_verdict` refusal onto the closed vocabulary."""
    if refusal == "not_owned":
        return "not_owned"
    if refusal == "not_blocking":
        return "not_blocking"
    return "artifact_missing"


def with_regress_findings(
    metadata: dict[str, object], task: TicketTask, worktree: Path
) -> dict[str, object]:
    """Return *metadata* with ``regress_review_findings`` set (null when withheld)."""
    outcome = regress_review_findings(task, worktree)
    findings = outcome.findings if outcome is not None else None
    value: dict[str, str] | None = findings.wire() if findings is not None else None
    return {**metadata, REGRESS_REVIEW_FINDINGS_KEY: value}
