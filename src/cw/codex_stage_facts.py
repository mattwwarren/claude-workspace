"""Stage facts stamped on a codex REVIEW result at the completion door (#3056).

The result builders (:func:`cw.codex_review.make_codex_blocked`, the empty-diff
and clean/large synthesis) have no worktree-level facts, so they hardcode
``plan_source="none"``, ``fork_point_sha=None`` and ``commits=[]``. FINALIZE
reads those fields, so a REVIEW result that shipped a fix-loop commit looked
like one with no plan and no commits.

:func:`stamp_stage_facts` writes the real values once, between the review and
``complete_session_through_door`` (the single choke point every codex REVIEW
result passes, the resumed-confirm path included):

- ``plan_source`` from the task, trusted only when ``.cw/plan.md`` exists in the
  worktree; otherwise ``"none"`` with a ``codex_plan_source_none: <why>``
  friction token. Never read from the tracker.
- ``fork_point_sha`` is ``git merge-base origin/<default> HEAD``, the same base
  the Claude convention uses, never the pre-loop HEAD. ``None`` when it does
  not resolve.
- ``commits`` is the fix loop's own commits only (subject starts with
  ``FIX_CYCLE_COMMIT_PREFIX``), newest first, as full shas. Listing every branch
  commit would make any later park look productive to
  ``claim_evidence._has_commits``.

An ``empty_diff_blocked`` result comes back unchanged. ``branch`` is not
stamped. Pure reads: no lock is held and nothing is written to the worktree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast, get_args

from cw._git import run_git
from cw.auto_dev_result import AutoDevResult, PlanSource
from cw.codex_review import FIX_CYCLE_COMMIT_PREFIX
from cw.exceptions import CwError
from cw.review_convergence import resolve_diff_base

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_log = logging.getLogger(__name__)

_PLAN_SOURCES = frozenset(get_args(PlanSource))
_PLAN_NONE: PlanSource = "none"
_PLAN_NONE_PREFIX = "codex_plan_source_none"
_EMPTY_DIFF_STATUS = "empty_diff_blocked"


def resolve_plan_source(
    task: TicketTask, worktree: Path
) -> tuple[PlanSource, str | None]:
    """Return ``(plan_source, friction token)`` for *task* in *worktree*.

    The task's value counts only when it is in the closed ``PlanSource`` set
    and ``.cw/plan.md`` is present. Anything else is ``"none"`` plus a
    ``codex_plan_source_none: <why>`` token.
    """
    value = task.plan_source
    if value is None or value == "":
        why = "task.plan_source unset"
    elif value not in _PLAN_SOURCES:
        why = f"'{value}' not in the closed PlanSource set"
    elif not (worktree / ".cw" / "plan.md").is_file():
        why = ".cw/plan.md absent from the worktree"
    else:
        return cast("PlanSource", value), None
    return _PLAN_NONE, f"{_PLAN_NONE_PREFIX}: {why}"


def fix_loop_facts(
    worktree: Path,
    default_branch: str,
    *,
    ticket_id: str = "",
    session_id: str = "",
) -> tuple[str | None, list[str]]:
    """Return ``(fork point sha, fix-loop commit shas newest first)``.

    ``(None, [])`` when the merge-base does not resolve; ``(fork, [])`` with a
    WARNING when the log cannot be read.
    """
    try:
        fork = resolve_diff_base(worktree, default_branch)
    except (CwError, OSError):
        return None, []
    try:
        log = run_git(
            ["log", f"{fork}..HEAD", "--format=%H %s"],
            cwd=worktree,
            capture_output=True,
        )
    except OSError:
        log = None
    if log is None or log.returncode != 0:
        _log.warning(
            "codex_stage_facts_git_log_failed ticket=%s session=%s fork=%s",
            ticket_id,
            session_id,
            fork,
        )
        return fork, []
    commits = []
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if sha and subject.startswith(FIX_CYCLE_COMMIT_PREFIX):
            commits.append(sha)
    return fork, commits


def stamp_stage_facts(
    result: AutoDevResult,
    *,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    session_id: str = "",
) -> AutoDevResult:
    """Return *result* with ``plan_source``, ``fork_point_sha`` and ``commits`` set.

    *result* itself for ``empty_diff_blocked``. Re-validated, so a bad value
    fails here at the source rather than at the door.
    """
    if result.status == _EMPTY_DIFF_STATUS:
        return result
    plan_source, note = resolve_plan_source(task, worktree)
    fork, commits = fix_loop_facts(
        worktree, default_branch, ticket_id=task.ticket_id, session_id=session_id
    )
    friction = list(result.friction_highlights)
    if note is not None:
        friction.append(note)
    stamped = result.model_copy(
        update={
            "plan_source": plan_source,
            "fork_point_sha": fork,
            "commits": commits,
            "friction_highlights": friction,
        }
    )
    return AutoDevResult.model_validate(stamped.model_dump(mode="json"))
