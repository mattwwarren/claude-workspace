"""Data models and vocabulary of the converge-to-clean ledger (#2787).

Everything here is plain data. The state machine lives in the sibling modules
(``_engine``, ``_release``, ``_tip``); this module only names the shapes they
exchange. Entry models are frozen so an engine function that "returns a new
ledger" cannot accidentally mutate one it was handed.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cw.exceptions import CwError
from cw.review_adjudication import AdjudicationOutcome
from cw.review_convergence._identity import FindingIdentity
from cw.review_findings import ReviewVerdict

GatingSeverity = Literal["MUST_FIX", "SHOULD_FIX"]
RoundKind = Literal["loop", "confirm"]
ConfirmBackend = Literal["codex", "claude", "none"]
LedgerStatus = Literal["in_progress", "awaiting_confirm", "converged", "stopped"]
LEDGER_STATUS_IN_PROGRESS: LedgerStatus = "in_progress"
LEDGER_STATUS_AWAITING_CONFIRM: LedgerStatus = "awaiting_confirm"
LEDGER_STATUS_CONVERGED: LedgerStatus = "converged"
LEDGER_STATUS_STOPPED: LedgerStatus = "stopped"
Decision = Literal[
    "clean",
    "continue",
    "confirm_required",
    "stop_not_converging",
    "stop_max_cycles",
    "stop_operator_action",
    "stop_confirm_unavailable",
    "stop_confirm_escalated",
]
ReleaseCause = Literal[
    "new_commits", "max_cycles_raised", "operator_action_done", "identities_settled"
]
TipDecision = Literal["accepted", "rereview_required"]
TipReason = Literal[
    "no_clean_round", "head_not_origin_tip", "commits_after_clean_round"
]

#: ``ConvergenceLedger.stop_reason`` values. The field is a plain ``str`` (not a
#: Literal) so a ledger written by a newer cw with a reason this version does
#: not know still loads, and is treated as a terminal stop.
STOP_NO_PROGRESS = "no_progress"
STOP_OSCILLATION = "oscillation"
STOP_MAX_CYCLES = "max_cycles"
STOP_OPERATOR_ACTION = "operator_action"

#: ``detect_no_progress`` compares this many consecutive round-to-round counts.
STALL_WINDOW = 2


class ConvergenceError(CwError):
    """A converge-to-clean precondition failed (wrong sha, unread findings...).

    A :class:`~cw.exceptions.CwError`, so ``handle_errors`` renders it as a
    one-line message with no traceback.
    """

    __slots__ = ()


class RoundFinding(BaseModel):
    """One adjudicated MUST_FIX or SHOULD_FIX finding, as a round records it.

    ``outcome`` is the adjudication outcome (``fix``, ``reject``, ``defer`` or
    ``operator_action``); ``rationale`` is the adjudication rationale (blank is
    legal for ``fix``).
    """

    model_config = ConfigDict(frozen=True)

    severity: GatingSeverity
    file: str
    summary: str
    outcome: AdjudicationOutcome
    rationale: str = ""


class SettledEntry(BaseModel):
    """A finding identity settled as reject/defer, so it never re-opens the loop.

    ``origin="adjudication"`` entries come from a round's reject/defer outcomes.
    ``origin="operator"`` entries come from ``operator_settle`` and carry the
    audit trail (``actor``, ``settled_at``, ``reviewed_sha``) of who silenced a
    recurring false positive, when, and against which sha.
    """

    model_config = ConfigDict(frozen=True)

    identity: FindingIdentity
    outcome: Literal["reject", "defer"]
    rationale: str
    round: int
    origin: Literal["adjudication", "operator"] = "adjudication"
    actor: str | None = None
    settled_at: str | None = None
    reviewed_sha: str | None = None


class FixedEntry(BaseModel):
    """An identity a fix cycle was dispatched for, and the sha it was seen at."""

    model_config = ConfigDict(frozen=True)

    identity: FindingIdentity
    round: int
    reviewed_sha: str


class StopRelease(BaseModel):
    """One audited release of a stop (or reopening of a stopped ledger)."""

    model_config = ConfigDict(frozen=True)

    at: str
    round: int
    reason: str
    cause: ReleaseCause
    sha: str


class LedgerReset(BaseModel):
    """A recoverable snapshot retained when init replaces a stale ledger."""

    model_config = ConfigDict(frozen=True)

    at: str
    reason: str
    previous_ledger: dict[str, object]


class ConfirmFailure(BaseModel):
    """One confirm-backend outage recorded against a candidate sha.

    ``candidate_sha`` (the ledger's ``awaiting_confirm_sha``) and ``round``
    (``len(rounds)`` at the failure) together with ``cause`` key the consecutive
    count (R26): the run resets when any of the three changes.
    """

    model_config = ConfigDict(frozen=True)

    at: str
    cause: str
    detail: str
    backend: str
    candidate_sha: str
    round: int


class SettleTarget(BaseModel):
    """One finding an operator asks ``operator_settle`` to silence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: GatingSeverity
    file: str
    summary: str

    @field_validator("file", "summary")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            msg = f"settle target field must be non-blank (got {value!r})"
            raise ValueError(msg)
        return value


class UnmatchedTarget(BaseModel):
    """A settle target the ledger has never recorded, with its nearest neighbour."""

    model_config = ConfigDict(frozen=True)

    target: SettleTarget
    nearest: FindingIdentity | None = None


class OperatorSettleReport(BaseModel):
    """What ``operator_settle`` did: entries written, targets refused, releasable."""

    settled: list[SettledEntry]
    unmatched: list[UnmatchedTarget]
    releasable: bool


class RoundRecord(BaseModel):
    """The counts of one recorded review round."""

    model_config = ConfigDict(frozen=True)

    round: int
    kind: RoundKind
    backend: str
    reviewed_sha: str
    new_accepted: int
    new_fix: int
    new_operator_action: int
    new_settled: int
    resurfaced_settled: int
    clean: bool
    roster: list[str]
    recorded_at: str


class FrozenCounts(BaseModel):
    """The cycle-0 review counts, frozen from the first verdict round.

    A suppression or a fix is not a re-discovery, so these keep saying what the
    first full review originally found.
    """

    model_config = ConfigDict(frozen=True)

    must_fix_initial: int
    should_fix: int
    agents_run: int
    rejected_count: int = 0
    rejected_count_by_severity: dict[str, int] = Field(default_factory=dict)


class ConvergenceLedger(BaseModel):
    """The whole persisted state of one converge-to-clean review.

    Keyed by ``(ticket_id, fork_point)``. Persisted at
    ``<worktree>/.cw/review-convergence.json`` by :mod:`~cw.review_convergence
    ._store`. The engine functions return updated copies and never mutate one.
    """

    schema_version: Literal[1] = 1
    ticket_id: str
    fork_point: str
    convergence: Literal["clean"] = "clean"
    max_cycles: int
    loop_backend: str
    confirm_backend: ConfirmBackend
    confirm_not_independent: bool = False
    stall_window: int = STALL_WINDOW
    checkpoint_3a_sha: str
    frozen: FrozenCounts | None = None
    rounds: list[RoundRecord] = Field(default_factory=list)
    settled: list[SettledEntry] = Field(default_factory=list)
    fixed: list[FixedEntry] = Field(default_factory=list)
    confirm_failures: list[ConfirmFailure] = Field(default_factory=list)
    awaiting_confirm_sha: str | None = None
    last_clean_sha: str | None = None
    epoch_start_round: int = 0
    window_start_round: int = 0
    fix_cycles_used: int = 0
    status: LedgerStatus = LEDGER_STATUS_IN_PROGRESS
    stop_reason: str | None = None
    stop_identities: list[FindingIdentity] = Field(default_factory=list)
    releases: list[StopRelease] = Field(default_factory=list)
    reset_history: list[LedgerReset] = Field(default_factory=list)


class RoundInput(BaseModel):
    """The ``record-round`` request envelope: a round with a verdict or findings."""

    model_config = ConfigDict(extra="forbid")

    kind: RoundKind
    backend: str
    reviewed_sha: str
    roster: list[str] = Field(default_factory=list)
    verdict: ReviewVerdict | None = None
    findings: list[RoundFinding] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> RoundInput:
        if (self.verdict is None) == (self.findings is None):
            msg = "exactly one of 'verdict' or 'findings' is required"
            raise ValueError(msg)
        return self


class UnreadFindings(BaseModel):
    """What a verdict lost unread: the guard input of ``record_round`` (R29).

    ``rejected_must_fix`` and ``discarded_run_failures`` count findings the
    consolidator or a reviewer failure threw away unread; ``failed_roles`` names
    the reviewer roles that failed outright and so never reviewed at all.
    """

    model_config = ConfigDict(frozen=True)

    rejected_must_fix: int = 0
    discarded_run_failures: int = 0
    failed_roles: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        """True when anything was lost unread."""
        return bool(
            self.rejected_must_fix or self.discarded_run_failures or self.failed_roles
        )


class RoundReport(BaseModel):
    """The result of recording a round (or a confirm-backend failure).

    ``reason`` is the ledger's ``stop_reason`` for a ``stop_*`` decision from
    ``record_round``, the confirm ``cause`` for the two confirm stops, and
    ``None`` otherwise. ``history`` is the ``new_accepted`` count of each round
    since ``epoch_start_round``, oldest first. ``next_fix_cycle`` is the number
    of the fix cycle a ``continue`` just dispatched (the post-increment
    ``fix_cycles_used``), and ``fix_cycles_used + 1`` for every other decision.
    """

    decision: Decision
    reason: str | None = None
    round: int
    new_accepted: int
    new_settled: int = 0
    resurfaced_settled: int = 0
    fix_cycles_used: int
    next_fix_cycle: int
    history: list[int]
    confirm_not_independent: bool = False


class TipVerdict(BaseModel):
    """The strict final-sha acceptance rule's answer (R22)."""

    model_config = ConfigDict(frozen=True)

    decision: TipDecision
    reason: TipReason | None = None


class Reconciled(NamedTuple):
    """Return type of ``reconcile_ledger_for_init``."""

    ledger: ConvergenceLedger
    reset_reason: str | None
    released: StopRelease | None
