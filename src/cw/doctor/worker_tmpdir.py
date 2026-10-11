"""Worker TMPDIR scope check for cw doctor (#2672).

``claude --bg`` forwards none of the caller's environment: every daemon worker
is a pre-warmed process of the supervisor, whose environment was seeded by
whichever spawn started it. Before #2672 that seeded value was the first
spawning worktree's ``.cw/tmp``, so every live worker shared one sibling
worktree's scratch dir. cw now writes each worker's own ``<cwd>/.cw/tmp`` into
the ``env`` block of ``<cwd>/.claude/settings.local.json``; this check flags
live cw workers whose effective TMPDIR is not under their own cwd.

How the effective value is read, per live cw worker (a roster worker whose cwd
holds ``.claude/cw-context.json``; other workers are ignored):

1. The settings ``env.TMPDIR`` wins when present. It is applied in-process at
   session start, so it is NOT observable in ``/proc``: a worker passing this
   way is reported as "configured", never as "verified".
2. Otherwise ``/proc/<pid>/environ``, the exec-time environment, read only when
   ``/proc/<pid>/stat`` field 22 (counted after the LAST ``)``) still equals the
   roster's ``procStart``, so a reused pid is never misread. The environ is
   split on NUL and only the ``TMPDIR`` value is kept; every other entry is
   discarded and nothing from it is rendered or logged.

``/proc`` is Linux-only; elsewhere, and on any ``OSError`` or ``procStart``
mismatch, a worker is "unverifiable", which is not a finding. Advisory
severity (``ok=True`` always, ``warn=True`` on findings): detection only
(ADR-0014). No logging, no events, no subprocess, no lock, no mutation.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.doctor._shared import CheckResult, SettingsReadFailure, _read_settings
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from cw.native_daemon import WorkerHome

_CHECK_NAME = "worker-tmpdir-scope"

# Offenders named in the detail line; the rest are summarised as a count.
_MAX_LISTED_OFFENDERS = 5

# ``/proc/<pid>/stat``: starttime is field 22 (1-based); fields 1-2 are the pid
# and the parenthesised comm, so the text after the last ``)`` starts at field 3.
_STAT_STARTTIME_FIELD = 22
_STAT_FIRST_FIELD_AFTER_COMM = 3

_DEFAULT_PROC_ROOT = Path("/proc")
_SETTINGS_FILENAME = "settings.local.json"
_TMPDIR_ENTRY_PREFIX = b"TMPDIR="

_CONFIGURED = "configured"
_VERIFIED = "verified"
_OFFENDER = "offender"
_UNVERIFIABLE = "unverifiable"

_ROSTER_UNREADABLE_DETAIL = (
    "could not read daemon roster; worker TMPDIR scope not checked"
)
_REMEDIATION = (
    "Fix: re-spawn the session through cw so spawn rewrites "
    "<cwd>/.claude/settings.local.json before the worker starts; a worker only "
    "picks up its own <cwd>/.cw/tmp from an env block written before session "
    "start."
)


class _ExecTimeRead(NamedTuple):
    """An exec-time environ read: whether it was trustworthy, and its TMPDIR."""

    readable: bool
    tmpdir: str | None


_UNREADABLE = _ExecTimeRead(readable=False, tmpdir=None)


class _Offender(NamedTuple):
    short_id: str
    cwd: Path
    tmpdir: str | None


def _in_scope(value: str, cwd: Path) -> bool:
    """True when *value* resolves (realpath) under *cwd*'s realpath."""
    return Path(value).resolve().is_relative_to(cwd.resolve())


def _settings_tmpdir(cwd: Path) -> str | None:
    """``env.TMPDIR`` from ``<cwd>/.claude/settings.local.json``, if usable."""
    data = _read_settings(cwd / HOOK_CONTEXT_RELATIVE_PATH.parent / _SETTINGS_FILENAME)
    if isinstance(data, SettingsReadFailure):
        return None
    env = data.get("env")
    if not isinstance(env, dict):
        return None
    value: object = env.get("TMPDIR")
    return value if isinstance(value, str) else None


def _proc_start_matches(proc_root: Path, pid: int, proc_start: str) -> bool:
    """True when ``/proc/<pid>/stat`` field 22 equals the roster's procStart."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    _, closing, rest = stat.rpartition(")")
    fields = rest.split()
    index = _STAT_STARTTIME_FIELD - _STAT_FIRST_FIELD_AFTER_COMM
    return bool(closing) and len(fields) > index and fields[index] == proc_start


def _tmpdir_from_environ(raw: bytes) -> str | None:
    """The ``TMPDIR`` value of a NUL-separated environ; nothing else is kept."""
    for entry in raw.split(b"\0"):
        if entry.startswith(_TMPDIR_ENTRY_PREFIX):
            return entry[len(_TMPDIR_ENTRY_PREFIX) :].decode("utf-8", errors="replace")
    return None


def _exec_time_tmpdir(proc_root: Path, home: WorkerHome) -> _ExecTimeRead:
    """Read the worker's exec-time TMPDIR from ``/proc``, guarded by procStart.

    The environ is read first and only trusted if the stat check passes
    afterwards: a pid reused between the two reads fails that check, so a
    stranger's environ is never attributed to the worker.
    """
    if home.pid is None or home.proc_start is None:
        return _UNREADABLE
    try:
        raw = (proc_root / str(home.pid) / "environ").read_bytes()
    except OSError:
        return _UNREADABLE
    if not _proc_start_matches(proc_root, home.pid, home.proc_start):
        return _UNREADABLE
    return _ExecTimeRead(readable=True, tmpdir=_tmpdir_from_environ(raw))


def _classify_worker(home: WorkerHome, proc_root: Path) -> tuple[str, str | None]:
    """Return ``(kind, tmpdir)`` for one live cw worker."""
    configured = _settings_tmpdir(home.cwd)
    if configured is not None:
        kind = _CONFIGURED if _in_scope(configured, home.cwd) else _OFFENDER
        return kind, configured
    read = _exec_time_tmpdir(proc_root, home)
    if not read.readable:
        return _UNVERIFIABLE, None
    if read.tmpdir is not None and _in_scope(read.tmpdir, home.cwd):
        return _VERIFIED, read.tmpdir
    return _OFFENDER, read.tmpdir


def _format_offender(offender: _Offender) -> str:
    value = "unset (host default)" if offender.tmpdir is None else offender.tmpdir
    return f"{offender.short_id} (cwd {offender.cwd}): TMPDIR {value}"


def _render(counts: Counter[str], offenders: list[_Offender]) -> CheckResult:
    """Turn the per-kind counts and the offender list into one CheckResult."""
    total = counts.total()
    if total == 0:
        return CheckResult(
            _CHECK_NAME, ok=True, warn=False, detail="no live cw workers"
        )
    summary = (
        f"{counts[_CONFIGURED]} configured via settings.local.json (effective "
        f"value not observable via /proc), {counts[_VERIFIED]} verified via "
        f"/proc, {counts[_UNVERIFIABLE]} unverifiable"
    )
    if not offenders:
        detail = (
            f"{total} live cw worker(s), none outside their own worktree "
            f"TMPDIR scope: {summary}"
        )
        return CheckResult(_CHECK_NAME, ok=True, warn=False, detail=detail)
    items = "; ".join(_format_offender(o) for o in offenders[:_MAX_LISTED_OFFENDERS])
    overflow = len(offenders) - _MAX_LISTED_OFFENDERS
    if overflow > 0:
        items += f"; ... and {overflow} more"
    detail = (
        f"{len(offenders)} of {total} live cw worker(s) have a TMPDIR outside "
        f"their own worktree: {items}. {summary}. {_REMEDIATION}"
    )
    return CheckResult(_CHECK_NAME, ok=True, warn=True, detail=detail)


def _check_worker_tmpdir_scope(*, proc_root: Path = _DEFAULT_PROC_ROOT) -> CheckResult:
    """Flag live cw workers whose effective TMPDIR is outside their own cwd.

    Returns one advisory :class:`CheckResult` (never ``ok=False``): a stale
    TMPDIR is a hygiene/disk-placement problem, and every pre-#2672 worker
    still running at upgrade time would otherwise fail ``cw doctor``. See the
    module docstring for how the effective value is read and why settings is
    consulted before ``/proc``. *proc_root* is a parameter so tests can point
    it at a fake tree.
    """
    homes = get_native_daemon_client().list_live_worker_homes()
    if homes is None:
        return CheckResult(
            _CHECK_NAME, ok=True, warn=True, detail=_ROSTER_UNREADABLE_DETAIL
        )
    counts: Counter[str] = Counter()
    offenders: list[_Offender] = []
    for home in sorted(homes, key=lambda h: h.short_id):
        if not (home.cwd / HOOK_CONTEXT_RELATIVE_PATH).is_file():
            continue
        kind, tmpdir = _classify_worker(home, proc_root)
        counts[kind] += 1
        if kind == _OFFENDER:
            offenders.append(_Offender(home.short_id, home.cwd, tmpdir))
    return _render(counts, offenders)
