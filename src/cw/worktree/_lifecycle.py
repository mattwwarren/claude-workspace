"""Worktree creation and removal: the :mod:`cw.worktree` entry points.

:func:`create_worktree` and :func:`remove_worktree` orchestrate the other
submodules (paths, freshness, unsaved-work detection, reuse refresh).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from cw.exceptions import (
    BranchHeldByWorktreeError,
    StaleWorktreeError,
    WorktreeError,
)
from cw.native_daemon import get_native_daemon_client
from cw.worktree._freshness import fetch_feature_branch
from cw.worktree._git import (
    _checked_out_branch,
    _git_dir,
    _ref_exists,
    _run_git,
    check_not_main_checkout,
)
from cw.worktree._paths import worktree_path_for
from cw.worktree._refresh import (
    ReuseRefreshReport,
    _raise_if_occupied,
    _refresh_reused_worktree,
)
from cw.worktree._unsaved import worktree_has_unsaved_work

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)


# Pattern appended to $GIT_COMMON_DIR/info/exclude so ephemeral per-session
# .cw/ artifacts are invisible to git status without touching .gitignore.
_CW_EXCLUDE_PATTERN = ".cw/"
# Matches git's "fatal: '<branch>' is already used by worktree at '<path>'"
# line so create_worktree can name the colliding worktree in a targeted error
# (#2034) instead of surfacing git's bare stderr.
_WORKTREE_HELD_BY_RE = re.compile(r"already used by worktree at '([^']+)'")


def _register_cw_exclude(git_cwd: Path) -> None:
    """Idempotently append .cw/ to $GIT_COMMON_DIR/info/exclude.

    Uses git rev-parse --git-common-dir so the write targets the shared
    object-store directory even when called from within a worktree. Never
    touches the committed .gitignore. Logs a warning and returns on any
    git or I/O failure rather than propagating — exclude registration is
    advisory and must not abort worktree creation.
    """
    try:
        result = _run_git("rev-parse", "--git-common-dir", cwd=git_cwd)
        common_dir_str = result.stdout.strip()
        if not common_dir_str:
            _log.warning(
                "_register_cw_exclude: empty --git-common-dir output in %s", git_cwd
            )
            return
        common_dir = (
            Path(common_dir_str)
            if Path(common_dir_str).is_absolute()
            else git_cwd / common_dir_str
        )
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        if _CW_EXCLUDE_PATTERN in existing.splitlines():
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a") as fh:
            fh.write(f"{separator}{_CW_EXCLUDE_PATTERN}\n")
    except (WorktreeError, OSError) as exc:
        _log.warning("_register_cw_exclude: failed for %s: %s", git_cwd, exc)


def _resolve_branch_start_point(client: ClientConfig, git_cwd: Path) -> str:
    """Resolve the start-point for a new branch in *client*'s repository.

    Three-level fallback matching the convention in ``_unpushed_commits_detail``:

    1. ``origin/<default_branch>`` — authoritative remote ref; independent of
       the operator's current checkout.
    2. ``<default_branch>`` — local fallback for offline / bare-clone scenarios.
    3. Raise :exc:`WorktreeError` — never fall back to HEAD, which is exactly
       the bug this prevents (#710).
    """
    origin_ref = f"origin/{client.default_branch}"
    result = _run_git("rev-parse", "--verify", origin_ref, cwd=git_cwd, check=False)
    if result.returncode == 0:
        # Why: origin/<default_branch> is already current because dispatch's freshness
        # gate fetched it earlier this tick; interactive cw start accepts a
        # possibly-one-fetch-stale origin ref — better than HEAD-based base.
        return origin_ref

    local_ref = client.default_branch
    result = _run_git("rev-parse", "--verify", local_ref, cwd=git_cwd, check=False)
    if result.returncode == 0:
        return local_ref

    msg = (
        f"Cannot resolve a start-point for new branch in {client.name}: "
        f"neither {origin_ref!r} nor {local_ref!r} exists. "
        f"Ensure the repository has a remote or local {local_ref!r} branch."
    )
    raise WorktreeError(msg)


def _parse_worktree_holder_path(message: str) -> Path | None:
    """Extract the colliding worktree's path from a git worktree-add failure.

    Returns ``None`` when *message* does not match the known
    "already used by worktree at '<path>'" shape — callers must treat that as
    "not this specific collision" and re-raise the original error unchanged.
    """
    match = _WORKTREE_HELD_BY_RE.search(message)
    return Path(match.group(1)) if match else None


def _branch_held_error(
    client: ClientConfig, branch: str, holder: Path
) -> BranchHeldByWorktreeError:
    """Build the diagnostic for a foreign worktree already holding *branch* (#2034).

    Names the holder, states whether it looks clean or dirty (per
    :func:`worktree_has_unsaved_work`'s ``wt_path`` override), and gives the
    exact removal command either way — never removes the holder itself.
    """
    if worktree_has_unsaved_work(client, branch, wt_path=holder):
        state_msg = (
            "has uncommitted or unpushed changes; verify before removing "
            f"with `git worktree remove {holder}`"
        )
    else:
        state_msg = (
            f"appears clean; safe to remove with `git worktree remove {holder}` "
            "if it is no longer needed"
        )
    msg = (
        f"Branch {branch!r} is already checked out in another worktree at "
        f"{holder}. This is likely an orphaned harness agent worktree "
        f"(see #2017) that cw did not create and cannot verify is finished "
        f"with. It was NOT removed automatically. That worktree {state_msg}."
    )
    return BranchHeldByWorktreeError(msg, holder_path=holder)


def create_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
    allow_dirty_reuse: bool = False,
    refresh_on_reuse: bool = False,
    refresh_report: ReuseRefreshReport | None = None,
    ticket_id: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> Path:
    """Create a git worktree for the given branch.

    Returns the worktree path. Idempotent: returns the existing path when it is
    already a worktree on *branch*. A pre-existing directory checked out on a
    *different* branch (or not a worktree at all) is treated as stale and
    raises :exc:`StaleWorktreeError` rather than being reused (see below).

    By default reuse is path resolution only: no fetch, no fast-forward.

    *refresh_on_reuse* (#2213) opts a caller into a best-effort refresh of a
    reused worktree, for callers whose purpose is a fresh per-ticket worktree
    (dispatch claim, fix-agent dispatch). It has side effects a path-resolution
    call would not suggest:

    - **Network:** it runs ``git fetch origin <branch>``, which can be slow or
      fail. A failed fetch skips the fast-forward entirely and uses the
      worktree as-is (a fast-forward from the stale tracking ref a failed
      fetch leaves behind would be a move to stale state); it never raises out
      of this function.
    - **Moves HEAD** only for a strict fast-forward (``merge --ff-only``) of a
      worktree that is unoccupied, clean, already on *branch*, and strictly
      behind a freshly fetched ``origin/<branch>``. The full occupancy check
      runs once before the fetch and again immediately before the merge.

    **"The refresh did not move it" means two different things**
    (:class:`RefreshOutcome`), and they are handled oppositely:

    - **Occupied -- ABORT, raised.** A live cw session in persisted state, a live
      daemon-roster worker, or an indeterminate read of either (unreadable
      state or roster, or any path that cannot be resolved -- fail closed, any
      ``OSError`` reads as occupied) means another worker may be operating in
      the tree. This function RAISES :exc:`~cw.exceptions.WorktreeOccupiedError`
      (carrying ``path`` and ``reason``) rather than returning the path, so a
      caller cannot spawn into, dispatch against or mutate the tree by
      forgetting a check. HEAD may already have moved if a fast-forward
      landed before the occupant was found (#2233); the worktree is never
      removed and nothing is spawned into it. It is not a
      :exc:`StaleWorktreeError`: never remove an occupied worktree.
    - **Not refreshed -- PROCEED, returned.** The tree is the caller's to use as
      it is, just not (known to be) up to date: unsaved work (uncommitted,
      untracked or unpushed -- what ``allow_dirty_reuse`` tolerates), the fetch
      failed (the tracking ref is stale, so it is never fast-forwarded from),
      the branch has diverged from origin (WARNING), ``--ff-only`` itself
      refuses (WARNING), ``origin/<branch>`` does not exist, or it already
      matches origin. This function returns the path; the reason is on
      *refresh_report* and, for failures, in a note. A wrong branch is
      stricter still: the identity guard above raises :exc:`StaleWorktreeError`
      before any refresh, and the pre-merge re-check raises the same error if
      the branch changed during the fetch (see :func:`_ff_reused_worktree`) --
      neither is returned as ``NOT_REFRESHED``.

    *refresh_report* (#2213) is an out-parameter (:class:`ReuseRefreshReport`)
    for callers that want more than the path. Its ``notes`` receive one line per
    refresh FAILURE (fetch failed, with git's reason; fast-forward refused by
    git; diverged; an OS error; a submodule sync failed), each naming the
    worktree and the reason. Its
    ``outcome`` and ``reason`` are the verdict, filled in before this function
    returns or raises. The occupancy refusal does not depend on it: it is the
    exception. Ignored unless *refresh_on_reuse* is set.

    *ticket_id* (#2213) names the ticket the caller is working, for the audit
    event only: a refresh that actually moves HEAD records one
    ``worktree.fast_forwarded`` event (see ``docs/events.md``) with it as the
    payload's ``ticket_id`` and the ``correlation_id`` (``None`` when the
    caller has no ticket). It has no other effect and is ignored unless
    *refresh_on_reuse* is set. The audit write is best-effort: an ``OSError``
    from it is logged and never changes the outcome.

    *native_daemon* (#2213 round 7) is the caller's own
    :class:`~cw.native_daemon.NativeDaemonClient`, threaded through to the
    occupancy check's daemon-roster read (:func:`live_home_reason`) instead of
    this function defaulting to :func:`~cw.native_daemon.get_native_daemon_client`
    internally. Defaults to that real client when omitted -- the same shape as
    :func:`cw.session.start_session` -- so a caller that injects
    :class:`~cw.native_daemon.FakeNativeDaemonClient` (dispatch claim, tests) is
    actually consulted, and the host's real roster is never read out from under
    an injected fake. Ignored unless *refresh_on_reuse* is set.

    See :func:`_refresh_reused_worktree`.

    When no existing worktree is reused, the branch itself is resolved via a
    three-way check (#2032): a local ``refs/heads/<branch>`` is used as-is; if
    absent, ``origin/<branch>`` is fetched and checked next so a branch whose
    local ref was deleted (e.g. a fix-loop's ``git branch -D`` reset) resumes
    its real pushed history instead of silently starting over; only when
    neither exists is a brand-new branch created from the client's default
    branch.

    *allow_dirty_reuse* relaxes the unsaved-work refusal **only** (the
    branch-identity check still fires). The staged pipeline reuses one
    per-ticket worktree across stages, where a prior stage legitimately leaves
    uncommitted churn (e.g. ``uv.lock``); without this the FINALIZE-stage
    reuse trips the guard and parks the ticket (#712). Cross-ticket protection
    is unaffected — a foreign branch at the path is still refused.
    """
    wt_path = worktree_path_for(client, branch)
    git_cwd = _git_dir(client)

    check_not_main_checkout(wt_path, client)

    if wt_path.exists():
        # Idempotent reuse is only safe when the existing worktree is still on
        # the branch we were asked for. A stale worktree left by a prior failed
        # dispatch (crash before reconcile's TIMED_OUT cleanup, see #404) can
        # carry a different branch — and thus a prior run's commits — into the
        # new session. Silently reusing it feeds the worker the wrong context
        # and has caused cross-ticket isolation breaches (#402). Refuse on
        # mismatch: the dispatch loop reverts the task to PENDING and reconcile
        # removes the stale tree so the retry starts clean.
        current_branch = _checked_out_branch(wt_path)
        if current_branch != branch:
            found = current_branch or "(none / detached HEAD / not a worktree)"
            msg = (
                f"Refusing to reuse stale worktree at {wt_path}: expected "
                f"branch {branch!r} but found {found}. Remove it with "
                f"`git worktree remove --force {wt_path}`, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
        if not allow_dirty_reuse and worktree_has_unsaved_work(client, branch):
            msg = (
                f"Refusing to reuse worktree at {wt_path} for branch {branch!r}: "
                f"it has unsaved work (uncommitted changes or unpushed commits). "
                f"Commit or push the work, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
        if refresh_on_reuse:
            daemon = native_daemon or get_native_daemon_client()
            refresh = _refresh_reused_worktree(
                client,
                branch,
                wt_path,
                refresh_report if refresh_report is not None else ReuseRefreshReport(),
                ticket_id=ticket_id,
                daemon=daemon,
            )
            # Occupied means another worker may be using the tree: no path
            # is handed back. Every other outcome is the caller's to use.
            _raise_if_occupied(refresh, branch, wt_path)
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # Three-way branch resolution: local ref / remote ref / neither (#2032).
    # refs/heads/ and refs/remotes/origin/ are checked explicitly so a
    # same-named tag never matches either.
    if _ref_exists(f"refs/heads/{branch}", git_cwd):
        # Local branch exists — create worktree from it.
        args = ["worktree", "add", str(wt_path), branch]
    else:
        # Local ref absent — before assuming the branch doesn't exist at
        # all, check the remote. A prior `git branch -D` (e.g.
        # auto-dev-review.md's fix-loop reset) leaves exactly this state
        # while origin still has the branch's real history.
        # The fetch result is deliberately unused: this only precedes the
        # local ref-exists check below, and ``_fetch_default_branch`` has
        # already logged a failure's reason.
        fetch_feature_branch(client, branch)
        if _ref_exists(f"refs/remotes/origin/{branch}", git_cwd):
            # Branch exists on the remote — resume its pushed history.
            args = ["worktree", "add", "-b", branch, str(wt_path), f"origin/{branch}"]
        else:
            # Branch doesn't exist locally or on the remote — create new
            # branch from the client's default branch.
            start_point = _resolve_branch_start_point(client, git_cwd)
            args = ["worktree", "add", "-b", branch, str(wt_path), start_point]

    if force:
        args.insert(2, "--force")

    try:
        _run_git(*args, cwd=git_cwd)
    except WorktreeError as exc:
        holder = _parse_worktree_holder_path(str(exc))
        if holder is None:
            raise
        raise _branch_held_error(client, branch, holder) from exc
    _register_cw_exclude(git_cwd)

    # Initialize submodules if the repo uses them
    if (git_cwd / ".gitmodules").exists():
        _run_git(
            "submodule",
            "update",
            "--init",
            "--recursive",
            cwd=wt_path,
            check=False,
        )

    return wt_path


def remove_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
) -> None:
    """Remove a git worktree for the given branch."""
    wt_path = worktree_path_for(client, branch)

    if not wt_path.exists():
        return

    args = ["worktree", "remove", str(wt_path)]
    if force:
        args.append("--force")

    _run_git(*args, cwd=_git_dir(client))
