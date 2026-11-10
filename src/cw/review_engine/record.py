"""Record one engine round's summary as the Repo Review Engine document (R9).

Ordered checks (the shared core, :mod:`cw.review_engine.validate`); the first
failure wins. A failure is recorded through
``record_failure`` with its :class:`EngineRoundFailure` reason, and the caller
exits 3. Success records the mapped document through ``record_document``. No
other path writes engine findings; ``cw review workspace record-document``
refuses ``source == "engine"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.review_engine.constants import ENGINE_ROLE, EngineRoundFailure
from cw.review_engine.materialize import engine_paths
from cw.review_engine.validate import (
    SummaryFailure,
    engine_precheck,
    validate_engine_summary,
)
from cw.review_findings import ReviewerRunFailure
from cw.review_workspace import record_document, record_failure

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class RecordResult:
    """What ``record`` prints. ``reason`` and ``friction`` are set on failure."""

    result: Literal["recorded", "failed"]
    reason: EngineRoundFailure | None
    friction: str | None
    findings_recorded: int


def _fail(worktree: Path, reason: EngineRoundFailure, detail: str) -> RecordResult:
    record_failure(
        worktree,
        role=ENGINE_ROLE,
        failure=ReviewerRunFailure(role=ENGINE_ROLE, reason=str(reason)),
    )
    return RecordResult(
        result="failed", reason=reason, friction=detail, findings_recorded=0
    )


def record_engine_round(
    worktree: Path,
    *,
    round_id: str,
    expect_head: str,
    summary_path: Path | None,
    skill: str,
) -> RecordResult:
    """Validate the engine summary and record it, or record why the round failed.

    The checks are the shared core in :mod:`cw.review_engine.validate`; this
    adapter only writes the outcome to the review workspace.
    """
    path = summary_path or engine_paths(worktree, round_id).summary_path
    outcome = engine_precheck(worktree, expect_head) or validate_engine_summary(
        path, expect_head=expect_head, skill=skill
    )
    if isinstance(outcome, SummaryFailure):
        return _fail(worktree, outcome.reason, outcome.detail)
    document = outcome.document
    record_document(worktree, role=ENGINE_ROLE, content=document.model_dump_json())
    return RecordResult(
        result="recorded",
        reason=None,
        friction=None,
        findings_recorded=len(document.findings),
    )
