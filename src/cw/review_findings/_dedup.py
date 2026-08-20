"""Cross-reviewer dedup and count aggregation (#1237).

Merges the ``(reviewer_role, Finding)`` candidates that survived
:mod:`cw.review_findings._validation` into :class:`AcceptedFinding` groups, and
aggregates those into the :class:`cw.auto_dev_result.Review` count block the
approval gate reads.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import Review
from cw.review_findings._models import AcceptedFinding

if TYPE_CHECKING:
    from cw.review_findings._models import Finding


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
