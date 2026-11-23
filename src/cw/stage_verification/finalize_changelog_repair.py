"""The one-shot CHANGELOG-union repair around the codex mergeability check (#3861).

Two sibling epic children each append a bullet to the same ``### Fixed`` hunk.
GitHub's conflict check ignores the ``CHANGELOG.md merge=union`` driver and
reports the PR ``DIRTY``; the local probe applies the driver and is clean.
:func:`check_mergeability_repairing` wraps :func:`finalize_pr.check_mergeability`:
on that one park, and only for a non-default base, it makes one plain
``git merge --no-ff`` of ``origin/<base>`` (through
:func:`cw.changelog_sync.sync_changelog`), one plain push and one re-check.
It never rebases, force-pushes, or repairs twice. A genuine conflict is left
as a park that names the files.

Imports only :mod:`cw.changelog_sync` and the ``cw.stage_verification`` modules
``finalize_pr``, ``finalize_result``, ``impl_feedback`` and ``posted_text``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from cw.changelog_sync import SyncState, sync_changelog
from cw.stage_verification.finalize_pr import check_mergeability, push_ahead
from cw.stage_verification.finalize_result import (
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    FinalizeBlock,
)
from cw.stage_verification.impl_feedback import fetch_default_branch
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "CHANGELOG_UNION_FRICTION",
    "MergeabilityOutcome",
    "check_mergeability_repairing",
]

#: Friction token recorded when the union merge cleared the GitHub conflict.
CHANGELOG_UNION_FRICTION: Final = "changelog_union_merged_after_sibling"

_DETAIL_CAP: Final = 300


@dataclass(frozen=True)
class MergeabilityOutcome:
    """The mergeability answer after the optional repair.

    ``block`` is ``None`` when the PR is mergeable. ``head`` is the new HEAD
    when the repair pushed a merge (the caller stores it in its run state),
    else ``None``. ``friction`` is every note the check and the repair made.
    """

    block: FinalizeBlock | None
    friction: list[str] = field(default_factory=list)
    head: str | None = None


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
        return MergeabilityOutcome(pushed, friction)
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
                f" union (files: {files or 'unknown'}); cw does not resolve conflicts"
                " (#3542)",
                retry_eligible=True,
            ),
            friction,
        )
    note = _changelog_note(synced.state, synced.disposition, synced.detail)
    return _with_note(block, friction, note)


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
