"""Record one engine round's summary as the Repo Review Engine document (R9).

Ordered checks; the first failure wins. A failure is recorded through
``record_failure`` with its :class:`EngineRoundFailure` reason, and the caller
exits 3. Success records the mapped document through ``record_document``. No
other path writes engine findings; ``cw review workspace record-document``
refuses ``source == "engine"``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw._git import git_clean_env
from cw.review_engine.constants import (
    ENGINE_ROLE,
    FRICTION,
    EngineRoundFailure,
)
from cw.review_engine.mapping import engine_document
from cw.review_engine.materialize import engine_paths
from cw.review_engine_summary_io import EngineSummaryError, load_review_engine_summary
from cw.review_findings import ReviewerRunFailure
from cw.review_workspace import record_document, record_failure

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.review_engine_summary import ReviewEngineSummary

_Failure = tuple[EngineRoundFailure, str]


@dataclass(frozen=True)
class RecordResult:
    """What ``record`` prints. ``reason`` and ``friction`` are set on failure."""

    result: Literal["recorded", "failed"]
    reason: EngineRoundFailure | None
    friction: str | None
    findings_recorded: int


def _git(worktree: Path, argv: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *argv],
        capture_output=True,
        check=True,
        text=True,
        env=git_clean_env(),
    )
    return completed.stdout


def _precheck(worktree: Path, expect_head: str) -> _Failure | None:
    """HEAD must be the pinned sha and the tree must be untouched by the engine."""
    head = _git(worktree, ["rev-parse", "HEAD"]).strip()
    if head != expect_head:
        reason = EngineRoundFailure.ENGINE_HEAD_MISMATCH
        detail = FRICTION[reason].format(
            summary="-", expected=expect_head[:12], head=head[:12]
        )
        return reason, detail
    dirty = _git(worktree, ["status", "--porcelain"]).strip()
    stat = _git(worktree, ["diff", "HEAD", "--stat"]).strip()
    if dirty or stat:
        reason = EngineRoundFailure.ENGINE_WORKTREE_MUTATED
        return reason, FRICTION[reason]
    return None


def _load_summary(path: Path) -> ReviewEngineSummary | _Failure:
    """The validated summary, or the failure the loader reports."""
    if not path.is_file():
        reason = EngineRoundFailure.ENGINE_NO_SUMMARY
        return reason, FRICTION[reason].format(path=str(path))
    try:
        return load_review_engine_summary(path)
    except EngineSummaryError as exc:
        reason = EngineRoundFailure.ENGINE_SUMMARY_INVALID
        detail = FRICTION[reason].format(
            reason=str(exc.reason), field_path=exc.field_path or "-"
        )
        return reason, detail


def _summary_failure(summary: ReviewEngineSummary, expect_head: str) -> _Failure | None:
    """Head, coverage and gating checks on a summary that validated."""
    if summary.head_sha != expect_head:
        reason = EngineRoundFailure.ENGINE_HEAD_MISMATCH
        detail = FRICTION[reason].format(
            summary=summary.head_sha[:12],
            expected=expect_head[:12],
            head=expect_head[:12],
        )
        return reason, detail
    if summary.coverage != "COMPLETE":
        reason = EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE
        return reason, FRICTION[reason].format(coverage=summary.coverage)
    gating = [f for f in summary.findings if f.severity in ("MUST_FIX", "SHOULD_FIX")]
    if summary.verdict == "FAIL" and not gating:
        reason = EngineRoundFailure.ENGINE_SUMMARY_INVALID
        detail = FRICTION[reason].format(
            reason="fail_without_gating_finding", field_path="-"
        )
        return reason, detail
    return None


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
    """Validate the engine summary and record it, or record why the round failed."""
    path = summary_path or engine_paths(worktree, round_id).summary_path
    failure = _precheck(worktree, expect_head)
    if failure is None:
        loaded = _load_summary(path)
        if isinstance(loaded, tuple):
            failure = loaded
        else:
            failure = _summary_failure(loaded, expect_head)
            if failure is None:
                document = engine_document(loaded, skill)
                record_document(
                    worktree, role=ENGINE_ROLE, content=document.model_dump_json()
                )
                return RecordResult(
                    result="recorded",
                    reason=None,
                    friction=None,
                    findings_recorded=len(document.findings),
                )
    reason, detail = failure
    return _fail(worktree, reason, detail)
