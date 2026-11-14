"""Same-round engine SHOULD_FIX defers recorded on a dev-queue row (GitHub #3625).

A review engine round whose gating findings are all deferrable SHOULD_FIX,
each settled ``defer`` by the operator or the adjudicator, is recorded here, one
:class:`ReviewEngineSettlement` per reviewed head, on
:attr:`cw.models.TicketTask.review_engine_settlements`. The review attestation
reads the list to reconcile that FAIL summary to PASS with the findings counted
as debt (:mod:`cw.dev_queue.review_engine_reconcile`).

Written only by cw's own CLI under ``dev_queue_lock``, never read from the
worktree. The reviewed branch cannot edit the file this lives in; that is the
whole guarantee, not that the branch cannot cause a write.

Its own small module (the ``review_debt_record`` precedent) so ``models/tasks``
gains only the field. Not re-exported from ``cw.models``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReviewEngineSettlement(BaseModel):
    """The deferred SHOULD_FIX fingerprints of one engine round at one head.

    ``head_sha`` is a plain ``str``: the writer validates the 40-hex shape and
    raises :class:`cw.exceptions.CwError`, so a malformed value never becomes a
    pydantic error on the seam that must never raise.
    """

    # NOT extra=forbid - persisted/runtime state, see #1200
    head_sha: str
    fingerprints: list[str]
    recorded_at: datetime
