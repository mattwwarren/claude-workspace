"""Bounded resolver for installed ``~/.claude`` files (#3662).

The worker-facing Step 2.5 fences and the Stage 2 appendix read files that
``scripts/install-skills.sh`` links into ``~/.claude``. A fast-forward of the
shared checkout can briefly leave such a link absent, dangling or zero-length
(git replaces a changed file by unlink then create), and the old ``[ -f ]``
probe either missed or accepted a half-written helper. This module classifies
each candidate, polls for a short bounded budget, and reports what it saw so
the next occurrence is self-explaining. It never raises on filesystem state
and never shells out.

Diagnostics only: nothing here changes which file is chosen. The first
candidate in the given order that classifies as ``ok`` wins, as before.
"""

from __future__ import annotations

import dataclasses
import os
import stat as stat_mod
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "DEFAULT_WAIT_S",
    "IN_FLIGHT_MARKERS",
    "POLL_INTERVAL_S",
    "WAIT_ENV_VAR",
    "CandidateReport",
    "Resolution",
    "State",
    "classify",
    "format_miss",
    "format_recovered",
    "format_report",
    "git_in_flight",
    "resolve_first_present",
]

DEFAULT_WAIT_S: Final = 10.0
POLL_INTERVAL_S: Final = 0.5
WAIT_ENV_VAR: Final = "CW_RESOLVE_INSTALLED_WAIT_S"
IN_FLIGHT_MARKERS: Final = ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply")

_GITDIR_PREFIX: Final = "gitdir:"


class State(StrEnum):
    """What one candidate path looks like right now."""

    OK = "ok"
    EMPTY = "empty"
    ABSENT = "absent"
    DANGLING = "dangling"
    NOT_FILE = "not_file"
    UNREADABLE = "unreadable"


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateReport:
    """Classification of one candidate, plus git state when it is a miss."""

    path: str
    state: State
    link_target: str | None = None
    in_flight: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of a bounded poll over an ordered candidate list."""

    path: str | None
    attempts: int
    elapsed_s: float
    first_miss: tuple[CandidateReport, ...]
    last: tuple[CandidateReport, ...]


def _link_target(path: Path) -> str | None:
    """Return the symlink target of *path*, or ``None`` if it cannot be read."""
    try:
        return str(path.readlink())
    except OSError:
        return None


def classify(path: str) -> CandidateReport:
    """Classify *path* by following ``stat``. Never raises."""
    candidate = Path(path)
    try:
        st = candidate.stat()
    except FileNotFoundError:
        if candidate.is_symlink():
            return CandidateReport(path, State.DANGLING, _link_target(candidate))
        return CandidateReport(path, State.ABSENT)
    except OSError:
        return CandidateReport(path, State.UNREADABLE)
    if not stat_mod.S_ISREG(st.st_mode):
        return CandidateReport(path, State.NOT_FILE)
    if st.st_size == 0:
        return CandidateReport(path, State.EMPTY)
    return CandidateReport(path, State.OK)


def _nearest_existing(path: Path) -> Path | None:
    """Return *path* or its nearest existing ancestor, or ``None``."""
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            return candidate
    return None


def _gitdir_of(dot_git: Path) -> Path | None:
    """Return the git directory named by a ``.git`` entry, or ``None``."""
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    try:
        first = dot_git.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith(_GITDIR_PREFIX):
        return None
    target = Path(first[len(_GITDIR_PREFIX) :].strip())
    if not target.is_absolute():
        target = dot_git.parent / target
    return target


def _find_git_dir(start: Path) -> Path | None:
    """Walk up from *start* to the first ``.git`` entry and resolve it."""
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if not os.path.lexists(dot_git):
            continue
        try:
            return _gitdir_of(dot_git)
        except OSError:
            return None
    return None


def git_in_flight(path: str) -> tuple[str, ...]:
    """Return the in-flight git markers of the repo that holds *path*.

    Pure filesystem walk: no subprocess, no ``GIT_*`` handling, never raises.
    A dangling link resolves to its target, and the walk starts from the
    nearest existing ancestor of that target, so the owning checkout is still
    found while its file is missing.
    """
    try:
        resolved = Path(os.path.realpath(path))
        anchor = _nearest_existing(resolved.parent)
        if anchor is None:
            return ()
        git_dir = _find_git_dir(anchor)
        if git_dir is None:
            return ()
        return tuple(m for m in IN_FLIGHT_MARKERS if os.path.lexists(git_dir / m))
    except (OSError, ValueError):
        return ()


def _annotate(reports: tuple[CandidateReport, ...]) -> tuple[CandidateReport, ...]:
    """Attach git in-flight markers to every candidate that is not ``ok``."""
    return tuple(
        r
        if r.state is State.OK
        else dataclasses.replace(r, in_flight=git_in_flight(r.path))
        for r in reports
    )


def resolve_first_present(
    candidates: Sequence[str],
    *,
    wait_s: float,
    interval_s: float = POLL_INTERVAL_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Resolution:
    """Poll *candidates* in order until one is ``ok`` or *wait_s* elapses.

    The first poll is always made. A miss sleeps for at most *interval_s*,
    clamped so the total slept time never exceeds *wait_s*. ``wait_s == 0``
    makes exactly one attempt and no sleep. ``first_miss`` records the state
    seen on the first poll, and is empty when that poll already hit.
    """
    do_sleep = sleep if sleep is not None else time.sleep
    now = clock if clock is not None else time.monotonic
    start = now()
    attempts = 0
    first_miss: tuple[CandidateReport, ...] = ()
    while True:
        attempts += 1
        reports = tuple(classify(c) for c in candidates)
        hit = next((r for r in reports if r.state is State.OK), None)
        if hit is not None:
            return Resolution(hit.path, attempts, now() - start, first_miss, reports)
        if attempts == 1:
            first_miss = _annotate(reports)
        elapsed = now() - start
        remaining = wait_s - elapsed
        if remaining <= 0:
            return Resolution(None, attempts, elapsed, first_miss, _annotate(reports))
        do_sleep(min(interval_s, remaining))


def format_report(report: CandidateReport) -> str:
    """Render one candidate as a single diagnostic line (no ``HOME``)."""
    detail = report.state.value
    if report.link_target is not None:
        detail += f" -> {report.link_target}"
    if report.in_flight:
        detail += f" (git in flight: {', '.join(report.in_flight)})"
    return f"{report.path}: {detail}"


def format_miss(resolution: Resolution, home: str) -> str:
    """Render the failure message: every candidate's last state, and HOME."""
    lines = [f"resolve-installed: no candidate present (HOME={home})"]
    lines.append(
        f"resolve-installed: {resolution.attempts} attempt(s) over "
        f"{resolution.elapsed_s:.1f}s"
    )
    lines.extend(f"resolve-installed: {format_report(r)}" for r in resolution.last)
    return "\n".join(lines)


def format_recovered(resolution: Resolution, home: str) -> str:
    """Render the recovery note for a hit that needed more than one poll."""
    head = (
        f"resolve-installed: recovered {resolution.path} after "
        f"{resolution.attempts} attempt(s) (HOME={home})"
    )
    misses = [
        f"resolve-installed: first miss: {format_report(r)}"
        for r in resolution.first_miss
        if r.state is not State.OK
    ]
    return "\n".join([head, *misses])
