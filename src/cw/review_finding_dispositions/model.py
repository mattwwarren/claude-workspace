"""The disposition record model and its ledger-key arithmetic (#1838, #2210).

:class:`FindingDisposition` is one operator decision about one finding, and
:data:`Outcome` is the vocabulary that decision is recorded in.
:func:`_disposition_key` is the key every holder of a record files it under —
``file::normalized_summary::sha256(verbatim summary)`` — and
:func:`split_disposition_key` is its inverse. Split out of the flat
``review_finding_dispositions.py`` (#2498); the identity design (why the key
starts from #1837's fingerprint, binds the verbatim summary, and carries no
evidence, severity or sha) is documented on the package and on
:func:`_disposition_key` itself.

Imports nothing from ``cw`` at module scope (see the package docstring's
"Import discipline" section): :func:`cw.review_debt.fingerprint_v1` is
imported inside :func:`_disposition_key`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel

#: The three decisions an operator can record about a finding (#1838 R2,
#: #2232). Only ``"REJECTED"`` participates in mechanical suppression;
#: ``"ACCEPTED"`` is a record-only annotation that reaches the prompt and
#: changes no gate. ``"REVERSED"`` (#2232) WITHDRAWS a prior ``cw review
#: settle`` entry for the same identity: it matches neither the exact nor the
#: claim tier, and — unlike ``"ACCEPTED"`` — is not shown to the reviewer as a
#: decision, because it is the absence of one. The newest-``recorded_at``-wins
#: merge in :func:`merge_finding_dispositions` is what makes it durable, so
#: rollback needs no second write path.
Outcome = Literal["ACCEPTED", "REJECTED", "REVERSED"]

_REJECTED: Outcome = "REJECTED"
#: Withdrawn, so it is never rendered into the reviewer's binding "previously
#: adjudicated" block — see ``codex_review._context._prompt_render``.
#:
#: PUBLIC, unlike its siblings (#2232). Three modules outside this one have to
#: recognise a withdrawal — the prompt renderer that must not show it, the
#: settle command that routes it to a distinct audit event, and the review
#: pass that audits one arriving through the comment thread — and the choice
#: is between one exported constant and either a raw ``"REVERSED"`` literal or
#: an underscore-prefixed cross-module import. ``_REJECTED`` stays private
#: because nothing outside this module tests for it.
REVERSED: Outcome = "REVERSED"

#: Joins the parts of a ledger key — ``file``, the normalized summary and the
#: verbatim-summary digest (see :func:`_disposition_key`) — into the string a
#: JSON object key has to be. A file path containing this sequence could in
#: principle collide with another entry — the same exact-match-only false-merge
#: review_debt already documents and accepts for the underlying fingerprint,
#: not a new class of risk. A NORMALIZED summary may contain it too, which is
#: why :func:`split_disposition_key` recognises the digest by its fixed shape
#: rather than by position.
_KEY_SEPARATOR = "::"

#: The trailing ``::<64 lowercase hex>`` of a current-shape ledger key: the
#: full SHA-256 hex digest, in full, because a truncated digest buys nothing
#: here (keys are JSON object keys, not display strings) and a full one makes a
#: collision between two different summaries negligible. Anchored and
#: fixed-width so a normalized summary that ends in something hex-like, or one
#: containing ``::`` of its own, is never mistaken for a digest.
_DIGEST_SUFFIX_RE = re.compile(rf"{_KEY_SEPARATOR}[0-9a-f]{{64}}$")


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
    still loads unchanged. Round 2 withdrew the other half of that reasoning:
    a hand-authored marker is no longer *legal* to act on — it still parses,
    but the reader refuses to apply it. See
    :func:`partition_enforceable_dispositions`.

    ``summary`` is the VERBATIM finding summary, and it is BOUND to the key: the
    key ends in a SHA-256 digest of exactly this text (see
    :func:`_disposition_key`), and :func:`_provenance_gaps` recomputes the key
    from it and refuses a record whose key was not minted from it. So a record
    can only ever be applied to the finding whose verbatim summary it carries,
    and the same text lets a future per-record rollback target exactly one
    entry. (Round 2 said the opposite — that ``summary`` was "deliberately not
    part of identity" — and the key then carried only the LOSSY normalized
    half, shared by every rewording that normalises alike, so a finding could
    drift onto a different finding's record.)

    Every field stays optional at the MODEL layer so a pre-#2210 marker or
    queue row still *loads*. Loading is not applying: whether a record may be
    acted on is decided by :func:`partition_enforceable_dispositions`, at the
    reader, on every consumption path.
    """

    outcome: Outcome
    rationale: str = ""
    recorded_at: str = ""
    actor: str = ""
    reviewed_sha: str = ""
    summary: str = ""


def _summary_digest(summary: str) -> str:
    """Full SHA-256 hex digest of *summary*'s exact text (#2210 round 3).

    VERBATIM: no normalization, no ``strip``, no case folding. The conservative
    direction is a non-match the operator can re-settle; the direction to
    refuse is a silent match against a finding nobody adjudicated. The
    ``surrogatepass`` handler keeps a lone surrogate — which ``json.loads``
    happily produces from a hand-pasted marker — from raising inside the
    reader; it hashes to a stable digest like any other text.
    """
    return hashlib.sha256(summary.encode("utf-8", errors="surrogatepass")).hexdigest()


def _disposition_key(file: str, summary: str) -> str | None:
    """The ledger key for a finding, or ``None`` when it cannot be keyed.

    ``file::normalized_summary::sha256(verbatim summary)``. The first two parts
    are :func:`cw.review_debt.fingerprint_v1` (#1838 R1) — the one
    normalization implementation, imported rather than re-written, so the
    ledger's notion of "same file, same claim after normalizing" is #1837's.
    The key EXTENDS that fingerprint rather than equalling it (the debt ledger
    keeps its own key), and the extension is the point (#2210 round 3): the
    normalized half is lossy and shared by every rewording that normalises
    alike, so a key made of it alone let a finding drift onto a different
    finding's record — a silent suppression nobody adjudicated. The digest
    makes the exact tier match a byte-identical summary only. The fuzzy claim
    tier stays the ONLY path that may match non-identical text.

    The reviewed sha is deliberately NOT part of the key, though it is a
    required provenance field of the record. The reviewer re-raises a settled
    finding on a LATER commit, so a key that included the sha it was settled at
    would stop matching after any fix commit and the whole ledger would go
    dead — defeating what #1814/#2210 exist for. The sha rides on the record
    and on the audit events instead. See ADR-0016.

    ``None`` for a ``file="N/A"`` finding (#1817's no-diff-anchor case): there
    is no path to key on, so it gets no cross-round memory. Mirrors
    ``promote_debt_finding``'s own ``if fingerprint is None: return None``
    short-circuit.
    """
    from cw.review_debt import fingerprint_v1

    fingerprint = fingerprint_v1(file, summary)
    if fingerprint is None:
        return None
    return _KEY_SEPARATOR.join((*fingerprint, _summary_digest(summary)))


def split_disposition_key(key: str) -> tuple[str, str]:
    """Recover ``(file, normalized_summary)`` from a ledger *key*.

    The inverse of :func:`_disposition_key`'s join, exposed because the prompt
    renderer needs the file and summary back out to write a readable line, and
    the claim tier compares the normalized halves. The verbatim digest is
    dropped: it is recognised by its fixed ``::<64 hex>`` tail and a key
    without one is treated as digest-less (a legacy key, which
    :func:`_provenance_gaps` refuses). Past the digest it splits on the FIRST
    separator, so a normalized summary that happens to contain one stays
    intact.
    """
    body = _DIGEST_SUFFIX_RE.sub("", key, count=1)
    file, _, summary = body.partition(_KEY_SEPARATOR)
    return file, summary
