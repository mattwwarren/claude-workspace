"""The codex IMPL result stamping: the early validation and the post-push finish.

Split out of :mod:`cw.codex_impl._run` to keep that module under the size
ceiling. :func:`early_stamp` validates the model's sentinel with pre-turn facts
before any side effect; :func:`finish` stamps the pushed commit's result with
the real post-push facts and emits the complete event, converting a stamping
fault into a durable terminal block (the commit is already pushed, so the row
must not return to PENDING). The advance disposition (#3529) reuses
:func:`finish` with a cw-built payload.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from cw._git import git_output
from cw.auto_dev_result.parse import BLOCKER_REASON_VALIDATION_FAILED
from cw.codex_impl._result import stamp_impl_result
from cw.codex_stage_completion import STAGE_FAULTS
from cw.codex_stage_facts import resolve_plan_source
from cw.local_runner import git_facts
from cw.models import OrchestratorEventType
from cw.models.enums import StageIdentifier
from cw.plan_files import resolve_review_scope_tier
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult, ScopeTier
    from cw.codex_impl._run import _Pre, _Run

#: The stage identifiers the driver's stage events carry.
STAGE_IMPL_STARTED: Final = StageIdentifier.S2_IMPL_STARTED.value
STAGE_IMPL_COMPLETE: Final = StageIdentifier.S2_IMPL_COMPLETE.value


def _tier(run: _Run, pre: _Pre, files: int, lines: int) -> ScopeTier | None:
    return resolve_review_scope_tier(pre.plan_text, run.task.scope_hint, (files, lines))


def early_stamp(
    run: _Run, pre: _Pre, payload: dict[str, Any]
) -> AutoDevResult | ImplBlock:
    """Validate the sentinel with pre-turn facts, before any side effect."""
    facts = pre.facts
    plan_source, _ = resolve_plan_source(run.task, run.worktree)
    try:
        return stamp_impl_result(
            payload,
            task=run.task,
            worktree=run.worktree,
            facts=facts,
            scope_tier=_tier(run, pre, facts["files"], facts["lines_actual"]),
            plan_source=plan_source,
            extra_friction=[],
        )
    except ValidationError as exc:
        return ImplBlock(
            BLOCKER_REASON_VALIDATION_FAILED, f"sentinel failed validation: {exc}"
        )


def _stamp_final(
    run: _Run, pre: _Pre, tokens: list[str], payload: dict[str, Any]
) -> AutoDevResult:
    """The final stamp with the real post-push git facts, then the complete event."""
    facts = git_facts(run.worktree, run.base_branch)
    plan_source, note = resolve_plan_source(run.task, run.worktree)
    friction = [*pre.friction, *tokens, *([note] if note else [])]
    result = stamp_impl_result(
        payload,
        task=run.task,
        worktree=run.worktree,
        facts=facts,
        scope_tier=_tier(run, pre, facts["files"], facts["lines_actual"]),
        plan_source=plan_source,
        extra_friction=friction,
    )
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_IMPL_COMPLETE,
        prev_stage=STAGE_IMPL_STARTED,
    )
    return result


def finish(
    run: _Run, pre: _Pre, tokens: list[str], payload: dict[str, Any]
) -> AutoDevResult:
    """Stamp the pushed commit; a stamping fault becomes a durable terminal block."""
    try:
        return _stamp_final(run, pre, tokens, payload)
    except STAGE_FAULTS as exc:
        # The commit is already pushed. Convert a post-push stamping failure
        # into a durable terminal result so the guarded-stage crash handler
        # cannot incorrectly return the row to PENDING and dispatch it again.
        try:
            sha = git_output(["rev-parse", "HEAD"], cwd=run.worktree).strip()
        except (subprocess.CalledProcessError, OSError):
            sha = "unknown"
        detail = redact_and_cap(str(exc))
        return run.blocked(
            ImplBlock(
                IMPL_FAILED,
                f"the IMPL commit {sha[:12]} was pushed, but cw could not "
                "complete the post-push result: "
                f"{detail or 'the completion facts were unavailable'}; "
                "the pushed commit remains for operator reconciliation",
            )
        )
