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

Identity starts from #1837's :func:`cw.review_debt.fingerprint_v1` — ``(file,
normalized_summary)``, with **no evidence and no severity** — and (#2210 round
3) adds a digest of the VERBATIM summary, so the exact tier matches a
byte-identical finding only. That is a deliberate divergence from
``_voided_fingerprint``: an evidence-anchored identity lapses the moment the
code moves, which is exactly the memory loss this ticket exists to remove. The
cost — a suppression that outlives the code it was granted for — is paid down by
making every suppression VISIBLE rather than by adding an expiry: see
:func:`_render_suppression_signal` and the
``review.finding_disposition_suppressed`` event.

Only the already-declared-shared ``review_findings`` types
(:class:`~cw.review_findings.AcceptedFinding`,
:class:`~cw.review_findings.ReviewVerdict`) are reused. Nothing here imports or
extends :func:`cw.review_adjudication.apply_adjudication`,
:class:`~cw.review_adjudication.Adjudication`, or the ``"defer"`` outcome whose
two meanings are why those seams must stay apart.

**Import discipline — load-bearing, not style.** This module MUST NOT import
anything from ``cw`` at module scope EXCEPT :mod:`cw.review_markers`, which
imports nothing from ``cw`` at all and so cannot be part of any cycle.
``cw.models.tasks`` imports :class:`FindingDisposition` from here, so any other
runtime ``cw.*`` import at module scope closes a cycle through ``cw.models``'
package ``__init__``: the shortest one is ``cw.review_findings ->
cw.auto_dev_result.schema -> cw.models -> cw.models.tasks -> (this module) ->
cw.review_debt -> cw.review_findings``, which raises ``ImportError`` on a
partially initialized ``cw.review_findings`` whenever ``cw.review_findings`` is
the first of the two to be imported. Every other ``cw`` import below therefore
lives either under ``TYPE_CHECKING`` (erased at runtime) or inside a function
body (resolved after every module has finished loading).
``tests/test_review_finding_dispositions.py`` pins this by importing the module
standalone in a subprocess, and ``tests/test_review_markers.py`` pins the leaf
module's own emptiness.

#2210 adds a second, fuzzy matching tier to the backstop below — see
:func:`_claim_similarity` and ADR-0016. It ships **gated per lane and off**:
while the gate is closed, every would-be claim-tier suppression is recorded as
a ``review.finding_claim_shadowed`` event instead of being applied, so the
matcher can be measured on real rewordings before anyone arms it. The exact
tier above is unaffected by the gate in either direction.

#2210's round-2 review found the other half of that risk: every guard the
settle path added lives in the WRITER, and the reader honoured any well-formed
block it was handed — including one a dispatch worker could write into a ticket
comment itself. :func:`partition_enforceable_dispositions` moves the contract
to the reader: a record is applied only when it carries the full provenance
set, and anything short of it is ignored, logged, and reported on the comment.

Round 3 finished the job on the WRITE side: ignoring an invalid record is not
enough if it can still replace a valid one, so :func:`merge_finding_dispositions`
— the one chokepoint every ledger write goes through — never writes a record
that fails provenance, and a record whose key does not bind its own verbatim
summary fails it. Validate first, write second.

Round 4 closed the layer underneath both of those: a record was recognised by
SHAPE alone, so a model-authored finding summary carrying a well-formed
sentinel block minted a suppression nobody authored, and the provenance checks
above were the only thing standing in front of it. Two independent layers now
sit in front of them — the renderer escapes marker syntax out of every piece of
untrusted text it interpolates (:func:`cw.review_markers.neutralise_marker_syntax`),
and :data:`_DISPOSITION_BLOCK_RE` honours a block only at the structural
POSITION :func:`render_finding_disposition_block` emits it at. Position, not
just shape, is what makes a record.

Public surface: :class:`FindingDisposition`, :data:`Outcome`,
:func:`build_finding_disposition_ledger`,
:func:`render_finding_disposition_block`,
:func:`parse_finding_disposition_block`,
:func:`partition_enforceable_dispositions`, :func:`log_refused_dispositions`,
:func:`merge_finding_dispositions`, :func:`split_disposition_key`,
:func:`suppress_adjudicated_findings`, :func:`disposition_drifted`.

#2232 closes the two gaps ADR-0016's Consequences section named as
preconditions for ever arming the claim tier. Rollback is a third ``Outcome``
value, ``"REVERSED"``, reusing ``cw review settle`` as its producer so the
ledger keeps its one write chokepoint. Staleness is
:func:`disposition_drifted` plus ``ReviewVerdict.stale_dispositions``: a record
whose code moved since it was settled stops being APPLIED and is reported,
rather than being expired — the same "make it visible instead of adding an
expiry" choice this module's identity design already made.

The marker's own vocabulary —
:data:`~cw.review_markers.DISPOSITION_SENTINEL`,
:data:`~cw.review_markers.SETTLE_SECTION_HEADING` and
:class:`~cw.review_markers.RefusedDisposition` — belongs to
:mod:`cw.review_markers`; import it from there.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, NamedTuple

from cw.review_finding_dispositions._constants import (
    _FIXED,
    _LOGGER_NAME,
    _MATCH_CLAIM,
    _MATCH_EXACT,
    _MUST_FIX,
)
from cw.review_finding_dispositions.drift import disposition_drifted
from cw.review_finding_dispositions.model import (
    _REJECTED,
    REVERSED,
    FindingDisposition,
    Outcome,
    _disposition_key,
    split_disposition_key,
)
from cw.review_finding_dispositions.provenance import (
    _provenance_gaps,
    log_refused_dispositions,
    merge_finding_dispositions,
    partition_enforceable_dispositions,
)
from cw.review_markers import (
    DISPOSITION_SENTINEL,
    RefusedDisposition,
    StaleDisposition,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.models.enums import OrchestratorEventType
    from cw.review_findings import AcceptedFinding, ReviewVerdict

__all__ = [
    "REVERSED",
    "FindingDisposition",
    "Outcome",
    "_disposition_key",
    "build_finding_disposition_ledger",
    "disposition_drifted",
    "disposition_event_payload",
    "disposition_event_type",
    "log_refused_dispositions",
    "merge_finding_dispositions",
    "parse_finding_disposition_block",
    "partition_enforceable_dispositions",
    "render_finding_disposition_block",
    "split_disposition_key",
    "suppress_adjudicated_findings",
]

_log = logging.getLogger(_LOGGER_NAME)

#: Ticket-comment header the ledger block renders under, and the sentinel that
#: is the actual contract. Mirrors ``_VOIDED_MD_TITLE``/``_VOIDED_SENTINEL``
#: (#1814) — a JSON payload inside an HTML comment, mechanically parsed. NOT
#: ``auto-dev-preflight-resolutions``' free-prose grammar, which would need an
#: LLM to read and would reintroduce the fragility class #1805 removed.
_DISPOSITION_MD_TITLE = "## Review Finding Dispositions"
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

#: A disposition record is recognised by POSITION as well as by shape (#2210
#: round 4). A comment carries a record only when it IS the marker: the
#: ``## Review Finding Dispositions`` title opens the body, and the sentinel
#: block follows it with nothing but whitespace between. That is byte for byte
#: what :func:`render_finding_disposition_block` produces and what ``cw review
#: settle --out`` hands the operator to post, including through
#: :func:`cw.gh.post_issue_comment`, which APPENDS its provenance marker and so
#: never displaces the title.
#:
#: The layer this adds is independent of the provenance checks below, and the
#: hole it closes is not hypothetical. The pipeline renders model-authored text
#: — a finding summary, a file path, quoted evidence — into the very comments
#: this parser reads on the next round, so a shape-only reader could not tell a
#: record an operator minted from one a REVIEWER wrote into its own finding
#: text. Rendered finding text is never at this position: it sits under the
#: verdict comment's own ``## Codex Review Verdict`` title, many lines in. The
#: renderer separately escapes the sentinel and the comment delimiters out of
#: every untrusted span (:func:`cw.review_markers.neutralise_marker_syntax`),
#: so an injection has to defeat both layers; the cost of this one is a single
#: anchored regex.
#:
#: ``\A`` (not ``^``): a title on some later line of a longer body does not
#: qualify, which is the whole point — ``re.MULTILINE`` would hand the
#: injection exactly the foothold this removes. One marker per comment; the
#: ledger's additive union across COMMENTS is unchanged and is how an operator
#: settles more findings later.
_DISPOSITION_BLOCK_RE = re.compile(
    r"\A[ \t\r\n]*"
    + re.escape(_DISPOSITION_MD_TITLE)
    + r"[ \t\r]*\n\s*"
    + rf"<!--\s*{DISPOSITION_SENTINEL}\s*(?P<body>.*?)"
    + rf"\s*{DISPOSITION_SENTINEL}\s*-->",
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
        f"<!-- {DISPOSITION_SENTINEL}\n{body}\n{DISPOSITION_SENTINEL} -->\n"
    )


def _parse_one_disposition_block(body: str) -> dict[str, FindingDisposition]:
    """Parse one sentinel body, degrading a malformed block to ``{}``."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _log.warning("auto-dev: ignoring malformed %s block", DISPOSITION_SENTINEL)
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
            _log.warning("auto-dev: ignoring malformed %s entry", DISPOSITION_SENTINEL)
    return entries


def parse_finding_disposition_block(
    comment_bodies: list[str],
) -> tuple[dict[str, FindingDisposition], list[RefusedDisposition]]:
    """Union every disposition sentinel across *comment_bodies*.

    Returns ``(enforceable, refused)``. Only a record that passes provenance
    reaches ``enforceable`` (#2210 round 3: validate first, write second — this
    is what the ledger is written FROM, so an invalid record must not be in
    it). Every record that fails is reported in ``refused`` instead, once per
    key, in key order, so the caller can log it and put it on the review
    output rather than losing it silently. Validation happens BEFORE the
    cross-comment fold, so a later invalid comment can never displace an
    earlier valid one for the same key.

    Fail-open throughout — a missing, truncated, or malformed block yields
    nothing and never raises, and one bad block never discards a good sibling.
    Same degrade contract, and same justification, as
    :func:`cw.review_adjudication.parse_voided_findings_block`: a review that
    could not read the ledger is strictly better than no review, and the missed
    suppression surfaces as the finding re-appearing, which an operator can act
    on.

    A key recorded validly in more than one comment resolves through
    :func:`merge_finding_dispositions` (newest ``recorded_at`` wins), which is
    the #1654 marker-supersession convention: an operator who changes their
    mind re-posts the marker rather than editing history.

    **Position is part of the grammar** (#2210 round 4): a body is read for a
    record only when it IS the marker — see :data:`_DISPOSITION_BLOCK_RE`. A
    sentinel block sitting inside rendered finding text, inside a fenced
    payload, or anywhere below other content is not a record and is not
    reported as a refusal either: nothing tried to settle anything, so there is
    no operator-facing refusal to raise.
    """
    merged: dict[str, FindingDisposition] = {}
    refused: dict[str, RefusedDisposition] = {}
    for body in comment_bodies:
        match = _DISPOSITION_BLOCK_RE.match(body)
        if match is None:
            continue
        enforceable, rejected = partition_enforceable_dispositions(
            _parse_one_disposition_block(match.group("body"))
        )
        merged = merge_finding_dispositions(merged, enforceable)
        for record in rejected:
            refused.setdefault(record.key, record)
    return merged, sorted(refused.values(), key=lambda record: record.key)


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

    "Exact" means BYTE-identical summary (#2210 round 3): the key carries a
    digest of the verbatim text, so a finding that merely normalizes like a
    recorded one misses ``ledger.get`` and falls through to the claim tier,
    which is the only path allowed to match non-identical text — and which is
    gated off and shadowed by default. A key can never reach
    :func:`_best_claim_match` while sitting in the ledger itself, because the
    exact lookup above has already decided that case either way.

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
    quietly omits it is the worst of both outcomes. It raises for an entry that
    fails provenance for the same reason: :func:`merge_finding_dispositions`
    would refuse to write it, and ``cw review settle`` always supplies the full
    set, so a refusal here is a bug to surface, not a record to drop.
    Duplicates fold through :func:`merge_finding_dispositions`, so the newest
    ``recorded_at`` wins.
    """
    ledger: dict[str, FindingDisposition] = {}
    for file, summary, entry in entries:
        key = _disposition_key(file, summary)
        if key is None:
            msg = f"cannot record a disposition for file={file!r}: no path to key on"
            raise ValueError(msg)
        gaps = _provenance_gaps(key, entry)
        if gaps:
            msg = (
                f"cannot record a disposition for file={file!r}: it fails "
                f"provenance (missing {', '.join(gaps)})"
            )
            raise ValueError(msg)
        ledger = merge_finding_dispositions(ledger, {key: entry})
    return ledger
