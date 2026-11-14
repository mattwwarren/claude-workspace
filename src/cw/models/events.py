"""Event-bus and PR-state models (``cw.models`` submodule).

Depends only on ``cw.models.enums``. See ``cw.models.__init__`` for DAG order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

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


class ShippedRedEpisode(BaseModel):
    """Per-episode latch for a red shipped PR (``cw.shipped_pr_signal``, #2945).

    One page is raised per latch lifetime (an *episode*: the row stays
    ``COMPLETED``, the PR head SHA is unchanged and a required check is still
    failing). ``alerted_at`` is set once a page was emitted; ``head_sha`` is
    then the episode's head and the sweep compares it with the live head to
    detect the episode end. ``hydrated_fingerprint`` is the hash of the
    hydrated ``failing_checks`` the row was last evaluated against;
    ``fingerprint`` / ``required_checks`` are the required-failing set of the
    last successful live read and are refreshed silently, never re-paged.
    An unalerted latch (``alerted_at`` None) is a pacing stamp: ``evaluated_at``,
    ``retry_not_before`` and ``failures`` only debounce read-only ``gh`` polls.
    Cleared by ``transition_task_status`` when the row leaves ``COMPLETED``.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    hydrated_fingerprint: str
    fingerprint: str | None = None
    required_checks: list[str] = Field(default_factory=list)
    head_sha: str | None = None
    evaluated_at: datetime
    alerted_at: datetime | None = None
    failures: int = 0
    retry_not_before: datetime | None = None


class WatchedPr(BaseModel):
    """An externally-requested PR the operator is watching (GitHub #1154).

    Registered when someone requests the operator's review on a PR the queue
    does not otherwise track — via ``cw review register <pr>`` (``source="cli"``)
    or the ``review_requested`` webhook (``source="webhook"``) — or, since
    GitHub #1927, when a ``stale_dispatch`` park needs an independent state
    source for its OWN blocking PR (``source="stale_dispatch_park"``, registered
    by ``cw.reconcile.stale_dispatch_watch``). Persisted as a top-level
    ``DevQueueStore.watched_prs`` entry (RFC 0011 S2), deliberately NOT a
    ``TicketTask``: it carries no ``lane`` and never occupies a dispatch lane
    slot; ``client`` is present only for producers that need cross-reference
    scoping (see the field comment below). ``pr_state`` is hydrated by the
    serve-tick pass (``cw.pr_hydrate._hydrate_watched_prs``) exactly like
    ``TicketTask.pr_state``.

    ``status`` reserves a ``"dismissed"`` terminal that no code sets this slice —
    the ``(repo, pr_number)`` dedup guard is scoped to ``"active"`` so a future
    dismiss transition can re-open registration (RFC 0011 S2, adopted #5).

    Since #2321 ``source="dependency_hold"`` is registered by ``cw dev-queue hold``
    and by ``cw.reconcile.stale_dispatch_watch`` for dependency parks. The JSON
    reader/writer carries a private compatibility marker and the old
    ``stale_dispatch_park`` wire value, so an older cw can read the store during
    a rollback; the current reader restores the more precise source. No schema
    bump accompanies this compatibility encoding. No code retires a watch yet
    (#3654).
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    pr_url: str
    repo: str
    pr_number: int
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requester_login: str | None = None
    source: Literal["webhook", "cli", "stale_dispatch_park", "dependency_hold"]
    status: Literal["active", "dismissed"] = "active"
    # GitHub #1927 — the owning dev-queue client, mirroring ``TicketTask.client``'s
    # role for ``cw.reconcile.tasks._merged_pr_numbers_by_client``: that index is
    # keyed per-client because a bare PR number is only unambiguous within one
    # client's repo (#1269). ``None`` for the two pre-#1927 producers (webhook,
    # cli), which register an operator's review request with no client context —
    # those entries are excluded from the index rather than guessed at. No
    # DEV_QUEUE_SCHEMA_VERSION bump accompanies this field: it is additive with a
    # ``None`` default, so every persisted pre-#1927 blob still parses.
    client: str | None = None
    pr_state: PrState | None = None

    @model_validator(mode="before")
    @classmethod
    def _restore_dependency_hold_source(cls, value: Any) -> Any:
        """Read the rollback-safe wire form used for dependency-hold watches."""
        if not isinstance(value, dict):
            return value
        if (
            value.get("dependency_hold_compat") is True
            and value.get("source") == "stale_dispatch_park"
        ):
            restored = dict(value)
            restored["source"] = "dependency_hold"
            return restored
        if value.get("source") == "dependency_hold":
            marked = dict(value)
            marked["dependency_hold_compat"] = True
            return marked
        return value

    @field_serializer("source", when_used="json")
    def _serialize_dependency_hold_source(self, value: str) -> str:
        """Emit an old-source wire value so rollback readers still load it."""
        if value == "dependency_hold":
            return "stale_dispatch_park"
        return value

    # Hidden from the JSON schema because it is only a rollback wire marker;
    # older cw versions ignore unknown fields while retaining the old source.
    dependency_hold_compat: SkipJsonSchema[bool | None] = Field(
        default=None, exclude_if=lambda value: value is None, repr=False
    )


def watched_pr_collides(
    existing: WatchedPr, *, repo: str, pr_number: int, client: str
) -> bool:
    """Return whether *existing* blocks a watch owned by another client."""
    return (
        existing.repo == repo
        and existing.pr_number == pr_number
        and existing.status == "active"
        and existing.client is not None
        and existing.client != client
    )
