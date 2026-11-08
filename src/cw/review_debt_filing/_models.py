"""Typed inputs and results for review-debt filing (#3041)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from cw.review_debt_filing._constants import DEBT_FILING_BUDGET_S
from cw.review_finding_dispositions import debt_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models.review_debt_record import ReviewDebtRecord, ReviewDebtTracker


class TrackerMode(StrEnum):
    """How a client's deferrals become tickets.

    ``github``: cw files through ``gh`` (when ``auto_create_issues`` is true).
    ``mcp``: cw emits payloads, the worker files them through MCP and reports
    the IDs back (Linear). ``skip``: any other or unset tracker; nothing filed.
    """

    GITHUB = "github"
    MCP = "mcp"
    SKIP_NO_TRACKER = "skipped_no_tracker"


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
