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

A captured ``None`` (clean) is a hit. The live helper no longer reads its own
errors as clean (#2662); it sorts each failure by cause:

- **Clean.** No path, or a missing worktree directory (nothing to protect).
- **Park, named.** A per-row deterministic cause -- an existing directory
  whose checked-out branch is unresolved (detached HEAD or not a git
  worktree), or a client absent from a non-empty ``clients.yaml`` -- comes
  back as a ``dirty check unavailable: <cause>`` reason string, so it is
  captured as dirty and parks the row at once through the existing dirty
  machinery. A git failure inside ``unsaved_work_reason`` still comes back
  as a reason string and parks, as before.
- **Defer, signalled.** A global, transient or unrecognized cause (an
  unreadable or invalid ``clients.yaml``, a missing or failing ``git``, any
  ``OSError``) raises ``WorktreeProbeError``. :meth:`DirtyChecks.capture`
  turns it into :class:`DirtyCheckDeferredError` and stores nothing, so the
  in-lock lookup misses and the row defers exactly like any other miss;
  :meth:`DirtyChecks.capture_all` logs ``dirty_check_deferred`` and goes on
  to the next session. :meth:`DirtyChecks.settle_outages`, run once per tick,
  emits one ``worktree_probe_unavailable`` attention event per failure kind
  on the edge (the kind appears after a tick without it), stays silent while
  it persists, and mutates no session or queue state.
- **Ordering.** Within one capture pass, sessions that deferred on the
  previous tick are captured after those that did not. The order does not
  span the backstop and phantom passes, which share one per-tick cap. Both
  the edge latch and that order are in-process state; nothing is persisted
  and no clock is compared.

Anything the helper does not classify propagates out of the dirty-check
capture and aborts that ``reconcile()`` call.

TOCTOU: the in-lock check used to run milliseconds before the act; a capture
is now typically seconds old, at most ``DIRTY_CHECK_MAX_AGE_SECONDS``. An
orphaned process could dirty the worktree after capture, and a capture inside
the bound is consumed as a hit (one at least that old is stale and defers), so
an in-bound clean capture is not deferred. The
freshness bound limits that window and a phantom has already been off the
roster for at least the 30 s spawn grace. What the window can cost is
bounded: no reconcile act removes a worktree; for the phantom sweep and
backstops the capture only selects the queue disposition (#1747, ADR-0014;
pinned by ``tests/test_reconcile_worktree_removal_guard.py``). Post-lock
surface stops do not read or depend on the dirt value (a merged phantom's
candidacy still requires a usable capture; a miss defers it). The residual is
an in-bound clean capture routing a row to PENDING, not BLOCKED_ON_USER (on
a ``reap_policy: auto`` lane for the phantom sweep, on any lane for the
backstops): the next claim's ``_reuse_occupancy`` refuses to move HEAD under
unsaved work, but does not block re-dispatch into a dirty tree under
``allow_dirty_reuse=True``. ("Destructive" here means on-disk work loss;
the RUNNING -> PENDING revert still consumes a capture up to
``DIRTY_CHECK_MAX_AGE_SECONDS`` old.) The accepted tradeoff stays
block > clobber. Accepted explicitly (#2671); see ADR-0006 Amendment (#2630).

A leaf module: it imports ``cw.reconcile._shared``,
``cw.reconcile.probe_store`` and ``cw.events``, and nothing from ``tasks``,
``phantom`` or ``core``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType, SessionOrigin
from cw.reconcile import _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    WorktreeProbeError,
    _looks_like_daemon_outage,
    compute_drift,
)
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from cw.models import CwState, Session
    from cw.reconcile._shared._worktree_evidence import WorktreeProbeKind

_log = logging.getLogger(__name__)

# Most live dirty checks one capture pass runs. Each is a few git subprocesses
# (about 1 s normally, up to ~10 s on a slow disk) and the pass also runs for
# ``cw start``/``cw doctor --reap``. Crashed or silently completed
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

# ``paused_status`` of the attention event a deferred dirty check emits once
# per outage episode (#2662). Imported by ``cw.attention_watch``.
WORKTREE_PROBE_UNAVAILABLE_STATUS = "worktree_probe_unavailable"

# In-process outage state, settled once per tick by
# ``DirtyChecks.settle_outages`` (#2662). ``reconcile()`` builds a fresh
# ``DirtyChecks`` each call, so this lives at module level; it runs on poller
# and anyio threads, so swaps and reads hold ``_OUTAGE_LOCK``.
# Failure kinds already announced in the current episode (the edge latch),
# each mapped to the token of the settle that claimed it. A failed emit rolls
# back only a claim it still owns, so it cannot undo a later settle's claim.
_ANNOUNCED_OUTAGES: dict[WorktreeProbeKind, object] = {}
# Session ids that deferred on the previous tick; captured last next tick.
_DEFERRED_LAST_TICK: set[str] = set()
_OUTAGE_LOCK = threading.Lock()


class DirtyCheckUnavailableError(Exception):
    """No usable dirty check for a session: capped, missing, mismatched or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: the consumers catch it by name and defer
    the session to the next tick.
    """


class DirtyCheckDeferredError(DirtyCheckUnavailableError):
    """One session's dirty check could not run and is deferred (#2662).

    Raised by :meth:`DirtyChecks.capture` for a ``WorktreeProbeError``; it
    carries the failure ``kind`` and ``detail``. A subclass so the consumers
    that catch the base keep deferring, while :meth:`DirtyChecks.capture_all`
    continues past it instead of stopping the pass.
    """

    def __init__(self, message: str, *, kind: WorktreeProbeKind, detail: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _Outage:
    """The first session in capture order that hit one failure kind this tick."""

    kind: WorktreeProbeKind
    client: str
    detail: str


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
        # Per failure kind, the first deferred session this tick (#2662).
        self._outages: dict[WorktreeProbeKind, _Outage] = {}
        # Sessions whose capture deferred this tick.
        self._deferred_ids: set[str] = set()
        # True once a pass stopped at the cap or budget: it did not observe
        # every candidate, so an absent kind is unobserved, not recovered.
        self._truncated = False

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
        git once the budget is spent or the per-tick cap is reached, and
        ``DirtyCheckDeferredError`` (storing nothing, after spending a cap
        slot) when the helper raises ``WorktreeProbeError``; the outage is
        recorded for :meth:`settle_outages`.
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
        except WorktreeProbeError as err:
            self._outages.setdefault(
                err.kind,
                _Outage(kind=err.kind, client=session.client, detail=err.detail),
            )
            self._deferred_ids.add(session.id)
            msg = f"the dirty check for {_who(session)} could not run: {err}"
            raise DirtyCheckDeferredError(
                msg, kind=err.kind, detail=err.detail
            ) from err
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
        """Capture *sessions*, stopping at the first budget/cap refusal.

        Order: the incoming order, except that sessions which deferred on the
        previous tick go after those that did not (a stable sort, so each
        group keeps its incoming order), so within this pass a persistently
        failing row does not starve healthy ones of the cap (#2662). The
        order does not span the backstop and phantom passes, which share one
        per-tick cap. A deferred session logs one
        ``dirty_check_deferred`` warning and the pass continues. A cap or
        budget refusal logs one warning naming how many worktree sessions
        were left unchecked; those defer in-lock. Sessions without a worktree
        are not counted.
        """
        pending = [s for s in sessions if s.worktree_path is not None]
        with _OUTAGE_LOCK:
            deferred_last_tick = frozenset(_DEFERRED_LAST_TICK)
        pending.sort(key=lambda s: s.id in deferred_last_tick)
        for index, session in enumerate(pending):
            try:
                self.capture(session)
            except DirtyCheckDeferredError as exc:
                _log.warning(
                    "dirty_check_deferred: session=%s name=%s kind=%s detail=%s;"
                    " deferring to the next tick",
                    session.id,
                    session.name,
                    exc.kind,
                    exc.detail,
                )
            except DirtyCheckUnavailableError as exc:
                self._truncated = True
                _log.warning(
                    "reconcile: dirty-check capture stopped: %s; %d of %d"
                    " session(s) left unchecked and deferred to the next tick",
                    exc,
                    len(pending) - index,
                    len(pending),
                )
                return

    def settle_outages(self) -> None:
        """Apply this tick's outages to the in-process edge latch. Lockless only.

        Called once per tick, after every capture pass (#2662). A failure kind
        present this tick and not yet announced emits one
        ``worktree_probe_unavailable`` attention event and is latched; one
        already latched stays silent; a latched kind absent this tick is
        dropped, ending its episode. The sessions that deferred this tick
        replace the previous tick's set, which orders them last next tick.

        A tick whose pass stopped at the cap or budget did not observe every
        candidate, so it only adds: it announces new kinds but drops none, and
        unions its deferred sessions into the set instead of replacing it.
        The fresh kinds are claimed (latched) in the same critical section
        that computes them, each tagged with this settle's own token, and
        emitted outside the lock in sorted order. Two concurrent settles
        cannot both announce one episode, and a clean settle running during
        an emission cannot be undone by a late latch. A claim is rolled back
        only while the latch still holds this settle's token, so a failed
        emit never clears a claim a later settle made after a clean tick
        ended the episode. An emit that fails with ``OSError`` is logged and
        its claim rolled back, leaving the kind unlatched so the next tick
        retries it. Any other exception from the emit rolls back this
        settle's claims for the kind that failed and every kind not yet
        emitted, then propagates, so no episode stays silently latched.
        """
        present = set(self._outages)
        token = object()
        with _OUTAGE_LOCK:
            if not self._truncated:
                for kind in [k for k in _ANNOUNCED_OUTAGES if k not in present]:
                    del _ANNOUNCED_OUTAGES[kind]
                _DEFERRED_LAST_TICK.clear()
            _DEFERRED_LAST_TICK.update(self._deferred_ids)
            fresh = sorted(present - _ANNOUNCED_OUTAGES.keys())
            for kind in fresh:
                _ANNOUNCED_OUTAGES[kind] = token
        for index, kind in enumerate(fresh):
            try:
                sent = _announce(self._outages[kind])
            except BaseException:
                _release_claims(fresh[index:], token)
                raise
            if not sent:
                _release_claims([kind], token)


def _release_claims(kinds: Iterable[WorktreeProbeKind], token: object) -> None:
    """Drop the latch entries in *kinds* that *token* still owns."""
    with _OUTAGE_LOCK:
        for kind in kinds:
            if _ANNOUNCED_OUTAGES.get(kind) is token:
                del _ANNOUNCED_OUTAGES[kind]


def _announce(outage: _Outage) -> bool:
    """Emit the ``worktree_probe_unavailable`` event for *outage*; True if sent.

    Client-scoped, not session-scoped: the cause is host-wide, so the event is
    attributed to the first affected session's client and the firing instant
    is folded into the synthetic ``session_id``. Mutates nothing and sends no
    push notification.
    """
    payload: dict[str, object] = {
        "session_id": f"worktree-probe:{outage.kind}@{datetime.now(UTC).isoformat()}",
        "session_name": "",
        "client": outage.client,
        "ticket_id": None,
        "claude_session_id": None,
        "paused_status": WORKTREE_PROBE_UNAVAILABLE_STATUS,
        "breadcrumbs": f"{outage.kind}: {outage.detail}",
        "crashed": False,
        "lane": None,
    }
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=None,
        )
    except OSError as exc:
        _log.warning(
            "reconcile: %s event for kind=%s was not recorded: %s; retrying next tick",
            WORKTREE_PROBE_UNAVAILABLE_STATUS,
            outage.kind,
            exc,
        )
        return False
    return True


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
