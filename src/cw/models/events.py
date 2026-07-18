"""Event-bus and PR-state models (``cw.models`` submodule).

Depends only on ``cw.models.enums``. See ``cw.models.__init__`` for DAG order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from cw.models.enums import OrchestratorEventType


class OrchestratorEvent(BaseModel):
    """A single event on the orchestrator event bus."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    type: OrchestratorEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consumed_at: datetime | None = None


class PrState(BaseModel):
    """Hydrated GitHub PR state persisted on a TicketTask (GitHub #929).

    Populated by the serve-tick hydration pass (``cw.pr_hydrate``) from a
    ``gh pr view --json`` response. ``attention_state`` is the operator-facing
    escalation signal derived by ``_compute_attention_state``; None for drafts
    and terminal (MERGED/CLOSED) PRs. ``failing_checks`` carries the failing
    check names for the ``pr.ci_failed`` event payload. ``is_draft``,
    ``reviewer_count``, and ``pending_count`` are the remaining
    ``_compute_attention_state`` ladder inputs the poll path always computes
    but previously never persisted (#1196) — storing them lets the webhook
    push path recompute ``attention_state`` from the carried baseline without
    re-fetching GitHub.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    state: str = "OPEN"
    mergeable: str | None = None
    merge_state_status: str = "UNKNOWN"
    ci_ok: bool = True
    review_decision: str = ""
    attention_state: str | None = None
    is_draft: bool = False
    reviewer_count: int = 0
    pending_count: int = 0
    failing_checks: list[str] = Field(default_factory=list)
    hydrated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchedPr(BaseModel):
    """An externally-requested PR the operator is watching (GitHub #1154).

    Registered when someone requests the operator's review on a PR the queue
    does not otherwise track — via ``cw review register <pr>`` (``source="cli"``)
    or the ``review_requested`` webhook (``source="webhook"``). Persisted as a
    top-level ``DevQueueStore.watched_prs`` entry (RFC 0011 S2), deliberately
    NOT a ``TicketTask``: it carries no ``client``/``lane`` and never occupies a
    dispatch lane slot. ``pr_state`` is hydrated by the serve-tick pass
    (``cw.pr_hydrate._hydrate_watched_prs``) exactly like ``TicketTask.pr_state``.

    ``status`` reserves a ``"dismissed"`` terminal that no code sets this slice —
    the ``(repo, pr_number)`` dedup guard is scoped to ``"active"`` so a future
    dismiss transition can re-open registration (RFC 0011 S2, adopted #5).
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    pr_url: str
    repo: str
    pr_number: int
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requester_login: str | None = None
    source: Literal["webhook", "cli"]
    status: Literal["active", "dismissed"] = "active"
    pr_state: PrState | None = None
