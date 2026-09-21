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

Public surface: :class:`FindingDisposition`, :class:`RefusedDisposition`,
:data:`Outcome`, :data:`SETTLE_SECTION_HEADING`,
:func:`build_finding_disposition_ledger`,
:func:`render_finding_disposition_block`,
:func:`parse_finding_disposition_block`,
:func:`partition_enforceable_dispositions`, :func:`log_refused_dispositions`,
:func:`merge_finding_dispositions`, :func:`split_disposition_key`,
:func:`suppress_adjudicated_findings`, :data:`DISPOSITION_SENTINEL`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel, Field

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

#: Ticket-comment header the ledger block renders under, and the sentinel that
#: is the actual contract. Mirrors ``_VOIDED_MD_TITLE``/``_VOIDED_SENTINEL``
#: (#1814) — a JSON payload inside an HTML comment, mechanically parsed. NOT
#: ``auto-dev-preflight-resolutions``' free-prose grammar, which would need an
#: LLM to read and would reintroduce the fragility class #1805 removed.
_DISPOSITION_MD_TITLE = "## Review Finding Dispositions"
#: The marker's sentinel. Public and owned HERE, next to the parser that keys on
#: it, because other modules must agree with it byte for byte: the reviewer
#: prompt (``codex_review._context._prompt_text``) and the blocking comment
#: (``codex_review._verdict._render``) both NAME it when they tell a reader the
#: hand-authored block is unsupported. A second spelling of a string a parser
#: keys on is how the writer and reader drift apart in a later change, so those
#: modules build their text from this constant instead of retyping it. Like
#: :data:`SETTLE_SECTION_HEADING`, a plain string needs no ``cw`` import.
DISPOSITION_SENTINEL = "REVIEW-FINDING-DISPOSITIONS"
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
    rf"<!--\s*{DISPOSITION_SENTINEL}\s*(?P<body>.*?)\s*{DISPOSITION_SENTINEL}\s*-->",
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


class RefusedDisposition(BaseModel):
    """One ledger record the reader refused to apply, and why (#2210 round 2).

    Carried on ``ReviewVerdict.refused_dispositions`` and rendered onto the
    posted review comment, because "ignored" must not mean "invisible": an
    operator has to be able to see that something tried to suppress a finding
    and was refused. ``key`` is the ledger key
    (``file::normalized summary::digest`` — or the digest-less legacy shape,
    which is itself one of the things refused),
    ``missing`` the provenance fields it could not produce, in the fixed order
    :func:`_provenance_gaps` checks them.

    Lives here rather than in :mod:`cw.review_findings` — beside the contract
    it enforces, and importable by :mod:`cw.models.tasks`' own importer
    without an import-cycle exemption (this module still imports nothing from
    ``cw`` at module scope).
    """

    key: str
    missing: list[str] = Field(default_factory=list)


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
    """
    merged: dict[str, FindingDisposition] = {}
    refused: dict[str, RefusedDisposition] = {}
    for body in comment_bodies:
        for match in _DISPOSITION_BLOCK_RE.finditer(body):
            enforceable, rejected = partition_enforceable_dispositions(
                _parse_one_disposition_block(match.group("body"))
            )
            merged = merge_finding_dispositions(merged, enforceable)
            for record in rejected:
                refused.setdefault(record.key, record)
    return merged, sorted(refused.values(), key=lambda record: record.key)


def merge_finding_dispositions(
    existing: dict[str, FindingDisposition], parsed: dict[str, FindingDisposition]
) -> dict[str, FindingDisposition]:
    """Fold *parsed* into *existing*, newest-``recorded_at``-wins, additively.

    Additive is the whole point (#1838 R3, forward-only): a key present in
    *existing* but absent from *parsed* is PRESERVED. The ledger is durable
    memory, not a mirror of whatever the current comment thread happens to
    say — clearing it on absence would re-open every finding the moment a
    comment was edited or a fetch degraded to ``[]``.

    **Validate first, write second** (#2210 round 3), and it is enforced HERE
    because this is the one chokepoint every ledger write goes through: the
    thread-derived merge, the dev-queue row sync, and ``cw review settle``. A
    *parsed* entry that fails :func:`_provenance_gaps` is never written — it
    can neither add a key nor replace an existing entry. Ignoring it for
    suppression (the reader's job) is not enough if it can still overwrite a
    valid entry: a malformed or hand-pasted block would destroy the provenance
    of a legitimately settled finding, on the very ledger that decides what
    stays suppressed. Only a VALID entry may replace an entry for the same key,
    and between two valid ones the newest ``recorded_at`` still wins. A valid
    entry also replaces an INVALID one already in *existing* (legacy history
    that applies nothing) whatever the timestamps say — that is a heal, not an
    eviction.

    Entries already in *existing* pass through untouched: legacy rows stay for
    the reader to keep refusing and reporting. Neither argument is mutated; a
    fresh dict is returned. Reporting a rejected entry is the caller's:
    :func:`parse_finding_disposition_block` returns it in its ``refused`` list.
    """
    merged = dict(existing)
    for key, entry in parsed.items():
        if _provenance_gaps(key, entry):
            continue
        current = merged.get(key)
        if (
            current is None
            or _provenance_gaps(key, current)
            or entry.recorded_at >= current.recorded_at
        ):
            merged[key] = entry
    return merged


def _is_utc_timestamp(value: str) -> bool:
    """Whether *value* is an ISO-8601 instant expressed in UTC (#2210 round 2).

    ``cw review settle`` stamps ``%Y-%m-%dT%H:%M:%SZ`` off its own clock, so a
    record that cannot be parsed back to a UTC instant did not come from it. A
    naive or offset stamp is refused rather than coerced: "when was this
    silenced" is an audit question, and an answer nobody can place on a
    timeline is not one.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _identity_is_bound(file: str, normalized: str, key: str, summary: str) -> bool:
    """Whether *key* is the key :func:`_disposition_key` mints for *summary*.

    A blank half is not an identity at all. Otherwise the key is recomputed
    from its own file and the record's VERBATIM summary and compared for
    equality, which pins three things at once: the digest is present, it is
    the digest of THIS summary, and the normalized half is that summary's
    normalization. A record whose payload summary is not the one its key was
    minted from — or whose key is a digest-less legacy one — cannot pass, and
    so cannot drift onto a different finding.

    The comparison is skipped for a blank summary: the ``summary`` gap already
    names that record's problem, and reporting the same defect twice would
    only make the refusal line harder to read.
    """
    if not (file.strip() and normalized.strip()):
        return False
    if not summary.strip():
        return True
    return _disposition_key(file, summary) == key


def _provenance_gaps(key: str, entry: FindingDisposition) -> list[str]:
    """Which parts of the audit record *entry* cannot produce (#2210 round 2).

    Empty means the record answers all five questions an applied suppression
    must answer: WHICH finding (the key's file and normalized summary, BOUND to
    the record's verbatim ``summary`` through the key's digest — #2210 round
    3), WHO settled it, WHEN, against WHAT code, and WHY. Order is fixed so the
    rendered line and the log message read the same way every time.
    """
    file, normalized = split_disposition_key(key)
    checks = (
        ("identity", _identity_is_bound(file, normalized, key, entry.summary)),
        ("actor", bool(entry.actor.strip())),
        ("recorded_at", _is_utc_timestamp(entry.recorded_at)),
        ("reviewed_sha", bool(entry.reviewed_sha.strip())),
        ("rationale", bool(entry.rationale.strip())),
        ("summary", bool(entry.summary.strip())),
    )
    return [name for name, satisfied in checks if not satisfied]


def partition_enforceable_dispositions(
    ledger: dict[str, FindingDisposition],
) -> tuple[dict[str, FindingDisposition], list[RefusedDisposition]]:
    """Split *ledger* into records that may be applied, and refusals (#2210).

    **The contract is enforced at the READER, and this is that reader.**
    Round 1 put every guard on the minting side — the mandatory ``--reason``,
    the resolved actor, the CLI-stamped clock, the refusal inside a DAEMON
    worker — all of which live in ``cw review settle``. A marker pasted by
    hand, or one a worker writes into a ticket comment itself, never passes
    through any of them, so a writer-only contract enforces nothing. Every
    consumption path (the reviewer prompt's binding "previously adjudicated"
    block, and :func:`suppress_adjudicated_findings`' mechanical backstop)
    goes through here instead, so no path can apply an under-provenanced
    record.

    Deliberately NOT a validator on :class:`FindingDisposition`: a record must
    still LOAD — a pre-#2210 marker, and every persisted queue row written
    before the provenance fields existed, are legitimate history and must not
    raise on parse. They simply may not be *acted on*. The same goes for an
    ``ACCEPTED`` entry, which suppresses nothing mechanically but reaches the
    reviewer as a binding decision, and is refused on the same terms.

    Logging is the caller's, not this function's: several readers run per
    pass, and one WARNING per refused record per pass is the signal — one per
    record per *reader* is noise.
    """
    enforceable: dict[str, FindingDisposition] = {}
    refused: list[RefusedDisposition] = []
    for key, entry in sorted(ledger.items()):
        gaps = _provenance_gaps(key, entry)
        if gaps:
            refused.append(RefusedDisposition(key=key, missing=gaps))
            continue
        enforceable[key] = entry
    return enforceable, refused


def log_refused_dispositions(refused: list[RefusedDisposition], ticket_id: str) -> None:
    """Say out loud, once per record, that a disposition was refused.

    Public because two places refuse: :func:`suppress_adjudicated_findings`
    (legacy rows already in the durable ledger) and the write path in
    ``codex_review._context`` (records parsed off the ticket thread, which
    never reach the ledger at all). Whoever refuses logs; a caller that has
    already logged a record passes it back through ``refused=`` so it is not
    logged a second time.
    """
    for record in refused:
        _log.warning(
            "auto-dev: refusing a finding disposition with incomplete or "
            "unbound provenance -- NOT applied as a suppression and NOT "
            "written to the ledger (ticket=%s, key=%s, missing=%s). "
            "`cw review settle` is the only supported producer of a "
            "disposition marker; hand-authored blocks are unsupported.",
            ticket_id,
            record.key,
            ", ".join(record.missing),
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


def suppress_adjudicated_findings(
    verdict: ReviewVerdict,
    ledger: dict[str, FindingDisposition],
    *,
    ticket_id: str,
    claim_tier_enabled: bool = False,
    reviewed_sha: str = "",
    refused: list[RefusedDisposition] | None = None,
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
