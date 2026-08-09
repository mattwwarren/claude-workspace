"""Reasoning-effort benchmark harness for the codex reviewer profile (#1711).

``StageExecutorConfig.reasoning_effort`` defaults to ``"high"`` as a starting
position, not a measured optimum. This module is how that position gets
evidence: it runs the *same* role set and the *same* prompts twice — once per
reasoning-effort value — through the ordinary :func:`run_codex_roles` path, and
folds each pass's documents, failures, and audit telemetry into one side of a
:class:`BenchmarkComparison`.

Deliberately a library function, not a CLI command: the operator supplies the
runner, so the harness is exercisable against a double in tests and against a
real ``codex exec`` when someone actually wants the numbers. Nothing here
shells out on its own.

Two passes cost two full review runs. Callers own that budget decision — the
harness never runs itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from cw.codex_review._roles import run_codex_roles

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.review_findings import (
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewerRunMetrics,
    )

_DEFAULT_EFFORT_VALUES: tuple[str, str] = ("medium", "high")
_DEGRADED = "degraded"

_EFFORT_VALUES_ERROR = 'effort_values must be exactly ("medium", "high")'


class _EffortRunResult(BaseModel):
    """One reasoning-effort pass, folded into comparable scalars."""

    model_config = ConfigDict(extra="forbid")

    effort: str
    findings_count: int
    # Roles whose document came back with status="degraded" — a reviewer that
    # ran but told us its own coverage was incomplete. Distinct from
    # failed_roles: a degraded role still produced findings.
    degraded_roles: list[str]
    failed_roles: list[str]
    wall_clock_seconds: float
    total_input_tokens: int
    total_cached_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int


class BenchmarkComparison(BaseModel):
    """The two-sided result: the lower effort against the higher one."""

    model_config = ConfigDict(extra="forbid")

    medium: _EffortRunResult
    high: _EffortRunResult


def _sum_metric(
    metrics: dict[str, ReviewerRunMetrics],
    extract: Callable[[ReviewerRunMetrics], int | None],
) -> int:
    """Sum one integer telemetry field across roles, treating ``None`` as 0.

    Takes an accessor rather than a key name so every lookup stays a
    ``TypedDict`` literal-key read — a dynamic ``bag[key]`` would be untypable
    against :class:`ReviewerRunMetrics` and would only be checkable at runtime.

    A role whose audit stream carried no usage block contributes nothing rather
    than poisoning the total — "unreported" and "zero" are the same number for a
    sum, and the per-role records keep the distinction where it matters.
    """
    return sum(value for bag in metrics.values() if (value := extract(bag)) is not None)


def _fold(
    *,
    effort: str,
    documents: list[ReviewerFindingsDocument],
    failures: list[ReviewerRunFailure],
    metrics_by_role: dict[str, ReviewerRunMetrics],
) -> _EffortRunResult:
    """Collapse one pass's raw outputs into an :class:`_EffortRunResult`."""
    durations = [
        bag["duration_seconds"]
        for bag in metrics_by_role.values()
        if bag.get("duration_seconds") is not None
    ]
    return _EffortRunResult(
        effort=effort,
        findings_count=sum(len(doc.findings) for doc in documents),
        degraded_roles=[
            doc.reviewer_role for doc in documents if doc.status == _DEGRADED
        ],
        failed_roles=[failure.role for failure in failures],
        # Sum, not max: roles run sequentially under one shared deadline, so
        # the pass's cost is their total, not its longest leg.
        wall_clock_seconds=sum(d for d in durations if d is not None),
        total_input_tokens=_sum_metric(
            metrics=metrics_by_role, extract=lambda bag: bag.get("input_tokens")
        ),
        total_cached_input_tokens=_sum_metric(
            metrics=metrics_by_role,
            extract=lambda bag: bag.get("cached_input_tokens"),
        ),
        total_output_tokens=_sum_metric(
            metrics=metrics_by_role, extract=lambda bag: bag.get("output_tokens")
        ),
        total_reasoning_tokens=_sum_metric(
            metrics=metrics_by_role, extract=lambda bag: bag.get("reasoning_tokens")
        ),
    )


def run_reasoning_effort_benchmark(
    *,
    runner: CodexRunner,
    worktree: Path,
    roles: list[str],
    prompts_by_role: dict[str, str],
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    effort_values: tuple[str, ...] = _DEFAULT_EFFORT_VALUES,
) -> BenchmarkComparison:
    """Run *roles* once per entry in *effort_values*; return the comparison.

    ``effort_values`` must be exactly ``("medium", "high")`` because those
    are the names exposed by :class:`BenchmarkComparison`. Rejecting custom
    pairs prevents a result labelled ``medium`` from actually containing a
    different effort value.

    Each pass goes through the ordinary :func:`run_codex_roles` path, so the
    numbers describe the profile production actually uses — including the
    per-session profile diagnostics each pass writes. The effort value
    discriminates those artifacts, preserving both passes under the caller's
    real ``session_id``.
    """
    if effort_values != _DEFAULT_EFFORT_VALUES:
        raise ValueError(_EFFORT_VALUES_ERROR)
    sides: list[_EffortRunResult] = []
    for effort in effort_values:
        documents, failures, metrics_by_role = run_codex_roles(
            runner=runner,
            worktree=worktree,
            roles=roles,
            prompts_by_role=prompts_by_role,
            model=model,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            session_id=session_id,
            reasoning_effort=effort,
            profile_diagnostics_discriminator=effort,
        )
        sides.append(
            _fold(
                effort=effort,
                documents=documents,
                failures=failures,
                metrics_by_role=metrics_by_role,
            )
        )
    return BenchmarkComparison(medium=sides[0], high=sides[1])
