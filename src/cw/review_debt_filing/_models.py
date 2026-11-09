"""Typed inputs and results for review-debt filing (#3041) and closing (#3044)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from cw.review_debt_filing._constants import (
    DEBT_CLOSE_SWEEP_INTERVAL_S,
    DEBT_FILING_BUDGET_S,
    MIN_CALL_TIMEOUT_S,
)
from cw.review_finding_dispositions import debt_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.gh import GhFailure
    from cw.models.review_debt_record import ReviewDebtRecord, ReviewDebtTracker
    from cw.review_debt_filing._events import AuditWarning


class TrackerMode(StrEnum):
    """How a client's deferrals become tickets.

    ``github``: cw files through ``gh`` (when ``auto_create_issues`` is true).
    ``mcp``: cw emits payloads, the worker files them through MCP and reports
    the IDs back (Linear). ``skip``: any other or unset tracker; nothing filed.
    """

    GITHUB = "github"
    MCP = "mcp"
    SKIP_NO_TRACKER = "skipped_no_tracker"


class DebtRunStatus(StrEnum):
    """The ``status`` values the ``cw review debt`` commands print (stdout JSON).

    One closed vocabulary shared by every subcommand, so a run's outcome is
    spelled once. The values are the wire contract the skill prose reads.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"
    EMITTED_ONLY = "emitted_only"
    MCP_PENDING = "mcp_pending"
    EMITTED = "emitted"
    NOTHING_TO_EMIT = "nothing_to_emit"
    REFUSED = "refused"
    RECORDED = "recorded"
    NOTHING_EXPECTED = "nothing_expected"
    VERIFY_FAILED = "verify_failed"
    LINKED = "linked"
    DEGRADED = "degraded"
    NOTHING_TO_LINK = "nothing_to_link"
    SKIPPED_NO_PR = "skipped_no_pr"


class LinkOutcome(StrEnum):
    """The outcome for one review-debt PR-link attempt."""

    LINKED = "linked"
    ALREADY = "already"
    FAILED = "failed"
    SKIPPED = "skipped"


class FilingFailureKind(StrEnum):
    """Why one entry stayed unticketed."""

    NETWORK = "network"
    AUTH = "auth"
    LABEL = "label"
    OTHER = "other"
    LOOKUP_FAILED = "lookup_failed"
    VISIBILITY_UNDETERMINED = "visibility_undetermined"
    BUDGET_EXHAUSTED = "budget_exhausted"


class DebtEntry(NamedTuple):
    """One deferred finding to file.

    ``ticket`` is the artifact's ``ticket:`` value, empty while unticketed.
    ``line``/``evidence`` are known only for entries applied in the same call;
    an entry read back from the artifact has neither. ``fingerprint`` and
    ``rule_id`` come from the review engine (RFC 0017 I1, #3039), which
    ``cw review adjudicate`` does not supply yet.
    """

    severity: str
    file: str
    summary: str
    rationale: str
    line: int | None = None
    evidence: str = ""
    rule_id: str = ""
    fingerprint: str | None = None
    ticket: str = ""

    @property
    def key(self) -> str:
        """The entry's ``debt_key``."""
        return debt_key(self.file, self.summary, fingerprint=self.fingerprint)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FilingContext:
    """Everything one filing run needs besides the entries and the tracker.

    ``ticket_id``/``client`` identify the queue ticket (the events'
    ``correlation_id`` and the dedup scope); ``ticket_ref`` is how a body
    names it (``#3041`` on GitHub). ``existing`` is the row's recorded debt,
    or None when the row could not be resolved, in which case nothing is
    created. ``visibility`` is the repo visibility, or None when it could not
    be determined, in which case nothing is filed or emitted. ``home`` and
    ``worktrees`` are the redaction anchors. ``clock`` and ``now`` are
    injectable for the budget and ``filed_at`` tests.
    """

    ticket_id: str
    client: str
    branch: str
    ticket_ref: str
    tracker: ReviewDebtTracker
    existing: tuple[ReviewDebtRecord, ...] | None
    visibility: str | None
    home: str = ""
    worktrees: tuple[str, ...] = ()
    budget_s: float = DEBT_FILING_BUDGET_S
    clock: Callable[[], float] = field(default=time.monotonic)
    now: Callable[[], datetime] = field(default=_utc_now)


class FilingFailure(NamedTuple):
    """One entry that stayed unticketed, and why."""

    key: str
    kind: FilingFailureKind
    detail: str
    summary: str


class FilingResult(NamedTuple):
    """What :func:`file_review_debt` did.

    ``records`` are new or marker-recovered records for the caller to upsert.
    ``deduped_keys`` were skipped because a ticket already exists (row record
    or body marker). The library never raises on a ``gh`` failure.
    """

    records: list[ReviewDebtRecord]
    failures: list[FilingFailure]
    deduped_keys: tuple[str, ...]
    audit_warnings: tuple[AuditWarning, ...] = ()


class CallBudget:
    """A wall-clock budget shared by a run's ``gh`` calls.

    Each call gets ``min(cap, remaining)`` seconds; once less than
    :data:`MIN_CALL_TIMEOUT_S` remains, :meth:`timeout` returns None and the
    caller stops. Shared by filing (#3041) and the close sweep (#3044).
    """

    def __init__(self, clock: Callable[[], float], budget_s: float) -> None:
        self._clock = clock
        self._deadline = clock() + budget_s

    def timeout(self, cap: int) -> int | None:
        """``min(cap, remaining)``, or None when the budget is spent."""
        remaining = self._deadline - self._clock()
        if remaining < MIN_CALL_TIMEOUT_S:
            return None
        return min(cap, int(remaining))


class CloseOutcome(StrEnum):
    """What the close sweep (#3044) did with one open debt issue."""

    CLOSED = "closed"
    SKIPPED_PR_OPEN = "skipped_pr_open"
    SKIPPED_PR_MERGED = "skipped_pr_merged"
    SKIPPED_PR_UNKNOWN = "skipped_pr_unknown"
    SKIPPED_REPO_MISMATCH = "skipped_repo_mismatch"
    FAILED = "failed"


class CloseResult(NamedTuple):
    """One debt issue the close sweep looked at.

    ``pr_urls`` are the PR markers parsed from its body. ``failure`` is the
    ``gh issue close`` failure kind, set only when ``outcome`` is ``FAILED``.
    """

    client: str
    issue: int
    pr_urls: tuple[str, ...]
    outcome: CloseOutcome
    failure: GhFailure | None = None


@dataclass(frozen=True)
class CloseContext:
    """The per-client context one close needs: its name and redaction anchors.

    The sweep runs from the dispatch tick, which has no ``cw-context.json``,
    so this stands in for :class:`FilingContext`.
    """

    client: str
    home: str = ""
    worktrees: tuple[str, ...] = ()


@dataclass
class DebtCloseState:
    """Process-local state the close sweep carries from one tick to the next.

    ``next_due`` is the interval gate; :meth:`claim` advances it before a
    sweep runs, so a sweep that raises still waits a full interval.
    ``merged_prs`` memoizes PRs seen MERGED (terminal, never re-read).
    ``reported`` holds the (client, issue, kind) tuples a
    ``review.debt_close_failed`` event was already recorded for.
    """

    clock: Callable[[], float] = field(default=time.monotonic)
    interval_s: float = DEBT_CLOSE_SWEEP_INTERVAL_S
    next_due: float | None = None
    merged_prs: set[str] = field(default_factory=set)
    reported: set[tuple[str, int, str]] = field(default_factory=set)

    def claim(self) -> bool:
        """True when a sweep is due; advances the gate before returning True."""
        now = self.clock()
        if self.next_due is not None and now < self.next_due:
            return False
        self.next_due = now + self.interval_s
        return True
