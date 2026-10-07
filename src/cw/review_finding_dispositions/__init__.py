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

**Import discipline — load-bearing, not style.** No file in this package may
import anything from ``cw`` at module scope EXCEPT :mod:`cw.review_markers`,
which imports nothing from ``cw`` at all and so cannot be part of any cycle,
and its own sibling submodules by their direct path
(``cw.review_finding_dispositions.<submodule>``, never through this package).
``cw.models.tasks`` imports :class:`FindingDisposition` from here, so any other
runtime ``cw.*`` import at module scope closes a cycle through ``cw.models``'
package ``__init__``: the shortest one is ``cw.review_findings ->
cw.auto_dev_result.schema -> cw.models -> cw.models.tasks -> (this package) ->
cw.review_debt -> cw.review_findings``, which raises ``ImportError`` on a
partially initialized ``cw.review_findings`` whenever ``cw.review_findings`` is
the first of the two to be imported. Every other ``cw`` import in the package
therefore lives either under ``TYPE_CHECKING`` (erased at runtime) or inside a
function body (resolved after every module has finished loading) — today in
``model``, ``drift``, ``emit`` and ``suppress``.
``tests/test_review_finding_dispositions_package.py`` pins this with an AST
check of every package file's module-scope imports (plus cold-interpreter
import smoke tests), and ``tests/test_review_markers.py`` pins the leaf
module's own emptiness.

This package was split out of a single ``review_finding_dispositions.py``
module (#2498); the import surface (``from cw.review_finding_dispositions
import X``) is preserved here via re-exports, and every submodule logs on the
historic ``cw.review_finding_dispositions`` logger (``_constants._LOGGER_NAME``),
never its own ``__name__``. The two names tests patch, ``cw.events.record_event``
and ``cw._git.run_git``, are imported inside the body of the function that
calls them, so a patch on their source module reaches every reader.
Submodules:

- ``_constants`` — ``_LOGGER_NAME`` and the verdict/match vocabulary
  ``match`` and ``suppress`` share; imports nothing.
- ``model`` — :class:`FindingDisposition`, :data:`Outcome` and ``REVERSED``,
  and the ledger-key arithmetic: ``_disposition_key`` and
  :func:`split_disposition_key`.
- ``drift`` — :func:`disposition_drifted`, the #2232 staleness predicate.
- ``provenance`` — the #2210 provenance gate: the one write chokepoint
  (:func:`merge_finding_dispositions`), the reader
  (:func:`partition_enforceable_dispositions`) and
  :func:`log_refused_dispositions`.
- ``match`` — resolving findings against the ledger, exact tier first, then
  the gated #2210 claim tier.
- ``emit`` — every audit event and log line a disposition produces, and the
  shared :func:`disposition_event_type`/:func:`disposition_event_payload`.
- ``parse`` — the ticket-comment marker:
  :func:`render_finding_disposition_block`,
  :func:`parse_finding_disposition_block` and the producer-side
  :func:`build_finding_disposition_ledger`.
- ``suppress`` — :func:`suppress_adjudicated_findings`, the mechanical
  backstop where every other submodule meets.

Public surface: :class:`FindingDisposition`, :data:`Outcome`, ``REVERSED``,
:func:`build_finding_disposition_ledger`,
:func:`render_finding_disposition_block`,
:func:`parse_finding_disposition_block`,
:func:`partition_enforceable_dispositions`, :func:`log_refused_dispositions`,
:func:`merge_finding_dispositions`, :func:`split_disposition_key`,
:func:`suppress_adjudicated_findings`, :func:`disposition_drifted`,
:func:`disposition_event_type`, :func:`disposition_event_payload`. The private
``_disposition_key`` is re-exported too, because tests outside this package
import it; every other private name lives in, and is imported from, its own
submodule.

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
