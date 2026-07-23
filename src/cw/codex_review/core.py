"""Top-level review orchestration for the codex-review package.

``run_review`` is ``CodexExecutor.spawn()``'s Step 3 delegation target: it
assembles one review pass's inputs (via :func:`_prepare_review_pass` — capture
the diff, select reviewers, materialize prompts), runs the shared-deadline
per-role loop, and synthesizes the typed result. ``_prepare_review_pass`` is
shared with ``cw.codex_fix_loop``'s per-cycle re-review (#1392).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.codex_review._context import (
    _build_reviewer_prompt,
    _categorize_changed_files,
    _load_agent_spec,
    _load_optional_text,
    _load_review_policy,
    _load_sensitive_hits,
    _load_ticket_context,
    _select_reviewer_roles,
)
from cw.codex_review._diff import _capture_diff
from cw.codex_review._roles import run_codex_roles
from cw.codex_review._verdict import synthesize_codex_review_result
from cw.local_runner import resolve_tier

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_findings import CapturedDiff, ReviewVerdict


class _ReviewPassInputs(NamedTuple):
    """Assembled, side-effect-free inputs for one per-role review pass (#1392).

    The output of :func:`_prepare_review_pass` — everything ``run_codex_roles``
    needs (selected ``roles`` and their materialized ``prompts_by_role``) plus
    the captured ``diff`` and ``reviewed_sha`` that
    ``synthesize_codex_review_result`` consumes. Extracted so the fix loop can
    re-run a fresh review pass each cycle without re-inlining ``run_review``'s
    input-assembly body.
    """

    roles: list[str]
    prompts_by_role: dict[str, str]
    diff: CapturedDiff
    reviewed_sha: str


def _prepare_review_pass(
    task: TicketTask, worktree: Path, default_branch: str
) -> _ReviewPassInputs:
    """Assemble one review pass's inputs: capture diff, select roles, build prompts.

    Pure extraction of ``run_review``'s former input-assembly body (everything
    before ``run_codex_roles`` was called) — no logic change, no side effects
    beyond the read-only git/\u200bfilesystem reads it already performed. Shared by
    ``run_review`` and ``cw.codex_fix_loop``'s per-cycle re-review (#1392).
    """
    diff, reviewed_sha, changed_files = _capture_diff(worktree, default_branch)
    scope_tier = resolve_tier(task.scope_hint)
    categories = _categorize_changed_files(changed_files)
    sensitive_hits = _load_sensitive_hits(worktree, changed_files, scope_tier)
    repo_policy = _load_review_policy(worktree, scope_tier)
    project_rubrics = _load_optional_text(worktree / ".claude" / "review-extras.md")
    plan_text, ticket_text = _load_ticket_context(worktree)
    mutates_persisted_state = (
        bool(sensitive_hits) or categories.python or categories.frontend
    )
    roles = _select_reviewer_roles(
        scope_tier,
        categories=categories,
        mutates_persisted_state=mutates_persisted_state,
        has_ticket_context=ticket_text is not None,
    )
    prompts_by_role = {
        role: _build_reviewer_prompt(
            role,
            agent_spec_text=_load_agent_spec(worktree, role),
            diff=diff,
            changed_files=changed_files,
            plan_text=plan_text,
            ticket_text=ticket_text,
            project_rubrics=project_rubrics,
            repo_policy_section=repo_policy.get(role),
            sensitive_hits=sensitive_hits,
        )
        for role in roles
    }
    return _ReviewPassInputs(
        roles=roles,
        prompts_by_role=prompts_by_role,
        diff=diff,
        reviewed_sha=reviewed_sha,
    )


def run_review(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the full per-role review pass; return ``(result, verdict)``.

    This is ``CodexExecutor.spawn()``'s Step 3 delegation target: capture the
    diff, select reviewers, materialize prompts, run the shared-deadline loop,
    and synthesize the typed result.
    """
    prepared = _prepare_review_pass(task, worktree, default_branch)
    documents, failures = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
    )
    return synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
    )
