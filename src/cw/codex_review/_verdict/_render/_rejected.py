"""The sections reporting findings validation dropped before adjudication.

Three siblings: the MUST_FIX findings mechanically rejected (#1714, with their
full original text since #2081), the sub-MUST_FIX ones (#2000), and the
findings a structurally-failed reviewer reported but nobody read (#2029).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from cw.codex_review._verdict._render._common import _safe
from cw.review_findings import Severity

if TYPE_CHECKING:
    from cw.review_findings import RejectedFinding, ReviewVerdict

# #2000: severity ordering for the rejected-findings section, derived from the
# `Severity` Literal itself rather than a hand-maintained rank table -- the
# same USE_EXISTING pattern `_classify._VALID_SEVERITIES` already applies to
# the same type, so a severity added later is ordered by construction.
_SEVERITY_ORDER: tuple[str, ...] = get_args(Severity)


def _render_rejected_finding_text(rf: RejectedFinding) -> list[str]:
    """Render a rejected MUST_FIX's full original text under its line (#2081).

    The one-line ``summary`` plus a rejection code reads as "the reviewer made
    a citation error"; the operator deciding whether to act on a mechanically
    rejected MUST_FIX needs what the reviewer actually said — its
    ``consequence``, ``suggested_fix`` and verbatim ``evidence`` — without
    opening the persisted verdict artifact. Each renders as an indented
    follow-up line in the same shape ``rf.detail`` already uses; ``evidence``
    goes in a fenced block because it is quoted source text and may span
    lines. Read via ``.get()`` like every other ``raw`` consumer, and a field
    that is missing or blank simply renders nothing.
    """
    lines: list[str] = []
    labelled_fields = (
        ("consequence", "consequence"),
        ("suggested fix", "suggested_fix"),
    )
    for label, key in labelled_fields:
        value = rf.raw.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"  - {label}: {_safe(value.strip())}")
    evidence = rf.raw.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        lines.append("  - evidence:")
        lines.append("    ```")
        # A fence stops markdown from RENDERING the quoted source; it does
        # nothing to the ledger parser, which reads the raw body, so quoted
        # code is escaped exactly like prose (#2210 round 4).
        lines.extend(f"    {_safe(line)}" for line in evidence.strip().splitlines())
        lines.append("    ```")
    return lines


def _render_rejected_must_fix(verdict: ReviewVerdict) -> list[str]:
    """Render the MUST_FIX findings validation dropped before adjudication.

    ``_render_findings`` iterates ``verdict.accepted`` only, so before #1714 a
    mechanically-rejected MUST_FIX was invisible on the posted comment even
    when it was the reason the pipeline blocked — the reader saw a park with no
    findings behind it. Rendered unconditionally (mirroring
    ``_render_failed_roles_note``'s empty-list-returns-``[]`` shape) so the
    mixed case, where an accepted MUST_FIX also blocks, still surfaces both.

    ``RejectedFinding.raw`` is the pre-validation ``Finding.model_dump()``, so
    it carries ``Finding``'s field names — but read via ``.get()`` because a
    rejected payload is by definition one that failed validation.

    ``rf.detail`` (#1792), when non-blank (populated for the
    ``evidence_not_in_diff`` reason specifically — see
    ``_evidence_window_discrepancy_detail``), renders as an indented
    follow-up line so the diagnosable discrepancy (declared vs. evidence
    line counts) reaches the operator reading the posted comment, not just
    the persisted verdict artifact. :func:`_render_rejected_finding_text`
    (#2081) follows it with the finding's full original text, same indented
    shape: the per-finding line itself is unchanged.
    """
    if not verdict.rejected_must_fix:
        return []
    lines = ["### MUST_FIX — mechanically rejected (not adjudicated)", ""]
    for rf in verdict.rejected_must_fix:
        loc = _safe(str(rf.raw.get("file", "<unknown file>")))
        line_start = rf.raw.get("line_start")
        if line_start is not None:
            loc = f"{loc}:{line_start}"
        summary = _safe(str(rf.raw.get("summary", "<no summary>")))
        lines.append(f"- **{loc}** — {summary} (rejected: {_safe(rf.reason)})")
        if rf.detail:
            lines.append(f"  - {_safe(rf.detail)}")
        lines.extend(_render_rejected_finding_text(rf))
    lines.append("")
    return lines


def _rejected_severity_rank(rf: RejectedFinding) -> int:
    """Rank *rf* by the severity it claimed, MUST_FIX-first (#2000).

    ``raw`` is a payload that failed validation, so its ``severity`` may be
    missing or not even a member of the Literal — anything unrecognized sorts
    last rather than raising.
    """
    severity = rf.raw.get("severity")
    if severity in _SEVERITY_ORDER:
        return _SEVERITY_ORDER.index(severity)
    return len(_SEVERITY_ORDER)


def _render_rejected_below_must_fix(verdict: ReviewVerdict) -> list[str]:
    """Render the sub-MUST_FIX findings validation dropped before adjudication.

    The #2000 sibling of :func:`_render_rejected_must_fix`, and deliberately a
    SECOND function rather than a widening of that one: #1714's section is
    load-bearing for the force-block park and its heading, iteration source,
    and per-finding line shape must stay exactly as they are (R3/R4). The
    per-finding line here is written to match that function's output rather
    than sharing a helper with it, so nothing in this file can change the
    MUST_FIX section's bytes by accident.

    Below MUST_FIX there is no force-block and none is wanted (round-1
    operator resolution: informational, not gating) — but "not blocking" is
    not "not worth saying". A finding deleted here was never evaluated on its
    merits, and rendering nothing is what let a review that threw findings
    away read as a clean pass.

    R5 (designed for noise): rejections collapse by ``(reviewer_role,
    reason)`` into one ``<details>`` block per group carrying its count, so a
    matcher that misfires twelve times costs twelve lines behind one
    disclosure triangle rather than twelve lines of comment. Groups are
    ordered by their highest-severity member. Empty-returns-``[]``, mirroring
    every other per-concern helper in this file.
    """
    below = [rf for rf in verdict.rejected if rf not in verdict.rejected_must_fix]
    if not below:
        return []
    groups: dict[tuple[str, str], list[RejectedFinding]] = {}
    for rf in below:
        groups.setdefault((rf.reviewer_role, rf.reason), []).append(rf)

    def _group_order(key: tuple[str, str]) -> tuple[int, tuple[str, str]]:
        return (min(_rejected_severity_rank(rf) for rf in groups[key]), key)

    lines = ["### Below MUST_FIX — mechanically rejected (not adjudicated)", ""]
    for key in sorted(groups, key=_group_order):
        role, reason = key
        members = groups[key]
        lines.append("<details>")
        lines.append(
            f"<summary>{_safe(role)} — {_safe(reason)} ({len(members)})</summary>"
        )
        lines.append("")
        for rf in members:
            loc = _safe(str(rf.raw.get("file", "<unknown file>")))
            line_start = rf.raw.get("line_start")
            if line_start is not None:
                loc = f"{loc}:{line_start}"
            summary = _safe(str(rf.raw.get("summary", "<no summary>")))
            lines.append(f"- **{loc}** — {summary} (rejected: {_safe(rf.reason)})")
            if rf.detail:
                lines.append(f"  - {_safe(rf.detail)}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return lines


def _render_run_failure_discarded_note(verdict: ReviewVerdict) -> list[str]:
    """Render the findings a structurally-failed reviewer threw away (#2029).

    The residual sibling of :func:`_render_rejected_must_fix` and
    :func:`_render_rejected_below_must_fix`. Those two iterate
    :class:`RejectedFinding` records and can name each finding's file and
    summary; here the document never parsed, so nothing survives to name — only
    the role, why it failed, and a best-effort count of what it was claiming.
    Saying that much is the whole point: an operator reading a park needs to
    know a reviewer reported findings nobody ever read.

    Empty-returns-``[]``, mirroring every other per-concern helper here.
    Severities are sorted for a stable rendering across runs.
    """
    failures = verdict.run_failures_with_should_fix_discards
    if not failures:
        return []
    lines = ["### Reviewer failures that discarded findings", ""]
    for failure in failures:
        severities = ", ".join(
            f"{_safe(severity)}: {count}"
            for severity, count in sorted(failure.discarded_finding_severities.items())
        )
        lines.append(
            f"- **{_safe(failure.role)}** ({_safe(failure.reason)}) — "
            f"{failure.discarded_finding_count} finding(s) reported but never "
            f"read ({severities})"
        )
    lines.append("")
    return lines
