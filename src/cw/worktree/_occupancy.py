"""Occupancy verdict for a reused worktree (#2213).

Decides whether the reuse refresh may move a reused worktree: a live occupant
(:func:`~cw.worktree._liveness.live_home_reason`), a branch mismatch, or
unsaved work. Consulted up front and again before each mutation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.exceptions import StaleWorktreeError
from cw.worktree._git import _checked_out_branch
from cw.worktree._liveness import live_home_reason
from cw.worktree._refresh_types import _LOGGER_NAME, RefreshOutcome, RefreshResult
from cw.worktree._unsaved import unsaved_work_reason

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

# Pinned to the pre-split module name so operator log filters on
# ``cw.worktree._refresh`` keep matching (#2569).
_log = logging.getLogger(_LOGGER_NAME)


class _Occupancy(NamedTuple):
    """The reuse refresh's occupancy verdict, split by what it lets a caller do.

    - ``live``: a live session or daemon worker may be homed on the worktree
      (:func:`live_home_reason`), or the state or roster could not be read
      (fail closed). Nothing may touch the tree.
    - ``branch_mismatch``: the checked-out branch is not the expected one (or
      HEAD is detached). Same refusal as :func:`create_worktree`'s own
      identity guard, just observed later -- see :func:`_occupancy_verdict`'s
      *raise_on_branch_mismatch*.
    - ``local``: unsaved work. The refresh must not move HEAD, but the
      worktree is still the caller's to use: the staged pipeline reuses one
      per-ticket worktree that legitimately carries churn (e.g. ``uv.lock``)
      from a prior stage.

    All three are always evaluated. Unsaved work is exactly what a live worker
    leaves behind, so it must never mask ``live``.
    """

    live: str | None
    branch_mismatch: str | None
    local: str | None

    @property
    def reason(self) -> str | None:
        """The most serious reason the refresh is refused, or None if free."""
        if self.live is not None:
            return self.live
        if self.branch_mismatch is not None:
            return self.branch_mismatch
        return self.local


def _reuse_occupancy(
    client: ClientConfig, branch: str, wt_path: Path, *, daemon: NativeDaemonClient
) -> _Occupancy:
    """Return whether *wt_path* is occupied (must not be moved), and why.

    The single predicate the reuse refresh consults, both up front and again
    immediately before it mutates (see :func:`_ff_reused_worktree`). Local
    reads only -- no network. Occupied means any of:

    - ``branch_mismatch``: the checked-out branch is not *branch* (or HEAD is
      detached); or
    - ``local``: :func:`unsaved_work_reason` reports uncommitted, untracked or
      unpushed work (checked regardless of the caller's ``allow_dirty_reuse``,
      which only tolerates such work, it does not license moving HEAD under
      it); or
    - ``live``: :func:`live_home_reason` -- a live session in cw state or a
      live worker in the daemon roster is homed on *wt_path*, or either could
      not be read (fail closed: this gates a mutation). The dev-queue RUNNING
      half of the GC guard is deliberately not consulted: at dispatch-claim time
      the task being claimed is itself RUNNING, so it would veto the very path
      this refresh serves, and a live session for that task already appears in
      the state half.
    """
    current = _checked_out_branch(wt_path)
    branch_mismatch: str | None = None
    local: str | None = None
    if current != branch:
        found = current or "(none / detached HEAD / not a worktree)"
        branch_mismatch = f"expected branch {branch!r} but found {found}"
    else:
        unsaved = unsaved_work_reason(client, branch, wt_path=wt_path)
        if unsaved is not None:
            local = f"unsaved work ({unsaved})"
    return _Occupancy(
        live=live_home_reason(wt_path, daemon=daemon),
        branch_mismatch=branch_mismatch,
        local=local,
    )


def _occupancy_verdict(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    *,
    action: str,
    raise_on_branch_mismatch: bool = False,
    daemon: NativeDaemonClient,
) -> RefreshResult | None:
    """Return the stopping result when the refresh must not go on, else ``None``.

    Logs *action* at DEBUG naming the path and reason. The verdict keeps the two
    kinds of refusal apart (see :class:`_Occupancy`): a live occupant is
    ``OCCUPIED_BY_LIVE_SESSION`` (the caller must abort), while unsaved work is
    ``NOT_REFRESHED`` (the tree is still the caller's to use). A live occupant
    wins when both hold, because unsaved work is exactly what a live worker
    leaves behind.

    *raise_on_branch_mismatch* (set only by :func:`_ff_reused_worktree`'s
    pre-merge re-check) makes a branch switch that appeared since the initial
    gate raise :exc:`~cw.exceptions.StaleWorktreeError`, mirroring
    :func:`create_worktree`'s own identity guard, instead of returning
    ``NOT_REFRESHED``: the caller's own branch-identity guard already refused
    this worktree once before the refresh started; a branch that changed out
    from under a slow fetch is the same stale-worktree condition, not a
    "tree merely changed" case that is still safe to use as-is. A live
    occupant still wins over a branch mismatch (checked first, via
    ``occupancy.live``), since only :exc:`WorktreeOccupiedError` may report an
    occupant.
    """
    occupancy = _reuse_occupancy(client, branch, wt_path, daemon=daemon)
    reason = occupancy.reason
    if reason is None:
        return None
    _log.debug(
        "create_worktree: %s (client=%s, path=%s): %s",
        action,
        client.name,
        wt_path,
        reason,
    )
    if (
        raise_on_branch_mismatch
        and occupancy.live is None
        and occupancy.branch_mismatch is not None
    ):
        msg = (
            f"Refusing to reuse worktree at {wt_path}: it switched off branch "
            f"{branch!r} during the reuse refresh ({occupancy.branch_mismatch}). "
            f"Remove it with `git worktree remove --force {wt_path}`, then "
            "re-dispatch."
        )
        raise StaleWorktreeError(msg)
    outcome = (
        RefreshOutcome.OCCUPIED_BY_LIVE_SESSION
        if occupancy.live is not None
        else RefreshOutcome.NOT_REFRESHED
    )
    return RefreshResult(outcome, reason)
