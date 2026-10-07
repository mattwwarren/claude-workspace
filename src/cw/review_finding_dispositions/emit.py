"""The audit trail: every event and log line a disposition produces (#1838).

One ``review.finding_disposition_suppressed`` event per applied suppression
(:func:`_emit_suppression`), the ``review.finding_claim_shadowed`` measurement
the closed claim-tier gate records instead (:func:`_emit_shadow`, #2210), and
the ``review.finding_disposition_stale`` record of a match the drift check
declined to apply (:func:`_emit_stale`, #2232). :func:`disposition_event_type`
and :func:`disposition_event_payload` are the type choice and payload shape
every emitter shares, so a consumer parses one shape whichever path recorded
it. Split out of the flat ``review_finding_dispositions.py`` (#2498).

Imports nothing from ``cw`` at module scope beyond this package's own
submodules (see the package docstring's "Import discipline" section):
:func:`cw.events.record_event` and
:class:`~cw.models.enums.OrchestratorEventType` are imported inside each
function body, so a test that patches ``cw.events.record_event`` still
reaches every emitter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.review_finding_dispositions._constants import _LOGGER_NAME
from cw.review_finding_dispositions.model import REVERSED, split_disposition_key

if TYPE_CHECKING:
    from cw.models.enums import OrchestratorEventType
    from cw.review_finding_dispositions.match import _LedgerMatch
    from cw.review_finding_dispositions.model import FindingDisposition
    from cw.review_findings import AcceptedFinding

_log = logging.getLogger(_LOGGER_NAME)


def _emit_suppression(af: AcceptedFinding, match: _LedgerMatch, ticket_id: str) -> None:
    """Log and record one applied suppression (#1838 mandatory audit trail)."""
    # Deferred for the import-cycle reason the module docstring gives: a
    # module-scope `cw.events` import here closes cw.models -> cw.models.tasks
    # -> this module -> cw.events -> cw.models.
    from cw.events import record_event
    from cw.models.enums import OrchestratorEventType

    _log.info(
        "auto-dev: suppressed re-derived finding already adjudicated by "
        "operator (ticket=%s, severity=%s, file=%s, recorded_at=%s, match=%s)",
        ticket_id,
        af.finding.severity,
        af.finding.file,
        match.entry.recorded_at,
        match.kind,
    )
    record_event(
        OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED,
        payload={
            "file": af.finding.file,
            "summary": af.finding.summary,
            "severity": af.finding.severity,
            "outcome": match.entry.outcome,
            "rationale": match.entry.rationale,
            "recorded_at": match.entry.recorded_at,
            "match_kind": match.kind,
            "similarity": match.similarity,
            "matched_key": match.key,
        },
        correlation_id=ticket_id,
    )


def _emit_shadow(
    af: AcceptedFinding, match: _LedgerMatch, ticket_id: str, reviewed_sha: str
) -> None:
    """Record a claim-tier match the closed gate did NOT apply (#2210).

    This is the measurement path ADR-0016 rests on: until an operator arms a
    lane, every finding the claim tier WOULD have suppressed leaves a durable,
    queryable record carrying the candidate's severity, the matched key and the
    score, so the thresholds can be judged against real rewordings.

    Purely observational, so it must never alter a verdict or abort a review
    pass. ``record_event`` has no handler of its own (its lock and append are
    file I/O), and this is the one ``record_event`` call added on the
    DEFAULT-OFF path — on lanes that never opted into anything. The INFO line
    is emitted BEFORE the event so a failed write still leaves the measurement
    in the log.
    """
    from cw.events import record_event
    from cw.models.enums import OrchestratorEventType

    _log.info(
        "auto-dev: claim-tier match NOT suppressed, gate off "
        "(ticket=%s, severity=%s, file=%s, similarity=%.2f, recorded_at=%s)",
        ticket_id,
        af.finding.severity,
        af.finding.file,
        match.similarity,
        match.entry.recorded_at,
    )
    try:
        record_event(
            OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED,
            payload={
                "file": af.finding.file,
                "summary": af.finding.summary,
                "severity": af.finding.severity,
                "similarity": match.similarity,
                "matched_key": match.key,
                "matched_recorded_at": match.entry.recorded_at,
                "matched_rationale": match.entry.rationale,
                "reviewed_sha": reviewed_sha,
            },
            correlation_id=ticket_id,
        )
    except OSError:
        _log.warning(
            "auto-dev: could not record claim-tier shadow event (ticket=%s)",
            ticket_id,
            exc_info=True,
        )


def disposition_event_type(entry: FindingDisposition) -> OrchestratorEventType:
    """The audit event type one settled *entry* is recorded under (#2210, #2232).

    The event TYPE carries the semantic, not a field inside the payload: a
    ``REVERSED`` entry emits ``review.finding_disposition_reverted`` and
    everything else emits ``review.finding_settled``, so an operator asking
    "what has been withdrawn" runs one ``cw event tail --type ...`` rather
    than filtering settles by their ``outcome``.

    Shared (#2232) by the two paths a disposition can reach the durable ledger
    through — ``cw review settle`` and the review pass's own comment-thread
    sync — so the type choice cannot drift between them, and so neither site
    needs a raw ``"REVERSED"`` literal.
    """
    from cw.models.enums import OrchestratorEventType

    return (
        OrchestratorEventType.REVIEW_FINDING_DISPOSITION_REVERTED
        if entry.outcome == REVERSED
        else OrchestratorEventType.REVIEW_FINDING_SETTLED
    )


def disposition_event_payload(key: str, entry: FindingDisposition) -> dict[str, object]:
    """The audit payload for one settled *entry*, keyed by its ledger *key*.

    The full provenance set — who, why, when, and against what code — plus the
    identity the record was minted for. Enumerated field by field rather than
    ``{**entry.model_dump()}`` so adding a field to
    :class:`FindingDisposition` is a deliberate decision about what an audit
    consumer sees, not an automatic one.

    Shared by every emitter so the shape a consumer parses is the same
    whichever path recorded it (#2232). :func:`_emit_stale`'s payload is
    deliberately this shape PLUS ``current_sha``.
    """
    return {
        "key": key,
        "file": split_disposition_key(key)[0],
        "summary": entry.summary,
        "outcome": entry.outcome,
        "reason": entry.rationale,
        "actor": entry.actor,
        "recorded_at": entry.recorded_at,
        "reviewed_sha": entry.reviewed_sha,
    }


def _emit_stale(
    af: AcceptedFinding, match: _LedgerMatch, ticket_id: str, current_sha: str
) -> None:
    """Record a ledger match the drift check declined to apply (#2232).

    Structurally :func:`_emit_shadow`'s twin — INFO line first so a failed
    write still leaves the measurement in the log, then the event, with an
    ``OSError`` warned rather than raised.

    The asymmetry with :func:`cw.cli.review.commands._emit_settle_events`,
    which aborts the whole settle on a failed emit, is deliberate and runs the
    same direction both times: there the audited act CREATES a durable
    suppression, so an unrecorded one is invisible; here the act is DECLINING
    to suppress, which is already the safe outcome and is already visible on
    the verdict (``stale_dispositions``) and in the finding that kept
    blocking. Aborting a review pass over an advisory record would trade a
    safe outcome for a parked run.

    The payload is :func:`disposition_event_payload`'s shape plus
    ``current_sha`` (#2232). Full parity with the settle/revert events is the
    point: someone triaging a drifted suppression is asking WHO silenced this
    finding and WHY, and a payload carrying only the two shas cannot answer
    either. ``file``/``summary`` are overridden with the finding as it came
    back this round rather than the record's stored copy — on a claim-tier
    match those differ, and the live text is what the reader is looking at.
    """
    from cw.events import record_event
    from cw.models.enums import OrchestratorEventType

    _log.info(
        "auto-dev: ledger match NOT suppressed, code drifted since the settle "
        "(ticket=%s, file=%s, match=%s, reviewed_sha=%s, current_sha=%s)",
        ticket_id,
        af.finding.file,
        match.kind,
        match.entry.reviewed_sha,
        current_sha,
    )
    try:
        record_event(
            OrchestratorEventType.REVIEW_FINDING_DISPOSITION_STALE,
            payload={
                **disposition_event_payload(match.key, match.entry),
                # The finding the ledger matched, not the record's own stored
                # copy: on a claim-tier match the two differ, and a consumer
                # triaging a drifted suppression needs the text that actually
                # came back this round.
                "file": af.finding.file,
                "summary": af.finding.summary,
                # Drift-specific, and the only field this payload adds to the
                # settle/revert shape: which commit the record was measured
                # against when it was declined.
                "current_sha": current_sha,
            },
            correlation_id=ticket_id,
        )
    except OSError:
        _log.warning(
            "auto-dev: could not record disposition-stale event (ticket=%s)",
            ticket_id,
            exc_info=True,
        )
