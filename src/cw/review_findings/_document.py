"""Document-level validation and parse-time rescue (#1237).

The entry points a caller actually reaches for, composing
:mod:`cw.review_findings._anchor`'s geometry and
:mod:`cw.review_findings._classify`'s verdicts over a whole reviewer document:
:func:`validate_reviewer_document` partitions one reviewer's findings into
``(accepted, rejected, stripped)``, applying the escalation-quote check to the
survivors.

:func:`parse_reviewer_document` (#2029) sits one step EARLIER than all of that:
it is the tolerant JSON→model boundary both executor paths call instead of
``ReviewerFindingsDocument.model_validate``, so one unparseable ``findings[]``
item costs that item rather than every sibling in the document. Nothing in the
sibling submodules can rescue a finding the document never carried, which is
why the split had to live at parse time rather than inside
``validate_reviewer_document``.

The rescue inventory, in the order a finding meets them: ``_nearest_added_line``
(#1715, ±3 lines) → ``_anchor_in_enclosing_def`` (#1743, worktree opt-in) →
``_content_rescue_anchor`` (#2007, unbounded content search) at the anchor gate;
``_nearest_hunk_line``/``_reconcile_evidence_window`` (#1738/#1792) →
``_diff_pair_rescue`` (#1976) → ``_content_rescue_anchor`` again, via
``_classify_mislocated_finding`` (#2019, the same unbounded content search,
now also reachable from an anchor-valid-but-evidence-misplaced miss) at the
evidence-quote gate.

Top of the validation half's dependency chain: imports ``_classify`` (which
imports ``_anchor``), never the reverse.

Split out of ``_validation.py`` (#2054), itself split out of the single
``review_findings.py`` module (#1818); import these names from
:mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cw.review_findings._anchor import _changed_files, _substring_in_diff
from cw.review_findings._classify import (
    _classify_finding,
    _rejection_detail,
    _resolved_finding,
)
from cw.review_findings._models import (
    _ESCALATION_STRIP_REASON,
    Finding,
    RejectedFinding,
    ReviewerFindingsDocument,
    StrippedEscalation,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings._models import CapturedDiff

_log = logging.getLogger(__name__)


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
            # #2000: announce EVERY mechanical rejection, at every severity.
            # Before this line, a rejection below MUST_FIX left no trace on any
            # surface -- #1714 gave the MUST_FIX case a verdict field and a
            # force-block, but a SHOULD_FIX/DEBT/NIT/PRINCIPLE finding was
            # deleted here in silence and the run reported as if nothing had
            # been found. INFO (not WARNING) mirrors the "unanchored" /
            # "no_diff_anchor" routing logs below: same category of event,
            # same level.
            _log.info(
                "auto-dev: mechanically rejected finding — will not reach "
                "adjudication (reviewer_role=%s, finding_index=%d, "
                "severity=%s, reason=%s, title=%s)",
                doc.reviewer_role,
                index,
                finding.severity,
                reason,
                finding.summary,
            )
            rejected.append(
                RejectedFinding(
                    raw=finding.model_dump(),
                    reviewer_role=doc.reviewer_role,
                    reason=reason,
                    detail=_rejection_detail(finding, reason, worktree),
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
        if finding.no_diff_anchor:
            # Mirrors the "unanchored" INFO above: a finding that skipped the
            # mechanical checks is always announced, so an operator can tell a
            # genuinely-verified acceptance from a marker-driven one.
            _log.info(
                "auto-dev: routed no_diff_anchor finding to adjudication "
                "(remedy is outside the diff; no mechanical anchor check "
                "performed) (reviewer_role=%s, finding_index=%d, severity=%s)",
                doc.reviewer_role,
                index,
                finding.severity,
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


def _raw_finding_payload(item: object) -> dict[str, Any]:
    """Preserve one unusable ``findings[]`` item as a ``RejectedFinding.raw``.

    A dict is kept as-is (keys coerced to ``str`` for the field's type), so
    every downstream ``.get()`` reader — ``_select_rejected_must_fix``,
    ``_count_rejected_by_severity``, the comment renderers — sees the payload
    the reviewer actually sent. Anything else (a bare string, a number, a
    nested list) is wrapped under ``"value"`` rather than dropped: the item is
    unusable, but the fact that the reviewer emitted it is not.
    """
    if isinstance(item, dict):
        return {str(key): value for key, value in item.items()}
    return {"value": item}


def _rescue_findings(payload: object) -> tuple[object, list[RejectedFinding]]:
    """Reduce *payload*'s ``findings[]`` to its schema-valid survivors (#2029).

    Returns ``(reduced_payload, rejected)``. Pydantic's list validation is
    all-or-nothing, so ``ReviewerFindingsDocument.model_validate(payload)``
    threw away every sibling finding whenever ONE ``findings[]`` item failed
    its field or model validators — before :func:`validate_reviewer_document`,
    which owns per-finding mechanical rejection, could run at all. This
    function closes that gap by validating each item independently first: the
    survivors replace ``findings`` in the returned payload, and each casualty
    becomes an ordinary :class:`RejectedFinding` with reason
    ``"schema_invalid"``.

    Expressing the casualties as ordinary ``RejectedFinding`` records is what
    makes this fix small: #1714's ``_select_rejected_must_fix`` force-block and
    #2000's severity counters are already generic ``.get()``-based readers of
    ``RejectedFinding.raw``, so a schema-invalid MUST_FIX gates the pipeline
    with no new gating code.

    The rescue is per-ITEM only. A ``findings`` key that is not a list at all
    (and a *payload* that is not a dict) is a structural defect with no usable
    items to salvage, so *payload* is returned unchanged for the caller's own
    strict ``model_validate`` to reject. This function never raises: a
    residual structural failure (the surviving findings still cannot satisfy
    the document's own invariants) is left entirely to the caller's
    construction step.

    ``payload`` is typed ``object`` rather than ``dict[str, Any]`` because both
    call sites (:func:`parse_reviewer_document` and, via #2042,
    ``cw.cli.review.consolidate``) hand it straight from ``json.loads`` or an
    already-decoded JSON value — a bare JSON array must fall through
    unchanged, not raise ``AttributeError`` on a ``.get()`` against a list.
    """
    if not isinstance(payload, dict):
        return payload, []
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        return payload, []

    reviewer_role = str(payload.get("reviewer_role", ""))
    survivors: list[object] = []
    rejected: list[RejectedFinding] = []
    for index, item in enumerate(raw_findings):
        try:
            Finding.model_validate(item)
        except ValidationError as exc:
            # INFO, matching validate_reviewer_document's per-rejection line
            # below: same category of event (a finding that will never reach
            # adjudication), same level.
            _log.info(
                "auto-dev: schema-invalid finding dropped at parse time — "
                "siblings retained (reviewer_role=%s, finding_index=%d, "
                "reason=schema_invalid)",
                reviewer_role,
                index,
            )
            rejected.append(
                RejectedFinding(
                    raw=_raw_finding_payload(item),
                    reviewer_role=reviewer_role,
                    reason="schema_invalid",
                    detail=str(exc),
                )
            )
            continue
        survivors.append(item)

    return {**payload, "findings": survivors}, rejected


def parse_reviewer_document(
    payload: object,
) -> tuple[ReviewerFindingsDocument, list[RejectedFinding]]:
    """Parse *payload* into a document, rescuing its usable findings (#2029).

    Returns ``(document, rejected)``. Thin wrapper around
    :func:`_rescue_findings`: the reduced payload it returns is handed to
    ``ReviewerFindingsDocument.model_validate``, whose :class:`ValidationError`
    propagates unchanged for a residual structural failure — a ``findings``
    key that was never a list, a *payload* that was never a dict, or surviving
    findings that still cannot satisfy the document's own invariants. Callers
    keep whatever handling they already had for that error; it now means
    "this document is structurally unusable", not "one of its findings was".
    """
    reduced, rejected = _rescue_findings(payload)
    document = ReviewerFindingsDocument.model_validate(reduced)
    return document, rejected


def _best_effort_discarded_tally(raw: object) -> tuple[int, dict[str, int]]:
    """Count what an unusable reviewer payload was claiming to report (#2029).

    For the residual case :func:`parse_reviewer_document` cannot rescue — a
    document that failed structurally, so there are no ``RejectedFinding``
    records to count. Accepts the raw ``-o`` file text or an already-decoded
    payload, and returns ``(0, {})`` for anything it cannot read rather than
    raising: this is diagnostics for a payload already known to be broken, so
    every step of the walk has to survive the next surprise.

    Mirrors :func:`~cw.review_findings._consolidate._count_rejected_by_severity`'s
    ``.get()``-defensive, ``"unknown"``-bucket idiom, applied one layer earlier
    (a whole discarded payload rather than a list of typed rejects).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return 0, {}
    if not isinstance(raw, dict):
        return 0, {}
    findings = raw.get("findings")
    if not isinstance(findings, list):
        return 0, {}
    counts: dict[str, int] = {}
    for item in findings:
        severity = (
            str(item.get("severity", "unknown"))
            if isinstance(item, dict)
            else "unknown"
        )
        counts[severity] = counts.get(severity, 0) + 1
    return len(findings), counts
