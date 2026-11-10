"""One repo review engine pass on a codex review lane (RFC 0018 A4, #3216).

The in-process codex version of ``auto-dev-review.md`` Step 3a.5. Each pass
reviews the full diff at the tip (R2), on cycle 0 and on every re-review:

1. capture HEAD and run the D-13 preflight against the loop's diff base (R19);
2. materialize the default-branch skill and load its ``cw-engine.json``;
3. run one ``codex exec`` per manifest role through the shared pass
   (``_prepare_review_pass`` with the engine roster, ``run_prepared_roles``);
4. run the read-only aggregation turn, which writes the summary;
5. validate the summary through the shared core and synthesize the verdict
   from the one ``Repo Review Engine`` document.

Every failure is a blocked result; none records an event. Only a prompt that
cannot fit propagates, as :class:`~cw.codex_review.ReviewInputTooLargeError`,
to the fix loop's ``review_pass`` wrapper, which owns that park.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.auto_dev_result import (
    REVIEW_ENGINE_MODIFIED_BLOCKER_REASON,
    REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
)
from cw.codex_review._const import _TRANSIENT_FAILURE_REASONS
from cw.codex_review._context import EngineRoster, _prepare_review_pass
from cw.codex_review._diff import _capture_head_sha
from cw.codex_review._engine_aggregate import run_codex_aggregator
from cw.codex_review._verdict import (
    make_codex_blocked,
    synthesize_codex_review_result,
)
from cw.codex_review.core import run_prepared_roles
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND
from cw.models.enums import DEFAULT_REVIEWER_SANDBOX_MODE, SandboxMode
from cw.review_convergence import resolve_loop_diff_base
from cw.review_engine.constants import (
    CAUSE_DEFAULT_BRANCH_UNRESOLVED,
    CAUSE_MATERIALIZE_FAILED,
    CAUSE_NO_CODEX_MANIFEST,
    ENGINE_MANIFEST_FILENAME,
    ENGINE_ROLE_PREAMBLE,
    FRICTION,
    ROLE_REASON_FAILED_DOCUMENT,
    ROLE_REASON_UNREVIEWED_SHARD,
    EngineRoundFailure,
    engine_refusal_details,
)
from cw.review_engine.materialize import (
    EnginePreconditionError,
    MaterializeError,
    materialize_skill,
)
from cw.review_engine.preflight import run_preflight
from cw.review_engine.skill import SkillResolutionError
from cw.review_engine.validate import (
    SummaryFailure,
    ValidatedSummary,
    engine_precheck,
    validate_engine_summary,
)
from cw.review_engine_manifest_io import (
    EngineManifestError,
    LoadedEngineManifest,
    load_engine_manifest,
    manifest_refusal_cause,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_review._context import _ReviewPassInputs
    from cw.codex_review._shard_run import ShardedRun
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_engine.context import EngineContext
    from cw.review_findings import Finding, ReviewVerdict

_log = logging.getLogger(__name__)

_UNREVIEWED_SHARD_ROLE = "*"


@dataclass(frozen=True)
class EngineRoundSpec:
    """The engine a codex lane runs, built in-process at the run's start."""

    context: EngineContext


class EnginePassOutcome(NamedTuple):
    """One pass's result and verdict; ``prepared`` is None before the prepare."""

    result: AutoDevResult
    verdict: ReviewVerdict | None
    prepared: _ReviewPassInputs | None


@dataclass(frozen=True)
class EnginePassInputs:
    """Everything one pass forwards to the shared review primitives."""

    runner: CodexRunner
    task: TicketTask
    worktree: Path
    default_branch: str
    model: str | None
    reasoning_effort: str | None
    wall_clock_budget_seconds: int | None
    session_id: str
    fix_loop_enabled: bool
    prior_open_findings: list[Finding] | None
    claim_tier_enabled: bool
    disposition_drift_check_enabled: bool
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None
    max_prompt_chars: int | None
    reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE


@dataclass(frozen=True)
class _Failure:
    """A pass that stopped early, with its blocked result."""

    result: AutoDevResult


class _Loaded(NamedTuple):
    head: str
    round_id: str
    summary_path: Path
    manifest: LoadedEngineManifest


def _agent_block(
    inputs: EnginePassInputs,
    cycle: int,
    reason: EngineRoundFailure,
    detail: str,
    *,
    failure_reason: str | None = None,
) -> _Failure:
    _log.warning(
        "codex engine round failed: ticket=%s cycle=%d failure=%s",
        inputs.task.ticket_id,
        cycle,
        reason.value,
    )
    return _Failure(
        make_codex_blocked(
            ticket_id=inputs.task.ticket_id,
            worktree=inputs.worktree,
            reason="agent_block",
            details=detail,
            retry_eligible=(
                reason
                in {
                    EngineRoundFailure.ENGINE_SUMMARY_INVALID,
                    EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE,
                    EngineRoundFailure.ENGINE_HEAD_MISMATCH,
                    EngineRoundFailure.ENGINE_UNRESOLVABLE,
                }
                or failure_reason in _TRANSIENT_FAILURE_REASONS
            ),
        )
    )


def _unresolvable(
    inputs: EnginePassInputs, cycle: int, skill: str, cause: str
) -> _Failure:
    reason = EngineRoundFailure.ENGINE_UNRESOLVABLE
    detail = FRICTION[reason].format(skill=skill, cause=cause)
    return _agent_block(inputs, cycle, reason, detail)


def _park(inputs: EnginePassInputs, cycle: int, reason: str, details: str) -> _Failure:
    _log.warning(
        "codex engine round failed: ticket=%s cycle=%d failure=%s",
        inputs.task.ticket_id,
        cycle,
        reason,
    )
    return _Failure(
        make_codex_blocked(
            ticket_id=inputs.task.ticket_id,
            worktree=inputs.worktree,
            reason=reason,
            details=details,
            retry_eligible=False,
        )
    )


def _preflight(
    spec: EngineRoundSpec, inputs: EnginePassInputs, cycle: int
) -> _Failure | None:
    """The D-13 guard: a branch touching the engine's own paths parks (R19)."""
    context = spec.context
    try:
        diff_base = resolve_loop_diff_base(inputs.worktree, context.default_branch)
        verdict = run_preflight(inputs.worktree, diff_base=diff_base, context=context)
    except SkillResolutionError as exc:
        return _unresolvable(inputs, cycle, context.skill, exc.cause)
    except CwError:
        return _unresolvable(
            inputs, cycle, context.skill, CAUSE_DEFAULT_BRANCH_UNRESOLVED
        )
    if verdict.decision == "park":
        details = (
            "the branch changed the repo review engine's skill or a guarded "
            f"path: {', '.join(verdict.touched)}; release with "
            "`cw review engine allow` naming the HEAD, then requeue"
        )
        return _park(inputs, cycle, REVIEW_ENGINE_MODIFIED_BLOCKER_REASON, details)
    return None


def _materialize_roster(
    spec: EngineRoundSpec, inputs: EnginePassInputs, cycle: int, round_id: str
) -> _Loaded | _Failure:
    """Copy the default-branch skill into the round dir and load its manifest."""
    context = spec.context
    head = _capture_head_sha(inputs.worktree)
    try:
        materialized = materialize_skill(
            inputs.worktree, round_id=round_id, expect_head=head, context=context
        )
    except MaterializeError as exc:
        return _unresolvable(inputs, cycle, context.skill, exc.cause)
    except EnginePreconditionError:
        return _unresolvable(inputs, cycle, context.skill, CAUSE_MATERIALIZE_FAILED)
    manifest_path = Path(materialized.skill_dir) / ENGINE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        details = engine_refusal_details(
            CODEX_BACKEND,
            CAUSE_NO_CODEX_MANIFEST,
            f"{context.skill} has no {ENGINE_MANIFEST_FILENAME}",
        )
        return _park(
            inputs, cycle, REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON, details
        )
    try:
        manifest = load_engine_manifest(manifest_path)
    except EngineManifestError as exc:
        cause = manifest_refusal_cause(exc)
        detail = f"{exc.reason}: {exc.field_path or '-'}"
        details = engine_refusal_details(CODEX_BACKEND, cause, detail)
        return _park(
            inputs, cycle, REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON, details
        )
    return _Loaded(head, round_id, materialized.summary_path, manifest)


def _engine_roster(loaded: LoadedEngineManifest) -> EngineRoster:
    """The manifest roles, each spec prefixed with the role preamble (Choice A)."""
    return EngineRoster(
        tuple(
            (role, f"{ENGINE_ROLE_PREAMBLE}\n\n{text}")
            for role, text in loaded.agent_specs.items()
        )
    )


def _role_failure(run: ShardedRun) -> tuple[str, str] | None:
    """``(role, reason)`` of the first role failure in *run*, if any (R17)."""
    if run.failures:
        failure = run.failures[0]
        return failure.role, failure.reason
    for document in run.documents:
        if document.status == "failed":
            return document.reviewer_role, ROLE_REASON_FAILED_DOCUMENT
    if any(not shard.reviewed for shard in run.coverage):
        return _UNREVIEWED_SHARD_ROLE, ROLE_REASON_UNREVIEWED_SHARD
    return None


def _run_roles(
    inputs: EnginePassInputs, cycle: int, prepared: _ReviewPassInputs
) -> ShardedRun | _Failure:
    """Every manifest role over the full diff; any failure skips the aggregator."""
    run = run_prepared_roles(
        prepared,
        runner=inputs.runner,
        worktree=inputs.worktree,
        model=inputs.model,
        reasoning_effort=inputs.reasoning_effort,
        wall_clock_budget_seconds=inputs.wall_clock_budget_seconds,
        session_id=inputs.session_id,
        max_prompt_chars=inputs.max_prompt_chars,
        reviewer_sandbox_mode=inputs.reviewer_sandbox_mode,
    )
    failed = _role_failure(run)
    if failed is None:
        return run
    reason = EngineRoundFailure.ENGINE_ROLE_FAILED
    detail = FRICTION[reason].format(role=failed[0], reason=failed[1])
    return _agent_block(inputs, cycle, reason, detail, failure_reason=failed[1])


def _run_aggregation(
    spec: EngineRoundSpec,
    inputs: EnginePassInputs,
    cycle: int,
    loaded: _Loaded,
    run: ShardedRun,
) -> ValidatedSummary | _Failure:
    """The aggregation turn, then the shared precheck and summary checks."""
    failure: SummaryFailure | None = run_codex_aggregator(
        runner=inputs.runner,
        worktree=inputs.worktree,
        head_sha=loaded.head,
        aggregation_prompt=loaded.manifest.aggregation_prompt,
        role_documents=[
            (doc.reviewer_role, doc.model_dump_json()) for doc in run.documents
        ],
        summary_path=loaded.summary_path,
        model=inputs.model,
        reasoning_effort=inputs.reasoning_effort,
        timeout_seconds=inputs.wall_clock_budget_seconds,
        max_prompt_chars=inputs.max_prompt_chars,
    )
    if failure is not None:
        outcome: SummaryFailure | ValidatedSummary = failure
    else:
        precheck = engine_precheck(inputs.worktree, loaded.head)
        outcome = precheck or validate_engine_summary(
            loaded.summary_path, expect_head=loaded.head, skill=spec.context.skill
        )
    if isinstance(outcome, SummaryFailure):
        failure_reason = None
        if outcome.reason is EngineRoundFailure.ENGINE_AGGREGATION_FAILED:
            failure_reason = outcome.detail.rsplit(": ", 1)[-1]
        return _agent_block(
            inputs,
            cycle,
            outcome.reason,
            outcome.detail,
            failure_reason=failure_reason,
        )
    return outcome


def _synthesize(
    inputs: EnginePassInputs,
    prepared: _ReviewPassInputs,
    validated: ValidatedSummary,
    run: ShardedRun,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """The verdict from the one engine document; nothing can reject it (R20)."""
    result, verdict = synthesize_codex_review_result(
        task=inputs.task,
        worktree=inputs.worktree,
        documents=[validated.document],
        failures=[],
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=inputs.session_id,
        default_branch=inputs.default_branch,
        fix_loop_enabled=inputs.fix_loop_enabled,
        metrics_by_role=run.metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        review_config=prepared.review_config,
        # R20: cw never rejects an engine finding, so neither suppression seam
        # (operator voids, the finding-disposition ledger) is applied.
        voided_findings=[],
        finding_dispositions={},
        refused_dispositions=[],
        pre_validation_rejected=run.pre_validation_rejected,
        claim_tier_enabled=inputs.claim_tier_enabled,
        disposition_drift_check_enabled=inputs.disposition_drift_check_enabled,
        settled_suppression=inputs.settled_suppression,
    )
    if verdict is not None and run.coverage:
        verdict = verdict.model_copy(update={"shards": run.coverage})
    return result, verdict


def _prepare(inputs: EnginePassInputs, loaded: _Loaded) -> _ReviewPassInputs:
    return _prepare_review_pass(
        inputs.task,
        inputs.worktree,
        inputs.default_branch,
        config_branch=inputs.default_branch,
        runner=inputs.runner,
        session_id=inputs.session_id,
        delta_from_sha=None,
        prior_open_findings=inputs.prior_open_findings,
        reviewer_sandbox_mode=inputs.reviewer_sandbox_mode,
        engine_roster=_engine_roster(loaded.manifest),
    )


def run_engine_pass(
    spec: EngineRoundSpec,
    inputs: EnginePassInputs,
    *,
    cycle: int,
    round_id: str | None = None,
) -> EnginePassOutcome:
    """Run one full-diff engine pass; see the module docstring for the steps.

    *round_id* defaults to a fresh ``codex-<cycle>-<hex>`` id. A prompt the
    planner cannot fit raises :class:`~cw.codex_review.ReviewInputTooLargeError`.
    """
    rid = round_id or f"codex-{cycle}-{uuid.uuid4().hex[:12]}"
    blocked = _preflight(spec, inputs, cycle)
    if blocked is not None:
        return EnginePassOutcome(blocked.result, None, None)
    loaded = _materialize_roster(spec, inputs, cycle, rid)
    if isinstance(loaded, _Failure):
        return EnginePassOutcome(loaded.result, None, None)
    _log.info(
        "codex engine round: ticket=%s cycle=%d roles=%d round_id=%s",
        inputs.task.ticket_id,
        cycle,
        len(loaded.manifest.agent_specs),
        rid,
    )
    prepared = _prepare(inputs, loaded)
    ran = _run_roles(inputs, cycle, prepared)
    if isinstance(ran, _Failure):
        return EnginePassOutcome(ran.result, None, prepared)
    validated = _run_aggregation(spec, inputs, cycle, loaded, ran)
    if isinstance(validated, _Failure):
        return EnginePassOutcome(validated.result, None, prepared)
    result, verdict = _synthesize(inputs, prepared, validated, ran)
    return EnginePassOutcome(result, verdict, prepared)
