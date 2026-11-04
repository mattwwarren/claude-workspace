"""``cw review converge verify-tip`` and ``finalize`` -- the strict tip check (#2787).

Both commands run one shared tip step, then the engine's strict
:func:`~cw.review_convergence.evaluate_tip` (R22): the tip is accepted only when
the ledger converged, HEAD is the pushed tip, and HEAD is the last clean round's
sha. Any non-merge commit after the last clean round forces another full round,
and so does a merge of ``origin/<default>`` that touches a file of the branch's
own diff. ``CHANGELOG.md`` and the client's ``merge_gate_ignore_paths`` are
exempt, and a client sets ``review.default_merge_tolerance`` false to make every
merge force a round. That tolerated-merge predicate lives in
:mod:`cw.review_convergence` (``_merge_tolerance``), shared with ``converge
init``, ``converge status`` and the dispatch REVIEW staleness gate (#2861), so
all four agree on which moves are tolerated; the commands here are the only
ones that fetch first.

The tip step runs ``git fetch origin <branch>`` BEFORE it reads
``origin/<branch>`` (R28): a stale remote-tracking ref must never pass a head
that is not the PR head, and the finalize session cannot rely on an earlier
fetch. It fails closed, printing a ``rereview_required`` decision with a
``detail`` and exiting 1. A fetch that cannot run (git missing) or fails for any
reason but a missing remote branch (refused credentials, an unreachable remote)
is ``tip_check_fetch_failed`` (#2910): the tip is unverified, not stale. A
branch origin does not have, an unreadable HEAD and an ``origin/<branch>`` that
does not resolve after the fetch are ``head_not_origin_tip``. Nothing is
written: ``finalize`` does NOT demote the ledger for either, because an
unverifiable tip is not evidence of a stale one and a full round cannot cure a
network or push fault.

A merge of ``origin/<default>`` rejected ONLY because it overlaps the branch's
own files (#2918) is named: the JSON adds ``overlapping_files`` (sorted) and a
``detail`` that says verify-tip decided (``verify-tip: merge of origin/<default>
overlaps branch files: ...``). This needs the tolerance on, whatever the ledger.
``verify-tip`` also adds ``delta_confirm_eligible: true`` when a delta confirm
round (``cw review converge delta-confirm``) may record the move. The Step 4.0
approval fence of ``auto-dev-finalize.md`` answers a different question (was
the approved sha followed only by default-branch merges?) and never checks file
overlap, so it can pass a tip this check parks. A fetch failure returns before
any of this, so it can never lead to a delta round.

Exit codes: 0 the tip is accepted; 1 ``rereview_required`` /
``confirm_required`` (the decision JSON is on stdout) or a ``CwError`` such as an
unreadable ledger (one-line message); 2 a Click usage error. Neither command
takes ``--default-branch``: the default branch comes from the worktree's
cw-context.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, NoReturn

import click

from cw._git import MISSING_REMOTE_REF_MARKER, git_output, run_git
from cw.cli._base import handle_errors
from cw.cli.review.converge import (
    converge,
    emit_json,
    ledger_option,
    load_ledger_or_none,
    merge_tolerance,
    worktree_option,
)
from cw.exceptions import CwError
from cw.review_convergence import (
    DEFAULT_MERGE_TOLERATED_KEY,
    DELTA_CONFIRM_ELIGIBLE_KEY,
    OVERLAPPING_FILES_KEY,
    TIP_CHECK_FETCH_FAILED,
    TipVerdict,
    assess_delta_round,
    default_merge_only,
    demote_for_rereview,
    evaluate_tip,
    finalize_ready,
    friction_lines,
    overlapping_default_merge,
    rev_parse_or_none,
    save_ledger,
    sentinel_review_fragment,
)
from cw.review_convergence._models import DECISION_CONFIRM_REQUIRED

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger, MergeTolerance, TipReason

_STDERR_TAIL = 300
_BRANCH_HELP = "The branch whose origin tip is checked. Default: the checked-out one."
_NO_CLEAN_ROUND = TipVerdict(decision="rereview_required", reason="no_clean_round")
_HEAD_NOT_ORIGIN_TIP: TipReason = "head_not_origin_tip"
#: How many overlapping files the park ``detail`` names before ``and N more``.
_DETAIL_FILE_LIMIT = 10
#: The suffix verify-tip's ``detail`` carries when a delta round is eligible.
DELTA_CONFIRM_HINT = "; run: cw review converge delta-confirm"


@dataclass(frozen=True)
class _TipCheck:
    """What the shared tip step found: the ledger, the verdict and a failure detail.

    ``default_merge`` is true only when the verdict was accepted because
    everything after the last clean round is a tolerated merge (#2815).
    ``overlapping_files`` names the branch files a merge rejected only for
    overlap touched (#2918), and ``delta_confirm_eligible`` whether a delta
    confirm round may record it.
    """

    ledger: ConvergenceLedger | None
    verdict: TipVerdict
    detail: str | None = None
    default_merge: bool = False
    overlapping_files: tuple[str, ...] = ()
    delta_confirm_eligible: bool = False


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


class _TipReadFailure(NamedTuple):
    """Why the tip step could not read the two shas, and the JSON ``detail``."""

    reason: TipReason
    detail: str


def _fetch_failure(stderr: str) -> TipReason:
    """Classify a non-zero ``git fetch origin <branch>`` (#2910).

    A branch origin does not have (never pushed, or deleted) is a push-state
    defect: ``head_not_origin_tip``. Anything else (refused credentials, an
    unreachable or missing remote) left the tip unverified, not stale:
    ``tip_check_fetch_failed``.
    """
    if MISSING_REMOTE_REF_MARKER in stderr:
        return _HEAD_NOT_ORIGIN_TIP
    return TIP_CHECK_FETCH_FAILED


def _fetch_and_read(worktree: Path, branch: str) -> tuple[str, str] | _TipReadFailure:
    """``(head, origin_tip)`` after a by-name fetch, or why they could not be read.

    The branch is fetched by name so the remote's default refspec updates
    ``refs/remotes/origin/<branch>``, which the next ``rev-parse`` reads. No
    ``timeout`` is passed: this runs in a worker session as a CLI subprocess,
    not in the dispatch loop, so the tick's fetch gate (#2910) does not apply
    and cw imposes no kill timer here (ADR-0014); a hung fetch is a queue-peek
    signal.

    A fetch that cannot run, or that fails for any reason but a missing remote
    branch, is ``tip_check_fetch_failed``; a missing remote branch, an
    unreadable HEAD and an ``origin/<branch>`` that does not resolve after a
    successful fetch stay ``head_not_origin_tip``.
    """
    prefix = f"git fetch origin {branch} failed: "
    try:
        fetched = run_git(
            ["fetch", "origin", branch], cwd=worktree, capture_output=True
        )
    except OSError as exc:
        return _TipReadFailure(TIP_CHECK_FETCH_FAILED, f"{prefix}{exc}")
    if fetched.returncode != 0:
        return _TipReadFailure(
            _fetch_failure(fetched.stderr), f"{prefix}{_tail(fetched.stderr)}"
        )
    head = rev_parse_or_none(worktree, "HEAD")
    if head is None:
        return _TipReadFailure(
            _HEAD_NOT_ORIGIN_TIP, f"{prefix}cannot read HEAD in {worktree}"
        )
    origin_tip = rev_parse_or_none(worktree, f"origin/{branch}")
    if origin_tip is None:
        return _TipReadFailure(
            _HEAD_NOT_ORIGIN_TIP,
            f"{prefix}origin/{branch} does not resolve after the fetch",
        )
    return head, origin_tip


def overlap_detail(default_branch: str, files: tuple[str, ...]) -> str:
    """The park ``detail`` naming an overlap-only merge's files (#2918)."""
    named = ", ".join(files[:_DETAIL_FILE_LIMIT])
    more = len(files) - _DETAIL_FILE_LIMIT
    tail = f", and {more} more" if more > 0 else ""
    return (
        f"verify-tip: merge of origin/{default_branch} overlaps branch files: "
        f"{named}{tail}"
    )


def _overlap_check(
    worktree: Path,
    ledger: ConvergenceLedger,
    verdict: TipVerdict,
    *,
    clean: str,
    head: str,
    tolerance: MergeTolerance | None,
) -> _TipCheck:
    """The rejected verdict, naming an overlap-only merge's files (#2918).

    The overlap comes from ``overlapping_default_merge`` directly, which reads
    no ledger, so it is named for any ledger; ``assess_delta_round`` (given
    that overlap) decides only ``delta_confirm_eligible``.
    """
    overlap = overlapping_default_merge(worktree, clean, head, tolerance)
    if not overlap or tolerance is None:
        return _TipCheck(ledger, verdict)
    assessment = assess_delta_round(
        ledger, worktree=worktree, head_sha=head, tolerance=tolerance, overlap=overlap
    )
    return _TipCheck(
        ledger,
        verdict,
        overlap_detail(tolerance.default_branch, overlap),
        overlapping_files=overlap,
        delta_confirm_eligible=assessment.plan is not None,
    )


def _tolerate(
    worktree: Path,
    ledger: ConvergenceLedger,
    shas: tuple[str, str],
    verdict: TipVerdict,
) -> _TipCheck:
    """Re-evaluate a ``commits_after_clean_round`` verdict under the tolerance."""
    head, origin_tip = shas
    clean = ledger.last_clean_sha
    if verdict.reason != "commits_after_clean_round" or clean is None:
        return _TipCheck(ledger, verdict)
    tolerance = merge_tolerance(worktree)
    if not default_merge_only(worktree, clean, head, tolerance):
        return _overlap_check(
            worktree, ledger, verdict, clean=clean, head=head, tolerance=tolerance
        )
    tolerated = evaluate_tip(
        ledger, head_sha=head, origin_tip_sha=origin_tip, default_merge_only=True
    )
    return _TipCheck(ledger, tolerated, default_merge=tolerated.decision == "accepted")


def _check_tip(worktree: Path, ledger_rel: Path, branch: str | None) -> _TipCheck:
    """The shared tip step of ``verify-tip`` and ``finalize`` (R28).

    Loads the ledger first; one that is not ``finalize_ready`` needs no network
    call. Otherwise fetches, reads the two tips and applies ``evaluate_tip``,
    re-applying it under the #2815 tolerance (read from the worktree's
    cw-context) when only HEAD moved past the last clean round.
    """
    ledger = load_ledger_or_none(worktree / ledger_rel)
    if ledger is None or not finalize_ready(ledger):
        return _TipCheck(ledger, _NO_CLEAN_ROUND)
    shas = _fetch_and_read(worktree, branch or _current_branch(worktree))
    if isinstance(shas, _TipReadFailure):
        verdict = TipVerdict(decision="rereview_required", reason=shas.reason)
        return _TipCheck(ledger, verdict, shas.detail)
    head, origin_tip = shas
    verdict = evaluate_tip(ledger, head_sha=head, origin_tip_sha=origin_tip)
    return _tolerate(worktree, ledger, shas, verdict)


def _decision_body(
    decision: str,
    reason: str | None,
    detail: str | None = None,
    *,
    default_merge: bool = False,
    overlapping_files: tuple[str, ...] = (),
    delta_confirm_eligible: bool = False,
) -> dict[str, object]:
    body: dict[str, object] = {"decision": decision, "reason": reason}
    if detail is not None:
        body["detail"] = detail
    if default_merge:
        body[DEFAULT_MERGE_TOLERATED_KEY] = True
    if overlapping_files:
        body[OVERLAPPING_FILES_KEY] = list(overlapping_files)
    if delta_confirm_eligible:
        body[DELTA_CONFIRM_ELIGIBLE_KEY] = True
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
    In clean mode any later non-merge commit needs another full round, and so
    does a merge of `origin/<default>` that touches a file of the branch's own
    diff (`CHANGELOG.md` and `merge_gate_ignore_paths` exempt; off entirely when
    the context sets `review.default_merge_tolerance` false). Takes no
    --default-branch.

    Prints {"decision": "accepted" | "rereview_required", "reason": null |
    no_clean_round | head_not_origin_tip | commits_after_clean_round |
    tip_check_fetch_failed} (plus a `detail` naming a failed fetch).
    `tip_check_fetch_failed` means origin could not be fetched (refused,
    unreachable, git missing); a branch origin does not have is
    `head_not_origin_tip`. Exits 0 when accepted, 1 otherwise. Never writes:
    an unverifiable tip is not evidence of a stale one.

    Adds `"default_merge_tolerated": true` when HEAD is past the last clean
    round by such tolerated merges only. `converge init` and `status` apply
    the same rule, so they keep such a ledger converged.

    A merge rejected only because it overlaps the branch's own files adds
    `overlapping_files` and a `detail` naming them and verify-tip as the
    decider; `delta_confirm_eligible: true` (and a `detail` ending `; run: cw
    review converge delta-confirm`) when a delta confirm round may record it.
    """
    check = _check_tip(worktree, ledger_rel, branch)
    eligible = check.delta_confirm_eligible
    detail = check.detail
    if eligible and detail is not None:
        detail += DELTA_CONFIRM_HINT
    body = _decision_body(
        check.verdict.decision,
        check.verdict.reason,
        detail,
        default_merge=check.default_merge,
        overlapping_files=check.overlapping_files,
        delta_confirm_eligible=eligible,
    )
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
    runs. `head_not_origin_tip` (not pushed, or pushed behind HEAD) never
    demotes: it is a push-state defect, HEAD is still the reviewed tip, and the
    re-run after the push needs no new round.
    `tip_check_fetch_failed` (origin could not be fetched) never demotes
    either: once the fetch works, a re-run of finalize re-checks the tip.
    An overlap-only merge adds `overlapping_files` and the naming `detail`
    (never `delta_confirm_eligible`: finalize demotes, so no delta round can
    follow it).

    On success prints {"decision": "accepted", "reason": null,
    "final_round_clean_sha": ..., "sentinel_review": <sentinel review
    fragment>, "friction_lines": [...]} and exits 0. The same
    `default_merge_tolerated: true` key is added only when HEAD is past the
    last clean round by tolerated merges only; `final_round_clean_sha` is still
    the reviewed commit.
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
        _refuse(
            _decision_body(
                verdict.decision,
                verdict.reason,
                check.detail,
                overlapping_files=check.overlapping_files,
            )
        )
    body = _decision_body(
        verdict.decision, verdict.reason, default_merge=check.default_merge
    )
    body["final_round_clean_sha"] = ledger.last_clean_sha
    body["sentinel_review"] = sentinel_review_fragment(ledger)
    body["friction_lines"] = friction_lines(ledger)
    emit_json(body)
