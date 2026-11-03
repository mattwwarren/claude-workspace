"""The disposition vocabulary of the conflict-repair review recipes.

:class:`RepairDisposition` names every outcome of the
``repair_changelog_conflict`` (#2771) and ``repair_mechanical_conflict``
(#2917) recipes. It lives in its own module, not beside the CHANGELOG
rebuild, because the shared repair engine
(``cw.reconcile.review_recipes.repair_engine``) parks with it for both.

Stdlib only: pure, no I/O.
"""

from __future__ import annotations

from enum import StrEnum


class RepairDisposition(StrEnum):
    """Every named outcome of a conflict-repair recipe.

    The single source of truth for the disposition vocabulary carried on
    ``pr.action_failed`` (and, for ``unarmed_review_fence``, on the
    ``session.needs_attention`` page). ``docs/events.md`` and
    ``docs/dispatch-runbook.md`` list every value; a doc-drift test pins that.
    """

    NON_CHANGELOG_CONFLICT = "non_changelog_conflict"
    CONFLICT_UNEXPLAINED = "conflict_unexplained"
    CHANGELOG_UNPARSEABLE = "changelog_unparseable"
    CHANGELOG_RELEASED_SECTION_TOUCHED = "changelog_released_section_touched"
    CHANGELOG_AMBIGUOUS = "changelog_ambiguous"
    REBASE_FAILED = "rebase_failed"
    VERIFY_FAILED = "verify_failed"
    GATE_FAILED = "gate_failed"
    PUSH_REJECTED = "push_rejected"
    PUSH_OUTCOME_UNKNOWN = "push_outcome_unknown"
    REARM_FAILED = "rearm_failed"
    ARM_SCRIPT_MISSING = "arm_script_missing"
    PR_HEAD_MISMATCH = "pr_head_mismatch"
    FETCH_FAILED = "fetch_failed"
    SCRATCH_WORKTREE_FAILED = "scratch_worktree_failed"
    DISARM_FAILED = "disarm_failed"
    POST_PUSH_FAILED = "post_push_failed"
    UNARMED_REVIEW_FENCE = "unarmed_review_fence"
    # repair_mechanical_conflict (#2917).
    OUT_OF_SCOPE_CONFLICT = "out_of_scope_conflict"
    MERGE_FAILED = "merge_failed"
    REPAIR_FAILED = "repair_failed"
    REPAIR_INCOMPLETE = "repair_incomplete"
    REPAIR_OUT_OF_SCOPE = "repair_out_of_scope"
    CHECK_FAILED = "check_failed"
