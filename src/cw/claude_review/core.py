"""One Claude review pass over a worktree: the Claude confirm round (#2791).

:func:`run_claude_review` mirrors :func:`cw.codex_review.run_review` and reuses
everything around the per-role call: :func:`_prepare_review_pass` (diff
capture, role selection, prompts), the reviewer document parser, consolidation
and :func:`synthesize_codex_review_result`. Only the role runner is Claude's
own (:mod:`cw.claude_review._roles`).

Three things differ from a codex pass:

- The diff base is ``base_ref`` when given (the convergence ledger's fork
  point), while ``default_branch`` still feeds scope measurement.
- No codex capability probe runs: Claude's ``Read``/``Grep``/``Glob`` reach the
  worktree by construction, so a fixed capable verdict is supplied.
- Every blocker's ``details`` starts ``backend=claude; `` and a rendered
  verdict is titled ``## Claude Review Verdict``, so a Claude failure is never
  read as a codex one.

``backend_down`` is computed from the per-role failures before synthesis (no
document, and every failure a backend-side ``CODEX_ERROR`` or
``CODEX_MODEL_CAPACITY``), so neither consumer re-parses ``details`` text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.claude_review._roles import run_claude_roles
from cw.codex_review._capability import _CodexFilesystemCapability, _CodexFingerprint
from cw.codex_review._const import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    CODEX_REVIEW_WORKTREE_MUTATED,
)
from cw.codex_review._context import _prepare_review_pass
from cw.codex_review._verdict import make_codex_blocked, synthesize_codex_review_result

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.claude_review._runner import ClaudeRunner
    from cw.models import TicketTask
    from cw.review_findings import (
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewVerdict,
    )

#: The Step 4b comment title and artifact-header label for a Claude round.
CLAUDE_REVIEWER_LABEL = "Claude"
#: Prefix of every ``Blocker.details`` a Claude round builds.
CLAUDE_BACKEND_DETAILS_PREFIX = "backend=claude; "
#: Prefix of the session id a confirm round files its diagnostics bundles under
#: (``cw review confirm`` and the codex-lane gate both build ``<prefix><sid>``).
CONFIRM_SESSION_ID_PREFIX = "confirm-"

_BACKEND_DOWN_REASONS = frozenset({CODEX_ERROR, CODEX_MODEL_CAPACITY})

# Recorded on the verdict the same way a probed codex verdict is. Built from
# the real fields; the fingerprint names the backend so it never matches a
# cached codex verdict.
_CLAUDE_CAPABILITY = _CodexFilesystemCapability(
    capable=True,
    reason=None,
    fingerprint=_CodexFingerprint(
        cli_version=None,
        platform="claude",
        install_type="claude",
        sandbox_mode="claude-tools",
    ),
)


class ClaudeReviewRound(NamedTuple):
    """One Claude pass: the result, its verdict, and whether the backend was down."""

    result: AutoDevResult
    verdict: ReviewVerdict | None
    backend_down: bool


def _backend_down(
    documents: list[ReviewerFindingsDocument], failures: list[ReviewerRunFailure]
) -> bool:
    """No document, and every failed role failed on the backend side.

    A timeout or a ``budget_exhausted`` skip is not an outage: the backend
    may be healthy and merely slow, and neither counts toward the
    consecutive-outage cap.
    """
    return (
        not documents
        and bool(failures)
        and all(failure.reason in _BACKEND_DOWN_REASONS for failure in failures)
    )


def _with_backend_prefix(result: AutoDevResult) -> AutoDevResult:
    blocker = result.blocker
    if blocker is None:
        return result
    prefixed = blocker.model_copy(
        update={"details": CLAUDE_BACKEND_DETAILS_PREFIX + blocker.details}
    )
    return result.model_copy(update={"blocker": prefixed})


def run_claude_review(
    *,
    runner: ClaudeRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    base_ref: str | None,
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> ClaudeReviewRound:
    """Run one full Claude review pass (no fix loop) and synthesize the result.

    A role that mutated the worktree (G4) short-circuits synthesis: the round
    is a ``CODEX_REVIEW_WORKTREE_MUTATED`` block with ``retry_eligible=False``,
    no verdict, and ``backend_down=False``. The worktree is left as found.
    """
    prepared = _prepare_review_pass(
        task,
        worktree,
        base_ref or default_branch,
        runner=None,
        session_id=session_id,
        capability=_CLAUDE_CAPABILITY,
    )
    outcome = run_claude_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
    )
    if outcome.mutation is not None:
        blocked = make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_WORKTREE_MUTATED,
            retry_eligible=False,
            details=(
                f"{CLAUDE_BACKEND_DETAILS_PREFIX}role={outcome.mutation.role}; "
                f"{outcome.mutation.describe()}"
            ),
        )
        return ClaudeReviewRound(blocked, None, backend_down=False)
    backend_down = _backend_down(outcome.documents, outcome.failures)
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=outcome.documents,
        failures=outcome.failures,
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
        default_branch=default_branch,
        fix_loop_enabled=False,
        metrics_by_role=outcome.metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        voided_findings=prepared.voided_findings,
        finding_dispositions=prepared.finding_dispositions,
        refused_dispositions=prepared.refused_dispositions,
        pre_validation_rejected=outcome.pre_validation_rejected,
        reviewer_label=CLAUDE_REVIEWER_LABEL,
    )
    return ClaudeReviewRound(_with_backend_prefix(result), verdict, backend_down)
