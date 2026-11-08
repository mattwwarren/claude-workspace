"""Review-debt tickets recorded on a dev-queue row (GitHub #3041, RFC 0017 D-7).

``cw review adjudicate`` files one debt ticket per ticketless ``defer`` entry
(``cw.review_debt_filing``), and the row keeps one :class:`ReviewDebtRecord`
per filed or emitted entry on :attr:`cw.models.TicketTask.review_debt`. The
list is the authoritative dedup store: a key recorded here is never filed
again. The ``<!-- cw-review-debt: <key> -->`` body marker on the ticket is the
recovery path when the row has no record (a fresh row, or a lost write).

Its own small module (the ``operator_constraint`` precedent) so
``models/tasks`` gains only the field. Not re-exported from ``cw.models``:
consumers import this module directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

#: How a record's ticket came to exist. ``filed``: cw created it through
#: ``gh``. ``emitted``: cw wrote a payload for an MCP tracker (or for a GitHub
#: client with ``auto_create_issues: false``) and the worker or operator files
#: it; ``ticket_id`` is set once ``cw review debt record`` verifies the
#: reported ID. ``commented``: reserved for #2111, which comments on an owning
#: ticket instead of filing; nothing in this release produces it.
ReviewDebtMode = Literal["filed", "commented", "emitted"]

#: The trackers debt filing supports; every other tracker skips filing.
ReviewDebtTracker = Literal["github-issues", "linear"]

FILED_MODE: ReviewDebtMode = "filed"
EMITTED_MODE: ReviewDebtMode = "emitted"


class ReviewDebtRecord(BaseModel):
    """One review-debt ticket recorded against a queue ticket.

    ``key`` is the entry's ``debt_key``. ``ticket_id``/``ticket_url`` are None
    only on an ``emitted`` record whose filed ID has not been reported yet.
    ``pr_url`` is set by ``cw review debt link-pr`` once the ticket links the
    PR that carried the deferral.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    key: str = Field(min_length=1)
    tracker: ReviewDebtTracker
    ticket_id: str | None
    ticket_url: str | None
    filed_at: datetime
    pr_url: str | None = None
    mode: ReviewDebtMode

    @model_validator(mode="after")
    def _filed_has_a_ticket(self) -> Self:
        """A ``filed`` record names the ticket cw created."""
        if self.mode == FILED_MODE and (
            self.ticket_id is None or self.ticket_url is None
        ):
            msg = "a filed review-debt record needs ticket_id and ticket_url"
            raise ValueError(msg)
        return self
