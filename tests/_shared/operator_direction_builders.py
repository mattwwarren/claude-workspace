"""Builders for the operator-directions tests (#3761): resolution and
operator-constraint entries with a caller-chosen ``recorded_at`` and actor,
and a minimal IMPL-stage ticket row holding them. Plain functions, no
fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from cw.models import Stage, TicketTask
from cw.models.operator_constraint import OperatorConstraint
from cw.models.operator_resolution import OperatorResolution
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Sequence

RECORDED_AT: Final = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    """``RECORDED_AT`` plus *minutes*."""
    return RECORDED_AT + timedelta(minutes=minutes)


def make_resolution(
    text: str = "Use the row.",
    *,
    round_: int = 1,
    recorded_at: datetime = RECORDED_AT,
    actor: str = "octocat",
) -> OperatorResolution:
    """One resolution entry."""
    return OperatorResolution(
        text=text, recorded_at=recorded_at, actor=actor, round=round_
    )


def make_operator_constraint(
    text: str = "Do not add `foo_bar`.",
    *,
    round_: int = 1,
    recorded_at: datetime = RECORDED_AT,
    actor: str = "octocat",
    kind: str = "operator",
) -> OperatorConstraint:
    """One constraint entry; pass a plan-derived *kind* for a non-operator one."""
    return OperatorConstraint.model_validate(
        {
            "text": text,
            "recorded_at": recorded_at,
            "actor": actor,
            "round": round_,
            "kind": kind,
        }
    )


def make_impl_row(
    *,
    resolutions: Sequence[OperatorResolution] = (),
    constraints: Sequence[OperatorConstraint] = (),
    **overrides: object,
) -> TicketTask:
    """A valid IMPL-stage row holding *resolutions* and *constraints*.

    *overrides* win over the defaults, so a caller can still set
    ``stage=Stage.PLAN``, ``ticket_id``, ``client`` or ``stage_high_water``.
    """
    fields: dict[str, object] = {
        "stage": Stage.IMPL,
        "operator_resolutions": list(resolutions),
        "operator_constraints": list(constraints),
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)
