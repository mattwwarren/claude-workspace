"""The repair_changelog_conflict recipe: CHANGELOG-only conflicts (#2771).

``CHANGELOG.md merge=union`` (``.gitattributes``) means a local rebase never
conflicts on CHANGELOG.md, while GitHub (which ignores merge drivers) reports
the PR ``CONFLICTING`` / ``DIRTY``. Every feat/fix PR adds an ``[Unreleased]``
bullet, so an open pipeline PR goes conflicting whenever another PR lands
first, and a textual rebase after a release silently files bullets under the
released version.

This recipe repairs such a PR when its ONLY conflict is CHANGELOG.md:

- **Detect** (pure, write-free): ``merge_blocked`` rows whose PR is
  ``CONFLICTING`` + ``DIRTY`` and whose queue row is ``COMPLETED`` (shipped,
  no worker that could push concurrently). The same row is also an
  ``escalate_merge_block`` candidate: that page still fires, by design.
- **Act** (under ``dev_queue_lock``, no subprocess): the shared
  :func:`~cw.reconcile.review_recipes.repair_engine.act_repairs` with
  :data:`CHANGELOG_SPEC`; the only state is the in-process :data:`_LATCH`.
- **Dispatch** (post-lock,
  :func:`~cw.reconcile.review_recipes.repair_engine.dispatch_repair`): the
  engine fetches and derives the conflict set; this module's strategy
  requires it to be exactly ``{CHANGELOG.md}``, rebuilds CHANGELOG.md
  structurally (:func:`cw.changelog_rebuild.rebuild_changelog`), rebases the
  rest in a detached scratch tree, verifies the non-CHANGELOG diff is
  unchanged and runs the freeze gate. The engine then disarms, force-pushes
  with a lease, emits ``PR_ACTION_TAKEN`` and re-arms behind the
  review-approval and tip fence, or leaves the PR unarmed and pages.

Every non-success exit emits ``PR_ACTION_FAILED`` with a
:class:`~cw.repair_disposition.RepairDisposition`; the row's status is never
changed. Not in ``RECIPE_ATTENTION_STATES`` / ``RECIPE_FIRED_AT_GETTERS``: it
has no persisted ``*_fired_at`` latch for the doctor liveness check to read.
No LLM session is spawned, so usage limits do not apply.

Leaf module: depends only on ``_shared``, ``repair_engine``, ``repair_git``
and ``cw.changelog_rebuild`` (plus ``cw`` infrastructure). Module-level
imports only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.changelog_rebuild import ChangelogRebuildError, rebuild_changelog
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_MERGE_BLOCKED,
    RECIPE_REPAIR_CHANGELOG_CONFLICT,
    _detect_by_attention_state,
)
from cw.reconcile.review_recipes.repair_engine import (
    _EVIDENCE_NEW_SHA,
    AttemptLatch,
    RepairSpec,
    Staged,
    act_repairs,
    is_repair_shape,
)
from cw.reconcile.review_recipes.repair_git import (
    CHANGELOG,
    RepairStopError,
    _commit_rebuilt,
    _head_sha,
    _probe_arm_script,
    _read_three_blobs,
    _rebase_onto,
    _run_changelog_gate,
    _verify_non_changelog_identical,
)
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask
    from cw.reconcile.review_recipes._shared import RepoSlugs, ReviewRecipeCandidate
    from cw.reconcile.review_recipes.repair_engine import (
        GuardTarget,
        Prepared,
        RepairJob,
        RepairState,
    )

_logger = logging.getLogger("cw.reconcile.review_recipes")

_CONFLICT_KIND = "changelog_only_candidate"
_UNARMED_ATTENTION_REASON = "changelog_repair_unarmed"

_LATCH = AttemptLatch()


def _detect_repair_changelog_conflict(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect COMPLETED rows whose PR is CONFLICTING/DIRTY (pure, write-free).

    ``BEHIND`` is not a conflict; a non-COMPLETED row could have a worker
    pushing concurrently, or an approved sha a rewrite would invalidate.
    """
    return _detect_by_attention_state(
        [task for task in tasks if is_repair_shape(task)],
        clients=clients,
        config=config,
        attention_state=_ATTENTION_MERGE_BLOCKED,
        recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
    )


class _ChangelogStaged(NamedTuple):
    """The rebuilt CHANGELOG.md and main's, for the rebase and the commit."""

    rebuilt: str
    main_text: str


def _check_changelog_only(conflicts: frozenset[str], _cfg: ClientConfig) -> None:
    """Step 5: the derived conflict set must be exactly ``{CHANGELOG.md}``."""
    if not conflicts:
        raise RepairStopError(
            RepairDisposition.CONFLICT_UNEXPLAINED,
            "GitHub reports CONFLICTING but no conflicted file was derived",
        )
    if conflicts != {CHANGELOG}:
        raise RepairStopError(
            RepairDisposition.NON_CHANGELOG_CONFLICT,
            f"conflicted files: {sorted(conflicts)}",
        )


def _stage_rewrite(job: RepairJob, prep: Prepared) -> Staged[_ChangelogStaged]:
    """Steps 6-7: rebuild CHANGELOG.md and find the arm script, pre-worktree.

    A branch bullet already present anywhere in main is not re-added (the
    idempotence rule); each one is logged here so a dropped bullet is visible.
    """
    base, branch_text, main_text = _read_three_blobs(
        prep.repo, prep.merge_base, prep.expected_sha, prep.default_ref
    )
    try:
        result = rebuild_changelog(base=base, branch=branch_text, main=main_text)
    except ChangelogRebuildError as exc:
        raise RepairStopError(exc.disposition, exc.detail) from exc
    for item in result.skipped_present:
        _logger.info(
            "changelog_repair_bullet_skipped client=%s ticket=%s subsection=%s"
            " already_in_main text=%r",
            job.client,
            job.ticket_id,
            item.subsection,
            item.normalized,
        )
    return Staged(
        arm_script=_probe_arm_script(prep.repo),
        summary=(
            f"bullets_inserted={len(result.inserted)}"
            f" bullets_skipped={len(result.skipped_present)}"
        ),
        data=_ChangelogStaged(rebuilt=result.text, main_text=main_text),
    )


def _rewrite_changelog(
    job: RepairJob,
    prep: Prepared,
    staged: Staged[_ChangelogStaged],
    scratch: Path,
    state: RepairState,
) -> str:
    """Steps 9-12 in the scratch tree: rebase, commit, verify, gate; the new head."""
    _rebase_onto(
        scratch,
        prep.default_ref,
        staged.data.main_text,
        prep.merge_base,
        prep.expected_sha,
    )
    _commit_rebuilt(scratch, staged.data.rebuilt, job.ticket_id, prep.default_branch)
    new_sha = _head_sha(scratch)
    state.evidence[_EVIDENCE_NEW_SHA] = new_sha
    _verify_non_changelog_identical(
        scratch, prep.merge_base, prep.expected_sha, prep.default_ref, new_sha
    )
    _run_changelog_gate(prep.repo, scratch)
    return new_sha


CHANGELOG_SPEC: RepairSpec[_ChangelogStaged] = RepairSpec(
    recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
    label="changelog_repair",
    conflict_kind=_CONFLICT_KIND,
    unarmed_reason=_UNARMED_ATTENTION_REASON,
    scratch_prefix=".changelog-repair-",
    unexpected=RepairDisposition.REBASE_FAILED,
    latch=_LATCH,
    hidden_changelog_conflict=True,
    record_conflicted_paths=False,
    check_conflicts=_check_changelog_only,
    stage=_stage_rewrite,
    rewrite=_rewrite_changelog,
)


def _act_repair_changelog_conflict(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
) -> list[RepairJob]:
    """Act phase: the shared :func:`act_repairs` with :data:`CHANGELOG_SPEC`.

    *guard_target* is ``auto_fix_ci._auto_fix_ci_guard_target`` (the client
    workspace), passed in by ``core`` so this leaf module imports no sibling
    recipe.
    """
    return act_repairs(
        CHANGELOG_SPEC,
        candidates,
        clients=clients,
        repo_slugs=repo_slugs,
        guard_target=guard_target,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
