"""The single seam that sets ``TicketTask.stage`` and its entry stamp together.

``enter_stage`` is the only place ``task.stage`` and ``task.stage_entered_at``
are written (GitHub #2155). The stamp is the ISO-8601 UTC whole-second time of
the last stage change, from the shared ``cw.lane_pause`` helpers.

Mirror contract: ``entered_at=None`` means "read the clock". The spawn mirror
in ``cw.dispatch.claim.spawn`` passes the stored row's stamp explicitly, so the
in-memory task carries the value that ``_advance_stage`` just persisted.

Same-stage rule: this helper is unconditional. Callers that can be same-stage
guard on ``old != new`` before calling it, so a same-stage requeue keeps its
original stamp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.lane_pause import iso_z, utc_now_seconds

if TYPE_CHECKING:
    from cw.models import Stage, TicketTask


def enter_stage(
    task: TicketTask, stage: Stage, *, entered_at: str | None = None
) -> None:
    """Set ``task.stage`` and ``task.stage_entered_at`` together.

    ``entered_at`` is used verbatim when given; otherwise the stamp is the
    current UTC time, whole seconds, ``Z`` suffix.
    """
    task.stage = stage
    task.stage_entered_at = (
        entered_at if entered_at is not None else iso_z(utc_now_seconds())
    )
