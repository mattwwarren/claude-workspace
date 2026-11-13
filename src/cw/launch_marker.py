"""The claim's launch marker and its owner-liveness verdict (GitHub #2615).

Dispatch stamps a :class:`~cw.models.LaunchMarker` (its own pid and process
start time) on a row at the RUNNING claim, and the row loses it once the spawn
binds a session or the row leaves RUNNING. A RUNNING row that is still unbound
and still carries a marker may have a launch in flight: ``create_worktree`` and
``claude --bg`` have no timeout, so no claim age can prove otherwise. The marker
can, and only in one direction.

The R9 rule: only positive evidence of absence proves the owner is gone.
``psutil.NoSuchProcess`` for the pid, or a live pid whose start time moved past
:data:`~cw.dispatch_loop_identity.START_TIME_TOLERANCE_NS` (a recycled pid),
reads as :attr:`LaunchOwnerState.OWNER_GONE`. Everything unreadable
(``AccessDenied``, a zombie, a timeout, a bad pid) reads as alive, because the
reader can run in another process context from the dispatcher (``cw doctor
--reap`` from a sandboxed shell, a ``hidepid`` ``/proc`` mount, another user).

Pid namespaces (#3328): an absent pid is evidence of absence only when the
observer shares the writer's pid namespace, and :class:`~cw.models.LaunchMarker`
records none. The proof is two-sided and fails closed, using the fixed link of
Linux's initial pid namespace (``pid:[4026531836]``):

- :func:`current_launch_marker` stamps a marker only from the initial pid
  namespace, so every marker it writes was written there;
- :func:`observed_launch_owner_state` demotes ``OWNER_GONE`` to ``IN_FLIGHT``
  unless the observer is in the initial pid namespace too.

A dispatcher or observer in a container or sandbox pid namespace therefore
stamps no marker, or honours no ``OWNER_GONE`` verdict. A platform without pid
namespaces (macOS) counts as the initial one.

Wall-clock steps (#3319): ``psutil``'s ``create_time()`` is epoch time, so an
NTP or manual clock step past the tolerance would read a live dispatcher as a
recycled pid. :func:`current_launch_marker` therefore also stamps the
clock-independent start (``/proc/<pid>/stat`` field 22, ticks since boot) and
the boot id, when both are readable. For such a marker the verdict compares the
since-boot start within the same tolerance, a different boot id is
``OWNER_GONE``, and anything unreadable is ``IN_FLIGHT``; it never falls back to
the wall clock. A marker without the pair (an older cw, or no ``/proc``) keeps
the wall-clock comparison.

Why a module of its own: :func:`cw.local_runner.read_process_start_time_ns`
collapses absent and unreadable into ``None``, so it cannot serve R9, and
``cw.local_runner`` is already past the module-size flag. This leaf imports
only ``psutil``, ``cw.models`` and ``cw.dispatch_loop_identity``, so both
``cw.dispatch`` and ``cw.reconcile`` can import it without a cycle (so it
carries its own small ``/proc/<pid>/stat`` parser rather than importing
``cw.doctor.worker_tmpdir``'s). The patch seams are the module-level bindings
:func:`probe_process_start_time_ns`, :func:`read_since_boot_ns` and
:func:`read_boot_id`.
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import psutil

from cw.dispatch_loop_identity import START_TIME_TOLERANCE_NS
from cw.models import LaunchMarker

_NS_PER_SECOND = 1_000_000_000
_PROC_ROOT = Path("/proc")
# readlink of /proc/self/ns/pid in Linux's initial pid namespace: the inode is
# the kernel constant PROC_PID_INIT_INO, the same on every host.
_INIT_PIDNS_LINK = "pid:[4026531836]"
_BOOT_ID_PATH = Path("sys", "kernel", "random", "boot_id")
# /proc/<pid>/stat: field 22 is starttime (clock ticks since boot). Fields are
# counted after the last ")", which closes the comm (field 2) and may itself
# contain spaces or parentheses; the first field after it is field 3.
_STAT_STARTTIME_FIELD = 22
_STAT_FIRST_FIELD_AFTER_COMM = 3


def read_boot_id(*, proc_root: Path = _PROC_ROOT) -> str | None:
    """This boot's ``/proc/sys/kernel/random/boot_id``, or None when unreadable."""
    try:
        text = (proc_root / _BOOT_ID_PATH).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def read_since_boot_ns(pid: int, *, proc_root: Path = _PROC_ROOT) -> int | None:
    """*pid*'s start in nanoseconds since boot, or None when unreadable (#3319).

    ``/proc/<pid>/stat`` field 22 counts clock ticks since boot, so a wall-clock
    step does not move it; it is converted with ``SC_CLK_TCK``. Missing,
    short or garbled stat (and any platform without ``/proc``) reads as None.
    """
    try:
        stat = (proc_root / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    _, closing, rest = stat.rpartition(")")
    fields = rest.split()
    index = _STAT_STARTTIME_FIELD - _STAT_FIRST_FIELD_AFTER_COMM
    if not closing or len(fields) <= index:
        return None
    try:
        ticks = int(fields[index])
        hertz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if ticks < 0 or hertz <= 0:
        return None
    return ticks * _NS_PER_SECOND // hertz


def in_initial_pid_namespace(*, proc_root: Path = _PROC_ROOT) -> bool:
    """True when this process runs in the initial pid namespace.

    A platform other than Linux has no pid namespaces and counts as the
    initial one. On Linux an unreadable link fails closed (False).
    """
    if not sys.platform.startswith("linux"):
        return True
    try:
        link = (proc_root / "self" / "ns" / "pid").readlink()
    except OSError:
        return False
    return str(link) == _INIT_PIDNS_LINK


class ProbeState(Enum):
    """What one read of a pid's process start time established."""

    PRESENT = "present"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class ProcessProbe(NamedTuple):
    """One probe result; ``start_time_ns`` is set only when ``PRESENT``.

    ``since_boot_ns`` (#3319) is the clock-independent start, set only when
    ``PRESENT`` and ``/proc`` was readable. Defaulted so a two-field probe stays
    valid.
    """

    state: ProbeState
    start_time_ns: int | None
    since_boot_ns: int | None = None


class LaunchOwnerState(Enum):
    """The verdict on a row's launch marker."""

    # The claiming dispatcher is alive, or its liveness could not be read.
    IN_FLIGHT = "in_flight"
    # Positive evidence the claiming process no longer exists.
    OWNER_GONE = "owner_gone"
    # No marker: claimed before v58, already cleared, or dropped by an old
    # in-memory ``serve`` loop that does not know the field (#2702).
    UNKNOWN = "unknown"


def probe_process_start_time_ns(pid: int) -> ProcessProbe:
    """Read *pid*'s start time, keeping absent and unreadable apart.

    The tri-state sibling of :func:`cw.local_runner.read_process_start_time_ns`,
    with the same start-time formula. The handler order is load-bearing:
    ``psutil.ZombieProcess`` subclasses ``psutil.NoSuchProcess``, and a zombie
    has not been reaped, so it is caught first and reported unreadable.
    """
    try:
        started = int(psutil.Process(pid).create_time() * _NS_PER_SECOND)
    except psutil.ZombieProcess:
        return ProcessProbe(ProbeState.UNREADABLE, None)
    except psutil.NoSuchProcess:
        return ProcessProbe(ProbeState.ABSENT, None)
    except (psutil.Error, ValueError):
        return ProcessProbe(ProbeState.UNREADABLE, None)
    return ProcessProbe(ProbeState.PRESENT, started, read_since_boot_ns(pid))


def launch_owner_state(marker: LaunchMarker | None) -> LaunchOwnerState:
    """Return whether *marker*'s claiming process may still be launching.

    ``OWNER_GONE`` only on positive evidence of absence (module docstring);
    an unreadable probe is ``IN_FLIGHT``. Start times are compared within
    :data:`~cw.dispatch_loop_identity.START_TIME_TOLERANCE_NS`, as
    ``dispatch_loop_identity`` does, because exact equality would read a live
    dispatcher's boot-time wobble as a recycled pid. A marker carrying the
    since-boot pair (#3319) is judged by :func:`_since_boot_verdict` and never by
    the wall clock; a legacy marker is judged by the wall-clock start.
    """
    if marker is None:
        return LaunchOwnerState.UNKNOWN
    probe = probe_process_start_time_ns(marker.pid)
    if probe.state is ProbeState.ABSENT:
        return LaunchOwnerState.OWNER_GONE
    if probe.state is not ProbeState.PRESENT:
        return LaunchOwnerState.IN_FLIGHT
    if marker.start_since_boot_ns is not None and marker.boot_id is not None:
        return _since_boot_verdict(
            probe, since_boot_ns=marker.start_since_boot_ns, boot_id=marker.boot_id
        )
    if probe.start_time_ns is not None:
        drift = abs(probe.start_time_ns - marker.start_time_ns)
        if drift > START_TIME_TOLERANCE_NS:
            return LaunchOwnerState.OWNER_GONE
    return LaunchOwnerState.IN_FLIGHT


def _since_boot_verdict(
    probe: ProcessProbe, *, since_boot_ns: int, boot_id: str
) -> LaunchOwnerState:
    """The clock-independent verdict for a PRESENT probe of a new-style marker.

    Another boot means the pid is another process (``OWNER_GONE``). An
    unreadable boot id or since-boot start fails closed (``IN_FLIGHT``). Same
    boot with the since-boot start past the tolerance is a recycled pid.
    """
    current_boot = read_boot_id()
    if current_boot is None:
        return LaunchOwnerState.IN_FLIGHT
    if current_boot != boot_id:
        return LaunchOwnerState.OWNER_GONE
    if probe.since_boot_ns is None:
        return LaunchOwnerState.IN_FLIGHT
    if abs(probe.since_boot_ns - since_boot_ns) > START_TIME_TOLERANCE_NS:
        return LaunchOwnerState.OWNER_GONE
    return LaunchOwnerState.IN_FLIGHT


def observed_launch_owner_state(marker: LaunchMarker | None) -> LaunchOwnerState:
    """:func:`launch_owner_state`, failing closed outside the initial pid namespace.

    An observer that cannot prove it shares the writer's pid namespace sees
    every host pid as absent, so its ``OWNER_GONE`` reads as ``IN_FLIGHT``.
    """
    state = launch_owner_state(marker)
    if state is LaunchOwnerState.OWNER_GONE and not in_initial_pid_namespace():
        return LaunchOwnerState.IN_FLIGHT
    return state


def current_launch_marker() -> LaunchMarker | None:
    """Return this process's launch marker, or ``None`` if its start time is
    unreadable or it runs outside the initial pid namespace (the claim then
    stores no marker, which reads as ``UNKNOWN``).

    The since-boot start and boot id (#3319) are stamped only when both are
    readable; otherwise the marker is legacy-shaped (e.g. macOS, no ``/proc``).
    """
    if not in_initial_pid_namespace():
        return None
    pid = os.getpid()
    probe = probe_process_start_time_ns(pid)
    if probe.state is not ProbeState.PRESENT or probe.start_time_ns is None:
        return None
    boot_id = read_boot_id()
    if probe.since_boot_ns is None or boot_id is None:
        return LaunchMarker(pid=pid, start_time_ns=probe.start_time_ns)
    return LaunchMarker(
        pid=pid,
        start_time_ns=probe.start_time_ns,
        start_since_boot_ns=probe.since_boot_ns,
        boot_id=boot_id,
    )
