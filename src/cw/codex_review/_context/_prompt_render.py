"""Reviewer-prompt assembly and codex output-document parsing.

Where the static text in ``_prompt_text`` meets the per-pass material the other
submodules loaded: :func:`_build_reviewer_prompt` interleaves them into one
role's materialized prompt, and :func:`_parse_reviewer_document` reads codex's
``-o`` output back. The two per-pass finding blocks that only this assembly
needs — unresolved prior findings and the binding cross-round adjudication
ledger — are rendered here alongside it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.codex_review._context._prompt_text import (
    _ADJUDICATED_HEADER,
    _DELTA_MODE_INSTRUCTIONS,
    _codex_output_format_supplement,
    _select_output_instructions,
)
from cw.codex_review._context._sensitive_files import _render_sensitive_block
from cw.review_finding_dispositions import split_disposition_key
from cw.review_findings import ReviewerFindingsDocument, parse_reviewer_document

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.codex_review._context._sensitive_files import _SensitiveHit
    from cw.review_finding_dispositions import FindingDisposition
    from cw.review_findings import CapturedDiff, Finding, RejectedFinding


def _render_prior_open_findings(findings: list[Finding]) -> str:
    """Render the still-open MUST_FIX findings from earlier fix cycles (#1837).

    Informational only: a reviewer may re-report any of these if it is still
    present, but is not asked to restate a verdict on each. A finding simply
    absent from a later cycle's output is treated as resolved.
    """
    lines = [
        "## Unresolved Prior Findings",
        "MUST_FIX findings still open from earlier fix cycles in this run. "
        "Re-report any that are still present; say nothing about the ones "
        "that are not.",
    ]
    lines.extend(
        f"- **{f.file}** — {f.summary}\n  - evidence: `{f.evidence}`" for f in findings
    )
    return "\n".join(lines)


def _render_adjudicated_findings_block(
    ledger: dict[str, FindingDisposition],
) -> str | None:
    """Render the cross-round "do not re-raise" block (#1838, R4a + R5).

    ``None`` for an empty ledger, following the gated-on-non-empty convention
    :func:`~cw.codex_review._context._repo_config._render_lint_grounding_block`
    and :func:`~cw.codex_review._context._sensitive_files._render_sensitive_block`
    already use — so a pass with no adjudication history produces a
    byte-identical prompt to the pre-#1838 one.

    Distinct from :func:`_render_prior_open_findings`, its nearest sibling, on
    exactly the axis that matters: that block is *informational* ("re-report
    any that are still present"), because those findings are unresolved. This
    one is *binding*, because an operator already decided them. Collapsing the
    two would tell a reviewer to re-report a settled finding, which is the bug
    this ticket exists to fix.

    Both outcomes render: an ``ACCEPTED`` entry tells the reviewer the finding
    was upheld and needs no restating, which is as useful as knowing one was
    rejected. Only ``REJECTED`` reaches the mechanical backstop in
    ``review_finding_dispositions.suppress_adjudicated_findings``.
    """
    if not ledger:
        return None
    lines = [
        _ADJUDICATED_HEADER,
        "An operator already adjudicated each finding below on an earlier "
        "review round, and that decision is BINDING. Do not re-raise one "
        "unless the code at that location has changed since the recorded "
        "date -- re-reporting a settled finding is noise, not a finding. If "
        "you believe a rejection is now wrong, say so in the finding's "
        "`consequence` field rather than re-filing it as MUST_FIX.",
    ]
    for key, entry in sorted(ledger.items()):
        file, summary = split_disposition_key(key)
        rationale = f" ({entry.rationale})" if entry.rationale else ""
        lines.append(
            f"- **{file}** — {summary} — previously adjudicated: "
            f"{entry.outcome}, do not re-raise unless the code at this "
            f"location changed{rationale}"
        )
    return "\n".join(lines)


def _build_reviewer_prompt(
    role: str,
    *,
    agent_spec_text: str,
    diff: CapturedDiff,
    changed_files: Iterable[str],
    plan_text: str | None,
    ticket_text: str | None,
    project_rubrics: str | None,
    repo_policy_section: str | None,
    sensitive_hits: list[_SensitiveHit],
    capable: bool = False,
    lint_grounding: str | None = None,
    operator_comments_text: str | None = None,
    pending_operator_comment: bool = False,
    prior_open_findings: list[Finding] | None = None,
    delta_mode: bool = False,
    adjudicated_findings: dict[str, FindingDisposition] | None = None,
) -> str:
    """Materialize one reviewer's full prompt, inlining every needed section.

    *capable* selects which ``_OUTPUT_INSTRUCTIONS`` variant closes the prompt
    (#1709). It defaults to ``False`` — the pre-#1709 text — purely so this
    function's variant-agnostic unit tests stay byte-identical; the sole
    production caller (:func:`~cw.codex_review._context.core._prepare_review_pass`)
    always passes the probed value explicitly, so the default never fires in
    production.

    *lint_grounding* (#1744) is the rendered repo-lint-configuration block —
    the repo's ruff opt-outs and complexity thresholds — so reviewers stop
    raising MUST_FIX findings against rules the repo has explicitly ignored.
    Same safe-default convention as *capable*: defaults to ``None`` for the
    variant-agnostic unit tests; ``_prepare_review_pass`` always passes it
    explicitly.

    *operator_comments_text* (#1730) is the live-fetched ticket comment thread,
    and *pending_operator_comment* the per-arrival marker saying this REVIEW
    re-entry followed a regress. When the marker is set, the comments section
    is prefixed with a banner making them a binding adjudication input rather
    than background context. Same safe-default convention again.

    *prior_open_findings* and *delta_mode* (#1837) are the fix-loop re-review
    pair: the MUST_FIX findings still open from earlier cycles, and the flag
    saying the inlined diff is a delta rather than the whole pull request.
    Both default off, so cycle 0's prompt is byte-identical to before.

    *adjudicated_findings* (#1838) is the cross-round adjudication ledger — the
    findings an operator already settled, which the reviewer is told outright
    not to re-raise. Same safe-default convention as every kwarg above: ``None``
    (or an empty ledger) leaves the prompt byte-identical to the pre-#1838 one.
    """
    parts = [
        f"# Reviewer Role: {role}",
        f"## Agent Specification\n{agent_spec_text}",
    ]
    supplement = _codex_output_format_supplement(role)
    if supplement:
        parts.append(supplement)
    if ticket_text:
        parts.append(f"## Ticket Context\n{ticket_text}")
    if operator_comments_text:
        banner = (
            "## Pending Operator Send-Back (#1730)\nThis REVIEW re-entry"
            " follows a regress or requeue. Read the comments below before"
            " finalizing findings -- a comment reflecting a prior operator"
            " adjudication on a specific finding is binding, not advisory.\n\n"
            if pending_operator_comment
            else ""
        )
        parts.append(
            "## Ticket Comments (live-fetched, chronological)\n"
            + banner
            + operator_comments_text
        )
    if plan_text:
        parts.append(f"## Approved Plan\n{plan_text}")
    if project_rubrics:
        parts.append(f"## Project Rubrics\n{project_rubrics}")
    if repo_policy_section:
        parts.append(f"## Repo Policy for {role}\n{repo_policy_section}")
    if lint_grounding:
        parts.append(f"## Repo Lint Configuration\n{lint_grounding}")
    if sensitive_hits:
        parts.append(_render_sensitive_block(sensitive_hits))
    if adjudicated_findings:
        parts.append(_render_adjudicated_findings_block(adjudicated_findings) or "")
    if prior_open_findings:
        parts.append(_render_prior_open_findings(prior_open_findings))
    parts.append("## Changed Files\n" + "\n".join(changed_files))
    parts.append(f"## Diff\n{diff.text}")
    parts.append(_select_output_instructions(capable))
    if delta_mode:
        parts.append(_DELTA_MODE_INSTRUCTIONS)
    return "\n\n".join(parts)


def _parse_reviewer_document(
    output_file_content: str | None,
) -> tuple[ReviewerFindingsDocument, list[RejectedFinding]] | None:
    """Parse codex's ``-o`` output into a document, failing closed to ``None``.

    Returns ``(document, rejected)`` since #2029, where ``rejected`` holds the
    ``findings[]`` items that could not become a :class:`Finding`. ``None`` now
    means only what it says on the tin — no output at all, undecodable JSON, or
    a STRUCTURAL schema failure that survived the per-finding rescue. A single
    bad finding no longer costs the role its entire document (and, downstream,
    a ``CODEX_REVIEW_UNPARSEABLE`` park over findings that were perfectly fine).
    """
    if output_file_content is None:
        return None
    try:
        data = json.loads(output_file_content)
    except json.JSONDecodeError:
        return None
    try:
        return parse_reviewer_document(data)
    except ValueError:
        return None
