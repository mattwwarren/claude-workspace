"""Top-level review orchestration for the codex-review package.

``run_review`` is ``CodexExecutor.spawn()``'s Step 3 delegation target: it
assembles one review pass's inputs (via :func:`_prepare_review_pass` — capture
the diff, select reviewers, materialize prompts), runs the shared-deadline
per-role loop, and synthesizes the typed result. ``_prepare_review_pass`` is
shared with ``cw.codex_fix_loop``'s per-cycle re-review (#1392), and so is
:func:`run_prepared_roles`, the plan/shard/run branch (#2877).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._context import _prepare_review_pass
from cw.codex_review._roles import run_codex_roles
from cw.codex_review._shard import plan_prompt_shards
from cw.codex_review._shard_run import ShardedRun, run_sharded_roles
from cw.codex_review._verdict import synthesize_codex_review_result

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_review._context import _ReviewPassInputs
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_convergence import ResolutionSpec
    from cw.review_findings import ReviewVerdict


def run_prepared_roles(
    prepared: _ReviewPassInputs,
    *,
    runner: CodexRunner,
    worktree: Path,
    model: str | None,
    reasoning_effort: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    max_prompt_chars: int | None,
) -> ShardedRun:
    """Run every role of *prepared*, sharded when over *max_prompt_chars* (#2864).

    The one plan, shard-or-not, run and merge branch shared by
    :func:`run_review` and the fix loop's per-cycle re-review (#2877). When the
    longest role prompt is over *max_prompt_chars*, the diff is planned into
    shards (:func:`~cw.codex_review._shard.plan_prompt_shards`) and every role
    reviews every shard in turn; the result carries one ``ShardRecord`` per
    shard in ``coverage``. Otherwise (and always when *max_prompt_chars* is
    ``None``) the roles run once over the prompts unchanged and ``coverage``
    is empty. A planner that cannot fit the input raises
    :class:`~cw.codex_review._shard.ReviewInputTooLargeError` before any codex
    call.
    """
    shards = (
        plan_prompt_shards(prepared, max_prompt_chars)
        if max_prompt_chars is not None
        else []
    )
    if shards:
        return run_sharded_roles(
            runner=runner,
            worktree=worktree,
            roles=prepared.roles,
            plans=shards,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            session_id=session_id,
        )
    documents, failures, metrics_by_role, rejected = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        reasoning_effort=reasoning_effort,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
    )
    return ShardedRun(documents, failures, metrics_by_role, rejected, coverage=[])


def run_review(
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
    claim_tier_enabled: bool = False,
    disposition_drift_check_enabled: bool = True,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None = None,
    max_prompt_chars: int | None = None,
    resolution: ResolutionSpec | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the full per-role review pass; return ``(result, verdict)``.

    This is ``CodexExecutor.spawn()``'s Step 3 delegation target: capture the
    diff, select reviewers, materialize prompts, run the shared-deadline loop,
    and synthesize the typed result.

    ``fix_loop_enabled`` (#1705) is forwarded to
    :func:`synthesize_codex_review_result`, which threads it into
    :func:`render_verdict_comment` on the blocking branch.

    The prepared pass's ``voided_findings`` (#1814) are forwarded to the same
    function, which applies them before deciding whether the pass blocks. Its
    ``finding_dispositions`` (#1838) ride the same hop, for the same reason —
    the prepared pass already merged the durable queue-row ledger with the
    ticket thread's marker, so this only has to thread the result — along with
    the marker records that merge refused (#2210 round 3), which are not in the
    ledger and so must ride beside it to reach the review output.

    ``claim_tier_enabled`` (#2210) is the lane-resolved gate for the ledger's
    fuzzy claim-match tier, forwarded to the same function. Defaulted False so
    this entry point — the whole fix-loop-disabled lane, and cycle 0 of the
    enabled one — is off unless a caller deliberately arms it.

    ``disposition_drift_check_enabled`` (#2232) rides the same hop and is
    likewise only forwarded. Its default is True, the opposite of the gate
    above, because the two fail in opposite directions: a claim tier that arms
    by omission suppresses a finding nobody settled, while a drift check that
    runs by omission costs one ``git diff``.

    ``run_codex_roles``' fourth return value (#2029) — the findings rescued out
    of their documents at parse time — rides the same hop as well, so a
    schema-invalid MUST_FIX reaches #1714's force-block instead of vanishing
    with its document.

    ``settled_suppression`` (#2816) is the clean-mode convergence ledger's
    ``suppress_settled`` seam, forwarded to the same function so a finding the
    ledger already settled does not block cycle 0. ``None`` (blocking mode,
    the default) applies nothing.

    ``max_prompt_chars`` (#2864) is the backend's prompt limit, applied by
    :func:`run_prepared_roles`. When the
    longest role prompt is over it, the diff is split into per-directory
    shards (:func:`~cw.codex_review._shard.plan_prompt_shards`), every role
    reviews every shard in turn, the shard results are merged before synthesis
    against the full diff, and the verdict carries one ``ShardRecord`` per
    shard. A file that cannot fit alone, or a prompt still over the limit,
    raises :class:`~cw.codex_review._shard.ReviewInputTooLargeError` before any
    codex call. ``cw review confirm`` and the codex lane's cycle 0 (#2867,
    through :func:`~cw.codex_fix_loop.run_review_with_fix_loop`, which parks
    that error as a non-retryable ``agent_block``) pass the limit. ``None``
    never shards, and a diff within the limit takes the unsharded path with
    its prompts unchanged.

    ``resolution`` (#2980) makes the pass a conflict-resolution round over that
    spec (see :func:`_prepare_review_pass`); ``None`` changes nothing.
    """
    prepared = _prepare_review_pass(
        task,
        worktree,
        default_branch,
        runner=runner,
        session_id=session_id,
        resolution=resolution,
    )
    run = run_prepared_roles(
        prepared,
        runner=runner,
        worktree=worktree,
        model=model,
        reasoning_effort=reasoning_effort,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
        max_prompt_chars=max_prompt_chars,
    )
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=run.documents,
        failures=run.failures,
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
        default_branch=default_branch,
        fix_loop_enabled=fix_loop_enabled,
        metrics_by_role=run.metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        voided_findings=prepared.voided_findings,
        finding_dispositions=prepared.finding_dispositions,
        refused_dispositions=prepared.refused_dispositions,
        claim_tier_enabled=claim_tier_enabled,
        disposition_drift_check_enabled=disposition_drift_check_enabled,
        pre_validation_rejected=run.pre_validation_rejected,
        settled_suppression=settled_suppression,
    )
    if verdict is not None and run.coverage:
        verdict = verdict.model_copy(update={"shards": run.coverage})
    return result, verdict
