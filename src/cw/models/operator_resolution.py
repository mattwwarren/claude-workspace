"""Operator resolutions recorded on a dev-queue row (GitHub #2702).

``cw dev-queue resolve`` appends one :class:`OperatorResolution` per distinct
set of answers the operator gives to a plan-stage park. The list lives on
:attr:`cw.models.TicketTask.operator_resolutions` and is threaded into the
next worker's ``.claude/cw-context.json`` as
``queue_metadata.operator_resolutions``, where the plan stage reads it as
binding resolutions.

Its own small module (the ``park_comment_marker`` precedent) so
``models/tasks.py`` gains only the field. Not re-exported from ``cw.models``:
consumers import this module directly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

OPERATOR_RESOLUTIONS_KEY = "operator_resolutions"
"""The ``queue_metadata`` wire key ``spawn`` threads the row's list under."""


class OperatorResolution(BaseModel):
    """One ``cw dev-queue resolve`` entry: the operator's decisions, verbatim.

    ``round`` is the 1-based row-local ordinal of the entry (the list's
    length after appending it), not the plan stage's park-round counter.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    text: str
    recorded_at: datetime
    actor: str
    round: int = Field(ge=1)
