"""Code identity of the running dispatch loop versus the checkout (#2729).

``cw dev-queue serve`` keeps its modules in memory for days. When the checkout
it was imported from moves on, the old loop keeps rewriting the dev queue with
the old models, and pydantic drops every row field the old code does not know
(#2702). This module records the loop's code identity at start and holds the
one comparison ``cw doctor`` (check ``dispatch-loop-code``) and
``cw dev-queue resolve``/``approve`` (a stderr line) both render.

The identity is the package version plus the commit of the source checkout,
written with the loop's pid and start time into the singleton lock file
(``cw.config.dispatch_loop_lock``), the one file only the holder writes. It is
recorded once per process and never refreshed: nothing polls the checkout, and
the loop never exits over a moved checkout (``_check_version_drift`` is the
only self-exit, and it compares dist-info versions only).

Known limits:

- HEAD only: uncommitted edits to the checkout are invisible.
- The identity is read at the first ``run_dispatch_loop`` entry, not at
  import, so a pull in the seconds between process start and that call is
  missed.
- The dist-info ``version`` of an editable install lags the checkout, so the
  commit decides whenever both sides have one; the versions are compared only
  when neither side has a commit (a wheel or registry install). A commit on
  exactly one side is unknown, never a version match.
- The loop's side of the version is ``cw.__version__`` (what this process
  imported); the checkout's side is the dist-info now.
- A ``serve --force`` loop records nothing, so it is invisible here.
- "The checkout" is the tree ``cw.__file__`` was imported from. Running
  ``uv run cw ...`` from a feature worktree compares that worktree's HEAD
  against the loop's recorded tree and can report a spurious STALE.

Leaf module: depends on ``cw.config``, ``cw._git`` and ``cw.local_runner``;
none of them imports it. No logging and no events: a git failure degrades to
an unknown commit, which the doctor check and the CLI line already surface.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import cw
from cw._git import capture_head_sha
from cw.config import dispatch_loop_lock_file
from cw.local_runner import read_process_start_time_ns

_CW_PACKAGE_NAME = "claude-workspace"
# Bounds the one ``git rev-parse HEAD``; a hung git must not stall the caller.
_GIT_TIMEOUT_S = 5.0
_COMMIT_LABEL_LEN = 12
# psutil derives a start time from boot time, which can wobble by about a
# second on some kernels; a recycled pid is off by far more than this.
START_TIME_TOLERANCE_NS = 2_000_000_000
_RESTART = "restart `cw dev-queue serve`"
# Keys of the loop's lock-file record. ``PID_KEY`` is written by
# ``cw.config.dispatch_loop_lock``; the rest by :func:`loop_record_fields`.
PID_KEY = "pid"
PROC_START_KEY = "proc_start_ns"
VERSION_KEY = "version"
COMMIT_KEY = "commit"


class LoopStatus(StrEnum):
    """How the running dispatch loop's code relates to the checkout."""

    NO_LOOP = "no_loop"
    MATCH = "match"
    IDENTITY_UNKNOWN = "identity_unknown"
    STALE = "stale"


@dataclass(frozen=True)
class CodeIdentity:
    """A package version and a checkout commit; either may be unknown."""

    version: str | None
    commit: str | None

    def label(self) -> str:
        """``1.85.1 @ 3f2a9c1d4e5b``, either half alone, or ``unknown``."""
        parts = [
            part
            for part in (self.version, (self.commit or "")[:_COMMIT_LABEL_LEN])
            if part
        ]
        return " @ ".join(parts) if parts else "unknown"


def _label(identity: CodeIdentity | None) -> str:
    return identity.label() if identity is not None else "unknown"


@dataclass(frozen=True)
class LoopAssessment:
    """The outcome of :func:`assess_dispatch_loop` and its one wording."""

    status: LoopStatus
    pid: int | None
    recorded: CodeIdentity | None
    current: CodeIdentity | None

    @property
    def message(self) -> str:
        """The single source of wording for doctor and the CLI."""
        loop = f"dispatch loop (pid {self.pid})"
        if self.status is LoopStatus.NO_LOOP:
            return "no dispatch loop lock holder recorded"
        if self.status is LoopStatus.MATCH:
            return f"{loop} matches the checkout ({_label(self.current)})"
        if self.status is LoopStatus.STALE:
            return (
                f"{loop} started on {_label(self.recorded)} but the checkout"
                f" is at {_label(self.current)}; {_RESTART}"
            )
        if self.recorded is None:
            return (
                f"{loop} did not record its code identity (it started before"
                f" this check existed); identity unknown, {_RESTART} to enable"
                " this check"
            )
        return (
            f"{loop} code identity cannot be compared with the checkout"
            f" (loop: {_label(self.recorded)}; checkout: {_label(self.current)});"
            " identity unknown"
        )


def _installed_version() -> str | None:
    try:
        return importlib.metadata.version(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def _source_root_of(pkg: Path) -> Path | None:
    """The checkout root holding *pkg* as ``<root>/src/cw``, else ``None``.

    A wheel or registry install, or one nested inside an unrelated repo, is
    not a checkout, so no foreign HEAD is ever read. ``.git`` may be a file
    (a linked worktree).
    """
    root = pkg.parent.parent
    if pkg.parent.name != "src" or not (root / "pyproject.toml").is_file():
        return None
    return root if (root / ".git").exists() else None


def _source_root() -> Path | None:
    return _source_root_of(Path(cw.__file__).resolve().parent)


def _checkout_commit() -> str | None:
    root = _source_root()
    if root is None:
        return None
    return capture_head_sha(root, strict=False, timeout=_GIT_TIMEOUT_S) or None


def checkout_code_identity() -> CodeIdentity:
    """The checkout's identity now. Never raises; unknowns are ``None``."""
    return CodeIdentity(version=_installed_version(), commit=_checkout_commit())


@lru_cache(maxsize=1)
def process_code_identity() -> CodeIdentity:
    """The identity of the code this process loaded, read once.

    The version is ``cw.__version__``, resolved when ``cw`` was imported, so an
    editable reinstall after start does not change what this process records.
    Memoized so a ``serve`` in-process restart after a crash records the
    modules still in memory, not a checkout that moved since.
    """
    return CodeIdentity(version=cw.__version__, commit=_checkout_commit())


def loop_record_fields() -> dict[str, object]:
    """The identity keys the dispatch loop adds to its lock-file record."""
    identity = process_code_identity()
    return {
        PROC_START_KEY: read_process_start_time_ns(os.getpid()),
        VERSION_KEY: identity.version,
        COMMIT_KEY: identity.commit,
    }


@dataclass(frozen=True)
class _LoopRecord:
    pid: int
    proc_start_ns: int | None
    # None for a record without the version key (a pre-#2729 loop).
    identity: CodeIdentity | None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_record() -> _LoopRecord | None:
    """The holder's record, read without taking the lock.

    A flock probe would make a loop starting at that instant fail with a
    spurious "already running". A torn or empty read is no record, so the
    worst case is a quiet miss, never a false alarm.
    """
    try:
        data: object = json.loads(dispatch_loop_lock_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = _positive_int(data.get(PID_KEY))
    if pid is None:
        return None
    identity = (
        CodeIdentity(
            version=_str_or_none(data.get(VERSION_KEY)),
            commit=_str_or_none(data.get(COMMIT_KEY)),
        )
        if VERSION_KEY in data
        else None
    )
    return _LoopRecord(pid, _positive_int(data.get(PROC_START_KEY)), identity)


def _holder_alive(record: _LoopRecord) -> bool:
    """The recorded pid is live and, when recorded, started when it said.

    A record without a start time is judged on the pid alone here;
    :func:`assess_dispatch_loop` never trusts that for a record that carries an
    identity.
    """
    started_ns = read_process_start_time_ns(record.pid)
    if started_ns is None:
        return False
    if record.proc_start_ns is None:
        return True
    return abs(started_ns - record.proc_start_ns) <= START_TIME_TOLERANCE_NS


def _judge(recorded: CodeIdentity, current: CodeIdentity) -> LoopStatus:
    """Commit decides when both sides have one.

    Versions are compared only when neither side has a commit. A commit on
    exactly one side means git failed on the other, and the dist-info version
    of an editable install lags the checkout, so that is unknown, not a match.
    """
    if recorded.commit is not None and current.commit is not None:
        same = recorded.commit == current.commit
    elif (
        recorded.commit is None
        and current.commit is None
        and recorded.version is not None
        and current.version is not None
    ):
        same = recorded.version == current.version
    else:
        return LoopStatus.IDENTITY_UNKNOWN
    return LoopStatus.MATCH if same else LoopStatus.STALE


def assess_dispatch_loop() -> LoopAssessment:
    """Compare the live dispatch loop's recorded identity with the checkout."""
    record = _read_record()
    if record is None or not _holder_alive(record):
        return LoopAssessment(LoopStatus.NO_LOOP, None, None, None)
    if record.identity is None:
        return LoopAssessment(LoopStatus.IDENTITY_UNKNOWN, record.pid, None, None)
    current = checkout_code_identity()
    if record.proc_start_ns is None:
        # The pid cannot be verified (psutil failed at loop start), so a
        # recycled pid would read as this loop: never STALE.
        return LoopAssessment(
            LoopStatus.IDENTITY_UNKNOWN, record.pid, record.identity, current
        )
    return LoopAssessment(
        _judge(record.identity, current), record.pid, record.identity, current
    )
