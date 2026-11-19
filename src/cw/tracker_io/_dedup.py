"""Courier parts and the provenance rule for what gets posted (O2b, R21).

A courier part is one already-marked, already-redacted body plus the outbox
marker line the engine composed for it. The emitted text is what the agent
posts and what the read-back checks compare against. Provenance (R21): the
agent marker is appended to every part EXCEPT when the outbox entry is
operator-authored; the flag is read from the entry, never inferred from the
binding or the tracker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.gh import agent_marked_body

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class CourierPart:
    """One part: *body* already ends with *marker_line* (the engine composes it)."""

    body: str
    marker_line: str


def bodies(parts: Sequence[CourierPart], *, operator_authored: bool) -> tuple[str, ...]:
    """The text each part is posted and read back as, with provenance applied."""
    if operator_authored:
        return tuple(part.body for part in parts)
    return tuple(agent_marked_body(part.body) for part in parts)


def expected_markers(
    parts: Sequence[CourierPart], present: Sequence[str]
) -> tuple[str, ...]:
    """The marker lines not yet on the ticket: what a delta must newly show."""
    already = set(present)
    return tuple(part.marker_line for part in parts if part.marker_line not in already)
