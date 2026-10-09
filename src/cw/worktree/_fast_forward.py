"""Fast-forward of a reused worktree, with audit event and submodule sync.

The reuse refresh's mutating step (#2213): ``merge --ff-only`` after an
occupancy re-check, one ``worktree.fast_forwarded`` audit event when HEAD
moved, and a submodule sync when the new tree carries ``.gitmodules`` (#2233).
Not to be confused with :func:`cw.worktree._freshness.fast_forward_main`, which
fast-forwards the main checkout.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType
from cw.worktree._git import _first_line, _run_git
from cw.worktree._occupancy import _occupancy_verdict
from cw.worktree._refresh_types import _LOGGER_NAME, RefreshOutcome, RefreshResult

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree._refresh_types import ReuseRefreshReport

# Pinned to the pre-split module name so operator log filters on
# ``cw.worktree._refresh`` keep matching (#2569).
_log = logging.getLogger(_LOGGER_NAME)


# Abbreviated-SHA width for fast-forward log lines.
_SHA_LOG_CHARS = 12


def _record_fast_forward(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    *,
    ticket_id: str | None,
    old_sha: str,
    new_sha: str,
) -> None:
    """Record one ``worktree.fast_forwarded`` audit event for a HEAD that moved.

    The reuse refresh can move a worktree's ``HEAD`` on its own, so an operator
    asking "why is my worktree at a different commit than I left it?" gets a
    durable answer: client, ticket, branch, path and the FULL before/after SHAs
    (the log line abbreviates them). ``correlation_id`` is *ticket_id* when
    known. Audit-only: not forwarded to the operator-attention channel.

    Only the write is guarded, and only for ``OSError`` (a full disk, an
    unwritable inbox, a lock failure): the fast-forward has already happened,
    so a lost audit line is logged at WARNING and must not turn it into
    ``NOT_REFRESHED`` or raise. Anything else is a bug and propagates.
    """
    payload = {
        "client": client.name,
        "ticket_id": ticket_id,
        "branch": branch,
        "worktree_path": str(wt_path),
        "old_sha": old_sha,
        "new_sha": new_sha,
    }
    try:
        record_event(
            OrchestratorEventType.WORKTREE_FAST_FORWARDED,
            payload,
            correlation_id=ticket_id,
        )
    except OSError as exc:
        _log.warning(
            "create_worktree: could not record the worktree.fast_forwarded audit "
            "event (client=%s, ticket=%s, path=%s, %s -> %s): %s",
            client.name,
            ticket_id,
            wt_path,
            old_sha[:_SHA_LOG_CHARS],
            new_sha[:_SHA_LOG_CHARS],
            _first_line(str(exc)) or type(exc).__name__,
        )


def _sync_reused_submodules(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    daemon: NativeDaemonClient,
) -> RefreshResult | None:
    """Sync submodules after a fast-forward that may have moved ``.gitmodules`` (#2233).

    Runs only when *wt_path* carries a ``.gitmodules`` file after the fast-
    forward already landed (``--ff-only`` updates the working tree to match
    the new HEAD on success, so this reads the post-merge tree, mirroring
    the first-time-creation check in
    :func:`~cw.worktree._lifecycle.create_worktree`, which checks the main
    checkout instead since it has no reused tree yet). Nothing to do
    otherwise: repositories without submodules see no change.

    Re-runs the full occupancy predicate (:func:`_occupancy_verdict`) first,
    exactly as :func:`_ff_reused_worktree` does before the merge itself: the
    fast-forward that just landed was local and fast, but
    ``git submodule update`` can itself fetch over the network, and must not
    run once a live session or worker may be operating in the tree. A live
    occupant (or a branch switch, which raises :exc:`StaleWorktreeError` the
    same way the pre-merge check does) overrides the fast-forward's own
    ``REFRESHED`` verdict for the CALLER's purposes: the fast-forward is not
    undone -- HEAD already moved and stays moved -- but nothing may spawn
    into, dispatch against or further mutate a tree another worker may now
    be using, and syncing submodules is itself such a mutation.

    A failed ``git submodule update --init --recursive`` is logged at
    WARNING and noted on *report* -- never raised, never turned into
    ``NOT_REFRESHED``. With more than one submodule, a failure partway
    through can leave the sync PARTIAL: git registers and fetches whatever
    submodule(s) it reaches before the one that fails, without checking any
    of them out (its checkout pass runs only after every submodule has been
    fetched). That partial registration alone is enough to turn the
    superproject's own ``git status --porcelain`` from clean to dirty, with
    no submodule actually checked out -- so a failed sync here can make the
    NEXT reuse refresh's occupancy check (:func:`unsaved_work_reason`) read
    this worktree as having unsaved work and decline to fast-forward it
    again until the orchestrator re-runs ``git submodule update`` or otherwise
    cleans it up. Init and update only: never ``deinit``, reset or
    delete a submodule to recover from this.
    """
    if not (wt_path / ".gitmodules").exists():
        return None
    verdict = _occupancy_verdict(
        client,
        branch,
        wt_path,
        action=(
            "reused worktree occupied after the fast-forward; submodule "
            "sync skipped, using worktree as-is"
        ),
        raise_on_branch_mismatch=True,
        daemon=daemon,
    )
    if verdict is not None:
        return verdict
    sync = _run_git(
        "submodule", "update", "--init", "--recursive", cwd=wt_path, check=False
    )
    if sync.returncode != 0:
        reason = _first_line(sync.stderr) or f"git exited {sync.returncode}"
        _log.warning(
            "create_worktree: submodule sync of %s in reused worktree %s failed: %s",
            branch,
            wt_path,
            reason,
        )
        report.notes.append(
            f"submodule sync of {branch} in reused worktree {wt_path} failed "
            f"({reason}); the sync may be partial (some submodules "
            "registered but none checked out), which can leave the "
            "worktree uncommitted-dirty until `git submodule update --init "
            "--recursive` is re-run there"
        )
    return None


def _ff_reused_worktree(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    target: str,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult:
    """Fast-forward *wt_path* to *target* with ``merge --ff-only``.

    First re-runs the FULL occupancy predicate (:func:`_reuse_occupancy`:
    expected branch, unsaved work, cw state, daemon roster), immediately before
    the first mutating git call. The caller's occupancy gate ran before a
    network fetch that can take a while, so a session or worker may have
    started, the tree been dirtied, or the checked-out branch changed, in the
    meantime. If anything changed the fast-forward is abandoned:
    ``OCCUPIED_BY_LIVE_SESSION`` when a live occupant appeared, ``NOT_REFRESHED``
    when the tree was merely dirtied. A branch switch RAISES
    :exc:`~cw.exceptions.StaleWorktreeError` instead of returning
    ``NOT_REFRESHED`` (``raise_on_branch_mismatch=True`` below): it is the same
    stale-worktree condition :func:`create_worktree`'s own identity guard
    refuses up front, just observed after the fetch instead of before it, and
    the tree is no longer safe to fast-forward or use as-is. This narrows the
    window but does not eliminate it: a session can still start, or the branch
    change again, between this check and the merge. That remaining window is
    accepted because the only alternative -- holding a lock across the network
    fetch (or across the check-then-merge) -- is worse: it would stall every
    other claim behind a slow remote.

    ``--ff-only`` cannot destroy work: it refuses (rc != 0, worktree untouched)
    when local uncommitted changes overlap files the merge must update, and
    carries non-overlapping local modifications through. A refusal is logged,
    noted on *report*, returned as ``NOT_REFRESHED``, and the worktree is left
    exactly as it was. A completed fast-forward is ``REFRESHED``.

    A fast-forward that actually MOVES ``HEAD`` (the SHA after differs from the
    SHA before) leaves one ``worktree.fast_forwarded`` audit event
    (:func:`_record_fast_forward`), carrying *ticket_id* (``None`` when the
    caller has none). Nothing is recorded when nothing moved (a merge that says
    "Already up to date"), and a failed audit write never undoes or
    reclassifies the completed fast-forward. When the new HEAD carries a
    ``.gitmodules`` file, a successful merge also triggers a submodule sync
    (:func:`_sync_reused_submodules`, #2233), whose own occupancy re-check can
    turn a would-be ``REFRESHED`` into the same abort the pre-merge re-check
    above gives.
    """
    verdict = _occupancy_verdict(
        client,
        branch,
        wt_path,
        action=(
            "reused worktree occupied after the fetch; fast-forward abandoned, "
            "using worktree as-is"
        ),
        raise_on_branch_mismatch=True,
        daemon=daemon,
    )
    if verdict is not None:
        return verdict
    old_sha = _run_git("rev-parse", "HEAD", cwd=wt_path, check=False).stdout.strip()
    merge = _run_git("merge", "--ff-only", target, cwd=wt_path, check=False)
    if merge.returncode != 0:
        reason = _first_line(merge.stderr) or f"git exited {merge.returncode}"
        _log.warning(
            "create_worktree: fast-forward of %s refused (client=%s, path=%s): %s",
            branch,
            client.name,
            wt_path,
            reason,
        )
        report.notes.append(
            f"fast-forward of {branch} in reused worktree {wt_path} was refused "
            f"by git ({reason}); it was not refreshed and may be behind origin"
        )
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"git refused the fast-forward: {reason}"
        )
    new_sha = _run_git("rev-parse", "HEAD", cwd=wt_path, check=False).stdout.strip()
    _log.info(
        "create_worktree: fast-forwarded reused worktree %s "
        "(client=%s, path=%s) %s -> %s",
        branch,
        client.name,
        wt_path,
        old_sha[:_SHA_LOG_CHARS],
        new_sha[:_SHA_LOG_CHARS],
    )
    if new_sha != old_sha:
        _record_fast_forward(
            client,
            branch,
            wt_path,
            ticket_id=ticket_id,
            old_sha=old_sha,
            new_sha=new_sha,
        )
        sync_stop = _sync_reused_submodules(
            client, branch, wt_path, report, daemon=daemon
        )
        if sync_stop is not None:
            return sync_stop
    return RefreshResult(
        RefreshOutcome.REFRESHED,
        f"fast-forwarded {branch} {old_sha[:_SHA_LOG_CHARS]} -> "
        f"{new_sha[:_SHA_LOG_CHARS]}",
    )
