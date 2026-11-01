"""Collect and build the history of one ticket's review (#2790).

``collect_history_inputs`` does all the I/O, and ``build_review_history`` turns
what it read into a :class:`~cw.models.review_history.ReviewHistory` with no
I/O at all. Rendering lives in :mod:`cw.review_history_render`; the command is
``cw review history`` (:mod:`cw.cli.review.history`).

Read-only by construction: no new store, no state lock, no subprocess, no
event emitted. The reads are the converge ledger and its per-round verdict
files, the session state the caller passes in, and two reads of the event
inbox (each under the inbox's own short leaf lock, as every ``read_events``
caller). Every data problem becomes a ``notes`` entry; nothing here raises on
bad data.

Sources, in precedence order: the converge ledger at
``<worktree>/.cw/review-convergence.json`` (owned by this ticket), else the
newest session ``AUTO_DEV_RESULT`` of this row (its ``review`` block and
``review_round_<n>`` friction lines); the row's ``finding_dispositions`` are
always listed on their own.

Event attribution. A ticket id is unique per client, not globally, and the
event inbox is global, so an event is used only when it can be tied to this
row. A ``stage.entered`` event must name one of the row's sessions in its
``session_id``. A ``review.finding_voided`` event carries no session, client
or worktree, so content alone cannot prove it is this row's. It is shown only
when a row record corroborates its comment id: its finding is in this row's
ledger ``settled`` or ``finding_dispositions`` AND that record's rationale
starts with the producer's ``VOIDED_RATIONALE_PREFIX``, the event's operator
comment id and a space. A content match whose rationale is not the
voided-suppression text (an ordinary rejection) corroborates nothing and is
counted in ``notes`` as a content-only tie; a record naming another comment,
or no record at all, is counted as one that could not be tied. Every event of
the ticket id that is not shown is counted in ``notes`` and never rendered.

Package layout (a dependency order with no cycles): ``inputs`` (data carriers)
<- ``collect`` (the only I/O), ``ledger``, ``sentinel``, ``attribution`` <-
``build`` (assembly) <- this ``__init__`` (re-exports).
"""

from __future__ import annotations

from cw.review_history.build import build_review_history
from cw.review_history.collect import collect_history_inputs
from cw.review_history.inputs import HistoryInputs
from cw.review_history.sentinel import _parse_friction_lines

# ``_parse_friction_lines`` is private but re-exported so the test import site
# is unchanged, the same economy ``cw.review_adjudication`` applies to
# ``_fix_is_substantiated``.
__all__ = [
    "HistoryInputs",
    "_parse_friction_lines",
    "build_review_history",
    "collect_history_inputs",
]
