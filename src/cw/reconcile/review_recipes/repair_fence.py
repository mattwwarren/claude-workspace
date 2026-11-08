"""Review-approval and tip fence for a repaired head (#2771 R2, #2917).

The fence for a head a repair recipe pushed that the review ledger never saw.
For the ``repair_changelog_conflict`` recipe that head is rewritten history,
every commit sha new. For the ``repair_mechanical_conflict`` recipe it is a
merge of ``origin/<default>`` on top of the reviewed tip. Either way the
recipe must not arm auto-merge on it unless it passes the same rule finalize
applies before every arm: Step 4.0 (large-tier review approval) and, in
clean mode, Step 4.0b (``verify-tip``). Neither can run as is from the
daemon: Step 4.0 is a bash block over the ticket worktree's
``cw-context.json``, and ``verify-tip`` reads that worktree's own (old) HEAD
and ledger, so both would fail by construction rather than by evaluation.

This module re-evaluates the same decisions in Python against the new head:

- :func:`cw.review_approval.evaluate_review_approval` applies Step 4.0 over the
  approval fields stored on the row (the fields finalize reads through
  cw-context): the one predicate ``cw review check-approval`` and the dispatch
  claim gate also call (#2773). It yields the decision tokens
  (``not_required``, ``recorded``, ``missing``, ``stale``, ``unreadable``,
  ``unresolved_tier``). A rewritten head fails it by construction; a repair
  merge keeps the reviewed commits as ancestors, and its second parent is on
  ``origin/<default>``, so a recorded approval still evaluates as
  ``recorded``.
- :func:`_check_tip` calls :func:`cw.review_convergence.evaluate_tip`, the one
  tip rule ``verify-tip`` itself applies, with the default
  ``default_merge_only=False``. The #2979 clean-default-merge rule (a clean
  merge of the default branch never stales the tip) is deliberately not
  extended here. A clean-mode head is accepted only when the ledger already
  accepts it, or when the ``repair_changelog_conflict`` recipe has proven
  every branch commit unchanged and passes its clean-sha remap (#2921), which
  this module applies in memory and never writes.

Lockless; git runs only inside
:func:`cw.review_approval.evaluate_review_approval` (through
:func:`cw._git.run_git`, each call with a timeout).
:func:`evaluate_repair_fence` never raises: a git failure or timeout is
``unreadable``, and an unreadable ledger is ``no_clean_round``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.review_approval import (
    ApprovalInputs,
    evaluate_review_approval,
    resolve_row_scope_tier,
)
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    TIP_CHECK_FETCH_FAILED,
    evaluate_tip,
    load_ledger,
    remap_clean_sha,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import (
        CleanShaRemap,
        ConvergenceLedger,
        TipDecision,
        TipReason,
    )

# Pinned to cw.review_convergence's Literals: a rename there fails mypy here.
_TIP_ACCEPTED: TipDecision = "accepted"
_TIP_NO_CLEAN_ROUND: TipReason = "no_clean_round"


class FenceSnapshot(NamedTuple):
    """The fence inputs, copied off the row under the lock (no I/O)."""

    ticket_id: str
    tier: str | None
    review_approver_kind: str | None
    review_approved_sha: str | None
    clean_mode: bool
    ledger_path: Path | None
    default_branch: str
    default_merge_tolerance: bool = True


class FenceVerdict(NamedTuple):
    """The fence's answer for one repaired head.

    ``remapped`` is set only when a clean-sha remap made the tip accept and the
    whole fence passed: the post-remap ledger the engine persists, with
    ``pre_image`` the exact ledger the fence loaded (the compare-and-set base).
    The evaluation itself writes nothing.
    """

    passed: bool
    approval_token: str
    tip_reason: TipReason | None
    detail: str
    remapped: ConvergenceLedger | None = None
    pre_image: ConvergenceLedger | None = None


class _TipCheck(NamedTuple):
    """Step 4.0b's answer: reason (None when accepted), detail, and any remap."""

    reason: TipReason | None
    detail: str
    remapped: ConvergenceLedger | None = None
    pre_image: ConvergenceLedger | None = None


def build_fence_snapshot(task: TicketTask, client_cfg: ClientConfig) -> FenceSnapshot:
    """Copy the fence inputs off *task* and its client config. Pure."""
    kind = task.review_approver_kind
    return FenceSnapshot(
        ticket_id=task.ticket_id,
        tier=resolve_row_scope_tier(task),
        review_approver_kind=None if kind is None else str(kind),
        review_approved_sha=task.review_approved_sha,
        clean_mode=client_cfg.review.is_clean,
        ledger_path=(
            None
            if task.worktree_path is None
            else task.worktree_path / LEDGER_RELATIVE_PATH
        ),
        default_branch=client_cfg.default_branch,
        default_merge_tolerance=client_cfg.review.default_merge_tolerance,
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


def _remap_for_tip(
    ledger: ConvergenceLedger, remap: CleanShaRemap | None
) -> tuple[ConvergenceLedger | None, str]:
    """The remapped ledger when *remap* applies, else None, with a detail note."""
    if remap is None:
        return None, ""
    result = remap_clean_sha(ledger, remap)
    if isinstance(result, str):
        return None, f"; ledger remap refused: {result}"
    return result, f"; ledger remapped {remap.from_sha} -> {remap.to_sha}"


def _check_tip(
    snapshot: FenceSnapshot,
    *,
    new_sha: str,
    origin_tip: str | None,
    remap: CleanShaRemap | None = None,
) -> _TipCheck:
    """Step 4.0b for *new_sha*, with the clean-sha remap applied in memory if given."""
    ledger, why = _load_own_ledger(snapshot)
    if ledger is None:
        return _TipCheck(_TIP_NO_CLEAN_ROUND, why)
    if origin_tip is None:
        # Why: one reason for every unreadable origin tip. ``_ls_remote_tip``
        # returns None for an ls-remote failure, a timeout and a successful
        # ls-remote that printed no ref (branch absent on origin). The fence
        # fails closed either way (``passed`` requires ``tip_reason is None``)
        # and its only consumer of the reason is the verdict detail, whereas
        # ``converge_tip`` can tell a refused fetch from a missing remote ref.
        return _TipCheck(
            TIP_CHECK_FETCH_FAILED,
            "origin's branch tip could not be read "
            "(ls-remote failed, timed out or returned no ref)",
        )
    remapped, note = _remap_for_tip(ledger, remap)
    effective = ledger if remapped is None else remapped
    verdict = evaluate_tip(effective, head_sha=new_sha, origin_tip_sha=origin_tip)
    if verdict.decision == _TIP_ACCEPTED:
        if remapped is None:
            return _TipCheck(None, f"ledger converged at {new_sha}")
        return _TipCheck(
            None,
            f"ledger converged at {new_sha}{note}",
            remapped=remapped,
            pre_image=ledger,
        )
    return _TipCheck(verdict.reason, f"verify-tip: {verdict.reason}{note}")


def evaluate_repair_fence(
    snapshot: FenceSnapshot,
    *,
    repo: Path,
    new_sha: str,
    origin_tip: str | None,
    remap: CleanShaRemap | None = None,
) -> FenceVerdict:
    """Apply finalize's Step 4.0 (+ Step 4.0b in clean mode) to *new_sha*.

    *repo* is the client git dir (it shares objects with the scratch tree the
    head was built in); *origin_tip* is ``ls-remote``'s answer for the
    branch, None when it could not be read. Passes only when the approval
    token is ``not_required`` or ``recorded`` and, in clean mode, the ledger
    accepts the tip. *remap* (#2921) is applied to the ledger in clean mode
    only; a remap that makes the tip accept is returned on the verdict, never
    written here. Never raises.
    """
    approval = evaluate_review_approval(
        ApprovalInputs(
            snapshot.tier,
            snapshot.review_approver_kind,
            snapshot.review_approved_sha,
            snapshot.default_branch,
        ),
        repo=repo,
        head_sha=new_sha,
    )
    token, detail = approval.token, approval.detail
    tip_reason: TipReason | None = None
    remapped: ConvergenceLedger | None = None
    pre_image: ConvergenceLedger | None = None
    if snapshot.clean_mode:
        tip = _check_tip(snapshot, new_sha=new_sha, origin_tip=origin_tip, remap=remap)
        tip_reason, detail = tip.reason, f"{detail}; {tip.detail}"
        remapped, pre_image = tip.remapped, tip.pre_image
    passed = approval.passed and tip_reason is None
    if not passed:
        remapped = pre_image = None
    return FenceVerdict(
        passed=passed,
        approval_token=token,
        tip_reason=tip_reason,
        detail=detail,
        remapped=remapped,
        pre_image=pre_image,
    )
