"""The fix_agent recipe: spawn the auto-dev-review fix loop as a cw session (#2017).

A narrow, single-call-site wrapper around spawn_create_impl, modeled on
address_review.py's _dispatch_address_review -- NOT an RFC-0010 detect/act
recipe (no dev-queue candidate, no dev_queue_lock, no fired_at latch).
Exists so .claude/commands/auto-dev-review.md's Step 3b can provision the
fix agent's worktree, refresh it against main, and dispatch it as a
first-class cw DAEMON session -- instead of an unsupervised harness
subagent that cw cannot see, control the placement of, or verify the
launch of (#2017).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.exceptions import CwError
from cw.models import SessionPurpose
from cw.worktree import _git_dir, _run_git, create_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig


def dispatch_fix_agent(
    *,
    client: ClientConfig,
    branch: str,
    prompt_file: Path,
    label: str,
    ticket_id: str,
    lane: str,
    parent: str,
) -> str:
    """Provision the ticket's worktree, refresh it against main, dispatch the fix agent.

    Provisioning reuses ``create_worktree(client, branch)`` -- the same
    branch-keyed helper ``dispatch/claim.py`` calls, resolving to the same
    per-ticket worktree path every pipeline stage reuses
    (``allow_dirty_reuse=True``: fix-loop cycle 2+ re-provisions the same
    branch).

    After ``create_worktree`` resolves the branch, this verifies HEAD landed on
    the expected ``origin/<branch>`` commit (replaces an agent eyeballing ``git
    log --oneline -1``), then fetches and merges ``origin/<default_branch>`` so
    the fix lands on top of any sibling PR that merged to main mid-pipeline --
    without this, a subsequent push would silently ship a branch missing main's
    commits (CI passes because it runs branch-HEAD, not the branch-merged-with-
    main state). On conflict: abort the merge (leaving the worktree clean) and
    raise :exc:`CwError` naming the conflicting files -- never force, never
    auto-resolve, mirroring ``_dispatch_address_review``'s CwError-to-caller
    shape.

    The HEAD-verification step assumes ``origin/<branch>`` already exists: the
    fix loop's own caller contract (auto-dev-review.md Step 3b) requires the
    implementation branch to already be pushed before the fix loop -- and hence
    this function -- is ever entered. That is deliberate, not an oversight: if
    ``origin/<branch>`` is absent, this raises immediately rather than silently
    dispatching against an unexpected/fresh branch.

    ``headless`` is deliberately NOT a parameter: this dispatch is always
    ``headless=False``. A headless session that never emits ``AUTO_DEV_RESULT``
    (the fix agent never does) defers forever in the Stop hook since ADR-0014
    (``_handle_headless_no_sentinel``).

    Passes NO ``task=`` kwarg (mirrors address_review's "Resolution 6: no
    dev-queue correlation") -- ``task.attempts`` and lane occupancy are
    untouched.

    Unlike ``_dispatch_address_review``, does NOT catch ``CwError``: this is a
    single ad hoc dispatch from an interactive skill's fix loop, not one
    candidate among many in a reconcile tick -- a failure here must propagate to
    the caller, surfacing as a non-zero exit from the ``uv run python -c``
    boundary in auto-dev-review.md Step 3b.
    """
    worktree = create_worktree(client, branch, allow_dirty_reuse=True)

    expected_sha = _run_git(
        "rev-parse", f"origin/{branch}", cwd=_git_dir(client)
    ).stdout.strip()
    actual_sha = _run_git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if actual_sha != expected_sha:
        msg = (
            f"dispatch_fix_agent: worktree HEAD ({actual_sha}) does not "
            f"match origin/{branch} ({expected_sha}) after create_worktree "
            "-- refusing to dispatch the fix agent against unexpected "
            "branch state."
        )
        raise CwError(msg)

    _run_git("fetch", "origin", cwd=_git_dir(client))
    merge_result = _run_git(
        "merge",
        f"origin/{client.default_branch}",
        "--no-edit",
        cwd=worktree,
        check=False,
    )
    if merge_result.returncode != 0:
        conflicts = _run_git(
            "diff",
            "--name-only",
            "--diff-filter=U",
            cwd=worktree,
            check=False,
        ).stdout.strip()
        _run_git("merge", "--abort", cwd=worktree, check=False)
        msg = (
            f"dispatch_fix_agent: merging origin/{client.default_branch} "
            f"into {branch} conflicted; merge aborted, worktree left "
            f"clean. Conflicting files:\n{conflicts}"
        )
        raise CwError(msg)

    # Function-local import breaks the cw.spawn <-> cw.reconcile cycle
    # (address_review.py does the identical thing for the same reason).
    from cw.spawn import spawn_create_impl

    return spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt=prompt_file.read_text(encoding="utf-8"),
        label=label,
        headless=False,
        ticket_id=ticket_id,
        lane=lane,
        parent=parent,
        purpose=SessionPurpose.FIX,
    )
