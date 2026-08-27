"""Rendering of a consolidated verdict into a GitHub-issue-comment body.

:func:`render_verdict_comment` picks one of four headlines and then appends
every per-concern note section unconditionally, each written to the same
empty-list-returns-``[]`` shape so a pass with nothing to say about a concern
produces no bytes for it. Nothing here reads or influences the disposition
table — the sections report what the verdict already decided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from cw.review_findings import Severity

if TYPE_CHECKING:
    from cw.auto_dev_result import Review
    from cw.review_findings import (
        AcceptedFinding,
        AgentSpecStatus,
        RejectedFinding,
        ReviewerRunRecord,
        ReviewVerdict,
    )

# #2000: severity ordering for the rejected-findings section, derived from the
# `Severity` Literal itself rather than a hand-maintained rank table -- the
# same USE_EXISTING pattern `_validation._VALID_SEVERITIES` already applies to
# the same type, so a severity added later is ordered by construction.
_SEVERITY_ORDER: tuple[str, ...] = get_args(Severity)


# Confidence values other than HIGH render an inline annotation on their
# finding line so a reader can weight it — confidence is display-only and
# must never gate/filter/reorder findings (R0, #1555). HIGH is the common
# case and stays unmarked to keep the common path uncluttered.
_CONFIDENCE_ANNOTATION = " _({confidence} confidence)_"

# A non-"fixed" disposition means the finding is no longer blocking — voided
# by an operator ("rejected", #1814), deferred, or never decided ("dropped",
# #1805). Before this annotation existed, `_render_findings` filtered on
# severity alone and discarded `disposition` before the loop body ran, so a
# suppressed MUST_FIX rendered byte-identically to a live one and the posted
# comment lied about its own contents. Display-only, exactly like
# _CONFIDENCE_ANNOTATION above: nothing is filtered, reordered, or split into
# a second heading.
_DISPOSITION_ANNOTATION = " _(suppressed — {disposition}{detail})_"


def _disposition_annotation(accepted: AcceptedFinding) -> str:
    """Annotate a finding whose disposition says it is no longer blocking.

    ``""`` for the ``"fixed"`` default (the common case stays uncluttered,
    same convention as HIGH confidence). ``disposition_detail`` is appended
    when the producer recorded one — it carries the *why* (which operator
    comment voided it, which adjudication deferred it), which is the whole
    point of surfacing this on the posted comment rather than only in the
    persisted verdict artifact.
    """
    if accepted.disposition == "fixed":
        return ""
    detail = f": {accepted.disposition_detail}" if accepted.disposition_detail else ""
    return _DISPOSITION_ANNOTATION.format(
        disposition=accepted.disposition, detail=detail
    )


def _render_findings(
    verdict: ReviewVerdict, severity: Severity, heading: str
) -> list[str]:
    # Iterates the AcceptedFinding, not just `.finding`, so `disposition` is
    # still in scope in the loop body (#1814/A1).
    accepted = [af for af in verdict.accepted if af.finding.severity == severity]
    if not accepted:
        return []
    lines = [f"### {heading}", ""]
    for af in accepted:
        finding = af.finding
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        annotation = (
            ""
            if finding.confidence == "HIGH"
            else _CONFIDENCE_ANNOTATION.format(confidence=finding.confidence)
        )
        suppression = _disposition_annotation(af)
        lines.append(f"- **{loc}**{annotation}{suppression} — {finding.summary}")
    lines.append("")
    return lines


def _render_clean_headline(review: Review, *, fix_loop_enabled: bool) -> str:
    """Render the non-blocking headline, distinguishing three histories (#1705).

    ``Review.fix_cycles_used``/``must_fix_initial``/``deferred`` alone cannot
    tell a fix-loop-disabled single pass apart from a fix-loop-enabled pass
    whose cycle-0 review was already clean — both produce
    ``fix_cycles_used == 0``. ``fix_loop_enabled`` (caller-known, threaded in
    via ``synthesize_codex_review_result``) is the discriminator (R1).

    Within the ``fix_cycles_used > 0`` (genuine fix-loop engagement) branch,
    ``Review.had_real_commit`` (#1723) further discriminates a converged loop
    that actually committed a change from one that converged purely because
    every fix cycle was a tolerated no-op — the latter renders an UNVERIFIED
    headline rather than claiming findings were resolved.
    """
    if review.fix_cycles_used > 0:
        resolved = review.must_fix_initial - review.deferred
        if review.had_real_commit is False:
            return (
                f"**UNVERIFIED** — the fix loop converged without changing "
                f"any file: {resolved} of {review.must_fix_initial} "
                f"originally-found MUST_FIX finding(s) show as resolved "
                f"across {review.fix_cycles_used} fix cycle(s), but no fix "
                "cycle actually committed a change. Treat this as unverified "
                "rather than genuinely fixed."
            )
        return (
            f"**Non-blocking** — {resolved} of {review.must_fix_initial} "
            f"originally-found MUST_FIX finding(s) resolved across "
            f"{review.fix_cycles_used} fix cycle(s); none remain open."
        )
    if fix_loop_enabled:
        return (
            "**Non-blocking** — no MUST_FIX findings. The fix loop was "
            "available for this run; none were needed."
        )
    return (
        "**Non-blocking** — no MUST_FIX findings. Single-pass review "
        "(fix loop disabled for this lane)."
    )


def _render_history_note(review: Review, *, fix_loop_enabled: bool) -> list[str]:
    """Render the blocking-branch history note (R1's blocking-branch half).

    Mirrors ``_render_clean_headline``'s discrimination for the still-blocking
    case: a fix-loop-disabled block must state its own single-pass state
    rather than silently looking like a fix loop that made no progress.
    """
    if not fix_loop_enabled:
        return ["_Single-pass review — fix loop disabled for this lane._", ""]
    if review.fix_cycles_used > 0:
        resolved = review.must_fix_initial - review.deferred
        return [
            f"_{resolved} of {review.must_fix_initial} originally-found "
            f"MUST_FIX finding(s) resolved across {review.fix_cycles_used} "
            f"fix cycle(s); {review.deferred} still open._",
            "",
        ]
    return []


def _render_failed_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "PARTIAL COVERAGE" note naming any role that failed to run.

    Reads ``verdict.agents_run`` (#1710's ``ReviewerRunRecord`` list) directly
    — no new plumbing needed. Surfaces reviewer-run failure onto the posted
    GitHub comment; previously only reached ``Blocker.details`` internally via
    ``_format_failures_detail`` on the zero-documents path.
    """
    failed_roles = [r.reviewer_role for r in verdict.agents_run if r.status == "failed"]
    if not failed_roles:
        return []
    roles = ", ".join(failed_roles)
    plural = "" if len(failed_roles) == 1 else "s"
    return [
        f"**PARTIAL COVERAGE** — {len(failed_roles)} role{plural} failed to run: "
        f"{roles}.",
        "",
    ]


def _degraded_role_label(record: ReviewerRunRecord) -> str:
    """Name one degraded role, with its stated reason if it gave one (#1775).

    ``record.detail`` is copied verbatim from the source
    ``ReviewerFindingsDocument`` by :func:`consolidate_verdict`, so a blank
    value here means the reviewer genuinely gave no reason -- not that the
    plumbing dropped it.
    """
    if record.detail:
        return f"{record.reviewer_role}: degraded — {record.detail}"
    return f"{record.reviewer_role}: degraded (no reason given)"


def _render_degraded_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "DEGRADED COVERAGE" note naming any role that ran degraded.

    Sibling of :func:`_render_failed_roles_note`: reads ``verdict.agents_run``
    directly, same empty-list-returns-``[]`` shape. A "failed" role (never
    produced a document) and a "degraded" role (produced a document but
    could not complete a required check) are distinct facts, so this note is
    additive to -- not a replacement for -- the partial-coverage note (#1775).
    """
    degraded = [r for r in verdict.agents_run if r.status == "degraded"]
    if not degraded:
        return []
    labels = ", ".join(_degraded_role_label(r) for r in degraded)
    plural = "" if len(degraded) == 1 else "s"
    return [
        f"**DEGRADED COVERAGE** — {len(degraded)} role{plural} ran degraded: {labels}.",
        "",
    ]


def _render_capability_note(verdict: ReviewVerdict) -> list[str]:
    """Render the probed filesystem-capability mode the review ran under.

    Deferred from #1709 pending #1705's rewrite of this function (#1725).
    ``capability_mode`` is ``None`` for any run that never probed (e.g. the
    LocalExecutor path, or a test verdict built without capability wiring) --
    that must render nothing, not "unknown", per #1709/#1725: an unprobed run
    and a probed-but-unclassifiable run are different facts, and only the
    probe (``_classify_capability_failure``) is allowed to say "unknown".
    """
    if verdict.capability_mode is None:
        return []
    if verdict.capability_mode == "capable":
        return ["_Reviewed with repo filesystem access (capable)._", ""]
    reason_suffix = (
        f" (reason: {verdict.capability_reason})" if verdict.capability_reason else ""
    )
    return [
        "_Reviewed in degraded mode — inlined-diff-only, no repo filesystem "
        f"access{reason_suffix}._",
        "",
    ]


def _agent_spec_label(status: AgentSpecStatus) -> str:
    """Name why *status*'s role ran without a loaded specification (#1773).

    ``empty_repo_file`` is checked first and independently of ``source``: once
    the repo-tracked file was found blank AND nothing usable replaced it, that
    is the actionable fact for whoever reads the comment, whichever source was
    consulted last.
    """
    if status.empty_repo_file:
        return "present but empty, no usable fallback"
    if status.source == "global":
        return "global spec found but empty"
    return "absent"


def _render_agent_spec_note(verdict: ReviewVerdict) -> list[str]:
    """Render the per-role agent-spec resolution summary (#1773).

    An empty ``agent_spec_status`` renders nothing: a verdict from a path that
    never resolved specs (the LocalExecutor path, a directly-synthesized test
    verdict) has no claim to make either way — same convention as
    ``_render_capability_note``'s unprobed case.

    A role counts as unspecified iff its final ``empty`` is True, whatever
    ``source`` says, which yields exactly one of three headlines. The
    recovered-empty-repo-file addendum is then appended independently of which
    headline won, so a truncated repo-tracked file still gets reported in a
    pass where some *other* role was also unspecified.
    """
    statuses = verdict.agent_spec_status
    if not statuses:
        return []
    unspecified = [s for s in statuses if s.empty]
    total = len(statuses)
    if not unspecified:
        line = f"_Agent specs loaded for all {total} reviewer role(s)._"
    elif len(unspecified) == total:
        line = (
            "**ALL AGENT SPECS UNSPECIFIED** — no reviewer role in this pass "
            "had a loaded agent specification (repo or global); every "
            "prompt's `## Agent Specification` section was empty."
        )
    else:
        named = ", ".join(f"{s.role} ({_agent_spec_label(s)})" for s in unspecified)
        line = (
            f"**AGENT SPEC(S) UNSPECIFIED** — {len(unspecified)} of {total} "
            f"role(s) ran without a loaded specification: {named}."
        )
    # A still-unspecified role already carries "(present but empty, no usable
    # fallback)" above, so only genuinely recovered ones get the addendum.
    for s in statuses:
        if s.empty_repo_file and not s.empty:
            line += (
                f" **NOTE:** {s.role}'s repo-tracked spec was present but "
                "empty — recovered via the global fallback; the repo-tracked "
                "file may be truncated or need attention."
            )
    return [line, ""]


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
    the persisted verdict artifact.
    """
    if not verdict.rejected_must_fix:
        return []
    lines = ["### MUST_FIX — mechanically rejected (not adjudicated)", ""]
    for rf in verdict.rejected_must_fix:
        loc = str(rf.raw.get("file", "<unknown file>"))
        line_start = rf.raw.get("line_start")
        if line_start is not None:
            loc = f"{loc}:{line_start}"
        summary = str(rf.raw.get("summary", "<no summary>"))
        lines.append(f"- **{loc}** — {summary} (rejected: {rf.reason})")
        if rf.detail:
            lines.append(f"  - {rf.detail}")
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
        lines.append(f"<summary>{role} — {reason} ({len(members)})</summary>")
        lines.append("")
        for rf in members:
            loc = str(rf.raw.get("file", "<unknown file>"))
            line_start = rf.raw.get("line_start")
            if line_start is not None:
                loc = f"{loc}:{line_start}"
            summary = str(rf.raw.get("summary", "<no summary>"))
            lines.append(f"- **{loc}** — {summary} (rejected: {rf.reason})")
            if rf.detail:
                lines.append(f"  - {rf.detail}")
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
            f"{severity}: {count}"
            for severity, count in sorted(failure.discarded_finding_severities.items())
        )
        lines.append(
            f"- **{failure.role}** ({failure.reason}) — "
            f"{failure.discarded_finding_count} finding(s) reported but never "
            f"read ({severities})"
        )
    lines.append("")
    return lines


def _render_delta_note(verdict: ReviewVerdict) -> list[str]:
    """Say which head this pass's diff was taken from, when it was a delta.

    ``None`` means say nothing (``_render_capability_note``'s convention): the
    pass reviewed the whole branch, which is the unremarkable case.
    """
    if verdict.previous_reviewed_sha is None:
        return []
    return [
        "This pass reviewed only what changed since "
        f"`{verdict.previous_reviewed_sha}` (fix-loop delta review).",
        "",
    ]


def _render_debt_note(verdict: ReviewVerdict) -> list[str]:
    """Render the debt the fix loop recorded instead of acting on (#1837).

    Two kinds land here: accepted DEBT-severity findings, and MUST_FIX
    findings the loop's admission gate refused because the latest fix cycle
    did not cause them. Neither blocks, and neither should vanish — this
    section is where an operator finds out what was set aside.

    Empty-returns-``[]``, mirroring ``_render_failed_roles_note``. The list is
    already deduplicated by fingerprint before it reaches the verdict, so
    there is no "already rendered" bookkeeping to do here.
    """
    if not verdict.debt:
        return []
    lines = ["### Debt — recorded, not blocking", ""]
    for record in verdict.debt:
        lines.append(
            f"- **{record.file}** — {record.summary} "
            f"({record.tracking_disposition}, fingerprint "
            f"`{record.fingerprint[1]}`)"
        )
        if record.suggested_follow_up:
            lines.append(f"  - {record.suggested_follow_up}")
    lines.append("")
    return lines


def render_verdict_comment(verdict: ReviewVerdict, *, fix_loop_enabled: bool) -> str:
    """Render a consolidated verdict into a GitHub-issue-comment markdown body.

    ``fix_loop_enabled`` is the caller's own already-known fix-loop state for
    this run — required (not optional) so no call site can silently fall back
    to a wrong default (#1705). It discriminates fix-loop-disabled from
    fix-loop-enabled-but-unneeded histories that would otherwise render
    identically from ``verdict.review`` alone.

    The headline is four-way as of #2000: blocking, mechanically-rejected-
    MUST_FIX, proceeding-but-something-below-MUST_FIX-was-deleted, or clean.
    Both rejected-findings *sections* render unconditionally regardless of
    which headline won, so the mixed case (something blocking AND something
    dropped) reports both.
    """
    lines = ["## Codex Review Verdict", ""]
    if verdict.blocking:
        lines.append(
            f"**BLOCKING** — {len(verdict.must_fix)} MUST_FIX finding(s) must be "
            "addressed before this branch can proceed."
        )
        lines.extend(
            _render_history_note(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    elif verdict.rejected_must_fix:
        # #1714: never render the clean headline here. Nothing survived to
        # block on, but a MUST_FIX was dropped unread -- "Non-blocking, no
        # MUST_FIX findings" would be the exact false all-clear this branch
        # exists to prevent.
        lines.append(
            f"**MUST_FIX REJECTED — OPERATOR REVIEW REQUIRED** — "
            f"{len(verdict.rejected_must_fix)} MUST_FIX finding(s) were "
            "mechanically rejected before adjudication (dropped, not evaluated "
            "on their merits) and require operator review before this branch "
            "can proceed."
        )
    elif verdict.rejected_count:
        # #2000: nothing MUST_FIX-shaped was dropped (the branch above already
        # returned if so), so this pass does proceed -- but it proceeds having
        # deleted findings nobody read, and the clean headline would say the
        # opposite. Qualified, not blocking: the round-1 operator resolution
        # keeps this informational rather than folding a matcher miss into
        # Health.recommendation's "coverage degraded" gate.
        lines.append(
            f"**PROCEED ({verdict.rejected_count} finding(s) mechanically "
            "rejected)** — no MUST_FIX findings survived validation, but "
            f"{verdict.rejected_count} finding(s) below MUST_FIX were "
            "mechanically rejected before adjudication and never evaluated "
            "on their merits — see the rejected-findings section below "
            "before treating this pass as clean."
        )
    else:
        lines.append(
            _render_clean_headline(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    lines.append("")
    lines.extend(_render_failed_roles_note(verdict))
    lines.extend(_render_degraded_roles_note(verdict))
    lines.extend(_render_capability_note(verdict))
    lines.extend(_render_agent_spec_note(verdict))
    lines.extend(_render_delta_note(verdict))
    lines.extend(_render_rejected_must_fix(verdict))
    lines.extend(_render_rejected_below_must_fix(verdict))
    lines.extend(_render_run_failure_discarded_note(verdict))
    lines.extend(_render_debt_note(verdict))
    lines.extend(_render_findings(verdict, "MUST_FIX", "MUST_FIX"))
    lines.extend(_render_findings(verdict, "SHOULD_FIX", "SHOULD_FIX"))
    return "\n".join(lines).rstrip() + "\n"
