"""The pure validate-and-map core of an engine round (R1, #3216).

Ordered checks; the first failure wins:

1. :func:`engine_precheck` -- HEAD is the pinned sha and the tree is untouched.
2. :func:`load_summary` -- the summary file exists and validates.
3. :func:`summary_failure` -- head, coverage and verdict/gating checks.

:func:`validate_engine_summary` runs all three and maps a good summary to the
one ``Repo Review Engine`` document. It writes nothing: the claude-native CLI
adapter (:mod:`cw.review_engine.record`) records the result in the review
workspace, and the codex lane feeds the document straight into its verdict
synthesis. Heavy module (runs git); import it by submodule path.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_clean_env
from cw.review_engine.constants import FRICTION, EngineRoundFailure
from cw.review_engine.mapping import engine_document
from cw.review_engine_summary_io import EngineSummaryError, load_review_engine_summary

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.review_engine_summary import ReviewEngineSummary
    from cw.review_findings import ReviewerFindingsDocument


class SummaryFailure(NamedTuple):
    """Why an engine round failed, and its friction line."""

    reason: EngineRoundFailure
    detail: str


class ValidatedSummary(NamedTuple):
    """A summary that passed every check, and the document it maps to."""

    summary: ReviewEngineSummary
    document: ReviewerFindingsDocument


def _git(worktree: Path, argv: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *argv],
        capture_output=True,
        check=True,
        text=True,
        env=git_clean_env(),
    )
    return completed.stdout


def engine_precheck(worktree: Path, expect_head: str) -> SummaryFailure | None:
    """HEAD must be the pinned sha and the tree must be untouched by the engine."""
    head = _git(worktree, ["rev-parse", "HEAD"]).strip()
    if head != expect_head:
        reason = EngineRoundFailure.ENGINE_HEAD_MISMATCH
        detail = FRICTION[reason].format(
            summary="-", expected=expect_head[:12], head=head[:12]
        )
        return SummaryFailure(reason, detail)
    dirty = _git(worktree, ["status", "--porcelain"]).strip()
    stat = _git(worktree, ["diff", "HEAD", "--stat"]).strip()
    if dirty or stat:
        reason = EngineRoundFailure.ENGINE_WORKTREE_MUTATED
        return SummaryFailure(reason, FRICTION[reason])
    return None


def load_summary(path: Path) -> ReviewEngineSummary | SummaryFailure:
    """The validated summary, or the failure the loader reports."""
    if not path.is_file():
        reason = EngineRoundFailure.ENGINE_NO_SUMMARY
        return SummaryFailure(reason, FRICTION[reason].format(path=str(path)))
    try:
        return load_review_engine_summary(path)
    except EngineSummaryError as exc:
        reason = EngineRoundFailure.ENGINE_SUMMARY_INVALID
        detail = FRICTION[reason].format(
            reason=str(exc.reason), field_path=exc.field_path or "-"
        )
        return SummaryFailure(reason, detail)


def summary_failure(
    summary: ReviewEngineSummary, expect_head: str
) -> SummaryFailure | None:
    """Head, coverage and gating checks on a summary that validated."""
    if summary.head_sha != expect_head:
        reason = EngineRoundFailure.ENGINE_HEAD_MISMATCH
        detail = FRICTION[reason].format(
            summary=summary.head_sha[:12],
            expected=expect_head[:12],
            head=expect_head[:12],
        )
        return SummaryFailure(reason, detail)
    if summary.coverage != "COMPLETE":
        reason = EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE
        return SummaryFailure(
            reason, FRICTION[reason].format(coverage=summary.coverage)
        )
    gating = [f for f in summary.findings if f.severity in ("MUST_FIX", "SHOULD_FIX")]
    if summary.verdict == "FAIL" and not gating:
        reason = EngineRoundFailure.ENGINE_SUMMARY_INVALID
        detail = FRICTION[reason].format(
            reason="fail_without_gating_finding", field_path="-"
        )
        return SummaryFailure(reason, detail)
    return None


def validate_engine_summary(
    path: Path, *, expect_head: str, skill: str
) -> ValidatedSummary | SummaryFailure:
    """The summary at *path* checked and mapped, or why the round failed.

    Steps 2 and 3 only; callers run :func:`engine_precheck` first, because the
    claude-native adapter and the codex lane pin HEAD at different moments.
    """
    loaded = load_summary(path)
    if isinstance(loaded, SummaryFailure):
        return loaded
    failure = summary_failure(loaded, expect_head)
    if failure is not None:
        return failure
    return ValidatedSummary(loaded, engine_document(loaded, skill))
