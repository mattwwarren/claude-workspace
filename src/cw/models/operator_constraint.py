"""Constraints recorded on a dev-queue row (GitHub #2964, #2984).

One store, two provenances. ``cw review constrain`` appends an operator entry
per distinct constraint text the operator records for a ticket (#2964), and
the plan stage's ``cw dev-queue record-plan-decisions`` appends the approved
plan's Adopted Assumptions, Non-Goals and Decisions (#2984). Each
:class:`OperatorConstraint` carries a ``kind`` provenance label; a pre-#2984
entry has none and loads as ``"operator"``. The list lives on
:attr:`cw.models.TicketTask.operator_constraints` and is read from the row by
the codex review prompt (and the Claude confirm round, which shares its pass
assembly), by the codex fix loop, and by ``cw review decided`` for the
Claude-native reviewers, on every tracker and independent of any tracker
comment, provenance marker or operator login.

Operator entries are binding and feed the fix loop's prohibition guard.
Plan-derived entries inform prompts only, as decided scope: they never become
a guard rule, because they have no retraction path yet.

Append-only: no edit, supersede or revoke path exists here (retraction is
tracked in #2968). Nothing is delivered through ``queue_metadata``, so there is
no wire key.

Its own small module (the ``operator_resolution`` precedent) so
``models/tasks.py`` gains only the field. It also holds the pure renderers,
because both the review context and the fix loop render them and
``cw.codex_fix_loop`` already imports ``cw.codex_review``: the model module is
the lowest layer both can import. Not re-exported from ``cw.models``:
consumers import this module directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from cw.lane_pause import iso_z

OPERATOR_CONSTRAIN_COMMAND = "cw review constrain"
"""The CLI command that records an operator row constraint."""

PLAN_ACTOR = "plan"
"""The fixed actor label of an entry recorded from the approved plan."""

if TYPE_CHECKING:
    from collections.abc import Sequence

PlanConstraintKind = Literal["adopted_assumption", "non_goal", "plan_decision"]
ConstraintKind = Literal["operator", "adopted_assumption", "non_goal", "plan_decision"]

OPERATOR_KIND: ConstraintKind = "operator"

# Render order and heading label of each plan-derived kind.
_PLAN_KIND_LABELS: dict[ConstraintKind, str] = {
    "adopted_assumption": "adopted assumption",
    "non_goal": "non-goal",
    "plan_decision": "plan decision",
}

DECIDED_SCOPE_INSTRUCTIONS = (
    "The choices below were made on purpose, by the approved plan (its Adopted"
    " Assumptions, Non-Goals and Decisions) or by the operator. They are"
    " decided scope, not open questions. Do not raise a finding whose remedy"
    " would reverse or work around one of them. If you believe a choice itself"
    " is unsafe because it causes a concrete defect, file the finding anyway"
    " and set `contests_decision` to a quote of the choice plus the concrete"
    " harm. Leave `contests_decision` empty on every other finding."
)
"""Reviewer wording shared by the codex ``## Decided Scope`` prompt section
and ``cw review decided`` (#2984), so the two lanes cannot drift."""


class OperatorConstraint(BaseModel):
    """One row constraint entry, verbatim.

    ``round`` is the 1-based row-local ordinal of the entry (the list's length
    after appending it), shared across kinds. ``actor`` is the operator's gh
    login, the literal ``unresolved`` when it could not be resolved, or
    :data:`PLAN_ACTOR` for a plan-derived entry; never empty. ``kind`` is the
    provenance label (#2984); ``"operator"`` for a ``cw review constrain``
    entry, including every entry recorded before the label existed.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    text: str
    recorded_at: datetime
    actor: str
    round: int = Field(ge=1)
    kind: ConstraintKind = OPERATOR_KIND


def split_constraints(
    entries: Sequence[OperatorConstraint],
) -> tuple[list[OperatorConstraint], list[OperatorConstraint]]:
    """Partition *entries* into ``(operator, plan-derived)``, order kept."""
    operator = [entry for entry in entries if entry.kind == OPERATOR_KIND]
    plan = [entry for entry in entries if entry.kind != OPERATOR_KIND]
    return operator, plan


def render_constraint_entries(entries: Sequence[OperatorConstraint]) -> str:
    """Render operator *entries* in order, or ``""`` when there are none.

    Each entry is a ``### Constraint {round} (recorded {time} by {actor})``
    line followed by its text; entries are joined by a blank line. The text
    is never truncated: ``cw review constrain`` caps it at record time.
    Callers pass operator entries only (see :func:`split_constraints`).
    """
    return "\n\n".join(
        f"### Constraint {entry.round} (recorded "
        f"{iso_z(entry.recorded_at)} by {entry.actor})\n{entry.text}"
        for entry in entries
    )


def render_decided_entries(entries: Sequence[OperatorConstraint]) -> str:
    """Render plan-derived *entries* grouped by kind, or ``""`` without any.

    Kinds render in :data:`_PLAN_KIND_LABELS` order, entries within a kind in
    round order. Each is a ``### Decided {label} {round} (from the approved
    plan, recorded {time})`` line followed by its verbatim text.
    """
    blocks = [
        f"### Decided {label} {entry.round} (from the approved plan, recorded"
        f" {iso_z(entry.recorded_at)})\n{entry.text}"
        for kind, label in _PLAN_KIND_LABELS.items()
        for entry in entries
        if entry.kind == kind
    ]
    return "\n\n".join(blocks)
