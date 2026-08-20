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
from cw.review_findings._models import ReviewerRunRecord, ReviewVerdict
from cw.review_findings._validation import validate_reviewer_document

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
