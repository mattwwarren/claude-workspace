"""Reuse refresh and occupancy for reused worktrees (#2213).

When :func:`~cw.worktree.create_worktree` reuses an existing worktree with
``refresh_on_reuse`` set, this module decides whether the tree is occupied
by a live session or daemon worker, fetches ``origin/<branch>``, and
fast-forwards a behind, unoccupied tree, syncing submodules a fast-forward
brings in (#2233).
"""

from __future__ import annotations

import contextlib
import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple, assert_never

from cw.config import load_state
from cw.events import record_event
from cw.exceptions import StaleWorktreeError, WorktreeOccupiedError
from cw.models import OrchestratorEventType, SessionStatus
from cw.worktree._freshness import (
    FetchOutcome,
    _ff_relation,
    fetch_default_branch,
    fetch_feature_branch,
)
from cw.worktree._git import (
    _checked_out_branch,
    _first_line,
    _ref_exists,
    _run_git,
)
from cw.worktree._scope import _has_commits_beyond_base
from cw.worktree._unsaved import unsaved_work_reason

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)


# Abbreviated-SHA width for fast-forward log lines.
_SHA_LOG_CHARS = 12


class RefreshOutcome(enum.Enum):
    """What the reuse refresh did with a reused worktree, in the caller's terms (#2213).

    The refresh can decline to move a worktree for two reasons that call for
    OPPOSITE handling, and one flag used to carry both. They are separate
    members so a caller cannot conflate them:

    - ``REFRESHED``: fast-forwarded to a freshly fetched ``origin/<branch>``.
    - ``NOT_REFRESHED``: the tree is the caller's to use, it is just not (known
      to be) up to date -- dirty, diverged from origin, git refused the
      fast-forward, the fetch failed, the remote branch is absent, or it already
      matches origin. **Proceed with it.**
    - ``OCCUPIED_BY_LIVE_SESSION``: a live cw session in persisted state, a live
      daemon-roster worker, or an INDETERMINATE read of either (fail closed, an
      un-normalizable path included) means another worker may be operating in
      the tree. **Every caller that would spawn into it, dispatch against it or
      mutate it must abort.** ``create_worktree`` does not return this; it
      raises :exc:`~cw.exceptions.WorktreeOccupiedError` instead, so the refusal
      cannot be ignored.

    Anything that dispatches on this enum does so exhaustively (``match`` with
    ``assert_never``), so a new member is a type error rather than a silent
    "proceed".
    """

    REFRESHED = "refreshed"
    NOT_REFRESHED = "not_refreshed"
    OCCUPIED_BY_LIVE_SESSION = "occupied_by_live_session"


@dataclass(frozen=True)
class RefreshResult:
    """The refresh helper's verdict: a :class:`RefreshOutcome` and a one-line reason."""

    outcome: RefreshOutcome
    reason: str


@dataclass
class ReuseRefreshReport:
    """What the reuse refresh learned, for callers that must act on it (#2213).

    ``create_worktree`` returns only a path. A caller that opts into
    ``refresh_on_reuse`` and needs more than that passes one of these in:

    - ``notes``: one single-line entry per refresh FAILURE the caller cannot
      otherwise see, each naming the worktree and the reason -- the fetch
      failed (with git's reason), git refused the fast-forward, the branch
      diverged from origin, an OS error aborted the refresh, or a submodule
      sync failed. A caller with a friction surface prints them. Designed
      non-actions add nothing: branch absent from origin with no commits of
      its own, already equal or ahead, or a worktree that is occupied. One
      exception (#2328): a branch absent from origin that DOES have commits
      of its own also gets a note, even though leaving it untouched is the
      designed action -- the reader (a fix_agent prompt, a later pipeline
      stage) needs to know it may be building on a stale base.
    - ``outcome`` / ``reason``: the refresh's verdict (:class:`RefreshOutcome`)
      and why, or ``None`` when no refresh ran. It is filled in BEFORE
      ``create_worktree`` returns or raises, so a caller that catches
      :exc:`~cw.exceptions.WorktreeOccupiedError` can still read it.

    The report does not carry the occupancy refusal to the caller: that is the
    exception. Reading ``outcome`` is for logging and friction surfaces, never
    for deciding whether it is safe to go on.
    """

    notes: list[str] = field(default_factory=list)
    outcome: RefreshOutcome | None = None
    reason: str | None = None


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
    ``NOT_REFRESHED``, and the worktree is left exactly as the failed sync
    left it. Init and update only: never ``deinit``, reset or delete a
    submodule.
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
            f"({reason}); submodules may be pointing at stale commits"
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


_NON_TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED}
)
# What ``cw.config.load_state`` can really raise reading sessions.json:
# ``OSError`` (open/read, the pre-migration backup copy) and ``ValueError`` --
# the parent of ``json.JSONDecodeError`` and ``UnicodeDecodeError`` (a corrupt
# file), of pydantic's ``ValidationError`` (a wrong-shaped file), and of the
# ``int()`` schema-version coercion. It raises no project-specific state error.
# Anything outside this set is a bug and must propagate (#2213).
_STATE_READ_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)


def live_session_worktree_paths() -> frozenset[Path] | None:
    """Return worktree paths of non-terminal sessions in cw state, or None.

    This helper REPORTS what it can determine; it does not decide what an
    indeterminate answer means -- EACH CALLER DECIDES that. It returns a
    frozenset of paths when the session state was read, or ``None`` when it is
    indeterminate: the state could not be read or parsed (``OSError`` /
    ``ValueError`` -- see :data:`_STATE_READ_ERRORS`; logged at WARNING). Any
    other exception is a bug, not a corrupt file, and propagates.

    The two callers deliberately treat ``None`` in OPPOSITE directions:

    - The reuse refresh (:func:`live_home_reason`, reached through
      :func:`_reuse_occupancy`, #2213) fails CLOSED. ``None`` means "cannot
      rule out a live session", i.e. occupied: ``create_worktree`` raises
      :exc:`~cw.exceptions.WorktreeOccupiedError` and no caller spawns into,
      dispatches against or fast-forwards the tree. Reading "unknown" as "free"
      would rewrite (or spawn a second worker into) a live worker's tree.
    - The worktree GC (``cw.worktree_gc._live_worktree_paths``) fails OPEN,
      unchanged from before #2213. ``None`` contributes nothing, so a corrupted
      state file never blocks garbage collection; GC keeps running with this
      live-session guard disabled for the run (the WARNING above is the trace).

    The split is deliberate. Do not "fix" either posture into consistency with
    the other.

    Returns the paths exactly as recorded (unresolved): the GC compares them
    against git-listed paths, and the refresh normalizes before comparing.

    Lives here rather than in ``cw.worktree_gc`` because that module imports
    ``cw.dev_queue``, whose requeue/lifecycle modules import this one -- a
    top-level import of it from here would be a cycle.
    """
    try:
        state = load_state()
    except _STATE_READ_ERRORS as exc:
        _log.warning("live-path guard: failed to load session state: %s", exc)
        return None
    live: set[Path] = set()
    for session in state.sessions:
        if (
            session.status in _NON_TERMINAL_SESSION_STATUSES
            and session.worktree_path is not None
        ):
            live.add(session.worktree_path)
    return frozenset(live)


def _normalize_path(path: Path) -> Path:
    """Return *path* fully resolved, or raise ``OSError`` if it cannot be.

    A bare non-strict ``Path.resolve()`` is not enough. Since Python 3.13 it
    swallows a symlink loop and hands back the path only partly resolved, and it
    does not surface access errors either; the result would compare unequal to
    the real home and read as "not occupied" -- the fail-open direction this
    guard exists to prevent (#2213). ``stat()`` follows the whole chain first
    and reports the truth: ``ELOOP``, ``EACCES``, ``ENOTDIR``,
    ``ENAMETOOLONG`` and every other ``OSError`` propagate, and the caller
    reads that as "cannot rule out occupancy". There is no ``OSError`` for which
    "assume it is free" is the safe answer.

    The one exception is ``FileNotFoundError``. A path that does not exist is a
    fact, not a failure to look: it cannot be the existing worktree being
    reused, and a stale record of a deleted worktree must not veto every future
    refresh. It falls through to ``resolve()``, which normalizes what exists.
    """
    with contextlib.suppress(FileNotFoundError):
        path.stat()
    return path.resolve()


# One distinct unresolvable-record warning: which side recorded it (session
# state vs. daemon roster), the record's raw path, and the OSError's
# rendered text. A caller-owned dedup set of these lets the SAME poisoned
# record stay quiet on a repeat call while a NEW one -- a different path, a
# different side, or the same path failing a new way -- still warns. Same
# shape as cw.worktree._freshness.FetchWarningKey / _warn_fetch_skip_once
# (#2213); kept local rather than shared because the two key shapes are
# structurally different and each module already owns its own failure
# domain (#2240).
type UnresolvablePathWarningKey = tuple[str, str, str]


def _warn_unresolvable_path_once(
    warned_unresolvable: set[UnresolvablePathWarningKey] | None,
    warn_key: UnresolvablePathWarningKey,
    message: str,
    *args: object,
) -> None:
    """Log *message* at WARNING once per *warn_key*, then remember the key.

    ``None`` for *warned_unresolvable* (a one-shot caller) always warns and
    remembers nothing -- same contract as
    :func:`cw.worktree._freshness._warn_fetch_skip_once`.
    """
    if warned_unresolvable is not None and warn_key in warned_unresolvable:
        return
    _log.warning(message, *args)
    if warned_unresolvable is not None:
        warned_unresolvable.add(warn_key)


def _normalize_records(
    paths: Iterable[Path],
    kind: Literal["session", "worker"],
    warned_unresolvable: set[UnresolvablePathWarningKey] | None,
) -> tuple[set[Path], int]:
    """Normalize each of *paths*, skipping (and counting) any that raise ``OSError``.

    An individual bad session/worker record must not veto an unrelated
    target (#2240) -- only the caller's own target path fails closed on
    its own ``OSError`` (see :func:`live_home_reason`). A skip still makes
    the overall answer conservative: the caller folds the count back into
    "cannot rule out a live session" when the target does not otherwise
    match a successfully normalized home. Each skip is logged once per
    ``(kind, path, error)`` via *warned_unresolvable* so a persistently
    poisoned record is diagnosable rather than silently invisible.
    """
    homes: set[Path] = set()
    skipped = 0
    for path in paths:
        try:
            homes.add(_normalize_path(path))
        except OSError as exc:
            skipped += 1
            _warn_unresolvable_path_once(
                warned_unresolvable,
                (kind, str(path), str(exc)),
                "live_home_reason: %s record %s could not be resolved (%s);"
                " skipping it rather than reading every worktree as occupied",
                kind,
                path,
                exc,
            )
    return homes, skipped


def _home_match_reason(
    target: Path, session_homes: set[Path], worker_homes: set[Path], skipped: int
) -> str | None:
    """Compare *target* against normalized homes; explain a skip as occupied.

    Split out of :func:`live_home_reason` to keep its own return count within
    the PLR0911 budget (CLAUDE.md) -- the added skip-count branch pushed the
    combined function over it.
    """
    if target in session_homes:
        return "a live session is homed on this worktree"
    if target in worker_homes:
        return "a live daemon worker is homed on this worktree"
    if skipped:
        plural = "" if skipped == 1 else "s"
        return (
            f"{skipped} recorded path{plural} cannot be resolved, "
            "cannot rule out a live session"
        )
    return None


def live_home_reason(
    wt_path: Path,
    *,
    daemon: NativeDaemonClient,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> str | None:
    """Return why a live session or daemon worker may be homed on *wt_path*.

    The one liveness predicate for a worktree, public because two paths must
    agree on it: the same-branch reuse refresh (:func:`_reuse_occupancy`) and
    the dispatch claim's stale-worktree handler (``cw.dispatch.claim``), which
    must not force-remove a wrong-branch tree a live worker is homed on (#2213).
    Both fail closed.

    Consults BOTH sources and reports occupied when either says so:

    - cw's persisted session state (:func:`live_session_worktree_paths`), and
    - *daemon*'s live workers, each recorded with the ``cwd`` it was spawned in
      (:meth:`~cw.native_daemon.NativeDaemonClient.list_live_worker_cwds`) --
      a worker can be live in the roster before, or after, cw state reflects
      it. *daemon* is the caller's own client -- never defaulted here -- so a
      test that injects :class:`~cw.native_daemon.FakeNativeDaemonClient` is
      actually consulted instead of this function silently reading the host's
      real roster.

    Fails closed on the TARGET path exactly as before: any ``OSError``
    other than the deliberate ``FileNotFoundError`` carve-out (see
    :func:`_normalize_path`) reads as "cannot rule out a live session".

    An individual session or worker RECORD that cannot be normalized does
    NOT, on its own, veto this call (#2240) -- it is skipped and counted
    instead, and logged once (deduped via *warned_unresolvable*, see
    :func:`_normalize_records`). Because a skipped record's true path is
    unknown, the target cannot be proven to differ from it, so the answer
    is still not "definitely free": if the target matched no successfully
    normalized home AND at least one record was skipped, this still
    returns occupied. That preserves the fail-closed posture for the case
    that actually warrants it, without ONE bad record silently vetoing
    EVERY unrelated worktree's occupancy check the way it did before
    #2240 -- the difference is diagnosability (a WARNING now names the
    broken record), not a change in the free/occupied verdict for a
    target that shares no relation to the poisoned record.

    *warned_unresolvable* defaults to ``None`` (always warn, dedup
    nothing) -- the dispatch loop's stale-worktree-occupancy check
    (:func:`cw.dispatch.claim._raise_if_stale_tree_occupied`) threads a
    process-lifetime-owned set through six call layers, mirroring
    ``dispatch_tick``'s ``warned_fetch_fail`` (see
    :func:`cw.dispatch.loop._run_dispatch_loop_body`); the reuse-refresh
    call site (:func:`_reuse_occupancy`) has no such loop-lifetime set and
    keeps the always-warn default.
    """
    sessions = live_session_worktree_paths()
    if sessions is None:
        return "session state unreadable, cannot rule out a live session"
    workers = daemon.list_live_worker_cwds()
    if workers is None:
        return "daemon roster unreadable, cannot rule out a live session"
    try:
        target = _normalize_path(wt_path)
    except OSError as exc:
        return f"a path cannot be resolved ({exc}), cannot rule out a live session"

    session_homes, skipped_sessions = _normalize_records(
        sessions, "session", warned_unresolvable
    )
    worker_homes, skipped_workers = _normalize_records(
        workers, "worker", warned_unresolvable
    )
    return _home_match_reason(
        target, session_homes, worker_homes, skipped_sessions + skipped_workers
    )


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
