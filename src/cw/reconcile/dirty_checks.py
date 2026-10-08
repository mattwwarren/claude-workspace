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
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from stat import S_ISDIR
from typing import TYPE_CHECKING, Literal

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
type _GenerationEntry = tuple[str, int, int, int, int, int]
type _WorktreeGeneration = tuple[_GenerationEntry, ...] | Literal["unavailable"]
type _GenerationValidation = Literal["same", "changed", "unavailable"]

_UNAVAILABLE_GENERATION: Literal["unavailable"] = "unavailable"


class DirtyCheckUnavailableError(Exception):
    """No usable dirty check for a session: capped, missing, mismatched or stale.

    A plain ``Exception``, deliberately not a ``CwError``, so no caller's broad
    ``except CwError`` swallows it: the consumers catch it by name and defer
    the session to the next tick.
    """

    def __init__(self, message: str, *, stop_capture: bool = True) -> None:
        super().__init__(message)
        self.stop_capture = stop_capture


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


def _worktree_generation(worktree: Path) -> _WorktreeGeneration:
    """Return a metadata generation for files and Git state in *worktree*.

    This runs during the lockless capture pass. The returned entries include
    directories, so checking their metadata later detects additions and
    removals without recursively scanning the worktree under the lock. Git's
    HEAD, index and refs metadata are included too, so a commit or ref change
    invalidates a clean capture. A missing worktree is a valid generation (the
    dirty helper treats it as clean); filesystem errors are unavailable.
    """
    try:
        root = worktree.lstat()
    except FileNotFoundError:
        # A missing worktree is a stable, valid generation.
        return ()
    except OSError:
        return _UNAVAILABLE_GENERATION
    root_entry = (
        "",
        root.st_mtime_ns,
        root.st_ctime_ns,
        root.st_size,
        root.st_mode,
        root.st_ino,
    )
    if not worktree.is_dir():
        return (root_entry,)
    return _scan_worktree_generation(worktree, root_entry)


def _scan_worktree_generation(
    worktree: Path, root_entry: _GenerationEntry
) -> _WorktreeGeneration:
    entries: list[_GenerationEntry] = [root_entry]
    directories = [worktree]
    try:
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as children:
                for child in children:
                    path = Path(child.path)
                    if ".git" in path.relative_to(worktree).parts:
                        continue
                    stat = child.stat(follow_symlinks=False)
                    entries.append(
                        (
                            str(path.relative_to(worktree)),
                            stat.st_mtime_ns,
                            stat.st_ctime_ns,
                            stat.st_size,
                            stat.st_mode,
                            stat.st_ino,
                        )
                    )
                    if S_ISDIR(stat.st_mode):
                        directories.append(path)
        git_entries = _git_metadata_generation(worktree)
        if git_entries == _UNAVAILABLE_GENERATION:
            return _UNAVAILABLE_GENERATION
        return tuple(sorted((*entries, *git_entries)))
    except OSError:
        # Other failures must not be represented by a value that can compare
        # equal later.
        return _UNAVAILABLE_GENERATION


def _git_metadata_generation(
    worktree: Path,
) -> list[_GenerationEntry] | Literal["unavailable"]:
    """Return cheap metadata entries whose changes invalidate Git state.

    Only Git's control files are traversed here; the worktree file snapshot is
    handled by the caller. ``refs`` directory entries are included so both
    existing ref updates and newly-created refs change the generation.
    """
    marker = worktree / ".git"
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        return _UNAVAILABLE_GENERATION

    git_dir = _git_dir_from_marker(marker, marker_stat)
    if git_dir == _UNAVAILABLE_GENERATION:
        return _UNAVAILABLE_GENERATION
    entries = (
        []
        if S_ISDIR(marker_stat.st_mode)
        else [_generation_entry(str(marker), marker_stat)]
    )

    metadata_roots = [git_dir]
    common = _common_git_dir(git_dir)
    if common == _UNAVAILABLE_GENERATION:
        return _UNAVAILABLE_GENERATION
    if common is not None:
        metadata_roots.append(common)

    for root in metadata_roots:
        try:
            entries.extend(_git_metadata_entries(root))
        except OSError:
            return _UNAVAILABLE_GENERATION
    return entries


def _git_dir_from_marker(
    marker: Path, marker_stat: os.stat_result
) -> Path | Literal["unavailable"]:
    if S_ISDIR(marker_stat.st_mode):
        return marker
    try:
        contents = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return _UNAVAILABLE_GENERATION
    prefix = "gitdir:"
    if not contents.lower().startswith(prefix):
        return _UNAVAILABLE_GENERATION
    git_dir = Path(contents[len(prefix) :].strip())
    return git_dir if git_dir.is_absolute() else marker.parent / git_dir


def _common_git_dir(git_dir: Path) -> Path | None | Literal["unavailable"]:
    commondir = git_dir / "commondir"
    try:
        commondir.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNAVAILABLE_GENERATION
    try:
        common = Path(commondir.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return _UNAVAILABLE_GENERATION
    return common if common.is_absolute() else git_dir / common


def _git_metadata_entries(root: Path) -> list[_GenerationEntry]:
    entries: list[_GenerationEntry] = []
    for name in ("HEAD", "index", "packed-refs", "commondir"):
        path = root / name
        try:
            stat = path.lstat()
        except FileNotFoundError:
            continue
        entries.append(_generation_entry(str(path), stat))

    refs = root / "refs"
    try:
        refs_stat = refs.lstat()
    except FileNotFoundError:
        return entries
    entries.append(_generation_entry(str(refs), refs_stat))
    directories = [refs]
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as children:
            for child in children:
                path = Path(child.path)
                stat = child.stat(follow_symlinks=False)
                entries.append(_generation_entry(str(path), stat))
                if S_ISDIR(stat.st_mode):
                    directories.append(path)
    return entries


def _generation_entry(path: str, stat: os.stat_result) -> _GenerationEntry:
    return (
        path,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
        stat.st_mode,
        stat.st_ino,
    )


def _revalidate_generation(
    worktree: Path, generation: tuple[_GenerationEntry, ...]
) -> _GenerationValidation:
    """Check a captured generation with bounded, non-recursive metadata reads."""
    if not generation:
        try:
            worktree.lstat()
        except FileNotFoundError:
            return "same"
        except OSError:
            return "unavailable"
        return "changed"
    for relative, mtime_ns, ctime_ns, size, mode, inode in generation:
        path = worktree / relative if relative else worktree
        validation = _validate_generation_entry(
            path, (mtime_ns, ctime_ns, size, mode, inode)
        )
        if validation != "same":
            return validation
    return "same"


def _validate_generation_entry(
    path: Path, expected: tuple[int, int, int, int, int]
) -> _GenerationValidation:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "changed"
    except OSError:
        return "unavailable"
    actual = (
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
        stat.st_mode,
        stat.st_ino,
    )
    return "same" if actual == expected else "changed"


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
        self._generations: dict[_DirtyKey, _WorktreeGeneration] = {}

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
            self._store.ensure_capture_available()
        except ProbeStoreUnavailableError as err:
            raise self._capture_unavailable(err, session) from err
        generation = _worktree_generation(worktree)
        if generation == _UNAVAILABLE_GENERATION:
            msg = f"the worktree generation was unavailable for {_who(session)}"
            raise DirtyCheckUnavailableError(msg, stop_capture=False)
        try:
            reason = self._store.capture(
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
        else:
            self._generations[(session.id, worktree)] = generation
            return reason

    def _capture_unavailable(
        self, err: ProbeStoreUnavailableError, session: Session
    ) -> DirtyCheckUnavailableError:
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
        return DirtyCheckUnavailableError(msg)

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
            reason = self._store.lookup((session.id, worktree))
            key = (session.id, worktree)
            before = self._generations.get(key)
            if before is None or before == _UNAVAILABLE_GENERATION:
                msg = f"the worktree generation was unavailable for {_who(session)}"
                raise DirtyCheckUnavailableError(msg)
            validation = _revalidate_generation(worktree, before)
            if validation == "unavailable":
                msg = f"the worktree generation was unavailable for {_who(session)}"
                raise DirtyCheckUnavailableError(msg)
            if validation == "changed":
                msg = f"the worktree changed since its dirty check for {_who(session)}"
                raise DirtyCheckUnavailableError(msg)
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
        else:
            return reason

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
                if not exc.stop_capture:
                    _log.warning(
                        "reconcile: dirty-check capture unavailable for %s;"
                        " continuing with the remaining session(s)",
                        session.id,
                    )
                    continue
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
