"""Consecutive gate-block backoff state on a dev-queue row (GitHub #2717).

A Rule 5 park for ``merge_gate_blocked`` (or blocker reason
``prior_pipeline_pr_open``) stamps one :class:`GateBlockBackoff` on
:attr:`cw.models.TicketTask.gate_block`, so a ticket that keeps re-reporting
the same gate is held off the claim screen for a growing delay instead of
flapping straight back to RUNNING. ``cw.dev_queue.gate_backoff`` owns every
write.

Its own small module (the ``operator_resolution`` precedent) so
``models/tasks.py`` gains only the field. Not re-exported from ``cw.models``:
consumers import this module directly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class GateBlockBackoff(BaseModel):
    """One row's run of consecutive gate-block parks with the same fingerprint.

    ``count`` is the 1-based length of the run. ``fingerprint`` is the
    ``status|blocker_reason|blocking_pr|stage|stage_base_ref`` key a later park
    must match to extend the run. ``until`` is the ``next_eligible_at`` this
    record stamped, so a clear can tell the gate's own wait from another
    writer's window (usage-limit, spawn-error) on the shared clock.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    count: int = Field(ge=1)
    fingerprint: str
    until: datetime
