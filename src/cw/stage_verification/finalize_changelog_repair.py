"""The one-shot CHANGELOG repairs around the merge gates (#3861, #3939).

Two sibling epic children each append a bullet to the same ``### Fixed`` hunk.
GitHub's conflict check ignores the ``CHANGELOG.md merge=union`` driver and
reports the PR ``DIRTY``; the local probe applies the driver and is clean.
:func:`check_mergeability_repairing` wraps :func:`finalize_pr.check_mergeability`:
on that one park, and only for a non-default base, it makes one plain
``git merge --no-ff`` of ``origin/<base>`` (through
:func:`cw.changelog_sync.sync_changelog`), one plain push and one re-check.

:func:`repair_arm_refusal` runs the same sync at the arm: a release-cut merge
freeze (``changelog_merge_freeze:``) on any base, and a ``pr_conflicting:``
refusal on a non-default base (#3939). Each kind is repaired at most once per
run by the caller's spent set.

It never rebases, force-pushes, or repairs twice. A genuine conflict is left
as a block that names the files.

Imports only :mod:`cw.changelog_sync` and the ``cw.stage_verification`` modules
``finalize_arm``, ``finalize_pr``, ``finalize_result``, ``impl_feedback`` and
``posted_text``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from cw.changelog_sync import SyncState, sync_changelog
from cw.stage_verification.finalize_arm import FREEZE_PREFIX, PR_CONFLICTING_PREFIX
from cw.stage_verification.finalize_pr import (
    check_mergeability,
    local_only_merge,
    push_ahead,
)
from cw.stage_verification.finalize_result import (
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    FinalizeBlock,
)
from cw.stage_verification.impl_feedback import fetch_default_branch
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from cw.changelog_sync import ChangelogSync
    from cw.stage_verification.finalize_arm import ArmResult

__all__ = [
    "CHANGELOG_UNION_FRICTION",
    "RELEASE_CUT_SYNC_FRICTION",
    "ArmRepair",
    "MergeabilityOutcome",
    "check_mergeability_repairing",
    "repair_arm_refusal",
]

#: Friction token recorded when the union merge cleared the GitHub conflict.
CHANGELOG_UNION_FRICTION: Final = "changelog_union_merged_after_sibling"
#: Friction token recorded when the release-cut sync cleared a merge freeze (#3939).
RELEASE_CUT_SYNC_FRICTION: Final = "changelog_synced_after_release_cut"

_DETAIL_CAP: Final = 300


@dataclass(frozen=True)
class MergeabilityOutcome:
    """The mergeability answer after the optional repair.

    ``block`` is ``None`` when the PR is mergeable. ``head`` is the new HEAD
    when the repair pushed a merge (the caller stores it in its run state),
    else ``None``. ``friction`` is every note the check and the repair made.
    ``refused`` is true when the seam answered ``refused``: nothing may then
    merge the base by another route.
    """

    block: FinalizeBlock | None
    friction: list[str] = field(default_factory=list)
    head: str | None = None
    refused: bool = False


def _with_note(
    block: FinalizeBlock, friction: list[str], note: str
) -> MergeabilityOutcome:
    """The original park with *note* appended to its details; no repair happened."""
    return MergeabilityOutcome(
        replace(block, details=f"{block.details}; {note}"), friction
    )


def _changelog_note(state: str, disposition: str | None, detail: str) -> str:
    """The note that says what the local CHANGELOG check found."""
    if state == SyncState.CLEAN:
        return (
            "changelog-sync clean: the GitHub conflict is not explained by"
            " CHANGELOG union"
        )
    shown = redact_and_cap(detail)[:_DETAIL_CAP]
    return f"changelog-sync {state} ({disposition or '-'}): {shown}"


def _after_move(
    worktree: Path,
    pr_url: str,
    *,
    branch: str,
    head_sha: str,
    friction: list[str],
) -> MergeabilityOutcome:
    """Push the union merge plainly, then re-check the PR once."""
    pushed = push_ahead(worktree, branch=branch, head_sha=head_sha)
    if pushed is not None:
        return MergeabilityOutcome(
            local_only_merge(
                pushed, merge="CHANGELOG union merge", branch=branch, head_sha=head_sha
            ),
            friction,
        )
    recheck, again = check_mergeability(worktree, pr_url)
    if recheck is None:
        return MergeabilityOutcome(
            None, [*friction, *again, CHANGELOG_UNION_FRICTION], head_sha
        )
    note = "still DIRTY after one CHANGELOG union merge"
    return MergeabilityOutcome(
        replace(recheck, details=f"{recheck.details}; {note}"),
        [*friction, *again],
        head_sha,
    )


def _repair(
    worktree: Path,
    pr_url: str,
    *,
    base_branch: str,
    default_branch: str,
    branch: str,
    friction: list[str],
    block: FinalizeBlock,
) -> MergeabilityOutcome:
    """Steps 3-5: fetch the base, run the seam, act on its state."""
    failed = fetch_default_branch(worktree, base_branch)
    if failed is not None:
        return _with_note(block, friction, f"repair not attempted: {failed}")
    synced = sync_changelog(
        worktree,
        base_branch,
        union_conflict=True,
        default_branch=default_branch,
    )
    if synced.state == SyncState.MOVED:
        if synced.head_sha is None:
            return MergeabilityOutcome(
                FinalizeBlock(
                    OPERATOR_UNAVAILABLE,
                    "changelog-sync moved but reported no head_sha",
                    retry_eligible=True,
                ),
                friction,
            )
        return _after_move(
            worktree,
            pr_url,
            branch=branch,
            head_sha=synced.head_sha,
            friction=friction,
        )
    if synced.state == SyncState.CONFLICT:
        files = ", ".join(redact_and_cap(f) for f in synced.conflicted_files)
        return MergeabilityOutcome(
            FinalizeBlock(
                MERGE_CONFLICT_POST_PUSH,
                f"{pr_url} conflicts with origin/{base_branch} beyond the CHANGELOG"
                f" union (files: {files or 'unknown'}); the union repair made no"
                " merge",
                retry_eligible=True,
            ),
            friction,
        )
    note = _changelog_note(synced.state, synced.disposition, synced.detail)
    noted = _with_note(block, friction, note)
    return replace(noted, refused=synced.state == SyncState.REFUSED)


def check_mergeability_repairing(
    worktree: Path,
    pr_url: str,
    *,
    base_branch: str,
    default_branch: str,
    branch: str,
) -> MergeabilityOutcome:
    """The mergeability check, with one CHANGELOG-union repair for a non-default base.

    Passes straight through unless the check parks ``merge_conflict_post_push``
    on a non-default base. The seam reads HEAD itself, so the caller's head is
    not needed here.
    """
    block, friction = check_mergeability(worktree, pr_url)
    if (
        block is None
        or block.reason != MERGE_CONFLICT_POST_PUSH
        or base_branch == default_branch
    ):
        return MergeabilityOutcome(block, friction)
    return _repair(
        worktree,
        pr_url,
        base_branch=base_branch,
        default_branch=default_branch,
        branch=branch,
        friction=friction,
        block=block,
    )


@dataclass(frozen=True)
class ArmRepair:
    """One arm-refusal repair: the refusal kind it spent and what came of it.

    ``block`` set: the repair failed and the arm block stands. ``head`` set: a
    merge was pushed, and the caller re-fences at it. Neither set: re-arm.
    ``friction`` is appended after the re-fence passes.
    """

    prefix: str
    block: FinalizeBlock | None = None
    head: str | None = None
    friction: str | None = None


_MERGE_FREEZE: Final = "release-cut CHANGELOG merge"
_UNION_MERGE: Final = "CHANGELOG union merge"


def _refusal_prefix(gh_stderr: str) -> str | None:
    """The refusal kind *gh_stderr* opens with, or ``None`` (prefix only)."""
    for prefix in (FREEZE_PREFIX, PR_CONFLICTING_PREFIX):
        if gh_stderr.startswith(prefix):
            return prefix
    return None


def _finish_arm_repair(
    worktree: Path,
    prefix: str,
    block: FinalizeBlock,
    synced: ChangelogSync,
    *,
    branch: str,
) -> ArmRepair:
    """Act on a sync that did not come back clean: block, push, or re-arm."""
    if synced.state != SyncState.MOVED:
        note = _changelog_note(synced.state, synced.disposition, synced.detail)
        return ArmRepair(prefix, block=_prefixed(block, f"{prefix} {note}"))
    if synced.head_sha is None:
        note = "changelog-sync moved but reported no head_sha"
        return ArmRepair(
            prefix,
            block=replace(_prefixed(block, f"{prefix} {note}"), retry_eligible=False),
        )
    merge = _MERGE_FREEZE if prefix == FREEZE_PREFIX else _UNION_MERGE
    pushed = push_ahead(worktree, branch=branch, head_sha=synced.head_sha)
    if pushed is not None:
        failed = replace(block, details=f"{block.details}; {pushed.details}")
        return ArmRepair(
            prefix,
            block=local_only_merge(
                failed, merge=merge, branch=branch, head_sha=synced.head_sha
            ),
        )
    token = (
        RELEASE_CUT_SYNC_FRICTION
        if prefix == FREEZE_PREFIX
        else CHANGELOG_UNION_FRICTION
    )
    return ArmRepair(prefix, head=synced.head_sha, friction=token)


def _prefixed(block: FinalizeBlock, lead: str) -> FinalizeBlock:
    """The arm block with *lead* on its own line before its original details."""
    return replace(block, details=f"{lead}\n{block.details}")


def _union_repair(
    worktree: Path,
    block: FinalizeBlock,
    *,
    base_branch: str,
    default_branch: str,
    branch: str,
) -> ArmRepair | None:
    """The ``pr_conflicting:`` repair: default base is never repaired (#3939)."""
    if base_branch == default_branch:
        return None
    failed = fetch_default_branch(worktree, base_branch)
    if failed is not None:
        note = f"repair not attempted: {failed}"
        return ArmRepair(
            PR_CONFLICTING_PREFIX,
            block=replace(block, details=f"{block.details}; {note}"),
        )
    synced = sync_changelog(
        worktree,
        base_branch,
        union_conflict=True,
        default_branch=default_branch,
    )
    return _finish_arm_repair(
        worktree, PR_CONFLICTING_PREFIX, block, synced, branch=branch
    )


def repair_arm_refusal(
    worktree: Path,
    refused: ArmResult,
    *,
    spent: Collection[str],
    base_branch: str,
    default_branch: str,
    branch: str,
) -> ArmRepair | None:
    """Repair one recognised arm refusal, or ``None`` to keep the arm block (#3939).

    ``None`` when there is no block, the refusal opens with no known prefix
    (the stderr tail never counts), or its kind is already in *spent*.
    """
    if refused.block is None:
        return None
    prefix = _refusal_prefix(refused.gh_stderr)
    if prefix is None or prefix in spent:
        return None
    if prefix == FREEZE_PREFIX:
        synced = sync_changelog(worktree, base_branch)
        if synced.state == SyncState.CLEAN:
            return ArmRepair(prefix)
        return _finish_arm_repair(
            worktree, prefix, refused.block, synced, branch=branch
        )
    return _union_repair(
        worktree,
        refused.block,
        base_branch=base_branch,
        default_branch=default_branch,
        branch=branch,
    )
