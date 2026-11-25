"""The operator-directions rule for an IMPL run (#3761).

An operator can record decisions on a row two ways: ``cw dev-queue resolve``
appends a resolution (an answer to a plan-stage park) and ``cw review
constrain`` appends an operator constraint. Both are persisted on the row, but
until now only the plan stage and the review lanes read them, so an IMPL run
never saw what the operator had decided. This module owns the one stateless
rule that merges the two lists into a single stream for the IMPL agent. Both
consumers call :func:`operator_directions`: the claude-native
``queue_metadata.operator_directions`` key (via :func:`with_operator_directions`)
and the codex IMPL prompt section.

Supersession. The two lists merge into one chronological stream by
``recorded_at`` (ties: resolution before constraint, then round), rendered
oldest first under an explicit rule that the later-recorded entry governs on
the same point. Entries are append-only and never deleted: this is rendering
guidance only, and real retraction is tracked in #2968.

The text is the operator's recorded decision, but it is still data: it is
redacted, bounded and delivered as untrusted content, never as authority to run
quoted commands, widen scope past the approved plan or bypass the
destructive-directive gate. Plan-derived constraint kinds (adopted assumptions,
non-goals, plan decisions) are not delivered: the approved plan already carries
them.

The rule is a pure function of the row. It reads no worktree, so it holds on
every IMPL retry and needs no artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.context_queue_metadata import OPERATOR_DIRECTIONS_KEY
from cw.lane_pause import iso_z
from cw.models import Stage
from cw.models.operator_constraint import render_constraint_entries, split_constraints
from cw.stage_verification.posted_text import redact_and_cap_bytes

if TYPE_CHECKING:
    from cw.models import TicketTask

#: Bound on the whole delivered text, preamble included. Intentionally equal to
#: the 64 KiB the regress findings text already puts in an IMPL prompt.
MAX_OPERATOR_DIRECTIONS_BYTES: Final = 64 * 1024
#: Room kept for the preamble so the final text can never exceed the bound.
_PREAMBLE_RESERVE_BYTES: Final = 4 * 1024

# The wording both prose surfaces (the codex prompt note and ``auto-dev-impl.md``)
# must carry. One source so a drift is a test failure, not a review catch.
OLDEST_FIRST_PHRASE: Final = "oldest first"
GOVERNS_PHRASE: Final = "the later-recorded entry governs on the same point"
NEVER_DELETED_PHRASE: Final = "entries are never deleted (retraction is #2968)"
FOLDED_PHRASE: Final = "may already be reflected in the approved plan"
UNTRUSTED_PHRASE: Final = "untrusted data"
SCOPE_PHRASE: Final = "files and Non-Goals in `.cw/plan.md`"
GATE_PHRASE: Final = "destructive-directive gate"
PINNED_PHRASES: Final = (
    OLDEST_FIRST_PHRASE,
    GOVERNS_PHRASE,
    NEVER_DELETED_PHRASE,
    FOLDED_PHRASE,
    UNTRUSTED_PHRASE,
    SCOPE_PHRASE,
    GATE_PHRASE,
)

COUNTS_LINE: Final = (
    "Delivered: {resolutions} resolutions and {constraints} constraints, oldest first."
)
OMISSION_LINE: Final = "{omitted} older entries omitted; the full record is on the row."

_SEPARATOR: Final = "\n\n"
_ENTRY_BUDGET_BYTES: Final = MAX_OPERATOR_DIRECTIONS_BYTES - _PREAMBLE_RESERVE_BYTES
_RESOLUTION_RANK: Final = 0
_CONSTRAINT_RANK: Final = 1


@dataclass(frozen=True)
class OperatorDirections:
    """The delivered stream and what it holds.

    ``resolutions`` and ``constraints`` count the entries delivered in ``text``
    (an entry cut with the truncation marker still counts as delivered);
    ``omitted`` counts the whole entries the byte budget dropped. So
    ``resolutions + constraints + omitted`` is the row's recorded resolutions
    plus operator-kind constraints.
    """

    text: str
    resolutions: int
    constraints: int
    omitted: int

    def wire(self) -> dict[str, object]:
        """The ``queue_metadata.operator_directions`` wire shape."""
        return {
            "text": self.text,
            "resolutions": self.resolutions,
            "constraints": self.constraints,
            "omitted": self.omitted,
        }


class _Entry(NamedTuple):
    """One rendered, individually capped entry and its sort facts."""

    recorded_at: datetime
    rank: int
    round: int
    block: str


def _capped(block: str) -> str:
    """Redact *block* and cap it so it fits the entry budget on its own."""
    return redact_and_cap_bytes(block, cap_bytes=_ENTRY_BUDGET_BYTES)


def _entries(task: TicketTask) -> list[_Entry]:
    """Every deliverable entry of *task*, oldest first."""
    entries = [
        _Entry(
            resolution.recorded_at,
            _RESOLUTION_RANK,
            resolution.round,
            _capped(
                f"### Resolution {resolution.round} (recorded "
                f"{iso_z(resolution.recorded_at)} by {resolution.actor})\n"
                f"{resolution.text}"
            ),
        )
        for resolution in task.operator_resolutions
    ]
    operator_constraints = split_constraints(task.operator_constraints)[0]
    entries.extend(
        _Entry(
            constraint.recorded_at,
            _CONSTRAINT_RANK,
            constraint.round,
            _capped(render_constraint_entries([constraint])),
        )
        for constraint in operator_constraints
    )
    entries.sort(key=lambda e: (e.recorded_at.astimezone(UTC), e.rank, e.round))
    return entries


def _preamble(*, resolutions: int, constraints: int, omitted: int) -> str:
    """The framing text above the entries; always within the reserve."""
    lines = [
        "These are the operator's recorded decisions for this ticket.",
        COUNTS_LINE.format(resolutions=resolutions, constraints=constraints),
        f"When two entries address the same point, {GOVERNS_PHRASE}; "
        f"{NEVER_DELETED_PHRASE}. A resolution {FOLDED_PHRASE}, so check the "
        "plan before acting on it.",
    ]
    if omitted > 0:
        lines.append(OMISSION_LINE.format(omitted=omitted))
    return "\n".join(lines)


def _newest_suffix(entries: list[_Entry]) -> list[_Entry]:
    """The newest run of whole entries that fits the entry budget, oldest first.

    The walk stops at the first entry that does not fit, so the kept set is
    always contiguous: an older, smaller entry is never delivered while a newer
    one is dropped. The newest entry always fits because each block is capped
    to the budget on its own.
    """
    kept: list[_Entry] = []
    used = 0
    for entry in reversed(entries):
        size = len(entry.block.encode("utf-8")) + (len(_SEPARATOR) if kept else 0)
        if used + size > _ENTRY_BUDGET_BYTES:
            break
        kept.append(entry)
        used += size
    kept.reverse()
    return kept


def operator_directions(task: TicketTask) -> OperatorDirections | None:
    """The directions delivered to this IMPL run, or ``None``.

    ``None`` when the row is not at IMPL or holds no resolution and no
    operator-kind constraint.
    """
    if task.stage != Stage.IMPL:
        return None
    entries = _entries(task)
    if not entries:
        return None
    kept = _newest_suffix(entries)
    resolutions = sum(1 for entry in kept if entry.rank == _RESOLUTION_RANK)
    constraints = len(kept) - resolutions
    omitted = len(entries) - len(kept)
    preamble = _preamble(
        resolutions=resolutions, constraints=constraints, omitted=omitted
    )
    text = _SEPARATOR.join([preamble, *(entry.block for entry in kept)])
    return OperatorDirections(text, resolutions, constraints, omitted)


def with_operator_directions(
    metadata: dict[str, object], task: TicketTask
) -> dict[str, object]:
    """Return *metadata* with ``operator_directions`` set (null when none)."""
    directions = operator_directions(task)
    value = directions.wire() if directions is not None else None
    return {**metadata, OPERATOR_DIRECTIONS_KEY: value}
