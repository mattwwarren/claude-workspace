"""Idle wait and exit bookkeeping for the dispatch loop (#3726).

Carved out of :mod:`cw.dispatch.loop` so ``_run_dispatch_loop_body`` stays
inside its statement budget and the module under its line ceiling:

- :func:`idle_wait` is the loop's only idle point, where a supervised child
  reads its SIGHUP re-exec flag (``cw.dispatch_reexec``);
- :func:`drain_codex_threads` is the bounded codex drain the loop's
  ``finally`` runs;
- :func:`loop_exit_payload` builds the ``dispatch.loop_exited`` payload.

``record_event`` itself stays in ``loop.py``, where the suite patches it.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
from typing import TYPE_CHECKING

from cw.codex_background import join_outstanding_codex_threads
from cw.exceptions import VersionDriftError

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.dispatch_reexec import ReexecControl

# Longest single sleep while a re-exec flag may arrive: the wake latency bound.
_IDLE_SLICE_SECONDS = 1.0


def idle_wait(
    seconds: float,
    control: ReexecControl | None,
    *,
    sleep: Callable[[float], None],
) -> bool:
    """Sleep the tick interval; return True when a re-exec was requested.

    With no *control* (every unsupervised loop) this is exactly one
    ``sleep(seconds)``. With one, it checks the flag first and then sleeps in
    slices of at most one second, checking between slices. The remaining time
    is counted down, never read from a clock, so a no-op ``sleep`` patch
    cannot spin.
    """
    if control is None:
        sleep(seconds)
        return False
    remaining = float(seconds)
    while not control.requested and remaining > 0:
        step = min(_IDLE_SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step
    return control.requested


def drain_codex_threads() -> int:
    """Join in-flight codex review threads; return how many are still running."""
    # Bounded drain of in-flight codex review threads (#1727). Deliberately
    # unconditional rather than gated on a clean exit: every path that
    # reaches this block at all is, by definition, not a SIGKILL, so Python
    # is still alive to give an almost-done review a few seconds to land —
    # and an exception unwinding the loop is exactly as likely to strand a
    # half-committed worktree as a clean shutdown is.
    #
    # Suppressed for the same reason the event write below is: shutdown
    # bookkeeping must never replace the exception that is already unwinding
    # this frame. A failed drain leaves the count at its 0 default rather
    # than swallowing the real cause of the exit.
    codex_threads_still_running = 0
    with contextlib.suppress(Exception):
        codex_threads_still_running = join_outstanding_codex_threads()
    return codex_threads_still_running


def loop_exit_payload(
    exc: BaseException | None,
    *,
    codex_threads_still_running: int,
    loaded_version: str,
    package_name: str,
    reexec: ReexecControl | None,
) -> dict[str, object]:
    """The ``dispatch.loop_exited`` payload for a loop unwinding with *exc*.

    Never raises: a failed installed-version lookup on a version-drift exit
    leaves ``installed_version`` out rather than dropping the event.
    """
    normal = exc is None or isinstance(exc, KeyboardInterrupt)
    payload: dict[str, object] = {
        "normal": normal,
        "exception_type": None if normal else type(exc).__name__,
        # Free observability for #1742: distinguishes "the loop exited" from
        # "the loop exited while N reviews were still running".
        "codex_threads_still_running": codex_threads_still_running,
    }
    if isinstance(exc, VersionDriftError):
        payload["reason"] = "version_drift"
        payload["loaded_version"] = loaded_version
        with contextlib.suppress(Exception):
            payload["installed_version"] = importlib.metadata.version(package_name)
    elif exc is None and reexec is not None and reexec.requested:
        payload["reason"] = "reexec"
    return payload
