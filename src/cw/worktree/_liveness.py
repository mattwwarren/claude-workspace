"""Live-session and daemon-worker liveness for worktrees (#2213, #2480).

:func:`live_home_reason` is the one liveness predicate for a worktree: the
reuse refresh's occupancy check (:mod:`cw.worktree._occupancy`) and the
dispatch claim's stale-worktree handler both consult it.
:func:`live_session_worktree_paths` is the session-state half, shared with the
worktree GC, which treats an unreadable state file the opposite way.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Literal

from cw.config import load_state
from cw.models import SessionStatus
from cw.worktree._refresh_types import _LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient

# Pinned to the pre-split module name so operator log filters on
# ``cw.worktree._refresh`` keep matching (#2569).
_log = logging.getLogger(_LOGGER_NAME)


_LIVE_SESSION_HOMED_REASON = "a live session is homed on this worktree"
_LIVE_DAEMON_WORKER_HOMED_REASON = "a live daemon worker is homed on this worktree"
_GENUINELY_LIVE_HOME_REASONS = frozenset(
    {_LIVE_SESSION_HOMED_REASON, _LIVE_DAEMON_WORKER_HOMED_REASON}
)


def is_genuinely_live_home_reason(reason: str | None) -> bool:
    """Return whether *reason* confirms a live session or daemon worker home."""
    return reason in _GENUINELY_LIVE_HOME_REASONS


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

    A dedicated module-level function (rather than a thin wrapper sharing a
    private state-loading helper with :func:`_non_terminal_session_surface_refs`
    below) so its existing test seam -- callers monkeypatch
    ``cw.worktree._liveness.live_session_worktree_paths`` directly to control
    what :func:`live_home_reason` sees for the session-homed side -- keeps
    working unchanged.
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


def _non_terminal_session_surface_refs() -> frozenset[str] | None:
    """Return ``surface_ref`` of every non-terminal cw session, or None (#2480).

    The companion :func:`live_home_reason` needs to tell a daemon-roster
    worker that still belongs to a live cw session apart from one whose
    owning session already finished (or never existed) -- matching by
    ``surface_ref`` rather than by worktree path, since a per-ticket
    worktree is reused by many sessions across its pipeline lifetime and a
    path match alone cannot tell which of them, if any, currently owns a
    given roster entry.

    Same fail-closed contract as :func:`live_session_worktree_paths`
    (``None`` means "cannot rule out a live session"), and deliberately a
    SEPARATE ``load_state()`` call rather than sharing one with it: the two
    have independent test seams (this one has none patched anywhere yet;
    ``live_session_worktree_paths`` is monkeypatched directly by many
    existing tests), and merging them would make ``live_home_reason`` stop
    consulting the monkeypatched ``live_session_worktree_paths`` seam that
    those tests depend on.
    """
    try:
        state = load_state()
    except _STATE_READ_ERRORS as exc:
        _log.warning(
            "live-path guard: failed to load session state for surface_ref lookup: %s",
            exc,
        )
        return None
    return frozenset(
        session.surface_ref
        for session in state.sessions
        if session.status in _NON_TERMINAL_SESSION_STATUSES
        and session.surface_ref is not None
    )


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
        return _LIVE_SESSION_HOMED_REASON
    if target in worker_homes:
        return _LIVE_DAEMON_WORKER_HOMED_REASON
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
    - *daemon*'s live workers whose ``surface_ref`` still names a non-terminal
      cw session (:meth:`~cw.native_daemon.NativeDaemonClient.
      list_live_worker_homes`) -- a worker can be live in the roster before
      cw state reflects it, so those are still counted. *daemon* is the
      caller's own client -- never defaulted here -- so a test that injects
      :class:`~cw.native_daemon.FakeNativeDaemonClient` is actually consulted
      instead of this function silently reading the host's real roster.

      A roster worker whose ``surface_ref`` maps to a session already in a
      TERMINAL status (COMPLETED/TIMED_OUT), or to no cw session at all
      (never tracked, or the record is gone), is NOT counted as occupying --
      it is a leaked daemon worker the session-completion stop should have
      cleaned up (#2480; see :mod:`cw.reconcile.leaked_workers` for the sweep
      that clears it). Without this filter a finished worker's idle roster
      entry reports its ticket's worktree occupied forever, and the ticket
      can never be re-dispatched.

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
    workers = daemon.list_live_worker_homes()
    if workers is None:
        return "daemon roster unreadable, cannot rule out a live session"
    surface_refs = _non_terminal_session_surface_refs()
    if surface_refs is None:
        return "session state unreadable, cannot rule out a live session"
    try:
        target = _normalize_path(wt_path)
    except OSError as exc:
        return f"a path cannot be resolved ({exc}), cannot rule out a live session"

    session_homes, skipped_sessions = _normalize_records(
        sessions, "session", warned_unresolvable
    )
    # Only a worker whose surface_ref still names a non-terminal cw session
    # counts as occupying -- a terminal-mapped or session-less roster entry
    # is a leaked worker, not a live one (#2480).
    live_worker_paths = (home.cwd for home in workers if home.short_id in surface_refs)
    worker_homes, skipped_workers = _normalize_records(
        live_worker_paths, "worker", warned_unresolvable
    )
    return _home_match_reason(
        target, session_homes, worker_homes, skipped_sessions + skipped_workers
    )
