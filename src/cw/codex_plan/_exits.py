"""The codex PLAN stage's exits: the park, the completion and their helpers.

Split out of :mod:`cw.codex_plan._run` (#3464) so the run's flow stays one
readable module. :func:`_park` writes the draft and renders the park comment
after advisory stations; :func:`_complete` runs the binding stations, writes
``.cw/plan.md`` and records the plan's decisions. :class:`_Run` (the run's
steps) stays in ``_run.py`` and is named here only for typing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.codex_plan._post import render_park_comment
from cw.codex_plan._result import (
    DECISIONS_NOT_RECORDED_PREFIX,
    PLAN_BLOCKED,
    PLAN_NO_OP,
    STAGE_PLAN_GENERATED,
    STAGE_PLAN_REVIEWED,
    PlanDraftError,
    backup_plan_md,
    clean_plan_text,
    draft_fingerprint_on_disk,
    existing_plan_md_fingerprint,
    on_disk_leading_block,
    plan_md_replaced_token,
    stamp_plan_result,
    write_plan_draft,
    write_plan_md,
)
from cw.codex_plan._stations import run_plan_stations, stamp_markers
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.dev_queue.operator_constraints import record_plan_constraints
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, Stage
from cw.plan_decisions import parse_plan_decisions
from cw.plan_fingerprint import PLAN_DRAFT_RELATIVE_PATH, compute_plan_draft_fingerprint
from cw.stage_verification.plan_gates import (
    CODEX_PLAN_ARTIFACT_MISSING,
    check_artifact,
    check_plan_files,
    check_scope_tier,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_plan._run import _Run
    from cw.stage_verification.plan_gates import ApprovalCheck, GateFailure

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Exit:
    """What a run ends with: the result, and the comment to post (if any)."""

    result: AutoDevResult
    post_text: str | None = None
    artifact_path: Path | None = None


def _gate(result: AutoDevResult, plan_text: str) -> GateFailure | None:
    if result.status in {PLAN_NO_OP, PLAN_BLOCKED}:
        return None
    failure = check_artifact(plan_text)
    if failure is not None:
        return failure
    return check_plan_files(plan_text) or check_scope_tier(plan_text, result.scope.tier)


def _with_friction(result: AutoDevResult, tokens: list[str]) -> AutoDevResult:
    if not tokens:
        return result
    return result.model_copy(
        update={"friction_highlights": [*result.friction_highlights, *tokens]}
    )


def _bound(
    run: _Run, result: AutoDevResult, *, bind_draft: bool = True
) -> AutoDevResult:
    """*result* with its fingerprint bound to the on-disk draft."""
    return stamp_plan_result(
        result.model_dump(mode="json"),
        ticket_id=run.task.ticket_id,
        worktree=run.worktree,
        bind_draft=bind_draft,
        resolution_evidence=result.resolution_evidence,
    )


def _write_draft_best_effort(run: _Run, plan_text: str) -> str | None:
    """Write the draft; its on-disk fingerprint, or ``None`` when not written."""
    try:
        write_plan_draft(
            run.worktree, plan_text, book=on_disk_leading_block(run.worktree)
        )
    except PlanDraftError:
        raise
    except OSError:
        _log.warning("codex plan draft not written ticket=%s", run.task.ticket_id)
        return None
    return draft_fingerprint_on_disk(run.worktree)


def _blocked_with_draft(
    run: _Run,
    reason: str,
    details: str,
    *,
    fingerprint: str | None,
    retry_eligible: bool = False,
) -> _Exit:
    """A blocked exit after the stations ran, carrying the draft's fingerprint."""
    return run.blocked(
        STAGE_PLAN_REVIEWED,
        reason,
        details,
        retry_eligible=retry_eligible,
        plan_draft_fingerprint=fingerprint,
    )


def _record_decisions(run: _Run, plan_text: str) -> str | None:
    """Record the plan's decisions on the row; a friction token on any shortfall."""
    if run.task.stage != Stage.PLAN:
        return None
    decisions = parse_plan_decisions(plan_text)
    if not decisions:
        return None
    try:
        outcome = record_plan_constraints(
            run.task.ticket_id, run.client.name, decisions, bounded=True
        )
    except (CwError, OSError) as exc:
        return f"{DECISIONS_NOT_RECORDED_PREFIX}{exc}"
    if outcome["unrecorded"]:
        reason = outcome["unrecorded_reason"] or "size cap"
        return f"{DECISIONS_NOT_RECORDED_PREFIX}{reason}"
    return None


def _park(run: _Run, plan_text: str, check: ApprovalCheck) -> _Exit:
    """A park exit: write the draft, run advisory stations, render the comment."""
    draft_fingerprint = _write_draft_best_effort(run, plan_text)
    result = _bound(run, check.result, bind_draft=draft_fingerprint is not None)
    stations = run_plan_stations(
        runner=run.runner,
        worktree=run.worktree,
        plan_text=plan_text,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        session_id=run.sid,
        binding=False,
    )
    comment = render_park_comment(
        ticket_id=run.task.ticket_id,
        client_name=run.client.name,
        result=result,
        draft_body=clean_plan_text(plan_text),
        approved_fingerprint=run.task.plan_approved_fingerprint,
        advisory_findings=stations.findings,
        mismatch=check.mismatch is not None,
    )
    return _Exit(result, comment, run.worktree / PLAN_DRAFT_RELATIVE_PATH)


def _complete(run: _Run, result: AutoDevResult, plan_text: str) -> _Exit:
    """A completing exit: binding stations, the stamped plan, the decisions.

    An existing ``.cw/plan.md`` (the normal state after a regress into PLAN)
    is replaced only once both stations are clean and both markers verify; it
    is snapshotted first and the replacement is audited as a friction token
    (#3498).
    """
    stations = run_plan_stations(
        runner=run.runner,
        worktree=run.worktree,
        plan_text=plan_text,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        session_id=run.sid,
        binding=True,
    )
    if stations.block is not None:
        block = stations.block
        return _blocked_with_draft(
            run,
            block.reason,
            block.details,
            fingerprint=_write_draft_best_effort(run, plan_text),
            retry_eligible=block.retry_eligible,
        )
    marked = stamp_markers(clean_plan_text(plan_text), today=datetime.now(UTC).date())
    if not _plan_body_signoff_ok(marked):
        return _blocked_with_draft(
            run,
            CODEX_PLAN_ARTIFACT_MISSING,
            "stamped plan does not carry both sign-off markers",
            fingerprint=_write_draft_best_effort(run, plan_text),
        )
    old_fingerprint = existing_plan_md_fingerprint(run.worktree)
    backup_ok = False
    fingerprint: str | None = None
    try:
        write_plan_draft(
            run.worktree, plan_text, book=on_disk_leading_block(run.worktree)
        )
        fingerprint = draft_fingerprint_on_disk(run.worktree)
        result = _bound(run, result)
        if old_fingerprint is not None:
            backup_ok = backup_plan_md(run.worktree)
        plan_path = write_plan_md(run.worktree, marked)
        written = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _blocked_with_draft(
            run,
            CODEX_PLAN_ARTIFACT_MISSING,
            f".cw/plan.md: {exc}",
            fingerprint=fingerprint,
        )
    if not _plan_body_signoff_ok(written):
        return _blocked_with_draft(
            run,
            CODEX_PLAN_ARTIFACT_MISSING,
            ".cw/plan.md does not carry both sign-off markers",
            fingerprint=fingerprint,
        )
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_PLAN_REVIEWED,
        prev_stage=STAGE_PLAN_GENERATED,
    )
    try:
        (run.worktree / PLAN_DRAFT_RELATIVE_PATH).unlink(missing_ok=True)
    except OSError:
        _log.warning(
            "codex plan draft cleanup failed ticket=%s",
            run.task.ticket_id,
            exc_info=True,
        )
    tokens = _replacement_friction(old_fingerprint, marked, backup_ok=backup_ok)
    token = _record_decisions(run, written)
    result = _with_friction(result, [*tokens, *([token] if token else [])])
    return _Exit(result, marked, plan_path)


def _replacement_friction(
    old_fingerprint: str | None, marked: str, *, backup_ok: bool
) -> list[str]:
    """The ``plan_md_replaced`` token when a prior ``.cw/plan.md`` was replaced."""
    if old_fingerprint is None:
        return []
    new_fingerprint = compute_plan_draft_fingerprint(marked)
    return [
        plan_md_replaced_token(old_fingerprint, new_fingerprint, backup_ok=backup_ok)
    ]
