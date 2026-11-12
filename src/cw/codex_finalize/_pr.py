"""PR detection, the live-identity bind, push, create and mergeability (#3062).

Built on the shared ``cw.gh`` reads and writes; no ``gh pr create`` or fetch
helper lives anywhere else in :mod:`cw.codex_finalize`.

* :func:`detect_branch_pr` is the Pre-Stage Detector Guard. The branch is the
  row's resolved feature branch (``resolve_feature_branch``), never
  ``git branch --show-current``.
* :func:`bind_live_pr` follows :func:`cw.codex_impl._feedback.verify_review_feedback`:
  every lookup is live (``gh repo view``, then ``gh pr view`` by URL) and it
  fails closed. Later reads address the PR by the bound URL.
* :func:`probe_default` is a thin call into the C2 default-branch
  fetch-and-probe (:func:`cw.codex_impl._feedback.probe_default_branch` with
  ``repair=True``, which never blocks). A conflict blocks
  ``merge_conflict_post_push`` and is never resolved (#3542).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result.schema._vocab import EXTERNAL_STATE_BLOCKER_REASON
from cw.codex_finalize._gates import check_branch
from cw.codex_finalize._result import (
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    PR_ALREADY_TERMINAL,
    FinalizeBlock,
    classify_unavailability,
)
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_fix_loop.push import push_and_verify_head
from cw.codex_impl._feedback import PROBE_UNAVAILABLE, probe_default_branch
from cw.gh import (
    create_pr,
    fetch_branch_prs,
    fetch_pr_view,
    fetch_repo_name_with_owner,
    github_pr_url,
    parse_pr_url,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PROBE_UNAVAILABLE",
    "BoundPr",
    "DetectedPr",
    "apply_override_section",
    "bind_live_pr",
    "check_mergeability",
    "create_pr_for",
    "detect_branch_pr",
    "probe_default",
    "push_ahead",
]

PR_OPEN: Final = "OPEN"
PR_MERGED: Final = "MERGED"
PR_CLOSED: Final = "CLOSED"
BEHIND_FRICTION: Final = "behind_default_no_action"

_GH_TIMEOUT: Final = 30
_BIND_FIELDS: Final = "number,url,headRefName,headRefOid"
_MERGE_FIELDS: Final = "mergeable,mergeStateStatus"
_BLOCKING_MERGE: Final = frozenset({"CONFLICTING", "DIRTY"})
_TAIL: Final = 500
_OVERRIDE_HEADING: Final = "## Operator override"
_NEXT_REGION: Final = re.compile(r"\n## |<!-- cw-review-signoff:")


@dataclass(frozen=True)
class DetectedPr:
    """The branch's PR the Detector Guard found."""

    number: int
    url: str
    state: str


@dataclass(frozen=True)
class BoundPr:
    """A PR bound to the live repo: its number and canonical URL."""

    number: int
    url: str


def detect_branch_pr(worktree: Path, branch: str) -> DetectedPr | FinalizeBlock | None:
    """The branch's PR (OPEN first, then MERGED), ``None``, or a block.

    CLOSED (with no OPEN or MERGED sibling) is ``pr_already_terminal``; a
    failed lookup is ``operator_unavailable`` (retryable).
    """
    prs, _available = fetch_branch_prs(branch, cwd=worktree)
    if prs is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr list --head {branch} failed; cw cannot tell whether the "
            "branch already has a PR, so nothing was created or reused",
            retry_eligible=True,
        )
    for state in (PR_OPEN, PR_MERGED):
        for pr in prs:
            if pr.state == state:
                return DetectedPr(pr.number, pr.url, pr.state)
    closed = [pr for pr in prs if pr.state == PR_CLOSED]
    if closed:
        return FinalizeBlock(
            PR_ALREADY_TERMINAL,
            f"PR #{closed[0].number} ({closed[0].url}) for branch {branch!r} is "
            "CLOSED without merging: a human decision the pipeline does not work "
            "around. Reopen it or close the ticket; cw creates no duplicate.",
        )
    return None


def _identity_block(why: str) -> FinalizeBlock:
    return FinalizeBlock(
        EXTERNAL_STATE_BLOCKER_REASON,
        f"the PR does not bind to the live repo: {why}; nothing was armed",
    )


def bind_live_pr(
    worktree: Path,
    *,
    pr_number: int,
    pr_url: str,
    branch: str,
    head_sha: str,
    require_head_match: bool,
) -> BoundPr | FinalizeBlock:
    """Bind *pr_url* to the live repo and PR, or fail closed (see module doc)."""
    live_repo = fetch_repo_name_with_owner(timeout=_GH_TIMEOUT, cwd=worktree)
    if live_repo is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            "gh repo view could not read the live repository",
            retry_eligible=True,
        )
    parsed = parse_pr_url(pr_url)
    if parsed != (live_repo, pr_number):
        return _identity_block(
            f"{pr_url!r} is not PR #{pr_number} of the live repo {live_repo}"
        )
    canonical = github_pr_url(live_repo, pr_number)
    view = fetch_pr_view(
        canonical, timeout=_GH_TIMEOUT, fields=_BIND_FIELDS, cwd=worktree
    )
    if view is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr view {canonical} could not read the live PR",
            retry_eligible=True,
        )
    if view.get("url") != canonical or view.get("headRefName") != branch:
        return _identity_block(
            f"the live PR reports url {view.get('url')!r} and head "
            f"{view.get('headRefName')!r}, expected {canonical!r} on {branch!r}"
        )
    if require_head_match and view.get("headRefOid") != head_sha:
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            f"PR #{pr_number}'s head {view.get('headRefOid')!r} is not the local "
            f"HEAD {head_sha}; the branch moved, so nothing was armed",
            retry_eligible=True,
        )
    return BoundPr(pr_number, canonical)


def probe_default(
    worktree: Path, *, default_branch: str, pr_exists: bool
) -> tuple[FinalizeBlock | None, tuple[str, ...]]:
    """Would merging ``origin/<default>`` conflict? ``(block, friction)``.

    Runs before the ahead-push, so a conflicting branch is never pushed. An
    unavailable fetch or probe is friction and the stage continues.
    """
    probe = probe_default_branch(worktree, default_branch=default_branch, repair=True)
    if not probe.needs_merge:
        return None, probe.friction
    files = redact_and_cap(", ".join(probe.conflicted_files))
    where = "the open PR" if pr_exists else "this branch (no PR exists yet)"
    return FinalizeBlock(
        MERGE_CONFLICT_POST_PUSH,
        f"merging origin/{default_branch} into {where} conflicts in {files}; "
        "the codex FINALIZE lane does not resolve conflicts (#3542). Merge or "
        "rebase the branch, push, then requeue finalize.",
        retry_eligible=pr_exists,
    ), probe.friction


def push_ahead(worktree: Path, *, branch: str, head_sha: str) -> FinalizeBlock | None:
    """Push HEAD to ``origin/<branch>`` and verify it, re-asserting the branch."""
    wrong = check_branch(worktree, branch)
    if wrong is not None:
        return wrong
    try:
        push_and_verify_head(worktree, head_sha)
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = str(getattr(exc, "stderr", "") or exc)
        tail = redact_and_cap(stderr.strip()[-_TAIL:])
        reason = classify_unavailability(stderr)
        if reason is not None:
            return FinalizeBlock(
                reason,
                f"git push origin {branch} failed with an unavailability "
                f"signature: {tail}",
                retry_eligible=True,
            )
        return FinalizeBlock(
            IMPL_NOT_PUSHED, f"git push origin {branch} failed: {tail}"
        )
    return None


def create_pr_for(
    worktree: Path, *, base: str, branch: str, title: str, body_file: Path
) -> tuple[int, str] | FinalizeBlock:
    """``gh pr create`` (never draft): ``(number, printed url)`` or a block."""
    done = create_pr(
        base=base, head=branch, title=title, body_file=body_file, cwd=worktree
    )
    if done is None or done.returncode != 0:
        stderr = "" if done is None else done.stderr
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            "gh pr create failed: " + redact_and_cap(stderr.strip()[-_TAIL:]),
            retry_eligible=True,
        )
    url = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    parsed = parse_pr_url(url)
    if parsed is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr create printed no PR URL: {redact_and_cap(done.stdout[-_TAIL:])}",
            retry_eligible=True,
        )
    return parsed[1], url


def check_mergeability(
    worktree: Path, pr_url: str
) -> tuple[FinalizeBlock | None, list[str]]:
    """Post-push mergeability: CONFLICTING/DIRTY block; BEHIND is friction."""
    view = fetch_pr_view(
        pr_url, timeout=_GH_TIMEOUT, fields=_MERGE_FIELDS, cwd=worktree
    )
    if view is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr view {pr_url} could not read mergeability",
            retry_eligible=True,
        ), []
    mergeable = str(view.get("mergeable") or "")
    state = str(view.get("mergeStateStatus") or "")
    if mergeable in _BLOCKING_MERGE or state in _BLOCKING_MERGE:
        return FinalizeBlock(
            MERGE_CONFLICT_POST_PUSH,
            f"{pr_url} is not mergeable after the push (mergeable={mergeable}, "
            f"mergeStateStatus={state}); cw does not resolve conflicts (#3542)",
            retry_eligible=True,
        ), []
    return None, [BEHIND_FRICTION] if state == "BEHIND" else []


def apply_override_section(body: str, section: str) -> str:
    """Replace (or append) the ``## Operator override`` region of *body*.

    The region runs from its heading to the next ``\\n## ``, a
    ``<!-- cw-review-signoff:`` marker, or the end of the body.
    """
    block = section.strip()
    if not block.startswith(_OVERRIDE_HEADING):
        block = f"{_OVERRIDE_HEADING}\n\n{block}"
    start = body.find(_OVERRIDE_HEADING)
    if start < 0:
        return f"{body.rstrip()}\n\n{block}\n"
    rest = body[start + len(_OVERRIDE_HEADING) :]
    found = _NEXT_REGION.search(rest)
    end = start + len(_OVERRIDE_HEADING) + (found.start() if found else len(rest))
    tail = body[end:]
    joined = body[:start] + block
    return joined + ("\n" + tail if tail and not tail.startswith("\n") else tail)
