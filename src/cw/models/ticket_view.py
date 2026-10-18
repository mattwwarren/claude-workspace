"""``TicketView``: the documented per-ticket view behind ``cw dev-queue show`` (#2691).

Output-only: built from a ``TicketTask`` plus the state around it (its bound
``Session``, the daemon roster, the lane config, the dispatch-tick event log)
and never persisted, so unlike the runtime models it is ``extra="forbid"``.
``cw schema show ticket-view`` prints its JSON schema.

Compatibility: adding a field is allowed; renaming or removing one bumps
``schema_version``.

Depends on ``cw.models.enums`` and, at runtime, on ``cw.lane_pause`` (a
pydantic/stdlib/``cw._util`` leaf that imports ``cw.models`` only under
``TYPE_CHECKING``, the same shape as ``cw.models.tasks``' runtime
``cw.review_finding_dispositions`` import). Imported by submodule path
(``from cw.models.ticket_view import TicketView``) and not re-exported from
``cw.models``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Runtime (not TYPE_CHECKING) because ``DispatchView.lane_pause``'s annotation
# resolves at class-build time under ``from __future__ import annotations``.
# Safe against an import cycle only because ``cw.lane_pause`` imports nothing
# from ``cw.models`` at module scope -- see its module docstring. Precedent:
# ``cw.models.tasks``' ``FindingDisposition`` import.
from cw.lane_pause import LanePauseView
from cw.models.enums import QueueItemStatus, Stage

TICKET_VIEW_SCHEMA_VERSION = 1


class DispatchSource(StrEnum):
    """Where a pending row's dispatch skip reason came from."""

    ROW = "row"
    EVENT = "event"
    LAST_TICK = "last_tick"
    NONE = "none"


class NextCommand(BaseModel):
    """One runnable next step for the row as it stands."""

    model_config = ConfigDict(extra="forbid")

    command: str
    purpose: str


class SessionView(BaseModel):
    """The cw ``Session`` the row is bound to (``TicketTask.session_id``).

    ``status`` is null when the row names a session that is no longer in
    state (pruned); every other field is then null too.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: str | None
    liveness_bucket: str | None
    in_roster: bool | None = Field(
        description=(
            "Whether the session's daemon worker is in the live roster; null"
            " when the roster could not be read or the session is not found."
        )
    )
    attention_state: str | None


class LastResultView(BaseModel):
    """The bound session's last sentinel result."""

    model_config = ConfigDict(extra="forbid")

    status: str | None
    source: str | None
    at: datetime | None = Field(
        description=(
            "The session's (else the row's) completion time; the sentinel"
            " itself carries no timestamp."
        )
    )


class PrView(BaseModel):
    """The row's PR as last hydrated; nothing here is fetched live."""

    model_config = ConfigDict(extra="forbid")

    url: str
    number: int | None
    state: str | None
    mergeable: str | None
    merge_state_status: str | None
    ci_ok: bool | None
    review_decision: str | None
    attention_state: str | None
    is_draft: bool | None
    hydrated_at: datetime | None = Field(
        description="When the PR state was last hydrated; null = never hydrated."
    )


class DispatchView(BaseModel):
    """Why a PENDING row is (or is not) being dispatched."""

    model_config = ConfigDict(extra="forbid")

    skip_reason: str | None
    source: DispatchSource
    as_of: datetime | None
    stale: bool
    backoff_until: datetime | None
    lane_paused: bool | None = Field(
        description="Effective lane pause; null when the client is not configured."
    )
    lane_pause: LanePauseView | None = Field(
        default=None,
        description=(
            "Why the lane is paused and how to resume it; null unless"
            " lane_paused is true."
        ),
    )
    lane_stats: dict[str, int] | None
    lane_occupants: list[dict[str, str]] | None


class MustFixOverrideView(BaseModel):
    """A recorded codex MUST_FIX override (the one approval with an actor)."""

    model_config = ConfigDict(extra="forbid")

    actor: str
    reason: str
    reviewed_sha: str
    recorded_at: datetime


class ApprovalView(BaseModel):
    """Approvals recorded on the row. There is no approver field: plan,
    review and signoff approvals do not record who approved."""

    model_config = ConfigDict(extra="forbid")

    plan_approved_at: datetime | None
    plan_approved_fingerprint: str | None
    scope_drift_approved_extra_files: list[str] | None
    scope_drift_approved_head: str | None
    must_fix_override: MustFixOverrideView | None


class TicketView(BaseModel):
    """One dev-queue row with the derived state an operator needs to act on it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = TICKET_VIEW_SCHEMA_VERSION
    ticket_id: str
    client: str
    status: QueueItemStatus
    stage: Stage
    disposition: str | None
    blocked_reason: str | None
    blocker_details: str | None = Field(
        description=(
            "Full blocker details from the bound session's last result;"
            " null when there is none or the session is gone."
        )
    )
    advisory_note: str | None
    lane: str
    priority: int
    scope_hint: str | None
    attempts: int
    unproductive_attempts: int
    attempt_ceiling: int | None = Field(
        description="Resolved attempt ceiling; null = the lane disables it."
    )
    session: SessionView | None
    last_result: LastResultView | None
    pr: PrView | None
    stage_base_ref: str | None
    dispatch: DispatchView | None = Field(
        description="Dispatch state; null unless the row is PENDING."
    )
    approval: ApprovalView | None = Field(
        description=(
            "Row-side approvals; null means none is recorded on the row."
            " Tracker-comment approvals are not mirrored onto the row."
        )
    )
    other_rows: int = Field(
        description="Other dev-queue rows for the same (ticket, client)."
    )
    created_at: datetime
    completed_at: datetime | None
    next: list[NextCommand]
