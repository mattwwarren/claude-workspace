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

from cw.review_finding_dispositions.drift import disposition_drifted
from cw.review_finding_dispositions.emit import (
    disposition_event_payload,
    disposition_event_type,
)
from cw.review_finding_dispositions.model import (
    REVERSED,
    FindingDisposition,
    Outcome,
    _disposition_key,
    split_disposition_key,
)
from cw.review_finding_dispositions.parse import (
    build_finding_disposition_ledger,
    parse_finding_disposition_block,
    render_finding_disposition_block,
)
from cw.review_finding_dispositions.provenance import (
    log_refused_dispositions,
    merge_finding_dispositions,
    partition_enforceable_dispositions,
)
from cw.review_finding_dispositions.suppress import suppress_adjudicated_findings

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
