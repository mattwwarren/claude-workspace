"""Fetch and freshness checks against origin for :mod:`cw.worktree`.

Answers "is this ref up to date with origin" for both a feature branch
(:func:`fetch_feature_branch`) and the client's default branch
(:func:`is_main_behind_origin`, :func:`check_main_ff_safety`,
:func:`fast_forward_main`), including the per-failure fetch-warning dedup.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never

from cw.exceptions import MissingWorkspaceError, WorktreeError
from cw.worktree._git import (
    _GIT_PORCELAIN_UNTRACKED,
    _first_line,
    _git_dir,
    _run_git,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_log = logging.getLogger(__name__)


# Why: git's stderr when ``fetch origin <branch>`` names a branch the remote
# does not have. A never-pushed feature branch is an expected state (a PLAN
# stage provisions the worktree before IMPL ever pushes), so
# ``fetch_feature_branch`` downgrades exactly this failure to DEBUG (#2213).
# git localizes its messages: under a non-English locale the marker misses and
# the failure degrades to the ordinary WARNING -- noise only, never a bug.
_MISSING_REMOTE_REF_MARKER = "couldn't find remote ref"


class FetchOutcome(enum.Enum):
    """What one ``git fetch origin <branch>`` established about the remote (#2213).

    Three states, because the two non-success outcomes need opposite handling:

    - ``FETCHED``: the tracking ref is fresh, so it is safe to fast-forward from.
    - ``BRANCH_ABSENT``: origin has no such branch. An expected, benign state for
      a fresh ticket (a PLAN stage provisions the worktree before IMPL ever
      pushes): there is nothing to refresh and nothing worth reporting.
    - ``FAILED``: the fetch itself failed (offline, auth, no ``origin``, missing
      workspace). The tracking ref is stale or unknown, so it must NOT be
      fast-forwarded from, and the failure is worth reporting.
    """

    FETCHED = "fetched"
    BRANCH_ABSENT = "branch_absent"
    FAILED = "failed"


@dataclass(frozen=True)
class FetchResult:
    """One ``git fetch origin <branch>``: what happened (:class:`FetchOutcome`) and why.

    ``reason`` is ``None`` for ``FETCHED``. For ``BRANCH_ABSENT`` and ``FAILED`` it
    is one line: git's exit status and first stderr line (``rc=128: fatal: ...``),
    or the workspace / OS problem that stopped git from running. That first line
    is what tells an operator an auth failure (``Permission denied``) from a
    network one (``Could not resolve hostname``) from a missing remote (``does
    not appear to be a git repository``), so a "not refreshed" is legible
    (#2213).
    """

    outcome: FetchOutcome
    reason: str | None = None


# One distinct fetch failure: ``(client name, outcome, reason)``. The caller-owned
# dedup set of these (``warned_fetch_fail``) is what lets a repeat of the SAME
# failure stay quiet while a DIFFERENT one for the same client -- an auth error
# after a network error -- still gets reported (#2213).
type FetchWarningKey = tuple[str, FetchOutcome, str]


def _warn_fetch_skip_once(
    warned_fetch_fail: set[FetchWarningKey] | None,
    warn_key: FetchWarningKey,
    message: str,
    *args: object,
) -> None:
    """Log *message* at WARNING once per *warn_key*, then remember the key.

    The single check-and-add every ``_fetch_default_branch`` failure path goes
    through, so none can bypass the dedup: the warn-key set lives for the whole
    dispatch loop, and a permanently missing workspace or git binary would
    otherwise warn on every tick. ``None`` for *warned_fetch_fail* (a one-shot
    caller) always warns and remembers nothing.
    """
    if warned_fetch_fail is not None and warn_key in warned_fetch_fail:
        return
    _log.warning(message, *args)
    if warned_fetch_fail is not None:
        warned_fetch_fail.add(warn_key)


def _fetch_default_branch(
    client_name: str,
    default_branch: str,
    git_dir: Path,
    warned_fetch_fail: set[FetchWarningKey] | None = None,
    *,
    quiet_missing_ref: bool = False,
) -> FetchResult:
    """Fetch origin/<default_branch> and report what happened, and why.

    Returns a :class:`FetchResult`: :attr:`FetchOutcome.FETCHED` on success,
    :attr:`FetchOutcome.BRANCH_ABSENT` when origin has no such branch (git's
    ``couldn't find remote ref``), and :attr:`FetchOutcome.FAILED` for every
    other failure. For the two non-success outcomes ``reason`` is one line: git's
    exit status and first stderr line (``rc=128: fatal: ...``), or the workspace
    or OS problem that kept git from running, so a caller can tell auth from
    network from a missing remote. The outcome does not depend on
    *quiet_missing_ref*; only the log level does: a branch absent from origin is
    logged at DEBUG instead of WARNING when it is set, and then does not touch
    *warned_fetch_fail*. Every other failure still WARNs.

    *warned_fetch_fail* is a caller-owned set of :data:`FetchWarningKey`
    (client, outcome, reason) that dedups the WARNING per distinct failure: a
    repeat of the same failure for the same client stays quiet, but a different
    one (an auth error after a network error) is new information and warns
    again, so silence never reads as "the earlier problem persists". ``None``
    always warns.
    """
    if not git_dir.exists():
        reason = f"workspace missing: {git_dir}"
        _warn_fetch_skip_once(
            warned_fetch_fail,
            (client_name, FetchOutcome.FAILED, reason),
            "freshness_check_skip: workspace missing for %s (%s)",
            client_name,
            git_dir,
        )
        return FetchResult(FetchOutcome.FAILED, reason)
    try:
        result = _run_git(
            "fetch", "origin", default_branch, "--quiet", cwd=git_dir, check=False
        )
    except (WorktreeError, FileNotFoundError, PermissionError) as exc:
        reason = _first_line(str(exc)) or type(exc).__name__
        _warn_fetch_skip_once(
            warned_fetch_fail,
            (client_name, FetchOutcome.FAILED, reason),
            "freshness_check_skip: %s (%s): %s",
            client_name,
            git_dir,
            exc,
        )
        return FetchResult(FetchOutcome.FAILED, reason)
    if result.returncode == 0:
        return FetchResult(FetchOutcome.FETCHED)
    stderr = result.stderr.strip()
    first_line = _first_line(stderr)
    reason = (
        f"rc={result.returncode}: {first_line}"
        if first_line
        else f"rc={result.returncode}"
    )
    branch_absent = _MISSING_REMOTE_REF_MARKER in stderr
    outcome = FetchOutcome.BRANCH_ABSENT if branch_absent else FetchOutcome.FAILED
    if branch_absent and quiet_missing_ref:
        _log.debug(
            "freshness_check_skip: fetch failed for %s (rc=%d): %s",
            client_name,
            result.returncode,
            first_line,
        )
        return FetchResult(outcome, reason)
    _warn_fetch_skip_once(
        warned_fetch_fail,
        (client_name, outcome, reason),
        "freshness_check_skip: fetch failed for %s (rc=%d): %s",
        client_name,
        result.returncode,
        first_line,
    )
    return FetchResult(outcome, reason)


def fetch_feature_branch(client: ClientConfig, branch_name: str) -> FetchResult:
    """Fetch origin/<branch_name> into the client's git directory.

    Resolves the stale-local-ref problem described in GitHub issue #381:
    when the impl agent pushes commits from an isolation worktree, the
    parent worktree's local ref for the feature branch is not updated.
    Calling this before computing ``git diff FORK_POINT...origin/<branch>``
    for reviewer prompts ensures the diff reflects the actual pushed state.

    Returns a :class:`FetchResult` and never raises for a fetch error: outcome
    ``FETCHED`` on success, ``BRANCH_ABSENT`` when origin has no such branch,
    ``FAILED`` for anything else, with git's reason carried on the result. The
    two non-success outcomes are distinct because they need opposite handling
    (#2213): a branch that is not on origin is an expected state for a
    never-pushed feature branch (logged at DEBUG, not WARNING), while a failed
    fetch means the tracking ref is stale.
    """
    return _fetch_default_branch(
        client.name, branch_name, _git_dir(client), quiet_missing_ref=True
    )


def fetch_default_branch(client: ClientConfig) -> FetchResult:
    """Fetch origin/<default_branch> into the client's git directory.

    Sibling of :func:`fetch_feature_branch`, for the reuse refresh's
    never-pushed-branch case (#2328): before deciding whether an unpushed
    feature branch has commits of its own, the default branch must be
    fetched fresh, or a stale local origin/<default_branch> could either
    fast-forward to an already-stale target or misclassify a branch as
    having "commits of its own" that are really just commits origin/<default>
    hasn't caught up to yet.
    """
    return _fetch_default_branch(client.name, client.default_branch, _git_dir(client))


def _get_behind_count(
    client_name: str, default_branch: str, git_dir: Path
) -> tuple[str, str, int] | None:
    """Get (local_sha, origin_sha, behind_count). Returns None on failure."""
    try:
        local_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
        origin_sha = _run_git(
            "rev-parse", f"origin/{default_branch}", cwd=git_dir
        ).stdout.strip()
        behind_count = int(
            _run_git(
                "rev-list",
                "--count",
                f"{default_branch}..origin/{default_branch}",
                cwd=git_dir,
            ).stdout.strip()
        )
    except (WorktreeError, ValueError):
        _log.warning(
            "is_main_behind_origin: rev-parse/rev-list failed for %s", client_name
        )
        return None
    else:
        return (local_sha, origin_sha, behind_count)


def is_main_behind_origin(
    client: ClientConfig,
    warned_fetch_fail: set[FetchWarningKey] | None = None,
) -> tuple[bool, str, str, int]:
    """Check whether the client's local default branch is behind origin.

    Fetches ``origin/<default_branch>`` then compares local and remote SHAs.

    Args:
        client: Client configuration.
        warned_fetch_fail: Caller-owned set of :data:`FetchWarningKey`
            ``(client, outcome, reason)`` entries that have already received a
            fetch-failure WARNING in this run. Suppresses a repeat of the SAME
            failure for the same client across ticks; a different failure for
            that client still warns. Pass ``None`` (default) to always log
            (correct for one-shot callers).

    Returns:
        A 4-tuple ``(is_stale, local_sha, origin_sha, behind_count)`` where
        *is_stale* is ``True`` when the local branch is behind the remote.
        On any fetch or parse failure returns ``(False, "", "", 0)`` and logs
        a WARNING — the caller should treat failure as non-stale.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    fetch = _fetch_default_branch(
        client.name, default_branch, git_dir, warned_fetch_fail=warned_fetch_fail
    )
    outcome = fetch.outcome
    match outcome:
        case FetchOutcome.FETCHED:
            pass
        case FetchOutcome.BRANCH_ABSENT | FetchOutcome.FAILED:
            # A default branch absent from origin is not benign here (unlike a
            # feature branch): the freshness check cannot tell, so not stale.
            return (False, "", "", 0)
        case _:
            assert_never(outcome)

    counts = _get_behind_count(client.name, default_branch, git_dir)
    if counts is None:
        return (False, "", "", 0)

    local_sha, origin_sha, behind_count = counts
    return (behind_count > 0, local_sha, origin_sha, behind_count)


def _ff_relation(
    local_ref: str, remote_ref: str, cwd: Path
) -> Literal["equal", "behind", "ahead", "diverged"]:
    """Classify *local_ref*'s directional relationship to *remote_ref*.

    Two ``merge-base --is-ancestor`` probes: "behind" means *local_ref* is a
    strict ancestor of *remote_ref* (fast-forward is safe), "ahead" the
    reverse. A probe error (e.g. an unresolvable ref, rc 128) reads as
    "not an ancestor", so any failure classifies as "diverged" and can never
    trigger a mutation.
    """
    # Two merge-base --is-ancestor calls for directional classification.
    local_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        local_ref,
        remote_ref,
        cwd=cwd,
        check=False,
    )
    remote_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        remote_ref,
        local_ref,
        cwd=cwd,
        check=False,
    )
    # returncode 0 means the first arg is a reachable ancestor of the second.
    is_local_ancestor = local_behind.returncode == 0  # local ≤ remote → behind
    is_remote_ancestor = remote_behind.returncode == 0  # remote ≤ local → ahead

    if is_local_ancestor and is_remote_ancestor:
        return "equal"
    if is_local_ancestor:
        return "behind"
    if is_remote_ancestor:
        return "ahead"
    return "diverged"


def check_main_ff_safety(
    client: ClientConfig,
) -> Literal["equal", "behind", "ahead", "diverged", "detached"]:
    """Classify local main's relationship to origin for dispatch auto-ff.

    Returns one of:
      "behind"   — local main is strictly behind origin; fast-forward is safe
      "equal"    — local main matches origin; no action needed
      "ahead"    — local main has unpushed commits; operator action required
      "diverged" — local main has both new commits and is behind; needs reconciliation
      "detached" — HEAD is detached; fast-forward would be unsafe

    Operative outcomes from the dispatch path (when stale=True is already
    established): "behind" triggers auto-ff; "diverged" and "detached" fall
    through to TICKET_NEEDS_SYNC + warn. "equal" and "ahead" exist for
    defensive completeness but are not reachable from the stale=True path.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    # Detached HEAD check — symbolic-ref exits non-zero when detached.
    # Prior art: _checked_out_branch() at line 148; fast_forward_main() below.
    sym = _run_git("symbolic-ref", "--short", "HEAD", cwd=git_dir, check=False)
    if sym.returncode != 0:
        return "detached"

    return _ff_relation(default_branch, f"origin/{default_branch}", git_dir)


def get_head_branch(client: ClientConfig) -> str | None:
    """Return the symbolic branch name of HEAD, or None if detached or on error.

    Callers in dispatch.py import this as ``cw.dispatch.get_head_branch`` so
    tests can patch it without reaching into worktree internals.
    """
    git_dir = _git_dir(client)
    result = _run_git("symbolic-ref", "--short", "HEAD", cwd=git_dir, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_main_checkout_dirty(client: ClientConfig) -> bool:
    """Return True if the main checkout has uncommitted tracked changes.

    Uses the same porcelain filter as fast_forward_main — untracked files
    (``??`` prefix) are ignored because git pull --ff-only is safe with them.
    Returns False on any git error so transient failures never block dispatch.
    """
    git_dir = _git_dir(client)
    try:
        status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout
    except WorktreeError:
        return False
    status_lines = [
        line for line in status_out.splitlines() if line[:2] != _GIT_PORCELAIN_UNTRACKED
    ]
    return bool(status_lines)


def fast_forward_main(
    client: ClientConfig, *, ignore_untracked: bool = False
) -> tuple[str, str]:
    """Fast-forward the client's local default branch to origin.

    Runs ``git pull --ff-only origin <default_branch>`` in the client's git
    directory.  Raises :exc:`MissingWorkspaceError` if the workspace directory
    does not exist, or :exc:`WorktreeError` if the pull fails (non-zero exit)
    or if the checkout is not on ``default_branch`` or has uncommitted changes
    — both conditions risk mutating the index unexpectedly (#428).

    Returns:
        ``(before_sha, after_sha)`` — the SHA before and after the pull.
        When already up to date both values are equal.
    """
    git_dir = _git_dir(client)
    if not git_dir.exists():
        msg = f"workspace missing for {client.name} ({git_dir})"
        raise MissingWorkspaceError(msg)
    default_branch = client.default_branch

    # Guard 1: ensure the checkout is on the expected default branch.
    current_branch = _run_git(
        "symbolic-ref", "--short", "HEAD", cwd=git_dir
    ).stdout.strip()
    if current_branch != default_branch:
        msg = (
            f"Refusing to fast-forward {client.name}: HEAD is on "
            f"'{current_branch}', expected '{default_branch}'. "
            f"Switch to '{default_branch}' before refreshing."
        )
        raise WorktreeError(msg)

    # Guard 2: ensure the working tree is clean (or only has untracked files).
    status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout
    status_lines = status_out.splitlines()
    if ignore_untracked:
        # Why: dispatch auto-ff may run against a workspace with untracked runtime
        # artifacts (.claude/scheduled_tasks.lock etc.); git pull --ff-only is
        # safe with untracked files because ff-only never rewrites the working tree.
        status_lines = [
            line for line in status_lines if line[:2] != _GIT_PORCELAIN_UNTRACKED
        ]
    if status_lines:
        msg = (
            f"Refusing to fast-forward {client.name}: working tree is dirty "
            f"(git status --porcelain reported changes). "
            f"Commit or stash changes before refreshing."
        )
        raise WorktreeError(msg)

    before_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    _run_git("pull", "--ff-only", "origin", default_branch, cwd=git_dir)
    after_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    return (before_sha, after_sha)
