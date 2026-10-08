"""Lockless capture of the worktree dirty checks (#2548).

The phantom sweep and the TIMED_OUT/COMPLETED task backstops both ask, before
they revert a RUNNING row, whether the session's worktree has unsaved work: a
dirty worktree parks the row BLOCKED_ON_USER instead of reverting it to
PENDING (#421). That check runs git (``git branch --show-current``, ``git
status --porcelain`` and the unpushed-commit ladder), and none of it may run
under ``sessions_lock`` (ADR-0019 invariant 3). So it is split the way the
codex clean probes (#2563) and the plan prefetch (#2545) are:

- **Capture, lockless.** ``reconcile()`` derives the sessions the in-lock
  sweeps will check -- backstop sessions with a RUNNING row first, then the
  crash-tail phantoms -- and runs each live check into a :class:`DirtyChecks`,
  bounded by ``DIRTY_CHECK_MAX_PER_TICK`` checks and
  ``DIRTY_CHECK_BUDGET_SECONDS``.
- **Consume, in-lock.** The sweeps call :func:`lookup_dirty_reason` /
  :func:`partition_dirty`, which never run git.
- **Defer on a miss.** A session with no usable capture (never captured,
  capped out, budget spent, captured at another worktree path, or at least
  ``DIRTY_CHECK_MAX_AGE_SECONDS`` old) logs one ``dirty_check_unavailable``
  warning and is skipped for the tick: its row stays RUNNING and bound, its
  session is untouched, nothing is emitted, and the next tick retries. A miss
  never parks; only a completed check that found unsaved work does.

A captured ``None`` (clean) is a hit. The live helper is unchanged: an error
escaping to its own fail-safe is captured as clean, and a git failure inside
``unsaved_work_reason`` comes back as a reason string, so it is captured as
dirty -- both exactly as the in-lock check behaved.

TOCTOU: the in-lock check used to run milliseconds before the act; a capture
is now typically seconds old, at most ``DIRTY_CHECK_MAX_AGE_SECONDS``. An
orphaned process could dirty the worktree after capture. The freshness bound
limits that window, a phantom has already been off the roster for at least
the 30 s spawn grace, and a stale capture only defers. The accepted tradeoff
stays block > clobber.

A leaf module: it imports ``cw.reconcile._shared`` and
``cw.reconcile.probe_store`` and nothing from ``tasks``, ``phantom`` or
``core``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import timedelta
from typing import TYPE_CHECKING

from cw.models import SessionOrigin
from cw.reconcile import _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _looks_like_daemon_outage,
    compute_drift,
)
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
    from pathlib import Path

    from cw.models import CwState, Session

_log = logging.getLogger(__name__)

# Most live dirty checks one capture pass runs. Each is a few git subprocesses
# (about 1 s normally, up to ~10 s on a slow disk) and the pass also runs for
# ``cw status``/``list``/``start``/``doctor``. Crashed or silently completed
# workers with a bound row are operator-scale; a capped-out session defers a
# tick, and captured ones leave the candidate set within one tick.
DIRTY_CHECK_MAX_PER_TICK = 12
# Wall-clock budget for one capture pass, checked before each check (git has
# no subprocess timeout here, so one in-flight check can overrun it).
DIRTY_CHECK_BUDGET_SECONDS: float = 30.0
# How old a capture may be when consumed under the lock: the 30 s capture
# budget (the first capture of a pass is that old when the pass ends), plus
# one in-flight check (~10 s), plus the lock wait and the in-lock sweeps that
# run before the consumers (the roster call alone is capped at 15 s), with
# margin. Strict ``<``: a capture exactly this old is stale, and a negative
# age (the clock went backwards) is stale too. A stale capture, clean or
# dirty, only defers its session; it never authorizes an act.
DIRTY_CHECK_MAX_AGE_SECONDS: float = 90.0
# How far the pre-pass shifts the grace boundaries (the 60 s completion grace
# and the 30 s spawn grace) forward, so a session that crosses one between the
# pre-pass and the lock is captured instead of deferred. Covers the usual
# pre-pass-to-lock gap (the roster call alone is capped at 15 s); kept small
# so sessions the dispatch consumer is still routing are not git-checked. A
# noise optimization only: an uncaptured session defers one tick.
DIRTY_CHECK_LOOKAHEAD_SECONDS: float = 15.0

# Reads the live daemon roster (``claude agents --json``); lockless only.
type RosterReader = Callable[[], list[dict[str, object]]]

type _DirtyKey = tuple[str, Path]


class DirtyCheckUnavailableError(Exception):
    """No usable dirty check for a session: capped, missing, mismatched or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: the consumers catch it by name and defer
    the session to the next tick.
    """


def _who(session: Session) -> str:
    return f"{session.name} ({session.id})"


def normalize_roster(
    agents: list[dict[str, object]],
) -> tuple[set[str], dict[str, str]]:
    """Return the short live ids and full ids from a native roster."""
    surface_to_full = {
        sid[:8]: sid for a in agents if isinstance(sid := a.get("sessionId"), str)
    }
    return set(surface_to_full), surface_to_full


class DirtyChecks:
    """Worktree dirty checks captured lockless, keyed by session and worktree.

    One pass captures (:meth:`capture`, runs git, lockless only), then the
    in-lock consumers look up (:meth:`lookup`, never runs git). The payload
    is the unsaved-work reason, or ``None`` for a clean worktree. With
    *budget_seconds* set, captures stop once that much monotonic time has
    passed since construction; with *max_captures* set, they stop after that
    many live checks.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None = None,
        max_captures: int | None = None,
    ) -> None:
        self._store: BoundedProbeStore[_DirtyKey, str | None] = BoundedProbeStore(
            budget_seconds=budget_seconds,
            max_captures=max_captures,
            max_age_seconds=DIRTY_CHECK_MAX_AGE_SECONDS,
        )

    @property
    def budget_seconds(self) -> float | None:
        return self._store.budget_seconds

    @property
    def max_captures(self) -> int | None:
        return self._store.max_captures

    @property
    def captures(self) -> int:
        """How many live checks this store has attempted, failed ones included."""
        return self._store.captures

    def capture(self, session: Session) -> str | None:
        """Run *session*'s dirty check live and keep it. Lockless only.

        A session with no worktree has nothing to check: ``None``, without
        spending the cap. Raises ``DirtyCheckUnavailableError`` before running
        git once the budget is spent or the per-tick cap is reached.
        """
        worktree = session.worktree_path
        if worktree is None:
            return None
        try:
            return self._store.capture(
                (session.id, worktree),
                read=lambda _captured_at: _shared.worktree_dirty_reason_by_path(
                    session.client, worktree
                ),
            )
        except ProbeStoreUnavailableError as err:
            if err.reason == "budget":
                msg = (
                    f"the {self.budget_seconds or 0:.0f}s dirty-check budget is"
                    f" spent; {_who(session)} was not checked"
                )
            else:
                msg = (
                    f"the {self.max_captures}-check per-tick dirty-check cap is"
                    f" reached; {_who(session)} was not checked"
                )
            raise DirtyCheckUnavailableError(msg) from err

    def lookup(self, session: Session) -> str | None:
        """Return *session*'s captured dirty reason if usable. Never runs git.

        Usable means captured for this same session id and worktree path, and
        aged in ``[0, DIRTY_CHECK_MAX_AGE_SECONDS)``. A session with no
        worktree is clean. Otherwise raises ``DirtyCheckUnavailableError``.
        """
        worktree = session.worktree_path
        if worktree is None:
            return None
        try:
            return self._store.lookup((session.id, worktree))
        except ProbeStoreUnavailableError as err:
            if err.age is None:
                msg = (
                    f"no dirty check was captured for {_who(session)} at this worktree"
                )
            else:
                msg = (
                    f"the dirty check for {_who(session)} is unusable at age"
                    f" {err.age:.1f}s"
                )
            raise DirtyCheckUnavailableError(msg) from err

    def capture_all(self, sessions: Iterable[Session]) -> None:
        """Capture *sessions* in order, stopping at the first budget/cap refusal.

        Logs one warning naming how many worktree sessions were left
        unchecked; those defer in-lock. Sessions without a worktree are not
        counted.
        """
        pending = [s for s in sessions if s.worktree_path is not None]
        for index, session in enumerate(pending):
            try:
                self.capture(session)
            except DirtyCheckUnavailableError as exc:
                _log.warning(
                    "reconcile: dirty-check capture stopped: %s; %d of %d"
                    " session(s) left unchecked and deferred to the next tick",
                    exc,
                    len(pending) - index,
                    len(pending),
                )
                return


def lookup_dirty_reason(checks: DirtyChecks | None, session: Session) -> str | None:
    """The in-lock dirty check: *session*'s captured reason, never git.

    ``None`` means nothing was captured, so every worktree session misses. A
    miss logs one ``dirty_check_unavailable`` warning and re-raises
    ``DirtyCheckUnavailableError`` for the caller to defer the session.
    """
    store = checks if checks is not None else DirtyChecks()
    try:
        return store.lookup(session)
    except DirtyCheckUnavailableError as exc:
        _log.warning(
            "dirty_check_unavailable: session=%s name=%s;"
            " deferring to the next tick: %s",
            session.id,
            session.name,
            exc,
        )
        raise


def partition_dirty(
    checks: DirtyChecks | None, sessions: Iterable[Session]
) -> tuple[dict[str, str], set[str]]:
    """Split *sessions* by their captured dirty check. Never runs git.

    Returns ``(dirty_reasons_by_session_id, deferred_session_ids)``. A clean
    hit appears in neither; a miss is deferred (and logged once).
    """
    dirty: dict[str, str] = {}
    deferred: set[str] = set()
    for session in sessions:
        try:
            reason = lookup_dirty_reason(checks, session)
        except DirtyCheckUnavailableError:
            deferred.add(session.id)
            continue
        if reason is not None:
            dirty[session.id] = reason
    return dirty, deferred


def prepass_phantom_ids(
    state: CwState,
    *,
    roster: RosterReader,
    acting: frozenset[str],
    now: datetime,
) -> set[str]:
    """The phantom set the in-lock sweep will likely see, derived lockless.

    The in-lock roster read happens under the lock, so the pre-pass makes its
    own. It is skipped (empty set) when no live DAEMON session has a worktree,
    so an idle tick costs no subprocess. A roster error, or an empty roster
    while live surfaces exist (a daemon outage, which the in-lock sweep
    aborts on too), also gives an empty set: never mass git. Sessions inside
    the spawn grace by less than ``DIRTY_CHECK_LOOKAHEAD_SECONDS`` are
    included; sessions with a mid-turn usage-limit act in flight (*acting*)
    are dropped, as the in-lock sweep drops them.
    """
    if not any(
        s.status in _LIVE_STATUSES
        and s.origin is SessionOrigin.DAEMON
        and s.worktree_path is not None
        for s in state.sessions
    ):
        return set()
    try:
        agents = roster()
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return set()
    native_live, _surface_to_full = normalize_roster(agents)
    if _looks_like_daemon_outage(state, False, native_live):
        return set()
    lookahead = now + timedelta(seconds=DIRTY_CHECK_LOOKAHEAD_SECONDS)
    drift = compute_drift(state, native_live, now=lookahead)
    return set(drift.phantom_session_ids) - acting
