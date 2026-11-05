"""Binding operator constraints recorded on a dev-queue row (GitHub #2964).

``cw review constrain`` appends one :class:`OperatorConstraint` per distinct
constraint text the operator records for a ticket. The list lives on
:attr:`cw.models.TicketTask.operator_constraints` and is read from the row by
the codex review prompt (and the Claude confirm round, which shares its pass
assembly) and by the codex fix loop, on every tracker and independent of any
tracker comment, provenance marker or operator login.

Append-only: no edit, supersede or revoke path exists here (retraction is
tracked in #2968). Nothing is delivered through ``queue_metadata``, so there is
no wire key.

Its own small module (the ``operator_resolution`` precedent) so
``models/tasks.py`` gains only the field. It also holds the pure
:func:`render_constraint_entries`, because both the review context and the fix
loop render it and ``cw.codex_fix_loop`` already imports ``cw.codex_review``:
the model module is the lowest layer both can import. Not re-exported from
``cw.models``: consumers import this module directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class OperatorConstraint(BaseModel):
    """One ``cw review constrain`` entry: the operator's constraint, verbatim.

    ``round`` is the 1-based row-local ordinal of the entry (the list's length
    after appending it). ``actor`` is the operator's gh login, or the literal
    ``unresolved`` when it could not be resolved; never empty.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    text: str
    recorded_at: datetime
    actor: str
    round: int = Field(ge=1)


def format_recorded_at(moment: datetime) -> str:
    """*moment* as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC (naive values are UTC)."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def render_constraint_entries(entries: Sequence[OperatorConstraint]) -> str:
    """Render *entries* in order, or ``""`` when there are none.

    Each entry is a ``### Constraint {round} (recorded {time} by {actor})``
    line followed by its text; entries are joined by a blank line. The text
    is never truncated: ``cw review constrain`` caps it at record time.
    """
    return "\n\n".join(
        f"### Constraint {entry.round} (recorded "
        f"{format_recorded_at(entry.recorded_at)} by {entry.actor})\n{entry.text}"
        for entry in entries
    )
