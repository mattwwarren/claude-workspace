"""``ReviewHistory``: the documented view behind ``cw review history`` (#2790).

Output-only: built from records that already exist (the converge ledger and
its per-round verdict files, a session's ``AUTO_DEV_RESULT``, the dev-queue
row's disposition records, and those events that can be tied to the row) and
never persisted, so unlike the runtime models it is ``extra="forbid"``.
``cw schema show review-history`` prints its JSON schema.

Compatibility: adding a field is allowed; renaming or removing one bumps
``schema_version``.

Pure pydantic with no ``cw`` import, so it cannot close an import cycle with
``cw.review_convergence``. Imported by submodule path (``from
cw.models.review_history import ReviewHistory``) and not re-exported from
``cw.models``, as ``cw.models.ticket_view`` is not.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REVIEW_HISTORY_SCHEMA_VERSION = 1

ConvergenceMode = Literal["clean", "blocking", "unknown"]
RoundViewKind = Literal["loop", "confirm", "initial"]
RoundSource = Literal["ledger", "sentinel_friction", "sentinel_cycle0"]


class SeveritySplit(BaseModel):
    """How many of a round's new findings were MUST_FIX and SHOULD_FIX."""

    model_config = ConfigDict(extra="forbid")

    must_fix: int
    should_fix: int


class SettledView(BaseModel):
    """One ledger finding settled as reject or defer, with its rationale."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    file: str
    summary: str = Field(description="The whitespace-normalized summary.")
    outcome: Literal["reject", "defer"]
    rationale: str
    round: int = Field(
        description=(
            "The round that settled it (adjudication origin), or the number of "
            "rounds recorded when the operator settled it (operator origin)."
        )
    )
    origin: Literal["adjudication", "operator"]
    actor: str | None = None
    settled_at: str | None = None
    reviewed_sha: str | None = None


class RoundView(BaseModel):
    """One review round, from the ledger or rebuilt from the sentinel."""

    model_config = ConfigDict(extra="forbid")

    round: int
    kind: RoundViewKind = Field(
        description=(
            "`loop` or `confirm` for a recorded round; `initial` for the one "
            "entry a blocking-mode review leaves (its cycle-0 counts)."
        )
    )
    source: RoundSource = Field(
        description=(
            "`ledger` (the converge ledger), `sentinel_friction` (a "
            "`review_round_<n>` friction line) or `sentinel_cycle0` (the "
            "sentinel's `must_fix_initial`/`should_fix`)."
        )
    )
    backend: str | None = None
    reviewed_sha: str | None = Field(
        default=None,
        description=(
            "Full sha from the ledger; the 7-char short sha from a friction line."
        ),
    )
    roster: list[str] = Field(default_factory=list)
    new_accepted: int | None = None
    new_fix: int | None = None
    new_operator_action: int | None = None
    new_settled: int | None = None
    resurfaced_settled: int | None = None
    settled_total: int | None = Field(
        default=None,
        description=(
            "`new_settled + resurfaced_settled`; the only settled count a "
            "friction line preserves."
        ),
    )
    new_by_severity: SeveritySplit | None = Field(
        default=None,
        description=(
            "Split of the new findings, from the round's verdict file; null "
            "when no verdict file was recorded or it disagrees with the ledger."
        ),
    )
    fix_dispatched: int | None = Field(
        default=None,
        description=(
            "Findings a fix cycle was dispatched for (`new_fix`). The ledger "
            "records dispatch, not verified fixes."
        ),
    )
    clean: bool | None = None
    recorded_at: str | None = None
    wall_seconds: float | None = Field(
        default=None,
        description=(
            "Seconds since the previous round's record (round 1: since this "
            "row's attributable `s3_review_started` event), so it includes "
            "the fix cycle in between; null when either end is unknown."
        ),
    )
    settled: list[SettledView] = Field(
        default_factory=list,
        description="Adjudication-origin settled entries whose `round` is this round.",
    )


class DispositionView(BaseModel):
    """One ``finding_dispositions`` record on the dev-queue row."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        description="`file::normalized_summary::sha256(verbatim summary)`."
    )
    file: str
    summary: str = Field(description="Verbatim summary; empty for a legacy record.")
    outcome: str = Field(description="`ACCEPTED`, `REJECTED` or `REVERSED`.")
    rationale: str
    actor: str
    recorded_at: str
    reviewed_sha: str


class VoidedView(BaseModel):
    """One ``review.finding_voided`` event attributed to this row."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    file: str = Field(description="From this row's matched record, never the event.")
    summary: str = Field(description="From this row's matched record, never the event.")
    original_rationale: str
    voided_at: str = Field(description="When the void was minted; empty if unknown.")
    operator_comment_id: str = Field(description="`<author>@<created_at>`.")
    event_at: str = Field(description="ISO time the suppression fired.")


class ReleaseView(BaseModel):
    """One audited release of a convergence stop."""

    model_config = ConfigDict(extra="forbid")

    at: str
    round: int = Field(description="Rounds recorded when the release happened.")
    reason: str = Field(description="The `stop_reason` that was released.")
    cause: str = Field(
        description=(
            "`new_commits`, `max_cycles_raised`, `operator_action_done` or "
            "`identities_settled`."
        )
    )
    sha: str


class HistorySummary(BaseModel):
    """The whole review in one block: mode, outcome, trend and stops."""

    model_config = ConfigDict(extra="forbid")

    convergence_mode: ConvergenceMode = Field(
        description=(
            "`clean` with a usable ledger or a clean sentinel; `blocking` with "
            "no usable ledger and a sentinel `review` block that is blocking "
            "or omits the mode; `unknown` with neither."
        )
    )
    rounds_run: int | None = None
    converged: bool | None = None
    final_clean_sha: str | None = None
    ledger_status: str | None = None
    stop_reason: str | None = None
    stop_identities: list[str] = Field(
        default_factory=list,
        description="`<severity>::<file>::<summary>` audit keys of the stop.",
    )
    fix_cycles_used: int | None = None
    max_cycles: int | None = None
    confirm_backend: str | None = None
    new_accepted_series: list[int] = Field(
        default_factory=list,
        description="`new_accepted` of every round, oldest first.",
    )
    deferred: list[SettledView] = Field(
        default_factory=list,
        description=(
            'The `outcome == "defer"` subset of the top-level `settled`; empty '
            "without a ledger, where only `deferred_count` is known."
        ),
    )
    deferred_count: int | None = Field(
        default=None,
        description=(
            "`len(deferred)` when a ledger was read, else the sentinel's "
            "`review.deferred`; null when neither was read."
        ),
    )
    releases: list[ReleaseView] = Field(default_factory=list)
    confirm_failures: int | None = Field(
        default=None,
        description=(
            "Every confirm-backend outage the ledger recorded, over its whole "
            "life; null when no ledger was read."
        ),
    )


class ReviewHistory(BaseModel):
    """A ticket's review, round by round (the ``cw review history`` view)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = REVIEW_HISTORY_SCHEMA_VERSION
    ticket_id: str
    client: str
    row_status: str = Field(description="The dev-queue row's status value.")
    row_stage: str = Field(description="The dev-queue row's stage value.")
    in_flight: bool = Field(
        description=(
            "Ledger `in_progress` or `awaiting_confirm`; with no usable "
            "ledger, a RUNNING row at the REVIEW stage."
        )
    )
    rounds: list[RoundView] = Field(default_factory=list)
    summary: HistorySummary
    settled: list[SettledView] = Field(
        default_factory=list,
        description="Every ledger settled entry, once, in ledger order.",
    )
    dispositions: list[DispositionView] = Field(default_factory=list)
    voided: list[VoidedView] = Field(default_factory=list)
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "Sources that contributed, in precedence order: `ledger`, "
            "`verdict_files`, `sentinel`, `row_dispositions`, `events`."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Every degradation and every unattributable event count.",
    )
