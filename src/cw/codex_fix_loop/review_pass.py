"""One codex review pass for the fix loop: cycle 0 and per-cycle re-review.

Split out of :mod:`cw.codex_fix_loop._driver` (#2888). Owns the concern "run one
review pass over the diff", including prompt-size handling: cycle 0's
``run_review`` under the lane's prompt limit (#2867), the delta re-review a fix
cycle runs afterwards (#1837, #2877), and the named non-retryable
``agent_block`` park for a prompt the shard planner cannot fit.

Nothing here imports :mod:`cw.codex_fix_loop._driver`; the driver imports
:func:`_review_cycle_zero` and :func:`_rereview` from this module and calls them
through its own namespace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.codex_review import (
    ReviewInputTooLargeError,
    _prepare_review_pass,
    make_codex_blocked,
    run_prepared_roles,
    run_review,
    synthesize_codex_review_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_review._context import _ReviewPassInputs
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_findings import Finding, ReviewVerdict

_log = logging.getLogger(__name__)


def _budget_seconds(remaining: float | None) -> int | None:
    """Coerce a float remaining-budget into the int the role runners expect."""
    return None if remaining is None else max(int(remaining), 0)


def _rereview(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    reasoning_effort: str | None,
    remaining: float | None,
    session_id: str,
    previous_reviewed_sha: str,
    prior_open_findings: list[Finding],
    cycle: int,
    claim_tier_enabled: bool = False,
    disposition_drift_check_enabled: bool = True,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None = None,
    max_prompt_chars: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None, _ReviewPassInputs]:
    """Run a per-role review pass over the delta since the last cycle (#1837).

    Reviews ``previous_reviewed_sha..HEAD``, not the whole branch: a full
    rescan surfaces fresh findings on code no fix cycle touched, which is the
    treadmill this ticket exists to stop. ``prior_open_findings`` is inlined
    so the reviewers still see what remains unresolved from earlier cycles.

    ``max_prompt_chars`` (#2877) is the lane's prompt limit: a delta over it
    is reviewed shard by shard with cycle 0's planner
    (:func:`~cw.codex_review.run_prepared_roles`) and the verdict carries one
    ``ShardRecord`` per shard; a delta within it (or ``None``) runs unsharded
    with its prompts unchanged. A delta the planner cannot shard returns
    cycle *cycle*'s named, non-retryable ``agent_block`` with no verdict,
    before any codex call for this cycle.

    Returns the prepared inputs alongside the usual pair — the caller needs
    this cycle's ``delta_diff``/``delta_changed_files`` to run the admission
    gate, and they are captured here.

    ``claim_tier_enabled`` (#2210) is forwarded untouched to
    ``synthesize_codex_review_result``; this function makes no decision with
    it. Cycles 1+ reach the ledger backstop only through here, so a gate
    threaded into cycle 0 alone would arm half the loop.
    ``disposition_drift_check_enabled`` (#2232) rides the same hop on the same
    terms and for the same reason, as does ``settled_suppression`` (#2816,
    the clean-mode ledger's seam; ``None`` in blocking mode).
    """
    prepared = _prepare_review_pass(
        task,
        worktree,
        default_branch,
        config_branch=default_branch,
        runner=runner,
        session_id=session_id,
        delta_from_sha=previous_reviewed_sha,
        prior_open_findings=prior_open_findings,
    )
    try:
        run = run_prepared_roles(
            prepared,
            runner=runner,
            worktree=worktree,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=_budget_seconds(remaining),
            session_id=session_id,
            max_prompt_chars=max_prompt_chars,
        )
    except ReviewInputTooLargeError as exc:
        _log_input_too_large(task, exc, cycle=cycle)
        return _input_too_large_block(task, worktree, exc, cycle=cycle), None, prepared
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=run.documents,
        failures=run.failures,
        diff=prepared.delta_diff if prepared.delta_diff is not None else prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
        default_branch=default_branch,
        # Literal True: _rereview is only ever called from inside the
        # already-entered fix loop (run_review_with_fix_loop's for-loop).
        fix_loop_enabled=True,
        metrics_by_role=run.metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        review_config=prepared.review_config,
        # #1814: re-fetched and re-applied every cycle, not carried over from
        # cycle 0 — an operator can void a finding mid-loop, and a fix cycle
        # can rewrite the code out from under a void's content anchor.
        voided_findings=prepared.voided_findings,
        # #1838: the ledger `_prepare_review_pass` merged for THIS cycle — the
        # running task's durable record plus any marker the operator posted
        # mid-loop. Re-merged every cycle for the same reason the voids are
        # re-fetched: an operator can settle a finding while the loop runs.
        finding_dispositions=prepared.finding_dispositions,
        # #2210 round 3: marker records refused at parse time; not in the
        # ledger above, so they ride beside it to reach the verdict.
        refused_dispositions=prepared.refused_dispositions,
        # #2210: the lane-resolved claim-tier gate, forwarded unchanged.
        claim_tier_enabled=claim_tier_enabled,
        # #2232: the lane-resolved drift-check gate, likewise unchanged.
        disposition_drift_check_enabled=disposition_drift_check_enabled,
        # #2029: this cycle's own parse-time rescues. Per-cycle, not carried
        # over — each re-review re-runs the roles and re-parses their output.
        pre_validation_rejected=run.pre_validation_rejected,
        settled_suppression=settled_suppression,
    )
    if verdict is not None:
        update: dict[str, object] = {"previous_reviewed_sha": previous_reviewed_sha}
        if run.coverage:
            update["shards"] = run.coverage
        verdict = verdict.model_copy(update=update)
    return result, verdict, prepared


def _log_input_too_large(
    task: TicketTask, exc: ReviewInputTooLargeError, *, cycle: int
) -> None:
    """The WARNING for a prompt the planner cannot fit, naming the pass."""
    label = "cycle 0 review" if cycle == 0 else f"cycle {cycle} re-review"
    _log.warning(
        "codex fix loop: %s prompt too large (ticket=%s, cause=%s, file=%s, "
        "actual_chars=%d, max_chars=%d); parking as agent_block",
        label,
        task.ticket_id,
        exc.cause,
        exc.file,
        exc.actual_chars,
        exc.max_chars,
    )


def _input_too_large_block(
    task: TicketTask, worktree: Path, exc: ReviewInputTooLargeError, *, cycle: int = 0
) -> AutoDevResult:
    """The non-retryable ``agent_block`` for a prompt that cannot fit (#2867).

    Raised by the shard planner before any codex call for the pass, so
    nothing was sent; a bare requeue would fail identically, hence
    ``retry_eligible=False``. ``str(exc)`` names the file (when one is set)
    and both sizes. Cycle 0's text is fixed (#2867); a fix-loop re-review's
    (*cycle* N, #2877) names the cycle and its unreviewed delta instead, since
    earlier passes were reviewed.
    """
    if cycle == 0:
        details = (
            f"codex review prompt too large ({exc.cause}): {exc}; "
            "nothing was reviewed, truncated or retried; split or shrink the "
            "named file, or fix the oversize input, then requeue"
        )
    else:
        details = (
            f"codex re-review prompt too large ({exc.cause}): {exc}; cycle "
            f"{cycle}'s delta since the previous reviewed head was not "
            "reviewed, truncated or retried; split or shrink the named file, "
            "or fix the oversize input, then requeue; fix commits through "
            f"cycle {cycle} are already pushed and stay on the branch, and "
            "a requeue restarts at cycle 0 over the whole diff, which is "
            "re-planned from scratch"
        )
    return make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason="agent_block",
        details=details,
        retry_eligible=False,
    )


def _review_cycle_zero(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    reasoning_effort: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    fix_loop_enabled: bool,
    claim_tier_enabled: bool,
    disposition_drift_check_enabled: bool,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None,
    max_prompt_chars: int | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Cycle 0's ``run_review`` under the lane's prompt limit (#2867).

    A :class:`~cw.codex_review.ReviewInputTooLargeError` (a file that cannot
    fit alone, or prompt overhead over the limit) becomes the named
    ``agent_block`` park with no verdict, the shape the no-documents path
    already returns. A sharded pass logs its shard coverage.
    """
    try:
        result, verdict = run_review(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            session_id=session_id,
            fix_loop_enabled=fix_loop_enabled,
            claim_tier_enabled=claim_tier_enabled,
            disposition_drift_check_enabled=disposition_drift_check_enabled,
            settled_suppression=settled_suppression,
            max_prompt_chars=max_prompt_chars,
        )
    except ReviewInputTooLargeError as exc:
        _log_input_too_large(task, exc, cycle=0)
        return _input_too_large_block(task, worktree, exc), None
    if verdict is not None and verdict.shards:
        reviewed = sum(1 for shard in verdict.shards if shard.reviewed)
        _log.info(
            "codex fix loop: cycle 0 sharded (ticket=%s, shards=%d, reviewed=%d)",
            task.ticket_id,
            len(verdict.shards),
            reviewed,
        )
    return result, verdict
