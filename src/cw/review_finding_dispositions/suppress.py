"""The mechanical suppression backstop (#1838, #2210, #2232).

:func:`suppress_adjudicated_findings` stamps every accepted finding a prior
round already REJECTED, makes each suppression VISIBLE
(:func:`_render_suppression_signal`) rather than silent, and records it. It is
where every other submodule meets: the provenance gate partitions the ledger,
the matcher resolves findings exact tier first, the closed claim-tier gate
shadows instead of applying, and the drift check declines a record whose code
moved. Split out of the flat ``review_finding_dispositions.py`` (#2498).

#2232 closed the two gaps ADR-0016's Consequences section named as
preconditions for ever arming the claim tier. Rollback is a third ``Outcome``
value, ``"REVERSED"``, reusing ``cw review settle`` as its producer so the
ledger keeps its one write chokepoint. Staleness is
:func:`~cw.review_finding_dispositions.drift.disposition_drifted` plus
``ReviewVerdict.stale_dispositions``: a record whose code moved since it was
settled stops being APPLIED and is reported, rather than being expired — the
same "make it visible instead of adding an expiry" choice the ledger's
identity design already made.

Imports nothing from ``cw`` at module scope beyond :mod:`cw.review_markers`
and this package's own submodules (see the package docstring's "Import
discipline" section): :func:`cw.review_debt.fingerprint_v1` is imported inside
:func:`_render_suppression_signal`, and ``cw.events.record_event`` is reached
only through the ``emit`` submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_finding_dispositions._constants import (
    _FIXED,
    _MATCH_CLAIM,
    _MATCH_EXACT,
    _MUST_FIX,
)
from cw.review_finding_dispositions.drift import disposition_drifted
from cw.review_finding_dispositions.emit import (
    _emit_shadow,
    _emit_stale,
    _emit_suppression,
)
from cw.review_finding_dispositions.match import _ledger_matches
from cw.review_finding_dispositions.model import split_disposition_key
from cw.review_finding_dispositions.provenance import (
    log_refused_dispositions,
    partition_enforceable_dispositions,
)
from cw.review_markers import StaleDisposition

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_finding_dispositions.match import _LedgerMatch
    from cw.review_finding_dispositions.model import FindingDisposition
    from cw.review_findings import AcceptedFinding, ReviewVerdict
    from cw.review_markers import RefusedDisposition

#: The operator-mandated visibility signal stamped into
#: ``AcceptedFinding.disposition_detail`` on every REJECTED suppression.
#: Deterministic so two passes over one ledger entry produce the same text.
_SUPPRESSION_SIGNAL = (
    "finding {file}:{summary} suppressed by prior REJECTED adjudication "
    "(recorded {recorded_at}) -- original rationale: {rationale} -- "
    "re-adjudicate if the code at this location has changed."
)


def _render_suppression_signal(
    file: str, summary: str, entry: FindingDisposition
) -> str:
    """The operator-mandated visible line one REJECTED suppression produces.

    This ticket's fingerprint is deliberately not evidence-anchored, so a
    suppression does NOT lapse when the code moves (unlike a ``VoidedFinding``
    match). The operator's resolution accepts that resilience on the condition
    that every suppression says so out loud — this string is that signal, and
    it needs no new rendering plumbing: ``codex_review._verdict``'s
    ``_disposition_annotation``/``_render_findings`` already surface any
    non-``"fixed"`` ``disposition_detail`` on the posted review comment.

    There is no "round N" to name — neither this ledger nor ``VoidedFinding``
    tracks a round counter — so the ledger's ``recorded_at`` stands in for it.
    The operator's stored ``rationale`` rides along for the same reason
    ``_VOIDED_RATIONALE`` carries ``original_rationale``: a reader deciding
    whether to re-adjudicate needs the *why*, not just the *that*.
    """
    from cw.review_debt import fingerprint_v1

    fingerprint = fingerprint_v1(file, summary)
    return _SUPPRESSION_SIGNAL.format(
        file=file,
        summary=fingerprint[1] if fingerprint is not None else summary,
        recorded_at=entry.recorded_at or "date not recorded",
        rationale=entry.rationale or "not recorded",
    )


#: Appended to the exact tier's visibility signal when the CLAIM tier made the
#: match, so a reader can see that the suppression rests on a rewording rather
#: than on an identical summary — and what the operator actually settled.
_CLAIM_NOTE = (
    " Matched by claim similarity {similarity:.2f} to recorded finding: "
    "{recorded_summary}."
)


def _stamp_suppressed(af: AcceptedFinding, match: _LedgerMatch) -> AcceptedFinding:
    """Return *af* stamped rejected, with the match's visibility signal."""
    detail = _render_suppression_signal(
        af.finding.file, af.finding.summary, match.entry
    )
    if match.kind == _MATCH_CLAIM:
        _, recorded_summary = split_disposition_key(match.key)
        detail += _CLAIM_NOTE.format(
            similarity=match.similarity, recorded_summary=recorded_summary
        )
    return af.model_copy(
        update={"disposition": "rejected", "disposition_detail": detail}
    )


def suppress_adjudicated_findings(
    verdict: ReviewVerdict,
    ledger: dict[str, FindingDisposition],
    *,
    ticket_id: str,
    claim_tier_enabled: bool = False,
    reviewed_sha: str = "",
    refused: list[RefusedDisposition] | None = None,
    worktree: Path | None = None,
    disposition_drift_check_enabled: bool = True,
) -> ReviewVerdict:
    """Suppress every accepted finding a prior round already REJECTED (#1838).

    The mechanical backstop half of R4 — the prompt-injection half lives in
    ``codex_review._context``. Both exist because neither alone is sufficient:
    an instruction the model ignores needs a backstop, and a backstop that
    silently deletes findings needs the model to have been told why.

    A matched finding is stamped ``disposition="rejected"`` with
    :func:`_render_suppression_signal`'s text in ``disposition_detail`` and
    leaves ``must_fix``/``blocking``; everything else passes through
    byte-identically.

    **Emits one ``review.finding_disposition_suppressed`` event per
    suppression, inline.** Deliberate coupling, mirroring
    :func:`cw.review_adjudication.apply_voided_suppression`'s own rationale
    (ADR-0015 invariant 3): suppression is the only way a finding stops
    blocking without anything in *this* pass deciding so, and the event is its
    only durable local record. Splitting emission into a separate call the
    caller must remember would make the audit trail optional for exactly the
    act that most needs it.

    Returns only the verdict — no ``Adjudication`` list, because the codex path
    has no ``ADJUDICATIONS`` array to append one to (the same reason
    ``apply_voided_suppression``'s returned list is discarded there).

    ``must_fix`` is recomputed from the stamped dispositions rather than from
    "index not in matches". This function runs AFTER
    ``apply_voided_suppression``, so a finding that pass already stamped
    ``"rejected"`` is in scope here; keying the recompute on membership in
    *this* pass's match set would resurrect it into ``must_fix``. Only
    ``"fixed"`` (nothing decided yet) and ``"rejected"`` are reachable at this
    point in the pipeline. ``must_fix_initial``, ``should_fix``, ``agents_run``
    and ``review.deferred`` are preserved verbatim — a suppression is not a
    fix, so the originally-found counts must keep saying what was found.

    **Every record is gated on provenance first** (#2210 round 2). The ledger
    is partitioned by :func:`partition_enforceable_dispositions` before
    anything is matched: a record that cannot say which finding, who settled
    it, when, against what code and why is never applied, is logged once at
    WARNING naming the ticket, and is stamped onto
    ``ReviewVerdict.refused_dispositions`` so the posted comment reports the
    attempt instead of swallowing it. A refusal is per-record — a
    well-formed sibling in the same ledger still applies.

    ``refused`` (#2210 round 3) carries the records the WRITE path already
    refused: since :func:`merge_finding_dispositions` never lets an invalid
    record into the ledger, partitioning the ledger can no longer discover
    them, yet they must still reach the review output. The caller has already
    logged them (``codex_review._context`` refuses at parse time), so they are
    merged into ``refused_dispositions`` without a second WARNING; only the
    refusals this function derives from legacy rows already in the durable
    ledger are logged here, and a key refused on both sides is reported once.

    ``claim_tier_enabled`` (#2210) arms the fuzzy second tier for THIS pass.
    It defaults to ``False``, which is the fail-safe floor: a call path that
    never threads it is off, and the exact tier behaves identically either
    way. With the gate closed, a claim-tier match leaves the verdict untouched
    (the same object is returned) and is recorded as a
    ``review.finding_claim_shadowed`` event instead — see :func:`_emit_shadow`
    and ADR-0016. ``reviewed_sha`` rides onto that shadow payload only, so a
    consumer can group a re-derived finding's events by ticket, file and
    summary and count the distinct reviewed commits behind them.

    ``worktree`` (#2232) turns on drift surfacing, the gap ADR-0016 named as a
    precondition for ever arming the claim tier. When it is supplied, a match
    whose file has changed between the record's own ``reviewed_sha`` and this
    pass's is NOT enforced: the finding keeps blocking, a
    :class:`~cw.review_markers.StaleDisposition` is stamped onto
    ``stale_dispositions``, and a ``review.finding_disposition_stale`` event
    records it. The ledger entry is left untouched — surfacing, never silent
    expiry. ``reviewed_sha`` is this pass's side of that comparison, so a
    caller threading ``worktree`` must thread it too; without it every
    comparison short-circuits to "no drift" and the check is inert.

    ``None`` is the fail-safe floor, the same convention
    ``claim_tier_enabled: bool = False`` follows: a call path that never
    threads a worktree behaves byte-for-byte as it did before this ticket.
    The direction differs on purpose, because the risks do. A claim tier that
    arms by accident suppresses a finding nobody settled; a drift check that
    fails to run merely leaves the pre-#2232 behaviour in place.

    ``disposition_drift_check_enabled`` (#2232) is the lane-resolved gate for
    that check, and governs ONLY this automatic call site. It defaults ``True``
    — a check is presumed wanted, unlike a feature — so the failure direction
    of a threading mistake is "the check ran when it could have been skipped",
    which costs one ``git diff``. ``False`` enforces a match exactly as if the
    check did not exist, whatever ``worktree`` says. ``cw review dispositions
    --worktree`` calls :func:`disposition_drifted` directly and is deliberately
    not gated by this, so an operator who turned the gate off to debug can
    still see what is stale.
    """
    enforceable, ledger_refused = partition_enforceable_dispositions(ledger)
    reported = {record.key: record for record in refused or []}
    fresh = [record for record in ledger_refused if record.key not in reported]
    log_refused_dispositions(fresh, ticket_id)
    reported.update((record.key, record) for record in fresh)
    if reported:
        verdict = verdict.model_copy(
            update={"refused_dispositions": [reported[key] for key in sorted(reported)]}
        )
    if not enforceable:
        return verdict
    matches = _ledger_matches(verdict.accepted, enforceable, ticket_id=ticket_id)
    if not matches:
        return verdict

    drift_check_on = worktree is not None and disposition_drift_check_enabled
    enforced: dict[int, _LedgerMatch] = {}
    stale: list[StaleDisposition] = []
    for index, match in matches.items():
        af = verdict.accepted[index]
        if match.kind != _MATCH_EXACT and not claim_tier_enabled:
            _emit_shadow(af, match, ticket_id, reviewed_sha)
            continue
        if drift_check_on and disposition_drifted(
            worktree, match.entry.reviewed_sha, reviewed_sha, af.finding.file
        ):
            stale.append(
                StaleDisposition(
                    key=match.key,
                    reviewed_sha=match.entry.reviewed_sha,
                    current_sha=reviewed_sha,
                )
            )
            _emit_stale(af, match, ticket_id, reviewed_sha)
            continue
        enforced[index] = match
    if stale:
        verdict = verdict.model_copy(
            update={
                "stale_dispositions": [
                    *verdict.stale_dispositions,
                    *sorted(stale, key=lambda record: record.key),
                ]
            }
        )
    if not enforced:
        return verdict

    stamped: list[AcceptedFinding] = []
    for index, af in enumerate(verdict.accepted):
        applied = enforced.get(index)
        if applied is None:
            stamped.append(af)
            continue
        stamped.append(_stamp_suppressed(af, applied))
        _emit_suppression(af, applied, ticket_id)

    must_fix = [
        af.finding
        for af in stamped
        if af.finding.severity == _MUST_FIX and af.disposition == _FIXED
    ]
    return verdict.model_copy(
        update={
            "accepted": stamped,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
        }
    )
