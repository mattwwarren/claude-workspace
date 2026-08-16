"""Cross-round adjudication memory for re-derived review findings (#1838).

The codex backend re-derives its findings mechanically on every review round,
so a finding an operator has already settled comes back identical on the next
round — and re-parks the run. :mod:`cw.review_adjudication`'s
:class:`~cw.review_adjudication.VoidedFinding` seam (#1814) closes half of
that: it suppresses a re-derived finding an operator voided, but only
*mechanically, after synthesis*, only for as long as the finding's **evidence**
still matches verbatim, and with no memory persisted on the queue row at all.

This module is the other half, and is deliberately a **parallel seam** rather
than a generalization of that one (#1838 R6). Two things are genuinely new
here:

1. **The reviewer is told.** The ledger reaches the prompt as a
   "previously adjudicated, do not re-raise" block
   (``codex_review._context._render_adjudicated_findings_block``) — the
   ``VoidedFinding`` record never does, by its own documented design.
2. **The memory is durable on the queue row.** ``TicketTask``'s
   ``finding_dispositions`` (schema v30) survives worktree teardown, regress,
   and redispatch, so round N+2 remembers what round N settled without
   re-reading anything.

Identity is #1837's :func:`cw.review_debt.fingerprint_v1` — ``(file,
normalized_summary)``, with **no evidence and no severity**. That is a
deliberate divergence from ``_voided_fingerprint``: an evidence-anchored
identity lapses the moment the code moves, which is exactly the memory loss
this ticket exists to remove. The cost — a suppression that outlives the code
it was granted for — is paid down by making every suppression VISIBLE rather
than by adding an expiry: see :func:`_render_suppression_signal` and the
``review.finding_disposition_suppressed`` event.

Only the already-declared-shared ``review_findings`` types
(:class:`~cw.review_findings.AcceptedFinding`,
:class:`~cw.review_findings.ReviewVerdict`) are reused. Nothing here imports or
extends :func:`cw.review_adjudication.apply_adjudication`,
:class:`~cw.review_adjudication.Adjudication`, or the ``"defer"`` outcome whose
two meanings are why those seams must stay apart.

**Import discipline — load-bearing, not style.** This module MUST NOT import
anything from ``cw`` at module scope. ``cw.models.tasks`` imports
:class:`FindingDisposition` from here, so any runtime ``cw.*`` import at module
scope closes a cycle through ``cw.models``' package ``__init__``: the shortest
one is ``cw.review_findings -> cw.auto_dev_result.schema -> cw.models ->
cw.models.tasks -> (this module) -> cw.review_debt -> cw.review_findings``,
which raises ``ImportError`` on a partially initialized ``cw.review_findings``
whenever ``cw.review_findings`` is the first of the two to be imported. Every
``cw`` import below therefore lives either under ``TYPE_CHECKING`` (erased at
runtime) or inside a function body (resolved after every module has finished
loading). ``tests/test_review_finding_dispositions.py`` pins this by importing
the module standalone in a subprocess.

Public surface: :class:`FindingDisposition`, :data:`Outcome`,
:func:`render_finding_disposition_block`,
:func:`parse_finding_disposition_block`, :func:`merge_finding_dispositions`,
:func:`split_disposition_key`, :func:`suppress_adjudicated_findings`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

#: The two decisions an operator can record about a finding (#1838 R2). Only
#: ``"REJECTED"`` participates in mechanical suppression; ``"ACCEPTED"`` is a
#: record-only annotation that reaches the prompt and changes no gate.
Outcome = Literal["ACCEPTED", "REJECTED"]

_REJECTED: Outcome = "REJECTED"
_MUST_FIX = "MUST_FIX"

#: Joins ``fingerprint_v1``'s ``(file, normalized_summary)`` tuple into the
#: string a JSON object key has to be. A file path containing this sequence
#: could in principle collide with another entry — the same exact-match-only
#: false-merge review_debt already documents and accepts for the underlying
#: fingerprint, not a new class of risk.
_KEY_SEPARATOR = "::"

#: Ticket-comment header the ledger block renders under, and the sentinel that
#: is the actual contract. Mirrors ``_VOIDED_MD_TITLE``/``_VOIDED_SENTINEL``
#: (#1814) — a JSON payload inside an HTML comment, mechanically parsed. NOT
#: ``auto-dev-preflight-resolutions``' free-prose grammar, which would need an
#: LLM to read and would reintroduce the fragility class #1805 removed.
_DISPOSITION_MD_TITLE = "## Review Finding Dispositions"
_DISPOSITION_SENTINEL = "REVIEW-FINDING-DISPOSITIONS"
#: Bump when the sentinel's on-the-wire shape changes in a way a reader must
#: branch on, following ``_VOIDED_SCHEMA_VERSION``'s convention.
_DISPOSITION_SCHEMA_VERSION = 1
_DISPOSITION_BLOCK_RE = re.compile(
    rf"<!--\s*{_DISPOSITION_SENTINEL}\s*(?P<body>.*?)\s*{_DISPOSITION_SENTINEL}\s*-->",
    re.DOTALL,
)

#: The operator-mandated visibility signal stamped into
#: ``AcceptedFinding.disposition_detail`` on every REJECTED suppression.
#: Deterministic so two passes over one ledger entry produce the same text.
_SUPPRESSION_SIGNAL = (
    "finding {file}:{summary} suppressed by prior REJECTED adjudication "
    "(recorded {recorded_at}) -- original rationale: {rationale} -- "
    "re-adjudicate if the code at this location has changed."
)


class FindingDisposition(BaseModel):
    """One operator decision about one finding, remembered across rounds.

    Keyed (by every holder of this model) on a stringified
    :func:`cw.review_debt.fingerprint_v1` — see :func:`_disposition_key`.

    ``rationale`` is the operator's own words for why, carried so a later
    round's reviewer and a later reader of the posted comment both see the
    reasoning rather than a bare verdict. ``recorded_at`` is an ISO-8601
    string rather than a ``datetime`` for the same reason
    :attr:`cw.review_adjudication.VoidedFinding.voided_at` is: it arrives from
    hand-authored marker JSON and may be blank when the producer had no clock
    handy. It is never part of identity — only :func:`merge_finding_dispositions`
    reads it, to resolve a duplicate key newest-wins.
    """

    outcome: Outcome
    rationale: str = ""
    recorded_at: str = ""


def _disposition_key(file: str, summary: str) -> str | None:
    """The ledger key for a finding, or ``None`` when it cannot be keyed.

    Wraps :func:`cw.review_debt.fingerprint_v1` verbatim (#1838 R1) — no second
    normalization implementation exists here, so the ledger and #1837's debt
    ledger can never disagree about what "the same finding" means.

    ``None`` for a ``file="N/A"`` finding (#1817's no-diff-anchor case): there
    is no path to key on, so it gets no cross-round memory. Mirrors
    ``promote_debt_finding``'s own ``if fingerprint is None: return None``
    short-circuit.
    """
    from cw.review_debt import fingerprint_v1

    fingerprint = fingerprint_v1(file, summary)
    if fingerprint is None:
        return None
    return _KEY_SEPARATOR.join(fingerprint)


def split_disposition_key(key: str) -> tuple[str, str]:
    """Recover ``(file, normalized_summary)`` from a ledger *key*.

    The inverse of :func:`_disposition_key`'s join, exposed because the prompt
    renderer needs the file and summary back out to write a readable line.
    Splits on the FIRST separator, so a normalized summary that happens to
    contain one stays intact.
    """
    file, _, summary = key.partition(_KEY_SEPARATOR)
    return file, summary


def render_finding_disposition_block(ledger: dict[str, FindingDisposition]) -> str:
    """Render *ledger* as the postable ``## Review Finding Dispositions`` comment.

    Embeds its own markdown header so the caller posts the returned text as-is,
    and carries the payload as JSON inside the HTML comment — both mirroring
    :func:`cw.review_adjudication.render_voided_findings_block`, and for the
    same reason: this record is read back by the codex backend, which has no
    LLM to interpret prose, so it must round-trip through ``json.loads`` while
    staying human-readable.

    Returns ``""`` for an empty ledger — nothing to record means no comment.
    """
    if not ledger:
        return ""
    payload = {
        "schema_version": _DISPOSITION_SCHEMA_VERSION,
        "dispositions": {
            key: entry.model_dump(mode="json") for key, entry in sorted(ledger.items())
        },
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    return (
        f"{_DISPOSITION_MD_TITLE}\n\n"
        f"<!-- {_DISPOSITION_SENTINEL}\n{body}\n{_DISPOSITION_SENTINEL} -->\n"
    )


def _parse_one_disposition_block(body: str) -> dict[str, FindingDisposition]:
    """Parse one sentinel body, degrading a malformed block to ``{}``."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _log.warning("auto-dev: ignoring malformed %s block", _DISPOSITION_SENTINEL)
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("dispositions")
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, FindingDisposition] = {}
    for key, item in raw.items():
        try:
            entries[str(key)] = FindingDisposition.model_validate(item)
        except ValueError:
            _log.warning("auto-dev: ignoring malformed %s entry", _DISPOSITION_SENTINEL)
    return entries


def parse_finding_disposition_block(
    comment_bodies: list[str],
) -> dict[str, FindingDisposition]:
    """Union every disposition sentinel across *comment_bodies*.

    Fail-open throughout — a missing, truncated, or malformed block yields
    nothing and never raises, and one bad block never discards a good sibling.
    Same degrade contract, and same justification, as
    :func:`cw.review_adjudication.parse_voided_findings_block`: a review that
    could not read the ledger is strictly better than no review, and the missed
    suppression surfaces as the finding re-appearing, which an operator can act
    on.

    A key recorded in more than one comment resolves through
    :func:`merge_finding_dispositions` (newest ``recorded_at`` wins), which is
    the #1654 marker-supersession convention: an operator who changes their
    mind re-posts the marker rather than editing history.
    """
    merged: dict[str, FindingDisposition] = {}
    for body in comment_bodies:
        for match in _DISPOSITION_BLOCK_RE.finditer(body):
            merged = merge_finding_dispositions(
                merged, _parse_one_disposition_block(match.group("body"))
            )
    return merged


def merge_finding_dispositions(
    existing: dict[str, FindingDisposition], parsed: dict[str, FindingDisposition]
) -> dict[str, FindingDisposition]:
    """Fold *parsed* into *existing*, newest-``recorded_at``-wins, additively.

    Additive is the whole point (#1838 R3, forward-only): a key present in
    *existing* but absent from *parsed* is PRESERVED. The ledger is durable
    memory, not a mirror of whatever the current comment thread happens to
    say — clearing it on absence would re-open every finding the moment a
    comment was edited or a fetch degraded to ``[]``.

    Neither argument is mutated; a fresh dict is returned.
    """
    merged = dict(existing)
    for key, entry in parsed.items():
        current = merged.get(key)
        if current is None or entry.recorded_at >= current.recorded_at:
            merged[key] = entry
    return merged


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


def _rejected_matches(
    accepted: list[AcceptedFinding], ledger: dict[str, FindingDisposition]
) -> dict[int, FindingDisposition]:
    """Indices into *accepted* that a REJECTED ledger entry suppresses.

    An ``ACCEPTED`` entry deliberately never appears here: it is a record-only
    annotation that reaches the reviewer prompt and changes no gate.
    """
    matches: dict[int, FindingDisposition] = {}
    for index, af in enumerate(accepted):
        key = _disposition_key(af.finding.file, af.finding.summary)
        if key is None:
            continue
        entry = ledger.get(key)
        if entry is not None and entry.outcome == _REJECTED:
            matches[index] = entry
    return matches


def suppress_adjudicated_findings(
    verdict: ReviewVerdict,
    ledger: dict[str, FindingDisposition],
    *,
    ticket_id: str,
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
    """
    if not ledger:
        return verdict
    matches = _rejected_matches(verdict.accepted, ledger)
    if not matches:
        return verdict

    # Deferred for the import-cycle reason the module docstring gives: a
    # module-scope `cw.events` import here closes cw.models -> cw.models.tasks
    # -> this module -> cw.events -> cw.models.
    from cw.events import record_event
    from cw.models.enums import OrchestratorEventType

    stamped: list[AcceptedFinding] = []
    for index, af in enumerate(verdict.accepted):
        entry = matches.get(index)
        if entry is None:
            stamped.append(af)
            continue
        stamped.append(
            af.model_copy(
                update={
                    "disposition": "rejected",
                    "disposition_detail": _render_suppression_signal(
                        af.finding.file, af.finding.summary, entry
                    ),
                }
            )
        )
        _log.info(
            "auto-dev: suppressed re-derived finding already adjudicated by "
            "operator (ticket=%s, severity=%s, file=%s, recorded_at=%s)",
            ticket_id,
            af.finding.severity,
            af.finding.file,
            entry.recorded_at,
        )
        record_event(
            OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED,
            payload={
                "file": af.finding.file,
                "summary": af.finding.summary,
                "outcome": entry.outcome,
                "rationale": entry.rationale,
                "recorded_at": entry.recorded_at,
            },
            correlation_id=ticket_id,
        )

    must_fix = [
        af.finding
        for af in stamped
        if af.finding.severity == _MUST_FIX and af.disposition == "fixed"
    ]
    return verdict.model_copy(
        update={
            "accepted": stamped,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
        }
    )
