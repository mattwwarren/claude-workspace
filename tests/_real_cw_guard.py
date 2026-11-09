"""Refuse a unit test's write under the real checkout's ``.cw/`` (#3113).

``cw review adjudicate`` tests whose cwd was the repo root once wrote their
fixture deferrals into the dispatch worktree's real
``.cw/deferred-findings.md``, and finalize then filed them as review-debt
tickets. ``tests/conftest.py``'s autouse ``_real_cw_guard`` arms this module
for every unit test, so such a write fails the test that made it.

Mechanism: one process-wide audit hook (:func:`sys.addaudithook`, installed
once by :func:`install_hook`) judges the write-shaped events in
:data:`_WRITE_EVENTS` -- ``open`` with a write flag, ``os.mkdir``,
``os.rename``, ``os.remove``, ``os.rmdir``, ``os.truncate`` and
``shutil.rmtree``. A path under the armed root and outside every allowed
subtree raises :class:`RealCwWriteError` (a ``PermissionError``) from the hook,
which aborts the operation before it touches disk, and is recorded in the
test's :class:`~tests._host_probe_guard.LaunchRecorder` as a ``cw_write``
violation, so a broad ``except OSError`` in ``src/cw`` cannot hide it.

The allowed subtrees are the pytest basetemp and :func:`tempfile.gettempdir`:
a dispatched worker's ``TMPDIR`` is ``<worktree>/.cw/tmp``, so every
``tmp_path`` lives under the real ``.cw/``.

Known limitations: paths are normalised with :func:`os.path.normpath`, never
``realpath`` (resolving would itself raise audit events), so a write through a
symlink into the root is missed; an event carrying a ``dir_fd`` is not judged;
``os.symlink`` and ``os.link`` are not in the event table; and subprocess
children (a ``git`` or a script writing under ``.cw/``) are not seen by an
in-process hook.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tests._host_probe_guard import LaunchRecorder

#: Any of these in an ``open`` event's flags makes it a write.
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
#: The ``dir_fd`` value an ``os.*`` audit event carries when none was passed.
_NO_DIR_FD = -1


class RealCwWriteError(PermissionError):
    """A unit test wrote under the real checkout's ``.cw/``."""


@dataclass(frozen=True, slots=True)
class _Armed:
    root: str
    allowed: tuple[str, ...]
    recorder: LaunchRecorder


class _State:
    """The process-wide guard state the hook reads (no ``global`` rebinding)."""

    active: _Armed | None = None
    installed: bool = False


_STATE = _State()
_IN_HOOK = threading.local()


def is_write_open(flags: int) -> bool:
    """True when ``open`` *flags* can create, truncate or write a file."""
    return bool(flags & _WRITE_OPEN_FLAGS)


def _text(path: object) -> str | None:
    """*path* as text; ``None`` for an fd or anything that is not a path."""
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if isinstance(path, bytes):
        return os.fsdecode(path)
    return path if isinstance(path, str) else None


def _dir_fds_unset(*dir_fds: object) -> bool:
    return all(fd in (_NO_DIR_FD, None) for fd in dir_fds)


def target_paths(event: str, args: tuple[object, ...]) -> tuple[object, ...]:
    """The paths a write-shaped audit *event* would change; ``()`` otherwise."""
    if event == "open":
        flags = args[2]
        return (args[0],) if isinstance(flags, int) and is_write_open(flags) else ()
    if event == "os.rename":
        return (args[0], args[1]) if _dir_fds_unset(args[2], args[3]) else ()
    if event == "os.mkdir":
        return (args[0],) if _dir_fds_unset(args[2]) else ()
    if event in {"os.remove", "os.rmdir", "shutil.rmtree"}:
        return (args[0],) if _dir_fds_unset(args[1]) else ()
    if event == "os.truncate":
        return (args[0],)
    return ()


def normalise(path: object) -> str | None:
    """*path* as an absolute, ``..``-collapsed string, or ``None``.

    A relative path resolves against the current cwd -- the incident shape,
    where the cwd was the repo root. ``None`` for an fd, a non-path, or a
    relative path when the cwd itself is gone.
    """
    text = _text(path)
    if text is None:
        return None
    if not Path(text).is_absolute():
        try:
            text = str(Path.cwd() / text)
        except OSError:
            return None
    return os.path.normpath(text)


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _refused_by(armed: _Armed, path: object) -> str | None:
    text = normalise(path)
    if text is None or not _within(text, armed.root):
        return None
    if any(_within(text, allowed) for allowed in armed.allowed):
        return None
    return text


def refuses(path: object) -> bool:
    """True when the armed guard would refuse a write to *path*."""
    armed = _STATE.active
    return armed is not None and _refused_by(armed, path) is not None


def _hook(event: str, args: tuple[object, ...]) -> None:
    armed = _STATE.active
    if armed is None or getattr(_IN_HOOK, "active", False):
        return
    _IN_HOOK.active = True
    try:
        refused = [
            text
            for path in target_paths(event, args)
            if (text := _refused_by(armed, path)) is not None
        ]
    finally:
        _IN_HOOK.active = False
    if refused:
        detail = f"{event} {refused[0]}"
        armed.recorder.record_cw_write(detail)
        msg = f"unit test write under the real .cw/ refused ({event})"
        raise RealCwWriteError(errno.EACCES, msg, refused[0])


def install_hook() -> None:
    """Register the audit hook once per process; later calls are no-ops."""
    if _STATE.installed:
        return
    sys.addaudithook(_hook)
    _STATE.installed = True


def arm(root: Path, allowed: Iterable[Path], recorder: LaunchRecorder) -> None:
    """Refuse writes under *root* outside *allowed*; replaces any prior arm."""
    install_hook()
    _STATE.active = _Armed(
        root=os.path.normpath(str(root)),
        allowed=tuple(os.path.normpath(str(path)) for path in allowed),
        recorder=recorder,
    )


def disarm() -> None:
    """Stop judging writes; safe whatever state is armed, or none."""
    _STATE.active = None


def is_armed() -> bool:
    """True while a root is armed."""
    return _STATE.active is not None
