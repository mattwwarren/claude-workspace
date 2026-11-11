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

Why a module of its own: :func:`cw.local_runner.read_process_start_time_ns`
collapses absent and unreadable into ``None``, so it cannot serve R9, and
``cw.local_runner`` is already past the module-size flag. This leaf imports
only ``psutil``, ``cw.models`` and ``cw.dispatch_loop_identity``, so both
``cw.dispatch`` and ``cw.reconcile`` can import it without a cycle. The patch
seam is the module-level binding :func:`probe_process_start_time_ns`.
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
    """One probe result; ``start_time_ns`` is set only when ``PRESENT``."""

    state: ProbeState
    start_time_ns: int | None


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
    return ProcessProbe(ProbeState.PRESENT, started)


def launch_owner_state(marker: LaunchMarker | None) -> LaunchOwnerState:
    """Return whether *marker*'s claiming process may still be launching.

    ``OWNER_GONE`` only on positive evidence of absence (module docstring);
    an unreadable probe is ``IN_FLIGHT``. Start times are compared within
    :data:`~cw.dispatch_loop_identity.START_TIME_TOLERANCE_NS`, as
    ``dispatch_loop_identity`` does, because exact equality would read a live
    dispatcher's boot-time wobble as a recycled pid.
    """
    if marker is None:
        return LaunchOwnerState.UNKNOWN
    probe = probe_process_start_time_ns(marker.pid)
    if probe.state is ProbeState.ABSENT:
        return LaunchOwnerState.OWNER_GONE
    if probe.state is ProbeState.PRESENT and probe.start_time_ns is not None:
        drift = abs(probe.start_time_ns - marker.start_time_ns)
        if drift > START_TIME_TOLERANCE_NS:
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
    stores no marker, which reads as ``UNKNOWN``)."""
    if not in_initial_pid_namespace():
        return None
    pid = os.getpid()
    probe = probe_process_start_time_ns(pid)
    if probe.state is not ProbeState.PRESENT or probe.start_time_ns is None:
        return None
    return LaunchMarker(pid=pid, start_time_ns=probe.start_time_ns)
