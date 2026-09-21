"""Rendering of a consolidated verdict into a GitHub-issue-comment body.

:func:`render_verdict_comment` picks one of four headlines and then appends
every per-concern note section unconditionally, each written to the same
empty-list-returns-``[]`` shape so a pass with nothing to say about a concern
produces no bytes for it. Nothing here reads or influences the disposition
table — the sections report what the verdict already decided.

The one section that is not purely a report is :func:`_render_settle_payloads`
(#2210): it hands the operator a ready-to-paste ``cw review settle`` payload
per blocking finding, which is the producer half of the cross-round
adjudication ledger's first real write path.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, get_args

from cw.review_debt import fingerprint_v1
from cw.review_finding_dispositions import SETTLE_SECTION_HEADING
from cw.review_findings import Severity

if TYPE_CHECKING:
    from cw.auto_dev_result import Review
    from cw.review_findings import (
        AcceptedFinding,
        AgentSpecStatus,
        Finding,
        RejectedFinding,
        ReviewerRunRecord,
        ReviewVerdict,
    )

# #2000: severity ordering for the rejected-findings section, derived from the
# `Severity` Literal itself rather than a hand-maintained rank table -- the
# same USE_EXISTING pattern `_classify._VALID_SEVERITIES` already applies to
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

# #2081: a finding whose line anchor validation dropped because the cited line
# resolved against nothing in the diff (but exists in the file on disk). It
# renders at file level, so without this note it would be indistinguishable
# from a finding the reviewer *filed* at file level — and the adjudicator
# needs to know the location is unverified and the text is what to weigh.
# Display-only, exactly like the two annotations above.
_ANCHOR_DEGRADED_ANNOTATION = (
    " _(line anchor degraded — the cited line did not resolve against the "
    "diff; adjudicate on the finding's text)_"
)

# #2099: the sibling routing, where the line anchor DID resolve and the
# evidence quote did not match its window (a formatter hook rewriting the file
# after the reviewer quoted it is the observed cause). A reader must be able to
# tell the two apart from the comment alone — the location here is a real,
# validated one, and it is the quote that needs re-locating, which is the
# opposite of the annotation above. Display-only, same as every annotation here.
_EVIDENCE_DEGRADED_ANNOTATION = (
    " _(evidence unmatched — the cited line resolved but the quote was not "
    "found in its diff window; re-anchor from the finding's text before "
    "bucketing)_"
)
# #2101: a finding whose file falls outside the approved plan's declared
# scope (`AcceptedFinding.in_plan_scope is False`) — a plan supplied, and the
# file is in neither its `## Files Modified` manifest nor the diff's own
# changed-file set. Display-only, exactly like the annotations above: nothing
# here rejects, drops, or reorders the finding, it only flags it for the
# coordinating session's Checkpoint 3a (4d) plan-scope precedence rule.
_OUT_OF_PLAN_SCOPE_ANNOTATION = " _(outside planned file set)_"

# #2210: the reviewer set `contests_adjudication`, i.e. it is knowingly
# re-raising a finding an operator settled and says what changed. Rendered
# whenever the field is non-blank, matched or not -- the label reports what the
# reviewer CLAIMS, and gating it on an actual ledger match would need the match
# result threaded into this renderer. Display-only, exactly like the
# annotations above: nothing here admits, filters, or reorders the finding.
_CONTEST_ANNOTATION = " _(contests prior adjudication — {claim})_"
# The claim is free text from a model; a comment line is not the place for an
# essay, and GitHub caps a comment body at 65,536 characters.
_CONTEST_CLAIM_MAX = 200

# The fence the settle payloads render in, widened past any backtick run in
# the body so a summary containing ``` cannot break out of its own block.
_MIN_FENCE = 3
_BACKTICK_RUN_RE = re.compile(r"`+")

_SETTLE_INTRO = (
    "Each payload below records one blocking finding as settled. Save one to "
    "a file, run `cw review settle <file> --reason '<why>' --out settle.md` "
    "**on your own machine**, and post `settle.md` as a ticket comment. "
    "`file`, `summary` and `reviewed_sha` are the finding's identity and "
    "provenance, copied verbatim, so nothing needs editing; put your "
    "reasoning in `--reason` (or a per-entry `rationale`), or set `outcome` "
    "to `ACCEPTED` if you uphold the finding. The command refuses to run "
    "inside a dispatch worker — a settled finding is never re-raised, so the "
    "pipeline must not be able to settle its own reviewer's findings. This "
    "works only on GitHub-tracked tickets: a marker posted on any other "
    "tracker is not read by the codex lane."
)

# Hard caps on the settle section. A blocking pass with many MUST_FIX findings
# would otherwise carry one JSON payload each, and GitHub rejects a comment
# body over 65,536 characters -- the rest of this comment (findings, debt,
# rejected sections) needs the remainder. Past either cap the remaining
# findings are named compactly and the operator is pointed at `cw review
# settle`; a JSON block is NEVER truncated mid-structure, because half a
# payload pastes into something that half-parses.
_SETTLE_MAX_PAYLOADS = 10
_SETTLE_SECTION_BUDGET_CHARS = 12_000
# The compact overflow list is bounded independently of the byte budget above
# (it is what the budget overflows INTO, so it cannot be charged to it): at
# most this many rows, each summary trimmed, then a counted residue line.
_SETTLE_MAX_COMPACT_ROWS = 40
_SETTLE_COMPACT_SUMMARY_MAX = 120
_SETTLE_OVERFLOW_NOTE = (
    "Not enough room for the remaining findings' payloads. Settle any of "
    "these by hand with `cw review settle` — the identity is the file and the "
    "verbatim summary from its MUST_FIX line above:"
)


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


def _degraded_annotation(finding: Finding) -> str:
    """Annotate a finding validation flagged as adjudication-routed (#2099).

    ``""`` unless ``anchor_degraded`` is set, exactly like the two annotation
    helpers above return ``""`` for the uncluttered common case. The flag has
    carried a reason since #2099, and the two reasons say opposite things about
    the finding's location, so this reads it rather than rendering one message
    for both. A flag with no reason (a verdict artifact persisted before #2099,
    reloaded) keeps the original #2081 wording — the older routing was the only
    one that existed then.
    """
    if not finding.anchor_degraded:
        return ""
    if finding.anchor_degraded_reason == "evidence_not_in_diff":
        return _EVIDENCE_DEGRADED_ANNOTATION
    return _ANCHOR_DEGRADED_ANNOTATION


def _truncate(text: str, limit: int) -> str:
    """Whitespace-collapse *text* and cap it at *limit* characters (#2210).

    Shared by the two places a comment line embeds model-authored free text —
    a contest claim and an over-budget settle row — so one verbose model
    cannot dominate the comment from either direction.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


def _contest_annotation(finding: Finding) -> str:
    """Annotate a finding that knowingly contests a settled decision (#2210).

    ``""`` for the blank default, same uncluttered-common-path convention as
    the annotations above. The claim is whitespace-collapsed and truncated so
    one verbose model cannot dominate the comment.
    """
    claim = _truncate(finding.contests_adjudication, _CONTEST_CLAIM_MAX)
    if not claim:
        return ""
    return _CONTEST_ANNOTATION.format(claim=claim)


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
        contest = _contest_annotation(finding)
        degraded = _degraded_annotation(finding)
        out_of_scope = (
            _OUT_OF_PLAN_SCOPE_ANNOTATION if af.in_plan_scope is False else ""
        )
        lines.append(
            f"- **{loc}**{annotation}{suppression}{contest}{degraded}"
            f"{out_of_scope} — {finding.summary}"
        )
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
            lines.append(f"  - {label}: {value.strip()}")
    evidence = rf.raw.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        lines.append("  - evidence:")
        lines.append("    ```")
        lines.extend(f"    {line}" for line in evidence.strip().splitlines())
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
        loc = str(rf.raw.get("file", "<unknown file>"))
        line_start = rf.raw.get("line_start")
        if line_start is not None:
            loc = f"{loc}:{line_start}"
        summary = str(rf.raw.get("summary", "<no summary>"))
        lines.append(f"- **{loc}** — {summary} (rejected: {rf.reason})")
        if rf.detail:
            lines.append(f"  - {rf.detail}")
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

    This prints ``record.fingerprint[1]`` — the SUMMARY half of the ledger
    identity, not the ``file::`` prefix — and only for DEBT records. That gap
    is what #2210's :func:`_render_settle_payloads` closes for the blocking
    findings an operator actually has to settle.
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


def _settle_fence(body: str) -> str:
    """A code fence guaranteed to be longer than any backtick run in *body*."""
    runs = _BACKTICK_RUN_RE.findall(body)
    longest = max((len(run) for run in runs), default=0)
    return "`" * max(_MIN_FENCE, longest + 1)


def _settleable_findings(verdict: ReviewVerdict) -> list[Finding]:
    """The blocking findings that can be keyed, one per fingerprint (#2210)."""
    seen: set[tuple[str, str]] = set()
    keyable: list[Finding] = []
    for finding in verdict.must_fix:
        fingerprint = fingerprint_v1(finding.file, finding.summary)
        # #1817's no-diff-anchor case: there is no path to key on, so this
        # finding gets no cross-round memory and no payload.
        if fingerprint is None or fingerprint in seen:
            continue
        seen.add(fingerprint)
        keyable.append(finding)
    return keyable


def _settle_payload_block(index: int, finding: Finding, reviewed_sha: str) -> list[str]:
    """One labelled, fenced payload for *finding* (#2210)."""
    body = json.dumps(
        {
            "entries": [
                {
                    "file": finding.file,
                    "summary": finding.summary,
                    "outcome": "REJECTED",
                    "rationale": "",
                    "reviewed_sha": reviewed_sha,
                }
            ]
        },
        indent=2,
        ensure_ascii=False,
    )
    fence = _settle_fence(body)
    return [f"**{index}. {finding.file}**", "", f"{fence}json", body, fence, ""]


def _settle_overflow_lines(overflow: list[Finding]) -> list[str]:
    """Name the findings that did not fit, compactly and with no JSON (#2210).

    The whole point of the caps above is that a payload is never cut in half,
    so what lands here must not look like one: identity in prose, nothing
    fenced, nothing that could half-parse if an operator copied it.
    """
    if not overflow:
        return []
    rows = [
        f"- **{finding.file}** — "
        f"{_truncate(finding.summary, _SETTLE_COMPACT_SUMMARY_MAX)}"
        for finding in overflow[:_SETTLE_MAX_COMPACT_ROWS]
    ]
    residue = len(overflow) - len(rows)
    if residue:
        rows.append(f"- …and {residue} more MUST_FIX finding(s) listed above.")
    return [_SETTLE_OVERFLOW_NOTE, "", *rows, ""]


def _render_settle_payloads(verdict: ReviewVerdict) -> list[str]:
    """Hand the operator a ready-to-paste settle payload per finding (#2210).

    The cross-round ledger (#1838) had a renderer, a parser and a backstop but
    no production writer, and its runbook told operators to compute the key by
    hand from a `python -c` one-liner. This section is the other end of
    ``cw review settle``: each payload carries the finding's VERBATIM ``file``
    and ``summary`` (the ledger's whole identity) plus the verdict's
    ``reviewed_sha`` (the provenance the record needs to say what code it
    silenced), so pasting it needs no editing and reproduces exactly the key a
    later re-raise will hit.

    Empty-returns-``[]`` like its siblings, and renders nothing at all unless
    the pass actually blocks — there is nothing to settle otherwise.

    **Bounded.** At most ``_SETTLE_MAX_PAYLOADS`` payloads, and at most
    ``_SETTLE_SECTION_BUDGET_CHARS`` characters of them; the remainder is
    listed compactly by :func:`_settle_overflow_lines`. Accounting is over the
    joined text actually emitted, and a block is tested WHOLE before it is
    kept, so the section can never end on a half-written JSON object.

    Deliberately NOT the postable ``REVIEW-FINDING-DISPOSITIONS`` marker
    itself: the dispositions reader ingests every comment body on the ticket,
    including this one, so printing the marker here would auto-settle every
    finding as REJECTED on the next pass. Per finding rather than one combined
    block, so an operator cannot settle the genuinely actionable finding by
    pasting everything at once.
    """
    if not verdict.blocking:
        return []
    keyable = _settleable_findings(verdict)
    if not keyable:
        return []
    head = [SETTLE_SECTION_HEADING, "", _SETTLE_INTRO, ""]
    blocks: list[list[str]] = []
    overflow: list[Finding] = []
    for index, finding in enumerate(keyable, start=1):
        candidate = [
            *blocks,
            _settle_payload_block(index, finding, verdict.reviewed_sha),
        ]
        joined = "\n".join([*head, *(line for block in candidate for line in block)])
        if (
            len(candidate) > _SETTLE_MAX_PAYLOADS
            or len(joined) > _SETTLE_SECTION_BUDGET_CHARS
        ):
            overflow = keyable[index - 1 :]
            break
        blocks = candidate
    body = [line for block in blocks for line in block]
    return [*head, *body, *_settle_overflow_lines(overflow)]


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
    # #2210: last, so the operator reads the findings before the machinery for
    # settling them. Every producer of this text (Blocker.details, the fix
    # loop's park, and the posted comment) goes through this one function, so
    # one insertion covers every lane.
    lines.extend(_render_settle_payloads(verdict))
    return "\n".join(lines).rstrip() + "\n"
