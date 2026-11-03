"""The repair_mechanical_conflict recipe: conflicts in declared paths (#2917).

Some conflicts are mechanical: two branches each add a migration and both
repoint the same head pointer, or both regenerate one lockfile. A client
declares such paths, and the command that resolves them, in its
``mechanical_conflict_repair`` entry (``clients.yaml``). This recipe repairs a
shipped PR whose conflicts ALL lie in those paths:

- **Detect** (pure, write-free): the CHANGELOG recipe's trigger, a
  ``merge_blocked`` row whose PR is ``CONFLICTING`` + ``DIRTY`` and whose
  queue row is ``COMPLETED``, for a client WITH an entry. The entry's
  presence is the opt-in (the resolver's floor for this recipe); a lane or
  ticket ``review_recipes`` ``false`` vetoes it. The row is also an
  ``escalate_merge_block`` and ``repair_changelog_conflict`` candidate; the
  CHANGELOG recipe acts first and its row is excluded here for the tick.
- **Act** (under ``dev_queue_lock``, no subprocess): the shared
  :func:`~cw.reconcile.review_recipes.repair_engine.act_repairs` with
  :data:`MECHANICAL_SPEC`; the only state is the in-process :data:`_LATCH`.
- **Dispatch** (post-lock, the shared engine): the conflict set is
  ``merge-tree``'s conflicted files (no hidden-CHANGELOG rule: a CHANGELOG
  edit on both sides must not park an in-glob conflict). This strategy
  requires every one to match a declared glob, then in a detached scratch
  tree merges ``origin/<default>`` into the branch, runs the repair command
  mid-merge, stages only in-scope paths, refuses leftover conflict markers,
  commits the merge, verifies that nothing outside the globs changed, and
  runs the verify command. The engine then disarms, force-pushes with a
  lease and re-arms behind the unchanged fence, or leaves the PR unarmed
  and pages (``paused_status: mechanical_repair_unarmed``).

It covers textual conflicts only: a silent duplicate head (two differently
named migrations off one parent, no textual conflict) is MERGEABLE/CLEAN and
never fires a recipe.

Leaf module: depends only on ``_shared``, ``repair_engine``, ``repair_git``
and ``mechanical_repair_git`` (plus ``cw`` infrastructure). Module-level
imports only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw._git import git_clean_env
from cw.reconcile.review_recipes._shared import (
    _ATTENTION_MERGE_BLOCKED,
    RECIPE_REPAIR_MECHANICAL_CONFLICT,
    _detect_by_attention_state,
)
from cw.reconcile.review_recipes.mechanical_repair_git import (
    _commit_merge,
    _scan_markers,
    _stage_scoped,
    _start_merge,
    path_in_scope,
    resolve_argv,
    run_check,
    run_repair,
    sample_paths,
    verify_outside_scope_identical,
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
    RepairStopError,
    _head_sha,
    _probe_arm_script,
)
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, TicketTask
    from cw.models.client import MechanicalConflictRepairConfig
    from cw.reconcile.review_recipes._shared import RepoSlugs, ReviewRecipeCandidate
    from cw.reconcile.review_recipes.repair_engine import (
        GuardTarget,
        Prepared,
        RepairJob,
        RepairState,
    )

_CONFLICT_KIND = "mechanical_paths_candidate"
_UNARMED_ATTENTION_REASON = "mechanical_repair_unarmed"
_ENV_DEFAULT_REF = "CW_REPAIR_DEFAULT_REF"
_ENV_MERGE_BASE = "CW_REPAIR_MERGE_BASE"
_ENV_BRANCH = "CW_REPAIR_BRANCH"
_ENV_CONFLICTED_PATHS = "CW_REPAIR_CONFLICTED_PATHS"

_LATCH = AttemptLatch()


def _has_entry(task: TicketTask, clients: dict[str, ClientConfig]) -> bool:
    client_cfg = clients.get(task.client)
    return client_cfg is not None and client_cfg.mechanical_conflict_repair is not None


def _entry(client_cfg: ClientConfig) -> MechanicalConflictRepairConfig:
    """The client's entry; its absence parks ``merge_failed`` (detect and act
    both require it, so this only fires if the config changed mid-tick)."""
    entry = client_cfg.mechanical_conflict_repair
    if entry is None:
        raise RepairStopError(
            RepairDisposition.MERGE_FAILED,
            f"client {client_cfg.name} has no mechanical_conflict_repair entry",
        )
    return entry


def _detect_repair_mechanical_conflict(
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[ReviewRecipeCandidate]:
    """Detect COMPLETED CONFLICTING/DIRTY rows of entry clients (pure)."""
    return _detect_by_attention_state(
        [task for task in tasks if is_repair_shape(task) and _has_entry(task, clients)],
        clients=clients,
        config=config,
        attention_state=_ATTENTION_MERGE_BLOCKED,
        recipe=RECIPE_REPAIR_MECHANICAL_CONFLICT,
    )


def _check_conflicts(conflicts: frozenset[str], client_cfg: ClientConfig) -> None:
    """Step 5: a non-empty set, every path matching a declared glob.

    A mixed set (one declared path plus anything else, CHANGELOG.md
    included) parks whole: never half-repair.
    """
    in_scope = path_in_scope(_entry(client_cfg).paths)
    if not conflicts:
        raise RepairStopError(
            RepairDisposition.CONFLICT_UNEXPLAINED,
            "GitHub reports CONFLICTING but no conflicted file was derived",
        )
    if not all(in_scope(path) for path in conflicts):
        outside = [path for path in conflicts if not in_scope(path)]
        raise RepairStopError(
            RepairDisposition.OUT_OF_SCOPE_CONFLICT,
            f"conflicted path(s) outside the declared globs: {sample_paths(outside)}",
        )


def _stage(_job: RepairJob, prep: Prepared) -> Staged[None]:
    """Steps 6-7: find the arm script, pre-worktree; no repo change."""
    return Staged(
        arm_script=_probe_arm_script(prep.repo),
        summary=f"conflicted_paths={len(prep.conflicts)}",
        data=None,
    )


def _command_env(prep: Prepared, unmerged: frozenset[str]) -> dict[str, str]:
    """The daemon's environment minus ``GIT_*``, plus the ``CW_REPAIR_*`` keys."""
    env = git_clean_env()
    env[_ENV_DEFAULT_REF] = prep.default_ref
    env[_ENV_MERGE_BASE] = prep.merge_base
    env[_ENV_BRANCH] = prep.branch
    env[_ENV_CONFLICTED_PATHS] = "\n".join(sorted(unmerged))
    return env


def _rewrite(
    job: RepairJob,
    prep: Prepared,
    _staged: Staged[None],
    scratch: Path,
    state: RepairState,
) -> str:
    """Steps 9-12 in the scratch tree: merge, repair, commit, verify; the head."""
    entry = _entry(job.client_cfg)
    in_scope = path_in_scope(entry.paths)
    client, ticket = job.client, job.ticket_id
    unmerged = _start_merge(scratch, prep.default_ref, in_scope)
    env = _command_env(prep, unmerged)
    run_repair(
        resolve_argv(entry.repair_command, prep.repo),
        cwd=scratch,
        env=env,
        timeout=entry.repair_timeout_seconds,
        client=client,
        ticket=ticket,
    )
    _stage_scoped(scratch, in_scope, client=client, ticket=ticket)
    _scan_markers(scratch, unmerged)
    _commit_merge(
        scratch,
        f"chore(merge): sync {prep.default_branch} and repair declared-path"
        f" conflicts (#{job.ticket_id})",
    )
    new_sha = _head_sha(scratch, stop=RepairDisposition.MERGE_FAILED)
    state.evidence[_EVIDENCE_NEW_SHA] = new_sha
    verify_outside_scope_identical(
        scratch,
        in_scope,
        prep.merge_base,
        prep.expected_sha,
        prep.default_ref,
        new_sha,
        client=client,
        ticket=ticket,
    )
    run_check(
        resolve_argv(entry.verify_command, prep.repo),
        cwd=scratch,
        env=env,
        timeout=entry.verify_timeout_seconds,
        client=client,
        ticket=ticket,
    )
    return new_sha


MECHANICAL_SPEC: RepairSpec[None] = RepairSpec(
    recipe=RECIPE_REPAIR_MECHANICAL_CONFLICT,
    label="mechanical_repair",
    conflict_kind=_CONFLICT_KIND,
    unarmed_reason=_UNARMED_ATTENTION_REASON,
    scratch_prefix=".mechanical-repair-",
    unexpected=RepairDisposition.MERGE_FAILED,
    latch=_LATCH,
    hidden_changelog_conflict=False,
    record_conflicted_paths=True,
    check_conflicts=_check_conflicts,
    stage=_stage,
    rewrite=_rewrite,
)


def _act_repair_mechanical_conflict(
    candidates: list[ReviewRecipeCandidate],
    *,
    clients: dict[str, ClientConfig],
    repo_slugs: RepoSlugs,
    guard_target: GuardTarget,
    config: OrchestratorConfig | None = None,
    repeat_fire_counts: dict[tuple[str, str, str], int] | None = None,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> list[RepairJob]:
    """Act phase: the shared :func:`act_repairs` with :data:`MECHANICAL_SPEC`.

    *exclude* holds the ``(client, ticket_id)`` rows the CHANGELOG recipe
    took this tick (``core``); they are skipped with the latch unbumped.
    """
    return act_repairs(
        MECHANICAL_SPEC,
        candidates,
        clients=clients,
        repo_slugs=repo_slugs,
        guard_target=guard_target,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
        exclude=exclude,
    )
