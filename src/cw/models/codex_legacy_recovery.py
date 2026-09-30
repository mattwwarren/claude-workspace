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

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cw.models.enums import SessionStatus, Stage

CODEX_LEGACY_RECOVERY_SCHEMA_VERSION = 1


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


class CodexLegacyRecoveryMarker(BaseModel):
    """Per-session outcomes plus counts derived from them.

    One ``outcomes`` entry per session id (latest wins). The counts are
    recomputed from ``outcomes`` on every write, so ``scanned`` always equals
    the sum of the five buckets and ``len(outcomes)``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CODEX_LEGACY_RECOVERY_SCHEMA_VERSION
    completed_at: datetime | None = None
    scanned: int = 0
    requeued: int = 0
    parked: int = 0
    failed: int = 0
    skipped_already_handled: int = 0
    skipped_writer_live: int = 0
    unresolved: list[UnresolvedEntry] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
