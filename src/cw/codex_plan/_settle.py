"""Validate the PLAN turn's settlement claims and check the deferred stubs (#3464).

The read-only turn transcribes operator replies into closed-vocabulary
settlement tokens (:class:`~cw.codex_plan._result.PlanSettlement`); every
claim is checked here before cw writes a ``plan-stage-settled`` line. A claim
cw cannot verify is unmappable: the item stays open and re-parks, bounded by
the round cap (fail closed).

The park ordinal ``k`` and the open item numbers come from cw-posted park
comments, read from the raw ``snapshot.comments`` (not the trust evaluator,
which drops agent-authored comments): a park comment is ``agent_authored``
and carries :data:`~cw.codex_plan._post.PARK_HEADER`. A park over the comment
cap posts as several comments (``cw.tracker_post.plan_parts``), the first led
by a ``(part 1 of N)`` label: that first part is the park, counted once, and
its continuation parts are joined into the newest park's item text. They are
numbered in snapshot order (the tracker's chronological order, oldest first);
the comment id breaks a tie only between comments with an equal ``created_at``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby
from typing import TYPE_CHECKING, Final

from cw.codex_plan._post import PARK_HEADER
from cw.plan_fingerprint import settled_line
from cw.tracker_post import strip_trailing_marker

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from cw.codex_plan._result import PlanSettlement
    from cw.ticket_snapshot import SnapshotComment, TicketSnapshot

#: The source spelling of a row resolution (``row:<round>``).
ROW_SETTLEMENT_PREFIX: Final = "row:"
#: The stub text a deferred premise carries until a scan classifies it.
DEFERRED_STUB: Final = "PENDING — agent must supply on next scan"
_DEFERRED_HEADING: Final = "## Deferred Premises"

_ITEM_ID = re.compile(r"(?:R(?P<round>[0-9]+)\.)?(?P<letter>[AP])(?P<number>[0-9]+)")
_ALT_TOKEN = re.compile(r"ALT-(?P<letter>[a-z])")
_NUMBERED_LINE = re.compile(r"^(?P<number>[0-9]+)\. ")
#: The ``(part i of n)`` label ``cw.tracker_post.wrap_part`` puts first in a split.
_PART_LABEL = re.compile(r"\(part (?P<index>[0-9]+) of (?P<total>[0-9]+)\)\n\n")


@dataclass(frozen=True)
class ParkState:
    """The park ordinal ``k`` and the newest park comment's body (``None``: none)."""

    ordinal: int
    newest_body: str | None

    def item_block(self, number: int) -> str | None:
        """The numbered line ``<number>.`` and its sub-lines; ``None`` if absent."""
        if self.newest_body is None:
            return None
        lines = self.newest_body.splitlines()
        for i, line in enumerate(lines):
            match = _NUMBERED_LINE.match(line)
            if match is None or int(match.group("number")) != number:
                continue
            block = [line]
            for follower in lines[i + 1 :]:
                if _NUMBERED_LINE.match(follower) or follower.startswith("#"):
                    break
                block.append(follower)
            return "\n".join(block)
        return None


def unlabelled(body: str) -> tuple[str, int, int]:
    """``(text, index, total)``: the body past its part label; unlabelled is 1 of 1."""
    text = body.lstrip()
    match = _PART_LABEL.match(text)
    if match is None:
        return text, 1, 1
    return text[match.end() :], int(match["index"]), int(match["total"])


def _is_park_comment(comment: SnapshotComment) -> bool:
    """A park's first (or only) comment; its continuation parts carry no header."""
    if not comment.agent_authored:
        return False
    text, index, _ = unlabelled(comment.body)
    return index == 1 and text.startswith(PARK_HEADER)


def _joined_park_body(
    head: SnapshotComment, comments: Sequence[SnapshotComment]
) -> str:
    """The head's body, or the head and its continuation parts joined in order.

    An unlabelled park is returned verbatim. A split park's later parts are the
    agent-authored comments after the head labelled ``(part i of N)`` for the
    next ``i`` of the same ``N``; labels and provenance markers are dropped.
    """
    text, _, total = unlabelled(head.body)
    if total == 1:
        return head.body
    parts = [strip_trailing_marker(text)]
    later = comments[next(i for i, c in enumerate(comments) if c is head) + 1 :]
    for comment in later:
        if not comment.agent_authored:
            continue
        part, index, of = unlabelled(comment.body)
        if of == total and index == len(parts) + 1:
            parts.append(strip_trailing_marker(part))
    return "\n".join(parts)


def park_state(snapshot: TicketSnapshot | None) -> ParkState:
    """Number the cw park comments of *snapshot*; ``k`` is the newest's number."""
    if snapshot is None:
        return ParkState(ordinal=0, newest_body=None)
    parks = [c for c in snapshot.comments if _is_park_comment(c)]
    ordered = [
        comment
        for _, run in groupby(parks, key=lambda c: c.created_at)
        for comment in sorted(run, key=lambda c: c.id)
    ]
    if not ordered:
        return ParkState(ordinal=0, newest_body=None)
    newest = _joined_park_body(ordered[-1], snapshot.comments)
    return ParkState(ordinal=len(ordered), newest_body=newest)


@dataclass(frozen=True)
class SettleOutcome:
    """The accepted claims (in order) and each rejected claim with its reason."""

    accepted: tuple[PlanSettlement, ...] = ()
    rejected: tuple[tuple[PlanSettlement, str], ...] = ()

    def settled_entries(self) -> tuple[str, ...]:
        """The ``<item id>: <token>`` values of the new ``plan-stage-settled`` lines."""
        return tuple(f"{s.item_id}: {s.token}" for s in self.accepted)

    def evidence(self) -> dict[str, object] | None:
        """``{comment_id, items}`` for ``resolution_evidence``; ``None`` if none.

        ``comment_id`` is the distinct sources joined with ``,`` in
        first-appearance order (a comment id, or ``row:<N>``).
        """
        if not self.accepted:
            return None
        sources = list(dict.fromkeys(s.source for s in self.accepted))
        return {
            "comment_id": ",".join(sources),
            "items": [s.item_id for s in self.accepted],
        }

    def friction(self, park_ordinal: int) -> list[str]:
        """One line per accepted item, one tally line for any rejections."""
        lines = [
            f"plan-stage item settled: {s.item_id} → {s.token} — round {park_ordinal}"
            for s in self.accepted
        ]
        if self.rejected:
            detail = "; ".join(f"{s.item_id}: {why}" for s, why in self.rejected)
            lines.append(
                f"plan-stage settlements unmappable: {len(self.rejected)} ({detail})"
            )
        return lines


def _settled_ids(settled: Sequence[str]) -> set[str]:
    return {entry.partition(": ")[0] for entry in settled}


def _grammar_rejection(
    claim: PlanSettlement, match: re.Match[str], park: ParkState
) -> str | None:
    """Why *claim*'s id and token fail the grammar or the park ordinal."""
    expected = None if park.ordinal == 1 else str(park.ordinal)
    if match.group("round") != expected:
        return f"round qualifier is not park ordinal {park.ordinal}"
    try:
        settled_line(claim.item_id, claim.token)
    except ValueError:
        return "not a settled-line token for this id"
    return None


def _context_rejection(
    claim: PlanSettlement,
    match: re.Match[str],
    park: ParkState,
    settled_ids: Collection[str],
    sources: Collection[str],
) -> str | None:
    """Why *claim* fails against the park comment, the draft or the sources."""
    block = park.item_block(int(match.group("number")))
    if block is None:
        return "item absent from the park comment"
    alt = _ALT_TOKEN.fullmatch(claim.token)
    if alt is not None and f"({alt.group('letter')})" not in block:
        return "alternative absent from the item"
    if claim.item_id in settled_ids:
        return "already settled"
    if claim.source not in sources:
        return "unknown source"
    return None


def _rejection(
    claim: PlanSettlement,
    park: ParkState,
    settled_ids: Collection[str],
    sources: Collection[str],
) -> str | None:
    """Why *claim* is unmappable, or ``None`` when cw accepts it."""
    if park.ordinal == 0:
        return "no park comment"
    match = _ITEM_ID.fullmatch(claim.item_id)
    if match is None:
        return "not an item id"
    return _grammar_rejection(claim, match, park) or _context_rejection(
        claim, match, park, settled_ids, sources
    )


def validate_settlements(
    claims: Sequence[PlanSettlement],
    *,
    park: ParkState,
    settled: Sequence[str],
    comment_ids: Collection[str],
    row_rounds: Collection[int],
) -> SettleOutcome:
    """Accept the claims cw can verify; the rest are unmappable.

    *settled* is the draft's existing ``<item id>: <token>`` entries,
    *comment_ids* the operator-authority comment ids the round observed and
    *row_rounds* the row resolutions' ``round`` values (``row:<round>``).
    A claim repeating an id accepted earlier in the same turn is rejected.
    """
    sources = {*comment_ids, *(f"{ROW_SETTLEMENT_PREFIX}{n}" for n in row_rounds)}
    seen = _settled_ids(settled)
    accepted: list[PlanSettlement] = []
    rejected: list[tuple[PlanSettlement, str]] = []
    for claim in claims:
        why = _rejection(claim, park, seen, sources)
        if why is None:
            accepted.append(claim)
            seen.add(claim.item_id)
        else:
            rejected.append((claim, why))
    return SettleOutcome(accepted=tuple(accepted), rejected=tuple(rejected))


def unresolved_stubs(plan_text: str) -> list[str]:
    """The ``## Deferred Premises`` lines still carrying :data:`DEFERRED_STUB`."""
    stubs: list[str] = []
    in_section = False
    for line in plan_text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == _DEFERRED_HEADING
            continue
        if in_section and DEFERRED_STUB in line:
            stubs.append(line.strip())
    return stubs


__all__ = [
    "DEFERRED_STUB",
    "ROW_SETTLEMENT_PREFIX",
    "ParkState",
    "SettleOutcome",
    "park_state",
    "unlabelled",
    "unresolved_stubs",
    "validate_settlements",
]
