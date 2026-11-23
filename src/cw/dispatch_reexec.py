"""The child-side re-exec signal of ``cw dev-queue serve --supervised`` (#3726).

A supervised dispatch loop runs as a child of :mod:`cw.dispatch_supervisor`.
The supervisor asks it to restart onto newly installed code with SIGHUP; the
child's handler only sets :attr:`ReexecControl.requested`, and the loop reads
that flag at its next idle point (``cw.dispatch.loop_lifecycle.idle_wait``),
so a tick in progress always finishes. The child then exits with
:data:`REEXEC_EXIT_CODE` and the supervisor starts a fresh process.

SIGHUP is handled only for a supervised child, and never when the process
inherited SIGHUP as ignored (``nohup``): an unsupervised loop keeps today's
default SIGHUP behaviour.

Leaf module: ``os``, ``signal`` and ``threading`` only.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import FrameType

    SignalHandler = Callable[[int, FrameType | None], object]

# Set by the supervisor on the child's environment: the supervisor's own pid.
SUPERVISOR_PID_ENV = "CW_DISPATCH_SUPERVISOR_PID"
# "Restart me from the installed code": the stdlib temp-fail exit status (75).
REEXEC_EXIT_CODE = os.EX_TEMPFAIL


class ReexecControl:
    """A plain flag the SIGHUP handler sets; nothing else happens in the handler.

    The handler must stay async-signal-safe and must never raise into a tick,
    so it only assigns a bool (no ``threading.Event``, no I/O).
    """

    def __init__(self) -> None:
        self.requested = False

    def request(self, _signum: int, _frame: FrameType | None) -> None:
        """Signal handler: record the re-exec request."""
        self.requested = True


def supervisor_pid() -> int | None:
    """The supervisor pid from the environment, or ``None`` when unset or bad."""
    raw = os.environ.get(SUPERVISOR_PID_ENV, "")
    if not raw.isdigit() or int(raw) <= 0:
        return None
    return int(raw)


def supervised_by_parent() -> bool:
    """True only when this process's parent is the supervisor that spawned it.

    An orphaned child (its supervisor died, so it was reparented) is not
    supervised, so a later start records no supervisor.
    """
    pid = supervisor_pid()
    return pid is not None and pid == os.getppid()


def sighup_ignored() -> bool:
    """True when SIGHUP is ignored, as under ``nohup``."""
    return signal.getsignal(signal.SIGHUP) is signal.SIG_IGN


@contextlib.contextmanager
def handling_signals(
    handlers: Mapping[signal.Signals, SignalHandler],
    *,
    respect_ignored: frozenset[signal.Signals] = frozenset(),
) -> Iterator[frozenset[signal.Signals]]:
    """Install *handlers* for the block and restore the previous ones after.

    Main thread only (Python allows handlers nowhere else): off it this is a
    no-op. A signal in *respect_ignored* whose current disposition is
    ``SIG_IGN`` keeps it, so ``nohup`` still protects the process. Yields the
    signals actually installed.
    """
    if threading.current_thread() is not threading.main_thread():
        yield frozenset()
        return
    previous = {
        sig: signal.signal(sig, handler)
        for sig, handler in handlers.items()
        if not (sig in respect_ignored and signal.getsignal(sig) is signal.SIG_IGN)
    }
    try:
        yield frozenset(previous)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextlib.contextmanager
def sighup_reexec(control: ReexecControl | None) -> Iterator[None]:
    """Route SIGHUP to *control* for the block; a no-op when unsupervised."""
    if control is None:
        yield
        return
    with handling_signals(
        {signal.SIGHUP: control.request}, respect_ignored=frozenset({signal.SIGHUP})
    ):
        yield
