"""The default-branch merge tolerance, shared by every clean-mode tip reader (#2815).

A clean-mode ledger records the sha its last clean round reviewed. HEAD may
move past it only by merges of ``origin/<default>`` that change none of the
files in the branch's own diff (``CHANGELOG.md`` and the client's
``merge_gate_ignore_paths`` exempt). :func:`default_merge_only` is the one git
predicate for that question. ``cw review converge verify-tip`` / ``finalize``,
``converge init`` / ``status`` and the dispatch REVIEW staleness gate all ask
it (#2861), so they cannot disagree about which moves are tolerated.

The tolerance itself is a :class:`MergeTolerance` (the default branch plus the
exempt paths) or ``None`` (strict). The CLI reads it from the worktree's
cw-context (:func:`tolerance_from_context`, given the already-parsed context,
so this package never imports :mod:`cw.cli`); the dispatch gate reads it from
the live client config (:func:`tolerance_for_client`). Both readers return
``None`` for the strict opt-out, ``review.default_merge_tolerance: false``.

Every unreadable context, ledger or git state answers the strict way: a move
that cannot be shown to be a tolerated merge is not one.

A merge the tolerance rejects ONLY because it overlaps the branch's own files
is named by :func:`overlapping_default_merge` (#2918): the sorted overlap, with
every other condition of :func:`default_merge_only` met. ``verify-tip`` and
``finalize`` put that list in their JSON, and ``_delta`` decides whether a
delta confirm round may record it (``MergeTolerance.delta_confirm``, the
client's ``review.default_merge_delta_confirm``, off by default).

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_output, run_git
from cw.models.client import DELTA_CONFIRM_CONTEXT_KEY
from cw.models.orchestrator_config.constants import CLAUDE_NATIVE_BACKEND
from cw.review_convergence._store import LEDGER_RELATIVE_PATH, load_ledger
from cw.review_convergence._tip import finalize_ready

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.review_convergence._models import ConvergenceLedger

#: Paths a tolerated merge may touch even when the branch changed them too
#: (#2815): the release flow edits the changelog on nearly every main merge.
#: Exact, root-anchored, repo-relative paths, unioned with the client's
#: ``merge_gate_ignore_paths``.
_DEFAULT_EXEMPT_PATHS = frozenset({"CHANGELOG.md"})


@dataclass(frozen=True)
class MergeTolerance:
    """The default branch a tolerated merge comes from, and the exempt paths.

    ``delta_confirm`` (#2918) is whether an overlap-only merge may be recorded
    by a delta confirm round; it never widens what :func:`default_merge_only`
    tolerates.
    """

    default_branch: str
    exempt_paths: frozenset[str]
    delta_confirm: bool = False


def tolerance_from_context(
    context: dict[str, object] | None, review_obj: dict[str, object] | None
) -> MergeTolerance | None:
    """The tolerance a cw-context names, or ``None`` (strict).

    *context* is the parsed ``.claude/cw-context.json`` and *review_obj* its
    ``review`` object (``None`` when unreadable or below schema 15). ``None``
    for either of them, a ``review.default_merge_tolerance`` that is present
    and not exactly ``True`` (``False`` is the opt-out; any other value is
    malformed and never widens the tolerance), a missing or empty
    ``world_state_snapshot.origin_main_branch``, or a non-string
    ``merge_gate_ignore_paths`` entry. An absent tolerance key is the
    documented default, tolerant; a non-list ignore list exempts nothing more.
    ``delta_confirm`` is true only for a literal ``True``
    ``review.default_merge_delta_confirm`` (#2918); absent or malformed is off.
    """
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
    return MergeTolerance(
        branch,
        _DEFAULT_EXEMPT_PATHS | {str(path) for path in listed},
        delta_confirm=review_obj.get(DELTA_CONFIRM_CONTEXT_KEY) is True,
    )


def tolerance_for_client(client: ClientConfig) -> MergeTolerance | None:
    """The tolerance the live client config names, or ``None`` (strict).

    The same three fields the cw-context producer writes (``default_branch``,
    ``merge_gate_ignore_paths``, ``review.default_merge_tolerance``), read from
    the cw-owned config rather than the worker-writable context file. ``None``
    unless the client is in clean mode with the tolerance on. ``delta_confirm``
    is ``review.default_merge_delta_confirm`` (#2918).
    """
    review = client.review
    if not review.is_clean or review.default_merge_tolerance is not True:
        return None
    return MergeTolerance(
        client.default_branch,
        _DEFAULT_EXEMPT_PATHS | set(client.merge_gate_ignore_paths),
        delta_confirm=review.default_merge_delta_confirm is True,
    )


def git_stdout_or_none(worktree: Path, argv: list[str]) -> str | None:
    """Stdout of ``git *argv``; ``None`` when git cannot run or exits non-zero."""
    try:
        result = run_git(argv, cwd=worktree, capture_output=True)
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def rev_parse_or_none(worktree: Path, ref: str) -> str | None:
    """The sha *ref* resolves to, ``None`` when it does not (or git cannot run)."""
    out = git_stdout_or_none(worktree, ["rev-parse", ref])
    if out is None:
        return None
    return out.strip() or None


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
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None
    if not out:
        return set()
    entries = out.split("\0")
    if entries[-1] or "" in entries[:-1]:
        return None
    return set(entries[:-1])


_MergeParents = list[tuple[str, list[str]]]
#: ``rev-list --first-parent --parents --merges`` prints
#: ``<merge> <parent1> <parent2> ...``.
_MIN_MERGE_FIELDS = 3


def _merge_parents(worktree: Path, span: str) -> _MergeParents | None:
    """``(merge, parents after the first)`` per branch merge in *span*, or ``None``.

    The one place the "extra parents" of a merge are defined: parent 1 is the
    branch side, every later parent is the side merged in. Only the branch's
    own first-parent chain is listed, so the default branch's own merge
    commits (a ``--no-ff`` PR merge synced in through a sync merge's second
    parent) are not the branch's merges; they stay covered by the own-commit
    and ancestry checks of :func:`_only_default_merges`. A line that is not a
    merge with at least one extra parent is unreadable, so the caller stays
    strict. No merge in *span* is the empty list.
    """
    argv = ["rev-list", "--first-parent", "--parents", "--merges", span]
    out = git_stdout_or_none(worktree, argv)
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
    own = git_stdout_or_none(
        worktree, ["rev-list", "--no-merges", span, f"^{default_sha}"]
    )
    if own is None or own.strip():
        return False
    return all(
        git_stdout_or_none(
            worktree, ["merge-base", "--is-ancestor", parent, default_sha]
        )
        is not None
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


class _PathSets(NamedTuple):
    """``B``, ``C`` and ``M`` of :func:`_untouched`, as read from git."""

    branch: set[str]
    landed: set[str]
    moved: set[str]


def _path_sets(
    worktree: Path, clean: str, head: str, default_sha: str
) -> _PathSets | None:
    """The branch's own diff, what landed after *clean*, what main moved.

    ``B`` is the merge base (of *clean* and the default branch) to *clean*,
    ``C`` is *clean* to *head*, ``M`` is the merge base to the default
    branch's tip. ``None`` when any of them is unreadable.
    """
    base = git_stdout_or_none(worktree, ["merge-base", clean, default_sha])
    if base is None:
        return None
    merge_base = base.strip()
    branch = _paths(worktree, merge_base, clean)
    landed = _paths(worktree, clean, head)
    moved = _paths(worktree, merge_base, default_sha)
    if branch is None or landed is None or moved is None:
        return None
    return _PathSets(branch, landed, moved)


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
    :func:`_merge_overlap` answers the same question but names the overlap.
    """
    sets = _path_sets(worktree, clean, head, default_sha)
    if sets is None:
        return False
    if (sets.branch & sets.landed) - exempt or not sets.landed <= sets.moved:
        return False
    return _merges_add_only_branch_paths(worktree, merges, sets.branch | exempt)


def _merge_overlap(
    worktree: Path,
    clean: str,
    head: str,
    default_sha: str,
    exempt: frozenset[str],
    merges: _MergeParents,
) -> frozenset[str] | None:
    """``(B & C) - exempt`` when every other check of :func:`_untouched` passes.

    The empty set is exactly :func:`_untouched`'s ``True``. ``None`` when git
    is unreadable, ``C`` is not within ``M``, or a merge carries a hand edit
    outside ``B`` and the exempt paths (#2918).
    """
    sets = _path_sets(worktree, clean, head, default_sha)
    if sets is None or not sets.landed <= sets.moved:
        return None
    if not _merges_add_only_branch_paths(worktree, merges, sets.branch | exempt):
        return None
    return frozenset((sets.branch & sets.landed) - exempt)


class MergeSpan(NamedTuple):
    """The default branch's tip sha, the ``clean..head`` span and its merges."""

    default_sha: str
    span: str
    merges: _MergeParents


def merge_span(
    worktree: Path, clean_sha: str, head_sha: str, tolerance: MergeTolerance | None
) -> MergeSpan | None:
    """The span after *clean_sha* when it holds at least one branch merge.

    The shared first half of :func:`default_merge_only` and
    :func:`overlapping_default_merge`: ``None`` for a ``None`` *tolerance*
    (no git call), an unresolvable ``origin/<default>`` tracking ref, a
    *clean_sha* that is not an ancestor of *head_sha*, an unreadable merge
    listing, or a span with no merge (a fast-forward is not a merge).
    """
    if tolerance is None:
        return None
    ref = f"refs/remotes/origin/{tolerance.default_branch}^{{commit}}"
    default_sha = rev_parse_or_none(worktree, ref)
    if default_sha is None:
        return None
    ancestry = ["merge-base", "--is-ancestor", clean_sha, head_sha]
    if git_stdout_or_none(worktree, ancestry) is None:
        return None
    span = f"{clean_sha}..{head_sha}"
    merges = _merge_parents(worktree, span)
    if not merges:
        return None
    return MergeSpan(default_sha, span, merges)


def default_merge_only(
    worktree: Path,
    clean_sha: str,
    head_sha: str,
    tolerance: MergeTolerance | None,
) -> bool:
    """True when everything after *clean_sha* is a tolerated merge (#2815).

    ``None`` *tolerance* is the strict rule and returns ``False`` without a git
    call. The default branch's tracking ref is not fetched here: a stale ref
    can only reject more merges. Every unreadable git state is ``False``
    (strict).
    """
    found = merge_span(worktree, clean_sha, head_sha, tolerance)
    if found is None or tolerance is None:
        return False
    default_sha, span, merges = found
    return _only_default_merges(worktree, span, default_sha, merges) and _untouched(
        worktree, clean_sha, head_sha, default_sha, tolerance.exempt_paths, merges
    )


def overlapping_default_merge(
    worktree: Path,
    clean_sha: str,
    head_sha: str,
    tolerance: MergeTolerance | None,
) -> tuple[str, ...] | None:
    """The branch files a default-branch merge after *clean_sha* overlaps (#2918).

    The sorted ``(B & C) - exempt`` of :func:`_untouched` when every other
    condition of :func:`default_merge_only` holds: only merges of the default
    branch after *clean_sha*, nothing landed that main did not move, and no
    hand edit outside the branch's own files. Non-empty therefore means
    "rejected solely because it overlaps"; ``()`` is a tolerated merge. ``None``
    for a ``None`` *tolerance* (no git call), any other rejection, and every
    unreadable git state. It reads no ledger, so it names the overlap whatever
    the loop or confirm backend.
    """
    found = merge_span(worktree, clean_sha, head_sha, tolerance)
    if found is None or tolerance is None:
        return None
    default_sha, span, merges = found
    if not _only_default_merges(worktree, span, default_sha, merges):
        return None
    overlap = _merge_overlap(
        worktree, clean_sha, head_sha, default_sha, tolerance.exempt_paths, merges
    )
    return None if overlap is None else tuple(sorted(overlap))


def tolerable_clean_sha(ledger: ConvergenceLedger) -> str | None:
    """The clean sha a merge past it could keep converged, else ``None``.

    The ledger preconditions every merge-past-the-clean-sha reader shares
    (:func:`converged_across_tolerated_merge` and the delta round's ledger
    check in ``_delta``, whose eligibility must equal this allow-list, #2918):
    a :func:`~cw.review_convergence._tip.finalize_ready` ledger with a clean
    sha, and a ``claude-native`` loop backend (an allow-list: codex, opencode
    and any unknown backend stay strict). Callers add their own HEAD check.
    """
    clean = ledger.last_clean_sha
    if not finalize_ready(ledger) or clean is None:
        return None
    if ledger.loop_backend != CLAUDE_NATIVE_BACKEND:
        return None
    return clean


def converged_across_tolerated_merge(
    ledger: ConvergenceLedger,
    *,
    worktree: Path,
    head_sha: str,
    tolerance: MergeTolerance | None,
) -> bool:
    """True when *ledger* stays converged although HEAD moved past its clean sha.

    The one keep-converged seam ``converge init``, ``converge status`` and the
    REVIEW staleness gate share (#2861). Requires the ledger preconditions of
    :func:`tolerable_clean_sha` (converged with a clean sha, a ``claude-native``
    loop backend), a HEAD that is not that clean sha, and
    :func:`default_merge_only` over the move. Git is consulted only when every
    ledger condition already holds.
    """
    clean = tolerable_clean_sha(ledger)
    if clean is None or head_sha == clean:
        return False
    return default_merge_only(worktree, clean, head_sha, tolerance)


def tolerates_review_move(
    client: ClientConfig | None,
    worktree: Path,
    *,
    reviewed_sha: str,
    head_sha: str,
) -> bool:
    """True when the move from *reviewed_sha* to *head_sha* is a tolerated merge.

    The REVIEW staleness gate's entry point (#2861). Derived by cw at gate
    time from the worktree ledger (``.cw/review-convergence.json``), the
    worktree's git state and the live client config, never from the sentinel.
    ``False`` (strict) for no client, a client not in clean mode, an unreadable
    or missing ledger, a ledger whose ``last_clean_sha`` is not *reviewed_sha*,
    or anything :func:`converged_across_tolerated_merge` refuses.
    """
    if client is None or not client.review.is_clean:
        return False
    try:
        ledger = load_ledger(worktree / LEDGER_RELATIVE_PATH)
    except (OSError, ValueError):
        return False
    if ledger is None or ledger.last_clean_sha != reviewed_sha:
        return False
    return converged_across_tolerated_merge(
        ledger,
        worktree=worktree,
        head_sha=head_sha,
        tolerance=tolerance_for_client(client),
    )
