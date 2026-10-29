"""``cw review converge verify-tip`` and ``finalize`` -- the strict tip check (#2787).

Both commands run one shared tip step, then the engine's strict
:func:`~cw.review_convergence.evaluate_tip` (R22): the tip is accepted only when
the ledger converged, HEAD is the pushed tip, and HEAD is the last clean round's
sha. Any non-merge commit after the last clean round forces another full round,
and so does a merge of ``origin/<default>`` that touches a file of the branch's
own diff. ``CHANGELOG.md`` and the client's ``merge_gate_ignore_paths`` are
exempt, and a client sets ``review.default_merge_tolerance`` false to make every
merge force a round.

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
takes ``--default-branch``: the default branch comes from the worktree's
cw-context.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

import click

from cw._git import git_output, run_git
from cw.cli._base import handle_errors
from cw.cli.review.converge import (
    context_review,
    converge,
    emit_json,
    ledger_option,
    load_ledger_or_none,
    read_context,
    worktree_option,
)
from cw.exceptions import CwError
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
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
#: Paths a tolerated merge may touch even when the branch changed them too
#: (#2815): the release flow edits the changelog on nearly every main merge.
#: Exact, root-anchored, repo-relative paths, unioned with the client's
#: ``merge_gate_ignore_paths``.
_DEFAULT_EXEMPT_PATHS = frozenset({"CHANGELOG.md"})


@dataclass(frozen=True)
class _TipCheck:
    """What the shared tip step found: the ledger, the verdict and a failure detail.

    ``default_merge`` is true only when the verdict was accepted because
    everything after the last clean round is a tolerated merge (#2815).
    """

    ledger: ConvergenceLedger | None
    verdict: TipVerdict
    detail: str | None = None
    default_merge: bool = False


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


def _run(worktree: Path, argv: list[str]) -> str | None:
    """Stdout of ``git *argv``; ``None`` when git cannot run or exits non-zero."""
    try:
        result = run_git(argv, cwd=worktree, capture_output=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _rev_parse(worktree: Path, ref: str) -> str | None:
    """The sha *ref* resolves to, ``None`` when it does not (or git cannot run)."""
    out = _run(worktree, ["rev-parse", ref])
    if out is None:
        return None
    return out.strip() or None


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


def _tolerance_context(worktree: Path) -> tuple[str, frozenset[str]] | None:
    """``(default_branch, exempt_paths)`` when the tolerance applies, else ``None``.

    ``None`` (strict) for an unreadable context or ``review`` object (schema
    below 15 included), a ``review.default_merge_tolerance`` that is present
    and not exactly ``True`` (``False`` is the opt-out; any other value is
    malformed and never widens the tolerance), a missing or empty
    ``world_state_snapshot.origin_main_branch``, or a non-string
    ``merge_gate_ignore_paths`` entry. An absent tolerance key is the
    documented default, tolerant; a non-list ignore list exempts nothing more.
    """
    context = read_context(worktree / HOOK_CONTEXT_RELATIVE_PATH)
    review_obj = context_review(context)
    if context is None or review_obj is None:
        return None
    if review_obj.get("default_merge_tolerance", True) is not True:
        return None
    snapshot = context.get("world_state_snapshot")
    branch = snapshot.get("origin_main_branch") if isinstance(snapshot, dict) else None
    if not isinstance(branch, str) or not branch:
        return None
    ignore = context.get("merge_gate_ignore_paths")
    listed: list[object] = ignore if isinstance(ignore, list) else []
    if not all(isinstance(path, str) for path in listed):
        return None
    return branch, _DEFAULT_EXEMPT_PATHS | {str(path) for path in listed}


def _paths(worktree: Path, a: str, b: str) -> set[str] | None:
    """The paths ``git diff`` lists between *a* and *b*; ``None`` if unreadable.

    ``-z`` keeps a non-ASCII path unquoted and ``--no-renames`` lists both
    sides of a rename. Empty output is the empty set (identical trees).
    Non-empty output must end with exactly one NUL and hold no empty entry;
    any other shape is unreadable, so the caller stays strict.
    """
    argv = ["diff", "--name-only", "--no-renames", "-z", a, b]
    try:
        out = git_output(argv, cwd=worktree)
    except (subprocess.CalledProcessError, OSError, UnicodeError):
        return None
    if not out:
        return set()
    entries = out.split("\0")
    if entries[-1] or "" in entries[:-1]:
        return None
    return set(entries[:-1])


_MergeParents = list[tuple[str, list[str]]]
#: ``rev-list --parents --merges`` prints ``<merge> <parent1> <parent2> ...``.
_MIN_MERGE_FIELDS = 3


def _merge_parents(worktree: Path, span: str) -> _MergeParents | None:
    """``(merge, parents after the first)`` per merge in *span*; ``None`` if unreadable.

    The one place the "extra parents" of a merge are defined: parent 1 is the
    branch side, every later parent is the side merged in. A line that is not
    a merge with at least one extra parent is unreadable, so the caller stays
    strict. No merge in *span* is the empty list.
    """
    out = _run(worktree, ["rev-list", "--parents", "--merges", span])
    if out is None:
        return None
    merges: _MergeParents = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < _MIN_MERGE_FIELDS:
            return None
        merges.append((fields[0], fields[2:]))
    return merges


def _only_default_merges(
    worktree: Path,
    span: str,
    default_sha: str,
    merges: _MergeParents,
) -> bool:
    """True when everything in *span* is a merge of the default branch.

    A non-merge commit reachable from the span's tip but not from the default
    branch is a change after the clean round. Every parent after the first of
    every one of *merges* (the span's merges, never empty: a fast-forward is
    not a merge) must be an ancestor of the default branch's tip.
    """
    own = _run(worktree, ["rev-list", "--no-merges", span, f"^{default_sha}"])
    if own is None or own.strip():
        return False
    return all(
        _run(worktree, ["merge-base", "--is-ancestor", parent, default_sha]) is not None
        for _, others in merges
        for parent in others
    )


def _merges_add_only_branch_paths(
    worktree: Path, merges: _MergeParents, allowed: set[str]
) -> bool:
    """True when each merge differs from its default-branch parents only in *allowed*.

    For every one of *merges* and each of its parents after the first, the
    paths that differ between that parent and the merge commit must lie
    within *allowed* (the branch's own diff plus the exempt paths). A merge
    that carries a hand edit to a file the branch never touched, even one the
    default branch changed, differs from the default-branch side there and is
    refused. An octopus merge is checked against every extra parent, so it is
    only accepted if each one passes.
    """
    for merge, others in merges:
        for parent in others:
            differs = _paths(worktree, parent, merge)
            if differs is None or not differs <= allowed:
                return False
    return True


def _untouched(
    worktree: Path,
    clean: str,
    head: str,
    default_sha: str,
    exempt: frozenset[str],
    merges: _MergeParents,
) -> bool:
    """True when the merges changed no branch file and carried no hand edit.

    ``B`` is the branch's own diff (merge base to *clean*), ``C`` what landed
    after the clean round, ``M`` what the default branch changed since the
    merge base. ``(B & C) - exempt`` must be empty, ``C`` must lie within
    ``M``, and each merge must differ from its default-branch parents only in
    ``B`` and the exempt paths (see :func:`_merges_add_only_branch_paths`), so
    a hand edit to a path main changed but the branch never touched is
    refused. Exempt paths leave the touching set only, so an exempt path that
    main changed is accepted unverified, a hand-resolved conflict included.
    """
    base = _run(worktree, ["merge-base", clean, default_sha])
    if base is None:
        return False
    merge_base = base.strip()
    branch = _paths(worktree, merge_base, clean)
    landed = _paths(worktree, clean, head)
    moved = _paths(worktree, merge_base, default_sha)
    if branch is None or landed is None or moved is None:
        return False
    if (branch & landed) - exempt or not landed <= moved:
        return False
    return _merges_add_only_branch_paths(worktree, merges, branch | exempt)


def _default_merge_only(worktree: Path, clean_sha: str, head_sha: str) -> bool:
    """True when everything after *clean_sha* is a tolerated merge (#2815).

    The default branch comes from the worktree's cw-context, never a caller
    flag. Its tracking ref is not fetched here: a stale ref can only reject
    more merges. Every unreadable context or git state is ``False`` (strict).
    """
    tolerance = _tolerance_context(worktree)
    if tolerance is None:
        return False
    default_branch, exempt = tolerance
    ref = f"refs/remotes/origin/{default_branch}^{{commit}}"
    resolved = _run(worktree, ["rev-parse", "--verify", "--quiet", ref])
    if resolved is None or not resolved.strip():
        return False
    default_sha = resolved.strip()
    ancestry = ["merge-base", "--is-ancestor", clean_sha, head_sha]
    if _run(worktree, ancestry) is None:
        return False
    span = f"{clean_sha}..{head_sha}"
    merges = _merge_parents(worktree, span)
    if not merges:
        return False
    return _only_default_merges(worktree, span, default_sha, merges) and _untouched(
        worktree, clean_sha, head_sha, default_sha, exempt, merges
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
    if not _default_merge_only(worktree, clean, head):
        return _TipCheck(ledger, verdict)
    tolerated = evaluate_tip(
        ledger, head_sha=head, origin_tip_sha=origin_tip, default_merge_only=True
    )
    return _TipCheck(ledger, tolerated, default_merge=tolerated.decision == "accepted")


def _check_tip(worktree: Path, ledger_rel: Path, branch: str | None) -> _TipCheck:
    """The shared tip step of ``verify-tip`` and ``finalize`` (R28).

    Loads the ledger first; one that is not ``finalize_ready`` needs no network
    call. Otherwise fetches, reads the two tips and applies ``evaluate_tip``,
    re-applying it under the #2815 tolerance when only HEAD moved past the last
    clean round.
    """
    ledger = load_ledger_or_none(worktree / ledger_rel)
    if ledger is None or not finalize_ready(ledger):
        return _TipCheck(ledger, _NO_CLEAN_ROUND)
    shas = _fetch_and_read(worktree, branch or _current_branch(worktree))
    if isinstance(shas, str):
        return _TipCheck(ledger, _NOT_ORIGIN_TIP, shas)
    head, origin_tip = shas
    verdict = evaluate_tip(ledger, head_sha=head, origin_tip_sha=origin_tip)
    return _tolerate(worktree, ledger, shas, verdict)


def _decision_body(
    decision: str,
    reason: str | None,
    detail: str | None = None,
    *,
    default_merge: bool = False,
) -> dict[str, object]:
    body: dict[str, object] = {"decision": decision, "reason": reason}
    if detail is not None:
        body["detail"] = detail
    if default_merge:
        body["default_merge_tolerated"] = True
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
    no_clean_round | head_not_origin_tip | commits_after_clean_round} (plus a
    `detail` naming a failed fetch). Exits 0 when accepted, 1 otherwise. Never
    writes: an unverifiable tip is not evidence of a stale one.

    Adds `"default_merge_tolerated": true` when HEAD is past the last clean
    round by such tolerated merges only.
    """
    check = _check_tip(worktree, ledger_rel, branch)
    body = _decision_body(
        check.verdict.decision,
        check.verdict.reason,
        check.detail,
        default_merge=check.default_merge,
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
    runs. `head_not_origin_tip` (not pushed, pushed behind HEAD, or the origin
    tip could not be verified) never demotes: it is a push-state defect, HEAD is
    still the reviewed tip, and the re-run after the push needs no new round.

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
        _refuse(_decision_body(verdict.decision, verdict.reason, check.detail))
    body = _decision_body(
        verdict.decision, verdict.reason, default_merge=check.default_merge
    )
    body["final_round_clean_sha"] = ledger.last_clean_sha
    body["sentinel_review"] = sentinel_review_fragment(ledger)
    body["friction_lines"] = friction_lines(ledger)
    emit_json(body)
