"""Running and merging a sharded review round (#2864).

:func:`run_sharded_roles` runs the full roster once per shard planned by
:mod:`cw.codex_review._shard`, one shard after another under one shared
deadline, and merges the shard results into the four values
``run_codex_roles`` returns plus the per-shard coverage records:

- documents: per role, its non-failed shard documents merge into one, so a
  round where every shard succeeds has exactly the unsharded ``agents_run``
  shape. A ``failed`` shard document cannot carry findings, so it passes
  through unmerged and the engine's failed-role guard still sees it.
- failures: every shard's failures in shard order, each annotated
  ``shard i/n`` ahead of its original detail.
- metrics: summed per role over the shards that ran it.
- pre-validation rejects: concatenated.

``time`` is imported as a module so tests can drive the shared deadline.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._roles import run_codex_roles
from cw.review_findings import ReviewerFindingsDocument, ShardRecord

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_review._shard import _ShardPlan
    from cw.codex_runner import CodexRunner
    from cw.review_findings import (
        RejectedFinding,
        ReviewerHealthStatus,
        ReviewerRunFailure,
        ReviewerRunMetrics,
    )


class ShardedRun(NamedTuple):
    """The merged result of a sharded round: ``run_codex_roles``' four + coverage."""

    documents: list[ReviewerFindingsDocument]
    failures: list[ReviewerRunFailure]
    metrics_by_role: dict[str, ReviewerRunMetrics]
    pre_validation_rejected: list[RejectedFinding]
    coverage: list[ShardRecord]


class _ShardResult(NamedTuple):
    """One shard's ``run_codex_roles`` result, documents keyed by roster role."""

    docs_by_role: dict[str, ReviewerFindingsDocument]
    failures: list[ReviewerRunFailure]
    metrics_by_role: dict[str, ReviewerRunMetrics]
    rejected: list[RejectedFinding]


def _remaining_seconds(deadline: float | None) -> int | None:
    """Seconds left before *deadline*, never negative; ``None`` stays ``None``."""
    if deadline is None:
        return None
    return max(int(deadline - time.monotonic()), 0)


def _docs_by_role(
    roles: Sequence[str],
    documents: Sequence[ReviewerFindingsDocument],
    failures: Sequence[ReviewerRunFailure],
) -> dict[str, ReviewerFindingsDocument]:
    """Key one ``run_codex_roles`` call's documents by the role that wrote them.

    That call runs *roles* in order and gives each exactly one document or one
    failure, so the documents belong, in order, to the roles without a
    failure. A count mismatch is an internal fault (``ValueError``).
    """
    failed = {failure.role for failure in failures}
    ran = [role for role in roles if role not in failed]
    return dict(zip(ran, documents, strict=True))


def run_sharded_roles(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    plans: Sequence[_ShardPlan],
    model: str | None,
    reasoning_effort: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> ShardedRun:
    """Run every role over every shard in *plans*, in order, and merge them.

    The shards share one monotonic deadline: each ``run_codex_roles`` call gets
    the seconds that remain, and ``None`` (no budget) stays ``None``.
    """
    deadline = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    results: list[_ShardResult] = []
    for plan in plans:
        documents, failures, metrics, rejected = run_codex_roles(
            runner=runner,
            worktree=worktree,
            roles=roles,
            prompts_by_role=plan.prompts_by_role,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=_remaining_seconds(deadline),
            session_id=session_id,
        )
        results.append(
            _ShardResult(
                _docs_by_role(roles, documents, failures), failures, metrics, rejected
            )
        )
    return merge_shard_results(roles, plans, results)


def merge_shard_results(
    roles: Sequence[str],
    plans: Sequence[_ShardPlan],
    results: Sequence[_ShardResult],
) -> ShardedRun:
    """Merge per-shard results into one round's documents, failures and metrics."""
    total = len(plans)
    documents: list[ReviewerFindingsDocument] = []
    metrics_by_role: dict[str, ReviewerRunMetrics] = {}
    for role in roles:
        shard_docs = [
            (index, result.docs_by_role[role])
            for index, result in enumerate(results, start=1)
            if role in result.docs_by_role
        ]
        kept = [(index, doc) for index, doc in shard_docs if doc.status != "failed"]
        if kept:
            documents.append(merge_shard_documents(kept, total))
        documents.extend(doc for _index, doc in shard_docs if doc.status == "failed")
        bags = [r.metrics_by_role[role] for r in results if role in r.metrics_by_role]
        if bags:
            metrics_by_role[role] = merge_shard_metrics(bags)
    failures = [
        annotate_shard_failure(failure, index, total)
        for index, result in enumerate(results, start=1)
        for failure in result.failures
    ]
    rejected = [finding for result in results for finding in result.rejected]
    return ShardedRun(
        documents=documents,
        failures=failures,
        metrics_by_role=metrics_by_role,
        pre_validation_rejected=rejected,
        coverage=shard_coverage(roles, plans, results),
    )


def merge_shard_documents(
    shard_docs: Sequence[tuple[int, ReviewerFindingsDocument]], total: int
) -> ReviewerFindingsDocument:
    """One role's non-failed shard documents (``(index, doc)``) as one document.

    Findings are concatenated in shard order; the status is ``degraded`` if any
    shard's is. ``detail`` joins each shard's own detail as ``shard i/n: ...``
    and, when none had one on an ``ok`` document with no findings, states the
    coverage instead so the merged document still says what it checked.
    """
    findings = [finding for _index, doc in shard_docs for finding in doc.findings]
    degraded = any(doc.status == "degraded" for _index, doc in shard_docs)
    status: ReviewerHealthStatus = "degraded" if degraded else "ok"
    detail = "; ".join(
        f"shard {index}/{total}: {doc.detail}"
        for index, doc in shard_docs
        if doc.detail
    )
    if not detail and status == "ok" and not findings:
        count = len(shard_docs)
        detail = (
            f"reviewed all {total} shards"
            if count == total
            else f"reviewed {count} of {total} shards"
        )
    return ReviewerFindingsDocument(
        reviewer_role=shard_docs[0][1].reviewer_role,
        status=status,
        detail=detail,
        findings=findings,
    )


def annotate_shard_failure(
    failure: ReviewerRunFailure, index: int, total: int
) -> ReviewerRunFailure:
    """*failure* with ``shard i/n`` prefixed to its detail, never replacing it."""
    prefix = f"shard {index}/{total}"
    detail = f"{prefix}: {failure.detail}" if failure.detail else prefix
    return failure.model_copy(update={"detail": detail})


def _sum_or_none[T: (int, float)](values: Sequence[T | None]) -> T | None:
    """The sum of the set *values*, or ``None`` when none is set."""
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def merge_shard_metrics(bags: Sequence[ReviewerRunMetrics]) -> ReviewerRunMetrics:
    """One role's per-shard telemetry summed over the shards that ran it.

    Durations, token counts and tool-call counts add up; ``thread_id`` and
    ``effective_model`` come from the first shard, ``terminal_event`` from the
    last.
    """
    first, last = bags[0], bags[-1]
    tool_calls: dict[str, int] = {}
    for bag in bags:
        for tool, count in bag.get("tool_call_counts", {}).items():
            tool_calls[tool] = tool_calls.get(tool, 0) + count
    return {
        "thread_id": first.get("thread_id"),
        "effective_model": first.get("effective_model"),
        "duration_seconds": _sum_or_none([b.get("duration_seconds") for b in bags]),
        "input_tokens": _sum_or_none([b.get("input_tokens") for b in bags]),
        "cached_input_tokens": _sum_or_none(
            [b.get("cached_input_tokens") for b in bags]
        ),
        "output_tokens": _sum_or_none([b.get("output_tokens") for b in bags]),
        "reasoning_tokens": _sum_or_none([b.get("reasoning_tokens") for b in bags]),
        "terminal_event": last.get("terminal_event"),
        "tool_call_counts": tool_calls,
        "had_command_evidence": any(b.get("had_command_evidence", False) for b in bags),
        "unexpected_tool_attempts": [
            attempt for b in bags for attempt in b.get("unexpected_tool_attempts", [])
        ],
    }


def shard_coverage(
    roles: Sequence[str],
    plans: Sequence[_ShardPlan],
    results: Sequence[_ShardResult],
) -> list[ShardRecord]:
    """One :class:`ShardRecord` per shard, from its plan and its result.

    A shard is ``reviewed`` only when every roster role returned a non-failed
    document for it; a failure or a ``failed`` document leaves it unreviewed.
    """
    records: list[ShardRecord] = []
    for plan, result in zip(plans, results, strict=True):
        reviewed_roles = [
            role
            for role in roles
            if role in result.docs_by_role
            and result.docs_by_role[role].status != "failed"
        ]
        records.append(
            ShardRecord(
                index=plan.index,
                files=list(plan.files),
                prompt_chars=plan.prompt_chars,
                roles_reviewed=reviewed_roles,
                reviewed=len(reviewed_roles) == len(roles),
            )
        )
    return records
