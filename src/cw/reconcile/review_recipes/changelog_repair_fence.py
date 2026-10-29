"""Review-approval and tip fence for a CHANGELOG-repaired head (#2771, R2).

A history rewrite changes every commit sha on the branch, so the
``repair_changelog_conflict`` recipe must not arm auto-merge on its rewritten
head unless that head passes the same rule finalize applies before every arm:
Step 4.0 (large-tier review approval) and, in clean mode, Step 4.0b
(``verify-tip``). Neither can run as is from the daemon: Step 4.0 is a bash
block over the ticket worktree's ``cw-context.json``, and ``verify-tip`` reads
that worktree's own (old) HEAD and ledger, so both would fail by construction
rather than by evaluation.

This module re-evaluates the same decisions in Python against the new head:

- :func:`_check_approval` ports Step 4.0 over the approval fields stored on
  the row (the fields finalize reads through cw-context) and reproduces its
  decision tokens (``not_required``, ``recorded``, ``missing``, ``stale``,
  ``unreadable``, ``unresolved_tier``).
- :func:`_check_tip` calls :func:`cw.review_convergence.evaluate_tip`, the one
  tip rule ``verify-tip`` itself applies, with the default
  ``default_merge_only=False``. The #2815 tolerance is deliberately not
  extended: a rewritten head is not a merge of ``origin/<default>``.

Lockless; subprocesses only through :func:`cw._git.run_git`, each with a
timeout. :func:`evaluate_repair_fence` never raises: a git failure or timeout
is ``unreadable``, and an unreadable ledger is ``no_clean_round``.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING, NamedTuple, get_args

from cw._git import run_git
from cw.auto_dev_result import SCOPE_TIER_LARGE, SCOPE_TIER_SMALL
from cw.models.tasks import ReviewApproverKind
from cw.reconcile.review_recipes.changelog_repair_git import _T_LOCAL
from cw.review_convergence import LEDGER_RELATIVE_PATH, evaluate_tip, load_ledger

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, TipDecision, TipReason

_APPROVER_KINDS = frozenset(get_args(ReviewApproverKind))
_REVIEWED_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# ``git merge-base --is-ancestor`` exits 1 for "not an ancestor"; any other
# non-zero exit is an error.
_NOT_ANCESTOR_RC = 1
# ``git rev-list --parents -n 1 <merge>`` prints ``<merge> <first> <other>...``.
_FIRST_OTHER_PARENT_FIELD = 2

APPROVAL_NOT_REQUIRED = "not_required"
APPROVAL_RECORDED = "recorded"
APPROVAL_MISSING = "missing"
APPROVAL_STALE = "stale"
APPROVAL_UNREADABLE = "unreadable"
APPROVAL_UNRESOLVED_TIER = "unresolved_tier"
_APPROVAL_PASSES = frozenset({APPROVAL_NOT_REQUIRED, APPROVAL_RECORDED})

# Pinned to cw.review_convergence's Literals: a rename there fails mypy here.
_TIP_ACCEPTED: TipDecision = "accepted"
_TIP_NO_CLEAN_ROUND: TipReason = "no_clean_round"
_TIP_HEAD_NOT_ORIGIN: TipReason = "head_not_origin_tip"


class FenceSnapshot(NamedTuple):
    """The fence inputs, copied off the row under the lock (no I/O)."""

    ticket_id: str
    tier: str | None
    review_approver_kind: str | None
    review_approved_sha: str | None
    clean_mode: bool
    ledger_path: Path | None
    default_branch: str


class FenceVerdict(NamedTuple):
    """The fence's answer for one rewritten head."""

    passed: bool
    approval_token: str
    tip_reason: TipReason | None
    detail: str


def _resolve_row_tier(task: TicketTask) -> str | None:
    """The row's scope tier, mirroring ``dispatch.routing._resolve_scope_tier``.

    ``large`` if either the operator ``scope_hint`` or the stamped
    ``computed_scope_tier`` is ``large``; else the stamped tier; else the hint.
    Only an exact ``small`` or ``large`` is accepted: anything else is None,
    which the fence parks as ``unresolved_tier``.
    """
    stamped = task.computed_scope_tier
    if SCOPE_TIER_LARGE in (task.scope_hint, stamped):
        return SCOPE_TIER_LARGE
    tier = stamped if stamped is not None else task.scope_hint
    return tier if tier in {SCOPE_TIER_SMALL, SCOPE_TIER_LARGE} else None


def build_fence_snapshot(task: TicketTask, client_cfg: ClientConfig) -> FenceSnapshot:
    """Copy the fence inputs off *task* and its client config. Pure."""
    kind = task.review_approver_kind
    return FenceSnapshot(
        ticket_id=task.ticket_id,
        tier=_resolve_row_tier(task),
        review_approver_kind=None if kind is None else str(kind),
        review_approved_sha=task.review_approved_sha,
        clean_mode=client_cfg.review.is_clean,
        ledger_path=(
            None
            if task.worktree_path is None
            else task.worktree_path / LEDGER_RELATIVE_PATH
        ),
        default_branch=client_cfg.default_branch,
    )


def _fence_git(repo: Path, *argv: str) -> subprocess.CompletedProcess[str] | None:
    """Run one read-only git call; None when it could not run or timed out."""
    try:
        return run_git(
            argv, cwd=repo, check=False, capture_output=True, timeout=_T_LOCAL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _rev_parse_commit(repo: Path, rev: str) -> str | None:
    result = _fence_git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ancestry(repo: Path, ancestor: str, descendant: str) -> str | None:
    """None if *ancestor* is an ancestor; else ``stale`` or ``unreadable``."""
    result = _fence_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result is None:
        return APPROVAL_UNREADABLE
    if result.returncode == 0:
        return None
    if result.returncode == _NOT_ANCESTOR_RC:
        return APPROVAL_STALE
    return APPROVAL_UNREADABLE


def _approval_preconditions(snapshot: FenceSnapshot) -> tuple[str, str] | None:
    """The tier/kind/sha checks that need no git; None to continue."""
    if snapshot.tier == SCOPE_TIER_SMALL:
        return APPROVAL_NOT_REQUIRED, "small tier: no review approval needed"
    if snapshot.tier != SCOPE_TIER_LARGE:
        return APPROVAL_UNRESOLVED_TIER, f"scope tier {snapshot.tier!r} is unresolved"
    if snapshot.review_approver_kind not in _APPROVER_KINDS:
        return APPROVAL_MISSING, "no review approval is recorded on the row"
    sha = snapshot.review_approved_sha or ""
    if not _REVIEWED_SHA_RE.match(sha):
        return APPROVAL_STALE, "the recorded approval carries no usable reviewed head"
    return None


def _merge_parents_on_default(
    repo: Path, full_sha: str, new_sha: str, main_sha: str, default: str
) -> tuple[str, str] | None:
    """Every non-first parent of every merge after the approval is on main."""
    merges = _fence_git(repo, "rev-list", "--merges", f"{full_sha}..{new_sha}")
    if merges is None or merges.returncode != 0:
        return APPROVAL_UNREADABLE, "git rev-list --merges failed"
    for merge_sha in merges.stdout.split():
        parents = _fence_git(repo, "rev-list", "--parents", "-n", "1", merge_sha)
        if parents is None or parents.returncode != 0:
            return APPROVAL_UNREADABLE, f"cannot read the parents of merge {merge_sha}"
        for parent in parents.stdout.split()[_FIRST_OTHER_PARENT_FIELD:]:
            token = _ancestry(repo, parent, main_sha)
            if token is not None:
                return token, (
                    f"merge {merge_sha} brought in {parent}, which is not on"
                    f" origin/{default}"
                )
    return None


def _new_non_merge_work(
    repo: Path, full_sha: str, new_sha: str, main_sha: str
) -> tuple[str, str] | None:
    """Non-merge commits after the approval that are not on main: a park."""
    new_work = _fence_git(
        repo, "rev-list", "--no-merges", f"{full_sha}..{new_sha}", f"^{main_sha}"
    )
    if new_work is None or new_work.returncode != 0:
        return APPROVAL_UNREADABLE, f"git rev-list failed for {full_sha}..{new_sha}"
    if new_work.stdout.strip():
        landed = " ".join(new_work.stdout.split())
        return (
            APPROVAL_STALE,
            f"non-merge commit(s) landed after the approval: {landed}",
        )
    return None


def _approval_ancestry(
    snapshot: FenceSnapshot, repo: Path, sha: str, new_sha: str
) -> tuple[str, str]:
    """The git half of Step 4.0, once the recorded sha is well-formed."""
    default = snapshot.default_branch
    main_sha = _rev_parse_commit(repo, f"refs/remotes/origin/{default}")
    if main_sha is None:
        return APPROVAL_UNREADABLE, f"origin/{default} is not resolvable"
    full_sha = _rev_parse_commit(repo, sha)
    if full_sha is None or not full_sha.startswith(sha):
        return APPROVAL_STALE, f"reviewed head {sha} is not a commit in this repository"
    token = _ancestry(repo, full_sha, new_sha)
    if token is not None:
        return token, f"reviewed head {full_sha} is not an ancestor of head {new_sha}"
    problem = _new_non_merge_work(repo, full_sha, new_sha, main_sha)
    if problem is None:
        problem = _merge_parents_on_default(repo, full_sha, new_sha, main_sha, default)
    if problem is not None:
        return problem
    return APPROVAL_RECORDED, f"review approval of {full_sha} still covers {new_sha}"


def _check_approval(
    snapshot: FenceSnapshot, *, repo: Path, new_sha: str
) -> tuple[str, str]:
    """Step 4.0 for *new_sha*: ``(token, detail)``. Never raises."""
    early = _approval_preconditions(snapshot)
    if early is not None:
        return early
    return _approval_ancestry(
        snapshot, repo, snapshot.review_approved_sha or "", new_sha
    )


def _load_own_ledger(
    snapshot: FenceSnapshot,
) -> tuple[ConvergenceLedger | None, str]:
    """The row's own ledger, or None with why it is not usable."""
    path = snapshot.ledger_path
    if path is None:
        return None, "the row has no worktree, so no ledger"
    try:
        ledger = load_ledger(path)
    except (OSError, ValueError) as exc:
        return None, f"ledger {path} unreadable ({type(exc).__name__})"
    if ledger is None:
        return None, f"no convergence ledger at {path}"
    if ledger.ticket_id != snapshot.ticket_id:
        return None, f"ledger {path} belongs to {ledger.ticket_id!r}"
    return ledger, ""


def _check_tip(
    snapshot: FenceSnapshot, *, new_sha: str, origin_tip: str | None
) -> tuple[TipReason | None, str]:
    """Step 4.0b for *new_sha*: ``(reason or None when accepted, detail)``."""
    ledger, why = _load_own_ledger(snapshot)
    if ledger is None:
        return _TIP_NO_CLEAN_ROUND, why
    if origin_tip is None:
        return _TIP_HEAD_NOT_ORIGIN, "origin's branch tip could not be read"
    verdict = evaluate_tip(ledger, head_sha=new_sha, origin_tip_sha=origin_tip)
    if verdict.decision == _TIP_ACCEPTED:
        return None, f"ledger converged at {new_sha}"
    return verdict.reason, f"verify-tip: {verdict.reason}"


def evaluate_repair_fence(
    snapshot: FenceSnapshot, *, repo: Path, new_sha: str, origin_tip: str | None
) -> FenceVerdict:
    """Apply finalize's Step 4.0 (+ Step 4.0b in clean mode) to *new_sha*.

    *repo* is the client git dir (it shares objects with the scratch tree the
    head was built in); *origin_tip* is ``ls-remote``'s answer for the
    branch, None when it could not be read. Passes only when the approval
    token is ``not_required`` or ``recorded`` and, in clean mode, the ledger
    accepts the tip. Never raises.
    """
    token, detail = _check_approval(snapshot, repo=repo, new_sha=new_sha)
    tip_reason: TipReason | None = None
    if snapshot.clean_mode:
        tip_reason, tip_detail = _check_tip(
            snapshot, new_sha=new_sha, origin_tip=origin_tip
        )
        detail = f"{detail}; {tip_detail}"
    passed = token in _APPROVAL_PASSES and tip_reason is None
    return FenceVerdict(
        passed=passed, approval_token=token, tip_reason=tip_reason, detail=detail
    )
