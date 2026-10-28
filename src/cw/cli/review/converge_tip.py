"""``cw review converge verify-tip`` and ``finalize`` -- the strict tip check (#2787).

Both commands run one shared tip step, then the engine's strict
:func:`~cw.review_convergence.evaluate_tip` (R22): the tip is accepted only when
the ledger converged, HEAD is the pushed tip, and HEAD is the last clean round's
sha. Any commit after the last clean round, a merge of ``origin/<default>``
included, forces another full review round.

The tip step runs ``git fetch origin <branch>`` BEFORE it reads
``origin/<branch>`` (R28): a stale remote-tracking ref must never pass a head
that is not the PR head, and the finalize session cannot rely on an earlier
fetch. It fails closed. A fetch that fails, git missing, or an origin ref that
does not resolve prints a ``head_not_origin_tip`` decision with a ``detail`` and
exits 1; nothing is written (``finalize`` does NOT demote the ledger, because an
unverifiable tip is not evidence of a stale one and a full round cannot cure a
network fault).

Exit codes: 0 the tip is accepted; 1 ``rereview_required`` /
``confirm_required`` (the decision JSON is on stdout) or a ``CwError`` such as an
unreadable ledger (one-line message); 2 a Click usage error. Neither command
takes ``--default-branch``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

import click

from cw._git import git_output, run_git
from cw.cli._base import handle_errors
from cw.cli.review.converge import (
    converge,
    emit_json,
    ledger_option,
    load_ledger_or_none,
    worktree_option,
)
from cw.exceptions import CwError
from cw.review_convergence import (
    TipVerdict,
    demote_for_rereview,
    evaluate_tip,
    finalize_ready,
    friction_lines,
    save_ledger,
    sentinel_review_fragment,
)
from cw.review_convergence._models import DECISION_CONFIRM_REQUIRED

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger

_STDERR_TAIL = 300
_BRANCH_HELP = "The branch whose origin tip is checked. Default: the checked-out one."
_NO_CLEAN_ROUND = TipVerdict(decision="rereview_required", reason="no_clean_round")
_NOT_ORIGIN_TIP = TipVerdict(decision="rereview_required", reason="head_not_origin_tip")


@dataclass(frozen=True)
class _TipCheck:
    """What the shared tip step found: the ledger, the verdict and a failure detail."""

    ledger: ConvergenceLedger | None
    verdict: TipVerdict
    detail: str | None = None


def _tail(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[-_STDERR_TAIL:]


def _current_branch(worktree: Path) -> str:
    """The checked-out branch of *worktree*, or a ``CwError`` (detached HEAD)."""
    try:
        branch = git_output(["branch", "--show-current"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"cannot read the current branch in {worktree}: {exc}"
        raise CwError(msg) from exc
    if not branch:
        msg = f"{worktree} has a detached HEAD; pass --branch <name> to name the branch"
        raise CwError(msg)
    return branch


def _rev_parse(worktree: Path, ref: str) -> str | None:
    """The sha *ref* resolves to, ``None`` when it does not (or git cannot run)."""
    try:
        result = run_git(["rev-parse", ref], cwd=worktree, capture_output=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _fetch_and_read(worktree: Path, branch: str) -> tuple[str, str] | str:
    """``(head, origin_tip)`` after a by-name fetch, or the failure detail.

    The branch is fetched by name so the remote's default refspec updates
    ``refs/remotes/origin/<branch>``, which the next ``rev-parse`` reads. No
    ``timeout`` is passed: cw imposes no kill timer (ADR-0014), so a hung fetch
    is a queue-peek signal.
    """
    prefix = f"git fetch origin {branch} failed: "
    try:
        fetched = run_git(
            ["fetch", "origin", branch], cwd=worktree, capture_output=True
        )
    except OSError as exc:
        return f"{prefix}{exc}"
    if fetched.returncode != 0:
        return f"{prefix}{_tail(fetched.stderr)}"
    head = _rev_parse(worktree, "HEAD")
    if head is None:
        return f"{prefix}cannot read HEAD in {worktree}"
    origin_tip = _rev_parse(worktree, f"origin/{branch}")
    if origin_tip is None:
        return f"{prefix}origin/{branch} does not resolve after the fetch"
    return head, origin_tip


def _check_tip(worktree: Path, ledger_rel: Path, branch: str | None) -> _TipCheck:
    """The shared tip step of ``verify-tip`` and ``finalize`` (R28).

    Loads the ledger first; one that is not ``finalize_ready`` needs no network
    call. Otherwise fetches, reads the two tips and applies ``evaluate_tip``.
    """
    ledger = load_ledger_or_none(worktree / ledger_rel)
    if ledger is None or not finalize_ready(ledger):
        return _TipCheck(ledger, _NO_CLEAN_ROUND)
    shas = _fetch_and_read(worktree, branch or _current_branch(worktree))
    if isinstance(shas, str):
        return _TipCheck(ledger, _NOT_ORIGIN_TIP, shas)
    head, origin_tip = shas
    verdict = evaluate_tip(ledger, head_sha=head, origin_tip_sha=origin_tip)
    return _TipCheck(ledger, verdict)


def _decision_body(
    decision: str, reason: str | None, detail: str | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"decision": decision, "reason": reason}
    if detail is not None:
        body["detail"] = detail
    return body


def _refuse(body: dict[str, object]) -> NoReturn:
    """Print the decision JSON and exit 1."""
    emit_json(body)
    raise click.exceptions.Exit(1)


@converge.command(name="verify-tip")
@worktree_option
@click.option("--branch", default=None, help=_BRANCH_HELP)
@ledger_option
@handle_errors
def converge_verify_tip(worktree: Path, branch: str | None, ledger_rel: Path) -> None:
    """Check that the pushed tip is exactly the last clean round (#2787).

    Fetches `origin <branch>` first, then accepts the tip only when the ledger
    converged, HEAD is the origin tip, and HEAD is the last clean round's sha.
    In clean mode ANY later commit, a merge of `origin/<default>` included,
    needs another full round. Takes no --default-branch.

    Prints {"decision": "accepted" | "rereview_required", "reason": null |
    no_clean_round | head_not_origin_tip | commits_after_clean_round} (plus a
    `detail` naming a failed fetch). Exits 0 when accepted, 1 otherwise. Never
    writes: an unverifiable tip is not evidence of a stale one.
    """
    check = _check_tip(worktree, ledger_rel, branch)
    body = _decision_body(check.verdict.decision, check.verdict.reason, check.detail)
    if check.verdict.decision != "accepted":
        _refuse(body)
    emit_json(body)


@converge.command(name="finalize")
@worktree_option
@click.option("--branch", default=None, help=_BRANCH_HELP)
@ledger_option
@handle_errors
def converge_finalize(worktree: Path, branch: str | None, ledger_rel: Path) -> None:
    """Run the final tip check, then print the sentinel fragment (#2787).

    Runs the same tip step as `verify-tip`. Exits 1 with `decision:
    "confirm_required"` when the ledger has not converged through its confirm
    rule, and with `decision: "rereview_required"` when the tip check fails.
    Only `commits_after_clean_round` (HEAD moved past the last clean sha, a
    review-state defect) demotes the ledger to in_progress so another full round
    runs. `head_not_origin_tip` (not pushed, pushed behind HEAD, or the origin
    tip could not be verified) never demotes: it is a push-state defect, HEAD is
    still the reviewed tip, and the re-run after the push needs no new round.

    On success prints {"decision": "accepted", "reason": null,
    "final_round_clean_sha": ..., "sentinel_review": <sentinel review
    fragment>, "friction_lines": [...]} and exits 0.
    """
    check = _check_tip(worktree, ledger_rel, branch)
    ledger = check.ledger
    verdict = check.verdict
    if ledger is None:
        _refuse(_decision_body(verdict.decision, verdict.reason))
    if not finalize_ready(ledger):
        _refuse(_decision_body(DECISION_CONFIRM_REQUIRED, None))
    if verdict.decision != "accepted":
        if verdict.reason == "commits_after_clean_round":
            save_ledger(worktree / ledger_rel, demote_for_rereview(ledger))
        _refuse(_decision_body(verdict.decision, verdict.reason, check.detail))
    body = _decision_body(verdict.decision, verdict.reason)
    body["final_round_clean_sha"] = ledger.last_clean_sha
    body["sentinel_review"] = sentinel_review_fragment(ledger)
    body["friction_lines"] = friction_lines(ledger)
    emit_json(body)
