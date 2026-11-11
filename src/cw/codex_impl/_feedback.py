"""The repair entry's live-PR bind and default-branch merge (#3061, RFC 0018 C2).

A repair row carries ``task.review_feedback`` (a review report captured against
an exact head commit). Before any turn, :func:`verify_review_feedback` binds it
to the live PR, the way the claude-native "Review-feedback guard" tells its
agent to: every lookup is live (never a cached PR record) and every mismatch or
failure fails closed as ``impl_failed`` with details beginning
:data:`STALE_PREFIX`.

The default-branch merge is two steps, split because
:func:`cw.codex_fix_loop.baseline.capture_cycle_baseline` refuses a worktree
with ``MERGE_HEAD`` or unmerged paths: :func:`probe_default_branch` is read-only
and runs before the baseline; :func:`merge_default_on_conflict` runs after it,
only when the probe saw a conflict on a repair row, and leaves the conflicts
in the tree for the turn to resolve.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_output, run_git
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_impl._result import IMPL_FAILED, ImplBlock
from cw.gh import fetch_pr_view, fetch_repo_name_with_owner, github_pr_url
from cw.merge_probe import merge_tree, probe_default_merge

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.models.review_feedback import ReviewFeedback

STALE_PREFIX: Final = "review_feedback_stale"
CONFLICT_PREFIX: Final = "merge_conflict_with_default_branch"
PROBE_UNAVAILABLE: Final = "merge_probe_unavailable"

_PR_FIELDS: Final = "number,headRefOid,headRefName,url"
_GH_TIMEOUT_SECONDS: Final = 30
_MERGE_CONFLICT_RC: Final = 1
_MERGE_CLEAN_RC: Final = 0
_ERROR_TAIL_CHARS: Final = 300


def _stale(why: str) -> ImplBlock:
    return ImplBlock(
        IMPL_FAILED,
        f"{STALE_PREFIX}: {why}. The feedback was not applied and no turn ran; "
        "re-capture the review against the current PR head.",
    )


def _local_state(worktree: Path) -> tuple[str, str] | None:
    """``(HEAD sha, current branch)``, or ``None`` when git cannot say."""
    try:
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        branch = git_output(["branch", "--show-current"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return head, branch


def _pr_mismatch(
    feedback: ReviewFeedback, view: dict[str, object], local: tuple[str, str]
) -> str | None:
    """Why the live PR view and the local worktree do not match *feedback*."""
    head, branch = local
    checks = (
        (
            view.get("number") == feedback.pr_number,
            f"live PR number {view.get('number')!r} != {feedback.pr_number}",
        ),
        (
            view.get("url") == github_pr_url(feedback.repo, feedback.pr_number),
            f"live PR url {view.get('url')!r} does not identify the PR",
        ),
        (
            view.get("headRefOid") == feedback.reviewed_sha,
            f"live PR head {view.get('headRefOid')!r} != reviewed sha "
            f"{feedback.reviewed_sha}",
        ),
        (
            head == feedback.reviewed_sha,
            f"local HEAD {head} != reviewed sha {feedback.reviewed_sha}",
        ),
        (bool(branch), "HEAD is detached"),
        (
            branch == view.get("headRefName"),
            f"current branch {branch!r} != PR head ref {view.get('headRefName')!r}",
        ),
    )
    return next((why for holds, why in checks if not holds), None)


def verify_review_feedback(task: TicketTask, worktree: Path) -> ImplBlock | None:
    """Bind *task*'s review feedback to the live PR; ``None`` when it holds.

    Rows without feedback skip the bind. Live ``gh repo view`` must equal
    ``feedback.repo``; live ``gh pr view`` (by URL, so ``gh`` infers the repo)
    must return the PR number and URL, a ``headRefOid`` equal to
    ``reviewed_sha`` and to the local ``HEAD``, and a ``headRefName`` equal to
    the current branch. Any mismatch or lookup failure is ``impl_failed``.
    """
    feedback = task.review_feedback
    if feedback is None:
        return None
    repo = fetch_repo_name_with_owner(timeout=_GH_TIMEOUT_SECONDS, cwd=worktree)
    if repo is None:
        return _stale("the live repository could not be read (gh repo view)")
    if repo != feedback.repo:
        return _stale(f"live repository {repo!r} != feedback repo {feedback.repo!r}")
    view = fetch_pr_view(
        github_pr_url(feedback.repo, feedback.pr_number),
        timeout=_GH_TIMEOUT_SECONDS,
        fields=_PR_FIELDS,
        cwd=worktree,
    )
    if view is None:
        return _stale("the live PR could not be read (gh pr view)")
    local = _local_state(worktree)
    if local is None:
        return _stale("the local HEAD and branch could not be read")
    why = _pr_mismatch(feedback, view, local)
    return _stale(why) if why is not None else None


@dataclass(frozen=True)
class ProbeOutcome:
    """What the read-only default-branch probe found."""

    block: ImplBlock | None = None
    friction: tuple[str, ...] = ()
    needs_merge: bool = False
    conflicted_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeOutcome:
    """A started conflict merge: the merge-side files and the conflicted tree.

    ``merge_files`` are the paths the default branch changed since the merge
    base (cw's merge brought them in, so the turn did not edit them);
    ``growth_tree`` is the conflicted merge-tree oid the growth guard measures
    the turn against.
    """

    merge_files: frozenset[str]
    growth_tree: str


def _fetch_default(worktree: Path, default_branch: str) -> str | None:
    """``None`` when ``origin/<default>`` was fetched, else why not."""
    try:
        fetched = run_git(
            ["fetch", "origin", default_branch],
            cwd=worktree,
            capture_output=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (
            f"git fetch origin {default_branch} did not run: {redact_and_cap(str(exc))}"
        )
    if fetched.returncode != 0:
        tail = redact_and_cap((fetched.stderr or "").strip()[-_ERROR_TAIL_CHARS:])
        return f"git fetch origin {default_branch} failed: {tail}"
    return None


def probe_default_branch(
    worktree: Path, *, default_branch: str, repair: bool
) -> ProbeOutcome:
    """Would merging ``origin/<default>`` conflict? Read-only; fetches first.

    A failed fetch or an unavailable probe is friction
    (``merge_probe_unavailable: ...``) and no merge. A conflict on a fresh row
    blocks ``impl_failed`` (``merge_conflict_with_default_branch``); on a repair
    row it sets ``needs_merge``. It never mutates the tree.
    """
    failed = _fetch_default(worktree, default_branch)
    if failed is not None:
        return ProbeOutcome(friction=(f"{PROBE_UNAVAILABLE}: {failed}",))
    probe = probe_default_merge(worktree, default_branch)
    if probe.state == "unavailable":
        return ProbeOutcome(
            friction=(f"{PROBE_UNAVAILABLE}: {redact_and_cap(probe.detail)}",)
        )
    if probe.state != "conflict":
        return ProbeOutcome()
    if repair:
        return ProbeOutcome(needs_merge=True, conflicted_files=probe.conflicted_files)
    files = redact_and_cap(", ".join(probe.conflicted_files))
    return ProbeOutcome(
        block=ImplBlock(
            IMPL_FAILED,
            f"{CONFLICT_PREFIX}: merging origin/{default_branch} into this branch "
            f"conflicts in {files}; a fresh IMPL row does not resolve default-"
            "branch conflicts. Rebase or merge the branch, then requeue.",
        )
    )


def merge_default_on_conflict(
    worktree: Path, *, default_branch: str
) -> MergeOutcome | ImplBlock:
    """Start ``git merge --no-commit --no-ff origin/<default>`` leaving conflicts.

    Call only after the clean-start baseline is captured and only when
    :func:`probe_default_branch` set ``needs_merge``. Returns the merge-side
    file set (``git diff --name-only HEAD...origin/<default>``) and the
    conflicted merge-tree oid, or an ``impl_failed`` block when git cannot
    start the merge.
    """
    ref = f"origin/{default_branch}"
    tree = merge_tree(worktree, "HEAD", ref)
    if tree.tree is None:
        return ImplBlock(
            IMPL_FAILED,
            "cannot measure the default-branch merge: "
            + redact_and_cap(tree.detail or tree.status),
        )
    merged = run_git(
        ["merge", "--no-commit", "--no-ff", ref], cwd=worktree, capture_output=True
    )
    if merged.returncode not in {_MERGE_CLEAN_RC, _MERGE_CONFLICT_RC}:
        tail = redact_and_cap(
            (merged.stderr or merged.stdout or "").strip()[-_ERROR_TAIL_CHARS:]
        )
        return ImplBlock(IMPL_FAILED, f"git merge {ref} failed: {tail}")
    names = git_output(
        ["diff", "--name-only", "--no-renames", f"HEAD...{ref}"], cwd=worktree
    )
    return MergeOutcome(
        merge_files=frozenset(line for line in names.splitlines() if line),
        growth_tree=tree.tree,
    )
