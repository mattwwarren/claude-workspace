"""Thin in-process supervisor for the dispatch loop.

Wraps ``run_dispatch_loop`` in a restart loop with exponential backoff and a
crash-window cap so the dispatch pipeline self-heals after transient failures
without hot-looping during sustained outages.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from cw.dispatch import run_dispatch_loop
from cw.exceptions import (
    DispatchLoopLockedError,
    DispatchServeError,
    VersionDriftError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)

# Maximum crashes before the supervisor gives up (exits non-zero).
_SERVE_MAX_CRASHES: int = 5
# Window (seconds) in which crashes are counted toward the cap.
_SERVE_CRASH_WINDOW_SECONDS: int = 300
# Starting backoff after the first crash.
_SERVE_INITIAL_BACKOFF_SECONDS: float = 5.0
# Backoff ceiling — never wait longer than this between restarts.
_SERVE_BACKOFF_CAP_SECONDS: float = 60.0
# A run that lasts at least this long is considered "healthy" and resets backoff.
_SERVE_HEALTHY_RUN_SECONDS: float = 60.0


def _prune_crash_window(crash_times: list[float], now: float) -> list[float]:
    """Return only those crash timestamps that fall within the current window."""
    cutoff = now - _SERVE_CRASH_WINDOW_SECONDS
    return [t for t in crash_times if t >= cutoff]


def run_dispatch_serve(
    *,
    max_parallel: int | None = None,
    use_plan: bool = False,
    parent: str | None = None,
    emit: Callable[[str], None] | None = None,
    auto_ff: bool = True,
    client: str | None = None,
    max_restarts: int = -1,
    force: bool = False,
) -> None:
    """Run the dispatch loop with automatic restart on crash.

    Wraps :func:`run_dispatch_loop` in a supervision loop.  On a clean
    exit (normal return or ``KeyboardInterrupt``) the supervisor exits
    without restarting.  On any other exception the loop is restarted
    after an exponential backoff delay.

    Args:
        max_parallel: Forwarded to ``run_dispatch_loop``.
        use_plan: Forwarded to ``run_dispatch_loop``.
        parent: Forwarded to ``run_dispatch_loop``.
        emit: Forwarded to ``run_dispatch_loop``.
        auto_ff: Forwarded to ``run_dispatch_loop``.
        client: Forwarded to ``run_dispatch_loop``.
        max_restarts: Maximum number of restarts.  -1 means unlimited.
            When the crash count hits this limit the supervisor logs a
            critical message and raises :exc:`DispatchServeGaveUp`.
        force: Forwarded to ``run_dispatch_loop`` on every (re)start to
            bypass the dispatch-loop singleton lock (#1362).
    """
    crash_times: list[float] = []
    restart_count: int = 0
    backoff: float = _SERVE_INITIAL_BACKOFF_SECONDS

    while True:
        run_start = time.monotonic()
        try:
            run_dispatch_loop(
                max_parallel=max_parallel,
                use_plan=use_plan,
                parent=parent,
                emit=emit,
                auto_ff=auto_ff,
                client=client,
                force=force,
            )
        except KeyboardInterrupt:
            # Ctrl-C — clean stop; do not restart.
            return
        except SystemExit:
            # Propagate clean shutdowns initiated by the loop itself.
            raise
        except DispatchLoopLockedError:
            # Another loop already holds the singleton lock (#1362). This is a
            # fail-fast condition, NOT a crash — re-raise immediately instead
            # of retrying through the backoff loop (which would delay the
            # blocked-launch error by up to ~300s across 5 attempts).
            raise
        except VersionDriftError:
            # Intentional reload — do NOT count toward crash_times or restart_count.
            # Return so the external supervisor starts a fresh process with fresh
            # imports; in-process restart cannot reload module-level globals.
            _log.info("dispatch_serve: version drift — exiting for fresh reload")
            time.sleep(_SERVE_INITIAL_BACKOFF_SECONDS)
            return
        except Exception:  # noqa: BLE001 — supervisor must survive any loop crash to self-heal via backoff/restart; see module docstring
            run_duration = time.monotonic() - run_start
            now = time.time()
            crash_times.append(now)
            crash_times = _prune_crash_window(crash_times, now)
            restart_count += 1

            _log.error(
                "dispatch_serve: loop crashed (restart #%d, run_duration=%.1fs)",
                restart_count,
                run_duration,
            )

            # Crash-window cap — too many crashes in a short window.
            if len(crash_times) >= _SERVE_MAX_CRASHES:
                _log.critical(
                    "dispatch_serve: %d crashes in %ds window — giving up",
                    len(crash_times),
                    _SERVE_CRASH_WINDOW_SECONDS,
                )
                msg = (
                    f"dispatch supervisor gave up: {len(crash_times)} crashes"
                    f" in {_SERVE_CRASH_WINDOW_SECONDS}s window"
                )
                raise DispatchServeError(msg) from None

            # Max-restarts cap.
            if max_restarts >= 0 and restart_count > max_restarts:
                _log.critical(
                    "dispatch_serve: max_restarts=%d exhausted — giving up",
                    max_restarts,
                )
                msg = (
                    "dispatch supervisor gave up:"
                    f" max_restarts={max_restarts} exhausted"
                )
                raise DispatchServeError(msg) from None

            _log.info("dispatch_serve: restarting in %.1fs", backoff)
            time.sleep(backoff)

            # Update backoff for the next crash: reset after a healthy run,
            # otherwise double (up to cap).
            if run_duration >= _SERVE_HEALTHY_RUN_SECONDS:
                backoff = _SERVE_INITIAL_BACKOFF_SECONDS
            else:
                backoff = min(backoff * 2, _SERVE_BACKOFF_CAP_SECONDS)
        else:
            # Clean return — operator stop or normal completion.
            return
