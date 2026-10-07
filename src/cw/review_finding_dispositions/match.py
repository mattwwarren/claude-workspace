"""Resolving accepted findings against the ledger: exact tier, then claim tier.

The exact tier matches a byte-identical finding through its ledger key. #2210
adds a second, fuzzy matching tier to the backstop — see
:func:`_claim_similarity` and ADR-0016. It ships **gated per lane and off**:
while the gate is closed, every would-be claim-tier suppression is recorded as
a ``review.finding_claim_shadowed`` event instead of being applied, so the
matcher can be measured on real rewordings before anyone arms it. The exact
tier is unaffected by the gate in either direction. Split out of the flat
``review_finding_dispositions.py`` (#2498).

Imports nothing from ``cw`` at module scope beyond this package's own
submodules (see the package docstring's "Import discipline" section).
"""

from __future__ import annotations

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
from cw.review_finding_dispositions.model import (
    _REJECTED,
    FindingDisposition,
    _disposition_key,
    split_disposition_key,
)

if TYPE_CHECKING:
    from cw.review_findings import AcceptedFinding

_log = logging.getLogger(_LOGGER_NAME)

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
