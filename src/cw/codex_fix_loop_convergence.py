"""Cross-cycle convergence machinery for the codex fix loop (#1837).

Before this module the fix loop re-reviewed the ENTIRE PR diff on every cycle,
so each pass could surface fresh MUST_FIX findings on code no fix cycle had
touched. Those findings restarted the loop, the next pass flapped to a
different set, and the run burned its cycle cap re-litigating debt instead of
converging on the work the cycle actually did.

The fix is an admission gate. From cycle 1 onward the loop reviews only the
delta since the previous reviewed head, and a *genuinely new* MUST_FIX is
admitted into the open-findings tracker only when it is anchored in that delta,
carries verbatim delta evidence of a causal link to it, or invokes the
release-critical exception with evidence substantiated against the working
tree. Everything else is diverted into the verdict's debt ledger — recorded,
never silently dropped — and emits a ``REVIEW_TREADMILL_DETECTED`` event.

Lives beside :mod:`cw.codex_fix_loop` rather than inside it: that module is
already at the repo's module-size ceiling, and the two functions whose
correctness is coupled to the new fingerprint-based survivor key
(:func:`_track_open_findings`, :func:`_survivors_only_verdict`) belong with the
gate that defines it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.review_debt import fingerprint_v1, promote_debt_finding, record_debt
from cw.review_findings import _dedup_key

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import Review
    from cw.review_findings import (
        AcceptedFinding,
        CapturedDiff,
        DebtRecord,
        Finding,
        ReviewVerdict,
    )

_log = logging.getLogger(__name__)

# Redefined locally rather than imported from `cw.codex_fix_loop`, which
# imports this module — the same local-constant convention
# `review_adjudication` already uses for this identical string.
_MUST_FIX = "MUST_FIX"
_DEBT = "DEBT"

# Matches review_findings._dedup_key's return shape (severity, file,
# line_start, line_end, evidence); None line endpoints map to -1 there.
_DedupKey = tuple[str, str, int, int, str]
# The open-findings tracker's key: a `fingerprint_v1` pair when the finding has
# a real file, falling back to the positional dedup key for the `file="N/A"`
# carve-out that cannot be fingerprinted at all.
_OpenFindingKey = tuple[str, str] | _DedupKey


def _open_finding_key(finding: Finding) -> _OpenFindingKey:
    """Return the cross-cycle identity key for *finding*."""
    return fingerprint_v1(finding.file, finding.summary) or _dedup_key(finding)


def _finding_in_delta(finding: Finding, delta_changed_files: frozenset[str]) -> bool:
    """Return True iff *finding*'s file is part of the latest delta.

    File membership, not line membership: a pure deletion contributes no added
    lines to the delta but is unquestionably something the fix cycle did, and a
    file-level finding on it must still be admitted.
    """
    return finding.file in delta_changed_files


def _evidence_in_worktree(finding: Finding, worktree: Path) -> bool:
    """Return True iff *finding*'s evidence quote is in its file's current text.

    The release-critical exception is a claim about code that exists right now
    but was never in any diff, so the diff cannot substantiate it — the working
    tree is the only place the quote could be. A missing or unreadable file
    fails closed.
    """
    target = worktree / finding.file
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return finding.evidence in content


def _admit_new_must_fix(
    finding: Finding,
    delta_diff: CapturedDiff,
    delta_changed_files: frozenset[str],
    *,
    worktree: Path,
) -> tuple[bool, str]:
    """Decide whether a genuinely-new MUST_FIX may enter the fix loop.

    Returns ``(admit, reason)``. The four admitting/rejecting reasons are
    distinct strings, not a bool plus prose: ``"unsubstantiated_evidence"`` in
    particular is deliberately NOT folded into ``"treadmill"`` — a reviewer
    that invoked the release-critical exception and failed to substantiate it
    is conceptually a different case from a reviewer that never claimed a link
    at all. The current caller (:func:`_track_open_findings`) does not itself
    branch on *reason* — both rejection reasons route to the same debt-ledger
    diversion and ``REVIEW_TREADMILL_DETECTED`` event, whose payload was
    deliberately scoped to five fields by operator pre-flight resolution and
    does not carry *reason*. *reason* exists for tests and any future caller
    that does need the distinction.
    """
    if _finding_in_delta(finding, delta_changed_files):
        return True, "in_delta"
    if (
        finding.transitive_impact_evidence
        and finding.transitive_impact_evidence in delta_diff.text
    ):
        return True, "causal_impact"
    if finding.release_critical_exception:
        if _evidence_in_worktree(finding, worktree):
            return True, "release_critical_exception"
        return False, "unsubstantiated_evidence"
    return False, "treadmill"


def _emit_treadmill_diagnostic(
    *,
    finding: Finding,
    fingerprint: tuple[str, str] | None,
    previous_reviewed_sha: str,
    ticket_id: str,
) -> None:
    """Log and record the refusal of an out-of-delta MUST_FIX."""
    _log.info(
        "auto-dev: refused an out-of-delta MUST_FIX as review treadmill "
        "(ticket=%s, file=%s, summary=%s, since=%s)",
        ticket_id,
        finding.file,
        finding.summary,
        previous_reviewed_sha,
    )
    record_event(
        OrchestratorEventType.REVIEW_TREADMILL_DETECTED,
        payload={
            "file": finding.file,
            "severity": finding.severity,
            "summary": finding.summary,
            "fingerprint": fingerprint,
            "previous_reviewed_sha": previous_reviewed_sha,
        },
        correlation_id=ticket_id,
    )


def _ledger_debt(
    af: AcceptedFinding,
    *,
    debt_ledger: dict[tuple[str, str], DebtRecord],
    discovery_sha: str,
) -> None:
    """Promote *af* into *debt_ledger*, if it can be fingerprinted at all."""
    record = promote_debt_finding(af, discovery_sha=discovery_sha)
    if record is not None:
        record_debt(debt_ledger, record)


def _track_open_findings(
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    accepted: list[AcceptedFinding],
    *,
    delta_diff: CapturedDiff | None,
    delta_changed_files: frozenset[str] | None,
    debt_ledger: dict[tuple[str, str], DebtRecord],
    previous_reviewed_sha: str | None,
    reviewed_sha: str,
    worktree: Path,
    ticket_id: str,
) -> dict[_OpenFindingKey, AcceptedFinding]:
    """Update the cross-cycle open-MUST_FIX tracker from a pass's accepted set.

    A key present in *accepted*'s open-MUST_FIX subset stays or gets refreshed;
    a key absent from it is dropped, because a finding nobody re-raised is
    implicitly resolved. Identity is ``fingerprint_v1``-first (#1837), so a
    finding re-raised after the code moved, or reworded around a changed count,
    is recognized as the SAME survivor rather than minting a new one every
    cycle.

    "Open" requires ``disposition == "fixed"`` as well as MUST_FIX severity
    (#1814): any other value means something upstream already decided the
    finding's fate — notably ``apply_voided_suppression``'s ``"rejected"``,
    which handing to the fix agent would re-open the operator's own decision.

    *delta_diff* is ``CapturedDiff | None`` so both call sites share this
    implementation. ``None`` is the pre-loop, cycle-0 seeding call: there is no
    prior head to restrict a "genuinely new" finding against, so every new
    MUST_FIX is admitted unconditionally and the gate never runs — cycle 0
    still reviews the whole PR and still blocks on everything it finds. A real
    *delta_diff* is every in-loop cycle, where :func:`_admit_new_must_fix`
    gates each newly-appearing finding and refusals are diverted into
    *debt_ledger* instead.

    Accepted DEBT-severity findings are ledgered on every call — they never
    enter the tracker, so this is the one place they would otherwise be lost.
    """
    survivors = dict(open_findings)
    current: dict[_OpenFindingKey, AcceptedFinding] = {
        _open_finding_key(af.finding): af
        for af in accepted
        if af.finding.severity == _MUST_FIX and af.disposition == "fixed"
    }
    for key in list(survivors):
        if key not in current:
            del survivors[key]

    for af in accepted:
        if af.finding.severity == _DEBT:
            _ledger_debt(af, debt_ledger=debt_ledger, discovery_sha=reviewed_sha)

    for key, af in current.items():
        if key in survivors or delta_diff is None:
            survivors[key] = af
            continue
        admit, _reason = _admit_new_must_fix(
            af.finding,
            delta_diff,
            delta_changed_files or frozenset(),
            worktree=worktree,
        )
        if admit:
            survivors[key] = af
            continue
        _ledger_debt(af, debt_ledger=debt_ledger, discovery_sha=reviewed_sha)
        _emit_treadmill_diagnostic(
            finding=af.finding,
            fingerprint=fingerprint_v1(af.finding.file, af.finding.summary),
            previous_reviewed_sha=previous_reviewed_sha or "",
            ticket_id=ticket_id,
        )
    return survivors


def _survivors_only_verdict(
    final_verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    review: Review,
) -> ReviewVerdict:
    """Rebuild a capped-exit verdict whose blocking state is survivor-derived.

    ``blocking`` is computed DIRECTLY from ``open_findings`` — NOT re-derived
    from ``bool(must_fix)`` over the disposition-stamped ``accepted`` list,
    which would spuriously read ``False`` once survivors are stamped
    ``disposition="deferred"`` for reporting. Each surviving MUST_FIX finding is
    stamped ``deferred`` in ``accepted``; every other accepted finding is
    unchanged. ``must_fix`` is exactly the survivor set.

    Survivor membership is tested by finding IDENTITY, not by recomputing a key
    and looking it up: ``open_findings``'s keys are fingerprints now, so a
    key-based check would silently stop matching and leave real survivors
    stamped ``"fixed"``. A list (not a set) because ``Finding`` is an
    unhashable pydantic model — ``BaseModel.__eq__`` gives the value equality
    this needs without ``__hash__``.
    """
    survivor_findings = [af.finding for af in open_findings.values()]
    accepted = [
        af.model_copy(update={"disposition": "deferred"})
        if af.finding.severity == _MUST_FIX and af.finding in survivor_findings
        else af
        for af in final_verdict.accepted
    ]
    return final_verdict.model_copy(
        update={
            "blocking": bool(open_findings),
            "must_fix": survivor_findings,
            "accepted": accepted,
            "review": review,
        }
    )
