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
   ``finding_dispositions`` (schema v31) survives worktree teardown, regress,
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

#2210 adds a second, fuzzy matching tier to the backstop below — see
:func:`_claim_similarity` and ADR-0016. It ships **gated per lane and off**:
while the gate is closed, every would-be claim-tier suppression is recorded as
a ``review.finding_claim_shadowed`` event instead of being applied, so the
matcher can be measured on real rewordings before anyone arms it. The exact
tier above is unaffected by the gate in either direction.

Public surface: :class:`FindingDisposition`, :data:`Outcome`,
:data:`SETTLE_SECTION_HEADING`, :func:`build_finding_disposition_ledger`,
:func:`render_finding_disposition_block`,
:func:`parse_finding_disposition_block`, :func:`merge_finding_dispositions`,
:func:`split_disposition_key`, :func:`suppress_adjudicated_findings`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

#: The two decisions an operator can record about a finding (#1838 R2). Only
#: ``"REJECTED"`` participates in mechanical suppression; ``"ACCEPTED"`` is a
#: record-only annotation that reaches the prompt and changes no gate.
Outcome = Literal["ACCEPTED", "REJECTED"]

_REJECTED: Outcome = "REJECTED"
_MUST_FIX = "MUST_FIX"
#: ``AcceptedFinding.disposition``'s post-consolidate default — "nothing has
#: decided anything about this finding yet". The claim tier below refuses to
#: re-stamp anything else, so a void pass's ``"rejected"`` survives untouched.
_FIXED = "fixed"

#: The heading the blocking review comment's ready-to-paste settle section
#: renders under (#2210). Public and owned HERE, next to the rest of the
#: record's wire grammar, because TWO modules must agree on it byte for byte:
#: ``codex_review._verdict._render`` emits it, and
#: ``codex_review._context.core`` builds its elision regex from it so a
#: pipeline-authored payload never re-enters the next reviewer's prompt as
#: evidence. A plain string needs no ``cw`` import, so the module's import
#: discipline is untouched.
SETTLE_SECTION_HEADING = "### Settle a finding"

#: Which tier produced a match, carried onto the event payload and the log so
#: an audit can tell an exact-identity suppression from a fuzzy one.
_MATCH_EXACT = "exact"
_MATCH_CLAIM = "claim"

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
#: branch on, following ``_VOIDED_SCHEMA_VERSION``'s convention. #2210's
#: ``actor``/``reviewed_sha``/``summary`` fields deliberately did NOT bump it:
#: they are optional and defaulted, the model ignores unknown keys, and no
#: reader branches on their presence — a v1 marker and a v1 queue row written
#: either side of that change load identically. Same reasoning applies to
#: ``DEV_QUEUE_SCHEMA_VERSION``, which ``TicketTask.finding_dispositions``
#: rides on: the migration ladder fills defaults for absent TicketTask FIELDS,
#: and this is a nested model gaining defaulted ones.
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

    ``actor``, ``reviewed_sha`` and ``summary`` are #2210's **provenance**
    fields, and they exist because this record is a durable, blocking
    SUPPRESSION: an entry that cannot answer "who silenced this finding, when,
    and against what code" is not an audit record. ``cw review settle`` always
    fills all three (and always stamps ``recorded_at`` from its own UTC clock —
    audit data is never operator-supplied); every one of them stays OPTIONAL
    and defaulted so a marker or a persisted queue row written before #2210
    still loads unchanged, and so a hand-authored marker stays legal.

    ``summary`` is the VERBATIM finding summary. The ledger key carries only
    the *normalised* half (see :func:`_disposition_key`), which is lossy and
    shared by every rewording that normalises alike — so the verbatim text is
    what lets a future per-record rollback target exactly one entry rather than
    a key's worth of them. It is deliberately not part of identity: nothing
    matches on it.
    """

    outcome: Outcome
    rationale: str = ""
    recorded_at: str = ""
    actor: str = ""
    reviewed_sha: str = ""
    summary: str = ""


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


#: Minimum length for a claim token or symbol. Two-letter words carry almost no
#: claim content ("up", "in", "is") and would inflate the overlap of any two
#: English sentences.
_MIN_TOKEN_LEN = 3

#: Two threshold regimes (#2210, ADR-0016). When BOTH summaries name at least
#: one symbol, the symbol sets must intersect and the looser anchored floors
#: apply — a shared identifier is strong evidence the two sentences are about
#: the same code. With a symbol on one side or neither, nothing anchors the
#: comparison, so the stricter prose floors apply. Both are judgement calls,
#: not corpus-derived; the shadow events exist to supply the corpus.
_ANCHORED_MIN_SHARED = 3
_ANCHORED_MIN_DICE = 0.6
_PROSE_MIN_SHARED = 4
_PROSE_MIN_DICE = 0.75

#: Function words that say nothing about WHAT a finding claims. Deliberately a
#: short, hand-listed set rather than a stemmer or a stopword library: the
#: whole matcher must stay stdlib-only (this module imports nothing from ``cw``
#: at module scope, let alone a third-party NLP dependency).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "and",
        "are",
        "but",
        "can",
        "does",
        "for",
        "from",
        "has",
        "have",
        "its",
        "may",
        "not",
        "should",
        "than",
        "that",
        "the",
        "this",
        "was",
        "were",
        "when",
        "will",
        "with",
    }
)

#: Every regex below operates on ALREADY-LOWERCASED text. Hyphens and dots
#: split tokens (so "early-return" contributes "early" and "return"), while
#: underscores stay inside one, which is what keeps a snake_case identifier a
#: single token.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
#: Used with ``fullmatch`` to decide whether a backticked span is an
#: identifier. There is deliberately NO free-standing dotted-identifier regex:
#: a bare ``foo.py``, ``baz.md`` or ``e.g`` must never count as a symbol, so
#: only backticked identifier spans and snake_case tokens qualify.
_IDENTIFIER_SPAN_RE = re.compile(r"[a-z0-9_.]+")


def _claim_tokens(text: str) -> frozenset[str]:
    """The content words of an already-normalized summary (#2210)."""
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= _MIN_TOKEN_LEN and token not in _STOPWORDS
    )


def _claim_symbols(text: str) -> frozenset[str]:
    """The code identifiers an already-normalized summary names (#2210).

    Two sources, both conservative: a backticked span that is entirely an
    identifier (with a trailing ``()`` call suffix tolerated), and any token
    that still contains an underscore once leading/trailing ones are stripped.
    A prose word, a filename and an abbreviation all fail both tests, which is
    what keeps the symbol veto below from firing on "foo.py vs baz.md".
    """
    lowered = text.lower()
    symbols: set[str] = set()
    for span in _BACKTICK_RE.findall(lowered):
        candidate = span.strip().removesuffix("()")
        if _IDENTIFIER_SPAN_RE.fullmatch(candidate):
            symbols.add(candidate)
    symbols.update(
        token for token in _TOKEN_RE.findall(lowered) if "_" in token.strip("_")
    )
    return frozenset(s for s in symbols if len(s) >= _MIN_TOKEN_LEN)


def _claim_similarity(recorded: str, candidate: str) -> float | None:
    """Score two already-normalized summaries, or ``None`` for "not a match".

    The score is the Dice coefficient ``2|A∩B| / (|A|+|B|)`` over claim tokens,
    returned ONLY when the applicable regime's shared-token and Dice floors
    both clear. Dice rather than the overlap coefficient on purpose: overlap
    scores a terse candidate that happens to be a subset of a long recorded
    summary at a perfect 1.0, which is exactly the false match this tier must
    not make.

    ``None`` — never a low score — is the whole "no match" signal, so a caller
    cannot accidentally treat a below-threshold pair as a weak match.
    """
    recorded_tokens = _claim_tokens(recorded)
    candidate_tokens = _claim_tokens(candidate)
    if not recorded_tokens or not candidate_tokens:
        return None
    recorded_symbols = _claim_symbols(recorded)
    candidate_symbols = _claim_symbols(candidate)
    if recorded_symbols and candidate_symbols:
        if not recorded_symbols & candidate_symbols:
            # The veto: both sides name code, and they name DIFFERENT code.
            # "`parse_config` missing null check" and "`load_config` missing
            # null check" are two findings, however alike they read.
            return None
        min_shared, min_dice = _ANCHORED_MIN_SHARED, _ANCHORED_MIN_DICE
    else:
        min_shared, min_dice = _PROSE_MIN_SHARED, _PROSE_MIN_DICE
    shared = len(recorded_tokens & candidate_tokens)
    dice = 2 * shared / (len(recorded_tokens) + len(candidate_tokens))
    if shared < min_shared or dice < min_dice:
        return None
    return dice


class _LedgerMatch(NamedTuple):
    """One finding's resolved ledger match, with the tier that produced it."""

    entry: FindingDisposition
    key: str
    kind: str
    similarity: float


def _best_claim_match(
    key: str, ledger: dict[str, FindingDisposition]
) -> _LedgerMatch | None:
    """The nearest same-file recorded decision to *key*, or ``None`` (#2210).

    Nearest DECISION, not nearest rejection: a closer ``ACCEPTED`` entry wins
    the contest and then vetoes, because an operator who upheld a finding
    worded almost exactly like this one has said the opposite of "suppress it".

    Candidates are built over ``sorted(ledger.items())`` and ``max`` keeps the
    first maximal element it meets, so a full tie (equal similarity AND equal
    ``recorded_at``) resolves to the alphabetically first ledger key, and a
    later ``recorded_at`` wins at equal similarity.
    """
    file, candidate = split_disposition_key(key)
    matches: list[_LedgerMatch] = []
    for entry_key, entry in sorted(ledger.items()):
        entry_file, recorded = split_disposition_key(entry_key)
        if entry_file != file:
            continue
        similarity = _claim_similarity(recorded, candidate)
        if similarity is None:
            continue
        matches.append(_LedgerMatch(entry, entry_key, _MATCH_CLAIM, similarity))
    if not matches:
        return None
    best = max(matches, key=lambda m: (m.similarity, m.entry.recorded_at))
    return best if best.entry.outcome == _REJECTED else None


def _match_ledger(
    af: AcceptedFinding, ledger: dict[str, FindingDisposition]
) -> _LedgerMatch | None:
    """Resolve one accepted finding against the ledger, exact tier first.

    An exact key hit is decisive in BOTH directions: a ``REJECTED`` entry
    suppresses, and an ``ACCEPTED`` one vetoes any fuzzy sibling rather than
    falling through to the claim tier.

    The claim tier is scoped to still-undecided MUST_FIX findings. A SHOULD_FIX
    or below does not block, so fuzzily suppressing one buys nothing and hides
    content; a finding a void pass already stamped must not be re-stamped here.
    """
    key = _disposition_key(af.finding.file, af.finding.summary)
    if key is None:
        return None
    entry = ledger.get(key)
    if entry is not None:
        if entry.outcome == _REJECTED:
            return _LedgerMatch(entry, key, _MATCH_EXACT, 1.0)
        return None
    if af.finding.severity != _MUST_FIX or af.disposition != _FIXED:
        return None
    return _best_claim_match(key, ledger)


def _ledger_matches(
    accepted: list[AcceptedFinding],
    ledger: dict[str, FindingDisposition],
    *,
    ticket_id: str,
) -> dict[int, _LedgerMatch]:
    """Indices into *accepted* a ledger entry matches, contests removed.

    An ``ACCEPTED`` entry deliberately never appears here: it is a record-only
    annotation that reaches the reviewer prompt and changes no gate.

    A finding whose ``contests_adjudication`` is non-blank is dropped from the
    match set entirely (#2210): the reviewer has said in a typed field that it
    is knowingly re-raising a settled finding and what changed. Admission is
    INFO-log only, by design — an audit event for it is follow-up F9.
    """
    matches: dict[int, _LedgerMatch] = {}
    for index, af in enumerate(accepted):
        match = _match_ledger(af, ledger)
        if match is None:
            continue
        if af.finding.contests_adjudication.strip():
            _log.info(
                "auto-dev: admitted contest of settled finding "
                "(ticket=%s, file=%s, match=%s, recorded_at=%s)",
                ticket_id,
                af.finding.file,
                match.kind,
                match.entry.recorded_at,
            )
            continue
        matches[index] = match
    return matches


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


def suppress_adjudicated_findings(
    verdict: ReviewVerdict,
    ledger: dict[str, FindingDisposition],
    *,
    ticket_id: str,
    claim_tier_enabled: bool = False,
    reviewed_sha: str = "",
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

    ``claim_tier_enabled`` (#2210) arms the fuzzy second tier for THIS pass.
    It defaults to ``False``, which is the fail-safe floor: a call path that
    never threads it is off, and the exact tier behaves identically either
    way. With the gate closed, a claim-tier match leaves the verdict untouched
    (the same object is returned) and is recorded as a
    ``review.finding_claim_shadowed`` event instead — see :func:`_emit_shadow`
    and ADR-0016. ``reviewed_sha`` rides onto that shadow payload only, so a
    consumer can group a re-derived finding's events by ticket, file and
    summary and count the distinct reviewed commits behind them.
    """
    if not ledger:
        return verdict
    matches = _ledger_matches(verdict.accepted, ledger, ticket_id=ticket_id)
    if not matches:
        return verdict

    enforced: dict[int, _LedgerMatch] = {}
    for index, match in matches.items():
        if match.kind == _MATCH_EXACT or claim_tier_enabled:
            enforced[index] = match
        else:
            _emit_shadow(verdict.accepted[index], match, ticket_id, reviewed_sha)
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


def build_finding_disposition_ledger(
    entries: Iterable[tuple[str, str, FindingDisposition]],
) -> dict[str, FindingDisposition]:
    """Fold ``(file, summary, entry)`` triples into a keyed ledger (#2210).

    The producer-side twin of :func:`parse_finding_disposition_block`, and the
    seam ``cw review settle`` builds its postable marker through — which is
    what finally gives :func:`render_finding_disposition_block` a production
    caller. Keying goes through :func:`_disposition_key`, so a marker minted
    here and a finding re-derived later cannot disagree about identity.

    Raises ``ValueError`` for an un-keyable file rather than silently dropping
    the entry: an operator asking to settle a finding and getting a marker that
    quietly omits it is the worst of both outcomes. Duplicates fold through
    :func:`merge_finding_dispositions`, so the newest ``recorded_at`` wins.
    """
    ledger: dict[str, FindingDisposition] = {}
    for file, summary, entry in entries:
        key = _disposition_key(file, summary)
        if key is None:
            msg = f"cannot record a disposition for file={file!r}: no path to key on"
            raise ValueError(msg)
        ledger = merge_finding_dispositions(ledger, {key: entry})
    return ledger
