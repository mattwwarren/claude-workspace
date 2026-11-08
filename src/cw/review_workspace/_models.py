"""The transaction phase vocabulary, manifest model, marker view and result.

Imports from no sibling. ``TransactionPhase`` stays beside ``_Transaction``
because pydantic resolves the ``phase`` annotation from this module's globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from cw.review_findings import ReviewerRunFailure

TransactionPhase = Literal[
    "prepared",
    "active_directory_archived",
    "active_marker_archived",
    "archive_committed",
    "staging_directory_promoted",
    "staging_marker_promoted",
    "committed",
]


class _Transaction(BaseModel):
    """On-disk manifest for one ordered archive and promotion transaction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ticket: str
    round_id: str
    phase: TransactionPhase
    has_old_round: bool
    previous_ticket: str | None = None
    previous_round_id: str | None = None
    active_directory: str
    active_marker: str
    archive_directory: str
    archive_marker: str
    staging_directory: str
    staging_marker: str


@dataclass(frozen=True)
class _Marker:
    """Validated view over a marker, retaining unknown legacy metadata."""

    raw: dict[str, object]
    ticket: str
    round_id: str
    status: str
    roles: dict[str, str]
    failures: tuple[ReviewerRunFailure, ...]
    friction: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceResult:
    """Summary returned by initialization or reconciliation."""

    action: Literal["started", "resumed"]
    ticket: str
    round_id: str
    pending_roles: tuple[str, ...]
    completed_roles: tuple[str, ...]
    failed_reviewers: tuple[ReviewerRunFailure, ...]
