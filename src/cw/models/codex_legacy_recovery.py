"""The legacy codex recovery marker (RFC 0014 B1, #2389).

``cw codex migrate-legacy`` records every per-session outcome here, at
``~/.local/share/cw/codex_legacy_recovery.json``. The marker is the run
record and the B2 gate: B2 may retire the boot sweep only once the marker
exists, ``completed_at`` is set, and ``unresolved`` is empty.

``extra="forbid"`` because a hand-edited or corrupted marker must fail loudly
(the loader turns it into ``CodexLegacyRecoveryMarkerError``) rather than be
read as "nothing to do".

Depends only on ``enums``; sits alongside it at the DAG root. See
``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, Field

from cw.models.enums import SessionStatus, Stage

CODEX_LEGACY_RECOVERY_SCHEMA_VERSION = 1


def _raise_marker_error(message: str) -> NoReturn:
    raise ValueError(message)


class CodexLegacyDisposition(StrEnum):
    """What the recovery did with one legacy session.

    ``FAILED`` and ``SKIPPED_WRITER_LIVE`` leave the session unresolved: a
    later run retries it, and the marker cannot complete while any remain.
    """

    REQUEUED = "requeued"
    PARKED = "parked"
    FAILED = "failed"
    SKIPPED_ALREADY_HANDLED = "skipped_already_handled"
    SKIPPED_WRITER_LIVE = "skipped_writer_live"


class LegacyRecoveryStatus(StrEnum):
    """How one ``cw codex migrate-legacy`` invocation ended."""

    COMPLETED = "completed"
    ALREADY_COMPLETED = "already_completed"
    PARTIAL = "partial"


class UnresolvedEntry(BaseModel):
    """A session the recovery could not resolve, and why."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    client: str
    ticket_id: str
    reason: str


class Outcome(BaseModel):
    """The latest disposition for one session, with its pre-recovery facts.

    ``prior_status``/``prior_stage`` are what an operator reads before
    reversing a disposition. ``prior_stage`` is None when the session had no
    dev-queue row.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    ticket_id: str
    client: str
    disposition: CodexLegacyDisposition
    prior_status: SessionStatus
    prior_stage: Stage | None = None


class PendingOutcome(BaseModel):
    """A durable intent for a session whose live recovery is in progress."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    ticket_id: str
    client: str
    prior_status: SessionStatus
    prior_stage: Stage | None = None


class CodexLegacyRecoveryMarker(BaseModel):
    """Per-session outcomes plus counts derived from them.

    One ``outcomes`` entry per session id (latest wins). The counts are
    recomputed from ``outcomes`` on every write, so ``scanned`` always equals
    the sum of the five buckets and ``len(outcomes)``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CODEX_LEGACY_RECOVERY_SCHEMA_VERSION
    # Records the last selected client for operator visibility.  It is not an
    # authorization to mutate another client; covered_clients gates completion.
    client_scope: str | None = None
    covered_clients: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None
    scanned: int = 0
    requeued: int = 0
    parked: int = 0
    failed: int = 0
    skipped_already_handled: int = 0
    skipped_writer_live: int = 0
    unresolved: list[UnresolvedEntry] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    pending: list[PendingOutcome] = Field(default_factory=list)

    def validate_consistency(self) -> None:
        """Reject a marker that cannot safely serve as the B2 run record."""
        if self.schema_version != CODEX_LEGACY_RECOVERY_SCHEMA_VERSION:
            _raise_marker_error(
                f"unsupported schema_version {self.schema_version}; expected "
                f"{CODEX_LEGACY_RECOVERY_SCHEMA_VERSION}"
            )
        counts = {
            "scanned": self.scanned,
            "requeued": self.requeued,
            "parked": self.parked,
            "failed": self.failed,
            "skipped_already_handled": self.skipped_already_handled,
            "skipped_writer_live": self.skipped_writer_live,
        }
        if any(value < 0 for value in counts.values()):
            _raise_marker_error("marker counts must be non-negative")
        if len({outcome.session_id for outcome in self.outcomes}) != len(self.outcomes):
            _raise_marker_error("marker outcomes must contain one entry per session")
        pending_ids = {pending.session_id for pending in self.pending}
        if len(pending_ids) != len(self.pending) or pending_ids & {
            outcome.session_id for outcome in self.outcomes
        }:
            _raise_marker_error("marker pending entries must be unique and unresolved")
        actual = Counter(outcome.disposition for outcome in self.outcomes)
        expected = {
            "requeued": actual[CodexLegacyDisposition.REQUEUED],
            "parked": actual[CodexLegacyDisposition.PARKED],
            "failed": actual[CodexLegacyDisposition.FAILED],
            "skipped_already_handled": actual[
                CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
            ],
            "skipped_writer_live": actual[CodexLegacyDisposition.SKIPPED_WRITER_LIVE],
        }
        if self.scanned != len(self.outcomes) or any(
            counts[name] != value for name, value in expected.items()
        ):
            _raise_marker_error("marker counts do not match outcomes")
        unresolved_ids = {entry.session_id for entry in self.unresolved}
        if len(unresolved_ids) != len(self.unresolved):
            _raise_marker_error("marker unresolved entries must be unique")
        outcomes = {outcome.session_id: outcome for outcome in self.outcomes}
        expected_unresolved = {
            outcome.session_id
            for outcome in self.outcomes
            if outcome.disposition
            in {
                CodexLegacyDisposition.FAILED,
                CodexLegacyDisposition.SKIPPED_WRITER_LIVE,
            }
        }
        if unresolved_ids != expected_unresolved:
            _raise_marker_error("unresolved entries do not match outcomes")
        for entry in self.unresolved:
            outcome = outcomes.get(entry.session_id)
            if outcome is None or outcome.disposition not in {
                CodexLegacyDisposition.FAILED,
                CodexLegacyDisposition.SKIPPED_WRITER_LIVE,
            }:
                _raise_marker_error("unresolved entries must match failed outcomes")
            if (entry.client, entry.ticket_id) != (outcome.client, outcome.ticket_id):
                _raise_marker_error("unresolved entry does not match its outcome")
        if len(set(self.covered_clients)) != len(self.covered_clients):
            _raise_marker_error("covered clients must be unique")
        if self.completed_at is not None and (self.unresolved or self.pending):
            _raise_marker_error(
                "a completed marker cannot have unresolved or pending sessions"
            )
