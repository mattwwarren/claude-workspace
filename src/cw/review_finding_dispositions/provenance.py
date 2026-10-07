"""The provenance gate every ledger read and write goes through (#2210).

#2210's round-2 review found that every guard the settle path added lived in
the WRITER, and the reader honoured any well-formed block it was handed —
including one a dispatch worker could write into a ticket comment itself.
:func:`partition_enforceable_dispositions` moves the contract to the reader: a
record is applied only when it carries the full provenance set, and anything
short of it is ignored, logged (:func:`log_refused_dispositions`), and
reported on the comment.

Round 3 finished the job on the WRITE side: ignoring an invalid record is not
enough if it can still replace a valid one, so :func:`merge_finding_dispositions`
— the one chokepoint every ledger write goes through — never writes a record
that fails provenance, and a record whose key does not bind its own verbatim
summary fails it. Validate first, write second. Split out of the flat
``review_finding_dispositions.py`` (#2498).

Imports nothing from ``cw`` at module scope beyond :mod:`cw.review_markers`
and this package's own submodules (see the package docstring's "Import
discipline" section).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from cw.review_finding_dispositions._constants import _LOGGER_NAME
from cw.review_finding_dispositions.model import (
    _disposition_key,
    split_disposition_key,
)
from cw.review_markers import RefusedDisposition

if TYPE_CHECKING:
    from cw.review_finding_dispositions.model import FindingDisposition

_log = logging.getLogger(_LOGGER_NAME)


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
