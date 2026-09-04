"""Verdict consolidation and persistence (#1108 artifact).

The top of the :mod:`cw.review_findings` dependency chain: runs every
reviewer's document through validation, dedups the survivors, derives the
count block, and assembles the single :class:`ReviewVerdict` that downstream
consumers (the approval gate, the codex fix loop, the PR comment renderer)
read — plus the atomic writer for the on-disk artifact.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.review_findings._dedup import dedupe_findings, derive_review_counts
from cw.review_findings._document import validate_reviewer_document
from cw.review_findings._models import ReviewerRunRecord, ReviewVerdict

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings._models import (
        CapturedDiff,
        Finding,
        RejectedFinding,
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewerRunMetrics,
        StrippedEscalation,
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


def _count_rejected_by_severity(rejected: list[RejectedFinding]) -> dict[str, int]:
    """Tally *rejected* by the severity each finding claimed for itself (#2000).

    A sibling of :func:`_select_rejected_must_fix`, deliberately NOT a
    generalization of it: that function selects the MUST_FIX subset backing
    #1714's force-block and must keep doing exactly that, unchanged. This one
    answers a different question — how many findings, at which severities, did
    validation delete before adjudication ever saw them.

    ``raw`` is the pre-validation ``Finding.model_dump()``, read defensively
    via ``.get()`` for the same reason: a rejected payload is by definition one
    that failed validation, so its ``severity`` may be missing or not even a
    string. Anything unusable is tallied under ``"unknown"`` rather than
    dropped — an uncountable rejection is still a rejection, and silently
    losing it here would reintroduce the exact invisibility this counter
    exists to close.
    """
    counts: dict[str, int] = {}
    for rf in rejected:
        severity = str(rf.raw.get("severity", "unknown"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


# #2029: the severities whose loss inside a structurally-unusable document
# gates the pass. One step stricter than `_select_rejected_must_fix`'s
# MUST_FIX-only rule on purpose -- see ReviewVerdict's class docstring for the
# asymmetry's reasoning (there the finding's text survives to be read; here
# only a count does).
_GATING_DISCARD_SEVERITIES = frozenset({"MUST_FIX", "SHOULD_FIX"})


def _select_run_failures_with_discards(
    failures: list[ReviewerRunFailure],
) -> list[ReviewerRunFailure]:
    """Select the *failures* that discarded a MUST_FIX or SHOULD_FIX (#2029).

    A tally entry counting zero does not qualify: a payload that carried the
    key but no findings under it discarded nothing, and treating that as a
    gate would park a pass over a shape rather than over a loss.
    """
    return [
        f
        for f in failures
        if any(
            severity in _GATING_DISCARD_SEVERITIES and count > 0
            for severity, count in f.discarded_finding_severities.items()
        )
    ]


def _in_plan_scope(
    finding: Finding, planned_files: list[str] | None, changed: frozenset[str]
) -> bool | None:
    """Tag *finding* against the plan's declared file set (#2101).

    ``None`` when no plan was supplied at all (``planned_files is None``) or
    when *finding* has no diff anchor (``no_diff_anchor`` — which the model
    guarantees pairs with ``file == "N/A"``): plan-file-set membership is a
    category error for a finding that names no real path. Otherwise ``True``
    iff *finding*'s file is in *planned_files* OR in *changed* — the diff's own
    changed-file set counts as in scope even when the plan's manifest omits it,
    so an incomplete ``## Files Modified`` list can never manufacture a false
    exclusion for a file the diff genuinely touches.
    """
    if planned_files is None or finding.no_diff_anchor:
        return None
    return finding.file in planned_files or finding.file in changed


def consolidate_verdict(
    documents: list[ReviewerFindingsDocument],
    diff: CapturedDiff,
    reviewed_sha: str,
    *,
    worktree: Path | None = None,
    failed_reviewers: list[ReviewerRunFailure] | None = None,
    fix_cycles_used: int = 0,
    metrics_by_role: dict[str, ReviewerRunMetrics] | None = None,
    pre_validation_rejected: list[RejectedFinding] | None = None,
    planned_files: list[str] | None = None,
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

    Since #2099 a finding classified ``evidence_not_in_diff`` reaches
    ``accepted`` (flagged, via ``validate_reviewer_document``'s adjudication
    routing) instead of ``rejected``. It therefore participates in
    ``must_fix``/``blocking`` like any other accepted finding and is absent
    from ``rejected_count``/``rejected_count_by_severity``/
    ``rejected_must_fix``. Nothing in this function changed to make that
    happen — the derivations below already read exactly the lists the routing
    decision moved it between, which is what made that fix a routing change
    rather than a counting one.

    ``rejected_must_fix`` (#1714) is the MUST_FIX-severity subset of
    ``rejected`` — see :func:`_select_rejected_must_fix`. It is computed
    independently of ``blocking``/``must_fix``, which continue to read accepted
    findings only: a mechanically-rejected finding must block the pipeline for
    an operator without ever entering the autofix loop.

    ``pre_validation_rejected`` (#2029, defaulting to ``None`` → ``[]`` for the
    same reason ``failed_reviewers`` does) carries the findings
    :func:`~cw.review_findings.parse_reviewer_document` dropped BEFORE their
    document was constructed — items that could not become a :class:`Finding`
    at all, so ``validate_reviewer_document`` never saw them. They are seeded
    into ``all_rejected`` ahead of the per-document loop, which is the whole
    integration: ``rejected_count``, ``rejected_count_by_severity`` and
    ``rejected_must_fix`` are all derived from that one list, so a
    schema-invalid MUST_FIX force-blocks through #1714's existing gate with no
    new gating code. Seeded first rather than appended so a rendered verdict
    reads in the order the failures happened — parse time, then anchor time.

    ``run_failures_with_should_fix_discards`` (#2029) is the residual signal for
    what ``pre_validation_rejected`` structurally cannot cover: a document that
    failed to construct at all leaves no per-finding record, so this selects
    the ``failed_reviewers`` entries whose own discard tally claimed a MUST_FIX
    or SHOULD_FIX — see :func:`_select_run_failures_with_discards` and
    :class:`ReviewVerdict`'s docstring for why that threshold is stricter than
    the per-finding one.

    ``rejected_count``/``rejected_count_by_severity`` (#2000) tally the SAME
    ``rejected`` list at every severity — see
    :func:`_count_rejected_by_severity`. They are stamped on the verdict AND
    threaded into the nested ``review`` block, so the count survives into the
    terminal ``AUTO_DEV_RESULT`` sentinel rather than living only in the
    on-disk artifact a human would have to go read.

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

    ``planned_files`` (#2101, default ``None``) is the plan's ``## Files
    Modified`` manifest — ``cw review consolidate``'s ``--plan`` option parses
    it via :func:`cw.plan_files.parse_plan_files_modified` and passes it
    through unchanged. It stamps ``AcceptedFinding.in_plan_scope`` on every
    accepted finding (see :func:`_in_plan_scope`) — see that attribute's
    docstring for the tri-state semantics. Defaulting to ``None``
    keeps every pre-#2101 caller producing a byte-identical verdict (every
    ``in_plan_scope`` stays ``None``). **This is adjudication input only — it
    never rejects, drops, or otherwise filters a finding here.** A mechanical
    file-set membership check silently dropping a finding is exactly the
    #1632 regression the ``"unanchored"`` routing exists to prevent; the
    coordinating session (``.claude/commands/auto-dev-review.md`` Checkpoint
    3a (4d)) decides what an out-of-scope finding's disposition should be.
    """
    failures = failed_reviewers if failed_reviewers is not None else []
    metrics = metrics_by_role if metrics_by_role is not None else {}
    changed = frozenset(diff.files)
    candidates: list[tuple[str, Finding]] = []
    all_rejected: list[RejectedFinding] = list(pre_validation_rejected or [])
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

    accepted_findings = [
        af.model_copy(
            update={"in_plan_scope": _in_plan_scope(af.finding, planned_files, changed)}
        )
        for af in dedupe_findings(candidates)
    ]
    # #2000: computed ONCE, from the same `all_rejected` list `rejected_must_fix`
    # is selected from, and threaded into both the nested `Review` (which
    # becomes `AutoDevResult.review`, what an unattended orchestrator reads) and
    # the `ReviewVerdict` constructor below. One computation, two consumers --
    # so the two numbers are identical by construction, not by convention.
    rejected_count = len(all_rejected)
    rejected_by_severity = _count_rejected_by_severity(all_rejected)
    review = derive_review_counts(
        accepted_findings,
        fix_cycles_used=fix_cycles_used,
        agents_run=len(documents),
        rejected_count=rejected_count,
        rejected_count_by_severity=rejected_by_severity,
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
        run_failures_with_should_fix_discards=_select_run_failures_with_discards(
            failures
        ),
        rejected_count=rejected_count,
        rejected_count_by_severity=rejected_by_severity,
    )


def write_review_verdict(verdict: ReviewVerdict, path: Path) -> None:
    """Atomically write *verdict* to *path* as JSON (#1108 artifact).

    Full-replace semantics via :func:`cw.atomic.atomic_write_text`. Not wired to
    any executor/CLI call site in this ticket.
    """
    atomic_write_text(path, verdict.model_dump_json(indent=2))
