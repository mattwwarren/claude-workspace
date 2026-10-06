"""Reuse refresh orchestration for reused worktrees (#2213).

When :func:`~cw.worktree.create_worktree` reuses an existing worktree with
``refresh_on_reuse`` set, this module runs the refresh: the occupancy gate
(:mod:`cw.worktree._occupancy`), the fetch of ``origin/<branch>``, the
branch-absent handling (#2328), and the classification of HEAD against the
fetched ref that hands a behind tree to the fast-forward
(:mod:`cw.worktree._fast_forward`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, assert_never

from cw.exceptions import WorktreeOccupiedError
from cw.worktree._fast_forward import _ff_reused_worktree
from cw.worktree._freshness import (
    FetchOutcome,
    _ff_relation,
    fetch_default_branch,
    fetch_feature_branch,
)
from cw.worktree._git import _first_line, _ref_exists
from cw.worktree._occupancy import _occupancy_verdict
from cw.worktree._refresh_types import _LOGGER_NAME, RefreshOutcome, RefreshResult
from cw.worktree._scope import _has_commits_beyond_base

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree._refresh_types import ReuseRefreshReport

# Pinned to the pre-split module name so operator log filters on
# ``cw.worktree._refresh`` keep matching (#2569).
_log = logging.getLogger(_LOGGER_NAME)


def _handle_branch_absent(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult:
    """Decide what to do with a branch absent from origin (#2328).

    ``origin/<branch>`` not existing is not, on its own, a reason to leave the
    worktree alone: a never-pushed branch with no commits beyond
    ``origin/<default_branch>`` is exactly as stale as a pushed one that fell
    behind, and gets the same fast-forward treatment (through the generalized
    :func:`_refresh_from_tracking_ref`, reusing its occupancy re-check and
    ``--ff-only`` safety verbatim). A branch WITH commits of its own is left
    untouched -- that base is the caller's own work, not something to rebase
    silently -- but is noted, since the reader may be building on it thinking
    it is current.
    """
    default_fetch = fetch_default_branch(client)
    if default_fetch.outcome is not FetchOutcome.FETCHED:
        reason = default_fetch.reason or "no reason reported"
        report.notes.append(
            f"origin/{branch} does not exist and fetching "
            f"origin/{client.default_branch} to check reused worktree {wt_path} "
            f"for a stale base failed ({reason})"
        )
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"origin/{branch} does not exist yet"
        )
    if _has_commits_beyond_base(wt_path, client.default_branch):
        report.notes.append(
            f"origin/{branch} does not exist and reused worktree {wt_path} has "
            f"commits of its own beyond origin/{client.default_branch}; it was "
            "left untouched and may be building on a stale base"
        )
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"origin/{branch} does not exist yet"
        )
    return _refresh_from_tracking_ref(
        client,
        branch,
        wt_path,
        report,
        target=f"refs/remotes/origin/{client.default_branch}",
        target_label=f"origin/{client.default_branch}",
        ticket_id=ticket_id,
        daemon=daemon,
    )


def _fetch_gate(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult | None:
    """Fetch ``origin/<branch>``; return a stopping result, or ``None`` if it landed.

    Handles :class:`FetchOutcome` exhaustively: only ``FETCHED`` lets the refresh
    go on. ``BRANCH_ABSENT`` (never pushed) delegates to
    :func:`_handle_branch_absent`, which decides whether the branch has commits
    of its own worth preserving as-is, or is stale enough to fast-forward to
    ``origin/<default_branch>`` (#2328). ``FAILED`` leaves the tracking ref at
    whatever it was before, so fast-forwarding "to origin" would really move
    HEAD to stale state: it stops, and is reported with git's reason. A member
    this function does not know is a type error (and, at runtime, an
    ``AssertionError``), never a silent "fetched".
    """
    fetch = fetch_feature_branch(client, branch)
    outcome = fetch.outcome
    match outcome:
        case FetchOutcome.FETCHED:
            return None
        case FetchOutcome.BRANCH_ABSENT:
            return _handle_branch_absent(
                client, branch, wt_path, report, ticket_id=ticket_id, daemon=daemon
            )
        case FetchOutcome.FAILED:
            reason = fetch.reason or "no reason reported"
            _log.debug(
                "create_worktree: fetch of origin/%s failed (%s); fast-forward "
                "skipped, using worktree as-is (client=%s, path=%s)",
                branch,
                reason,
                client.name,
                wt_path,
            )
            report.notes.append(
                f"fetch of origin/{branch} failed while refreshing reused "
                f"worktree {wt_path} ({reason}); it was not fast-forwarded and "
                "may be behind origin"
            )
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED,
                f"fetch of origin/{branch} failed: {reason}",
            )
        case _:
            assert_never(outcome)


def _refresh_from_tracking_ref(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    target: str,
    target_label: str,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult:
    """Classify HEAD against the freshly fetched *target* and act on it.

    *target* is the ref to classify against (e.g. ``refs/remotes/origin/<branch>``
    for the pushed-branch path, or ``refs/remotes/origin/<default_branch>`` for
    the never-pushed-branch path, #2328) and *target_label* its
    human-readable form for messages (e.g. ``origin/<branch>``). NOT the
    removed upstream-first ``_resolve_remote_ref`` helper (deleted in #2266):
    that ladder was upstream-first, and a misconfigured ``@{u}`` of
    ``origin/<default>`` (the #2114 failure mode) would have fast-forwarded a
    feature branch onto main. Target absent (a fetch can succeed
    without creating the tracking ref, under a narrow ``remote.origin.fetch``
    refspec): nothing to move.

    Equal or ahead: nothing to do (unpushed commits kept). Diverged (remote
    history rewritten since the worktree pushed): WARNING, untouched --
    reconciling is not this function's job. Behind: the fast-forward, after the
    occupancy re-check (:func:`_ff_reused_worktree`).
    """
    if not _ref_exists(target, wt_path):
        return RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"{target} does not exist after the fetch"
        )
    relation = _ff_relation("HEAD", target, wt_path)
    match relation:
        case "equal" | "ahead":
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED,
                f"already up to date with {target_label} ({relation})",
            )
        case "diverged":
            _log.warning(
                "create_worktree: reused worktree diverged from %s; "
                "leaving untouched (client=%s, path=%s)",
                target_label,
                client.name,
                wt_path,
            )
            report.notes.append(
                f"reused worktree {wt_path} has diverged from {target_label}; it "
                "was left untouched and not refreshed"
            )
            return RefreshResult(
                RefreshOutcome.NOT_REFRESHED, f"diverged from {target_label}"
            )
        case "behind":
            return _ff_reused_worktree(
                client,
                branch,
                wt_path,
                target,
                report,
                ticket_id=ticket_id,
                daemon=daemon,
            )
        case _:
            assert_never(relation)


def _refresh_reused_worktree_steps(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult:
    """The ordered steps of :func:`_refresh_reused_worktree`, which see OSError."""
    verdict = _occupancy_verdict(
        client,
        branch,
        wt_path,
        action="not refreshing reused worktree",
        daemon=daemon,
    )
    if verdict is not None:
        return verdict
    stopped = _fetch_gate(
        client, branch, wt_path, report, ticket_id=ticket_id, daemon=daemon
    )
    if stopped is not None:
        return stopped
    return _refresh_from_tracking_ref(
        client,
        branch,
        wt_path,
        report,
        target=f"refs/remotes/origin/{branch}",
        target_label=f"origin/{branch}",
        ticket_id=ticket_id,
        daemon=daemon,
    )


def _refresh_reused_worktree(
    client: ClientConfig,
    branch: str,
    wt_path: Path,
    report: ReuseRefreshReport,
    *,
    ticket_id: str | None,
    daemon: NativeDaemonClient,
) -> RefreshResult:
    """Best-effort fetch, then fast-forward a *behind, unoccupied* reused worktree.

    Called from :func:`create_worktree` only when ``refresh_on_reuse`` is set
    (#2213), after its branch-identity and unsaved-work guards. Closes the
    asymmetry between the first-time path (which fetches) and the reuse path
    (which used to return the worktree untouched): a per-ticket worktree reused
    across pipeline stages could sit on a stale HEAD while ``origin/<branch>``
    had moved on.

    Returns a :class:`RefreshResult` (also recorded on *report*), whose
    :class:`RefreshOutcome` says what the caller may do next:

    - ``REFRESHED``: fast-forwarded. Proceed.
    - ``NOT_REFRESHED``: the tree is the caller's, just not up to date. Proceed.
    - ``OCCUPIED_BY_LIVE_SESSION``: another worker may be operating in it. The
      caller must NOT spawn into it, dispatch against it or mutate it.
      :func:`create_worktree` turns this into
      :exc:`~cw.exceptions.WorktreeOccupiedError`; this helper only reports it.

    A branch switch discovered at the step-4 re-check does not appear as a
    :class:`RefreshResult` at all: :func:`_ff_reused_worktree` raises
    :exc:`~cw.exceptions.StaleWorktreeError` directly (see below), the same
    exception :func:`create_worktree`'s own identity guard raises up front.

    Order of operations:

    1. **Occupancy gate, local reads only, no network**
       (:func:`_reuse_occupancy`). A live session in cw state, a live worker in
       the daemon roster homed here, an unreadable state or roster, or a path
       that cannot be normalized (fail closed) is ``OCCUPIED_BY_LIVE_SESSION``.
       The checked-out branch not being the expected one, or unsaved work, is
       ``NOT_REFRESHED``. Either way a DEBUG log names the path and the reason
       and nothing else happens (no fetch, no move). (``create_worktree``'s own
       identity guard *raises* on a wrong branch before this helper is reached;
       the predicate repeats the branch check because step 4 re-runs it.)
    2. ``git fetch`` of ``origin/<branch>`` via :func:`fetch_feature_branch`,
       which returns a :class:`FetchResult`. This is a network call: it can be
       slow, or fail. Only ``FETCHED`` proceeds (see :func:`_fetch_gate`); a
       failed fetch is ``NOT_REFRESHED`` and reported with git's reason.
    3. Target and relation: see :func:`_refresh_from_tracking_ref`.
    4. Behind: the full occupancy predicate is re-run immediately before
       ``merge --ff-only`` (a session may have started, or the branch changed,
       during the fetch; see :func:`_ff_reused_worktree`). A branch switch here
       RAISES ``StaleWorktreeError`` instead of returning ``NOT_REFRESHED`` --
       the tree is stale, not merely unrefreshed. Otherwise, the fast-forward. A
       fast-forward that moved HEAD records one ``worktree.fast_forwarded``
       audit event carrying *ticket_id*; no other path records anything.
    5. Submodule sync (#2233): when the fast-forward moved HEAD and the new
       tree carries a ``.gitmodules`` file, the occupancy predicate is re-run a
       THIRD time and, if still clear, ``git submodule update --init
       --recursive`` runs (see :func:`_sync_reused_submodules`). A live
       occupant found here overrides the fast-forward's own ``REFRESHED``
       verdict the same way step 4's re-check does -- the fast-forward is not
       undone, but nothing further may run. A sync failure is reported on
       *report* and logged at WARNING; it never changes the outcome away from
       ``REFRESHED``.

    *report* is the caller-supplied surface (see :class:`ReuseRefreshReport`).
    Every FAILURE the caller cannot otherwise see -- a failed fetch, a diverged
    branch, a fast-forward git refused, an ``OSError`` -- appends exactly one
    note naming the worktree and the reason. Designed non-actions (occupied,
    branch absent, equal or ahead) add none. A step-4 branch-switch raise adds
    no note either: it never reaches *report*, the same as the occupancy raise.

    Never raises for a git or OS failure and never resets, ``checkout -f``s or
    deletes. Can raise :exc:`~cw.exceptions.StaleWorktreeError` for a branch
    switch discovered at the step-4 re-check (see above). Anything else that is
    not an ``OSError`` is a bug and propagates.
    """
    try:
        result = _refresh_reused_worktree_steps(
            client, branch, wt_path, report, ticket_id=ticket_id, daemon=daemon
        )
    except OSError as exc:
        reason = _first_line(str(exc)) or type(exc).__name__
        _log.warning(
            "create_worktree: refresh of reused worktree failed "
            "(client=%s, path=%s): %s",
            client.name,
            wt_path,
            reason,
        )
        report.notes.append(
            f"refresh of reused worktree {wt_path} failed with an OS error "
            f"({reason}); it was not refreshed and may be behind origin"
        )
        result = RefreshResult(
            RefreshOutcome.NOT_REFRESHED, f"OS error during the refresh: {reason}"
        )
    report.outcome = result.outcome
    report.reason = result.reason
    return result


def _raise_if_occupied(result: RefreshResult, branch: str, wt_path: Path) -> None:
    """Turn ``OCCUPIED_BY_LIVE_SESSION`` into :exc:`WorktreeOccupiedError`.

    Exhaustive over :class:`RefreshOutcome`: the two "proceed" outcomes return,
    the occupied one raises, and a member this function does not know is a type
    error (``assert_never``), never a silent "proceed".
    """
    outcome = result.outcome
    match outcome:
        case RefreshOutcome.OCCUPIED_BY_LIVE_SESSION:
            msg = (
                f"Refusing to reuse worktree at {wt_path} for branch {branch!r}: "
                f"another worker may be operating in it ({result.reason}). HEAD "
                "may already have moved if a fast-forward landed before this "
                "occupant was found; the worktree was not removed and nothing "
                "was spawned into it. Retry once the occupant is gone."
            )
            raise WorktreeOccupiedError(msg, path=wt_path, reason=result.reason)
        case RefreshOutcome.REFRESHED | RefreshOutcome.NOT_REFRESHED:
            return
        case _:
            assert_never(outcome)
