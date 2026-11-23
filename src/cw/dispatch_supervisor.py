"""The parent process of ``cw dev-queue serve --supervised`` (#3726).

A long-lived dispatch loop keeps the modules it imported; when the checkout
moves on, the identity guard (:mod:`cw.dispatch_loop_identity`) refuses
row writes until someone restarts the loop. This supervisor owns that restart.
It runs ``cw dev-queue serve`` as a child process and polls
:func:`assess_dispatch_loop`. On STALE it pre-flights the new install with an
import probe, sends the child SIGHUP (the child finishes its tick and exits
with ``REEXEC_EXIT_CODE`` at its next idle point) and starts a fresh child
from the code installed now.

Restarts are bounded four ways: a minimum interval after a child start, one
automatic restart per checkout identity (an operator SIGHUP overrides it), a
doubling re-probe backoff while the import probe fails, and a start-failure
counter for children launched after a re-exec. Crashes are not restarted
here: the child's in-process ``run_dispatch_serve`` backoff owns them. A child
that has not exited ten minutes after the forwarded SIGHUP raises a loud
signal (``dispatch.restart_stalled`` plus a stderr line); the supervisor never
forces that exit.

The supervisor never takes the dispatch-loop lock: it waits for each child to
exit before starting the next. Its own code is not re-executed; a change to
this module needs a manual restart of the ``--supervised`` command.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol

import click

from cw.dispatch_loop_identity import LoopStatus, assess_dispatch_loop
from cw.dispatch_reexec import (
    REEXEC_EXIT_CODE,
    SUPERVISOR_PID_ENV,
    handling_signals,
    sighup_ignored,
)
from cw.dispatch_serve import (
    _SERVE_BACKOFF_CAP_SECONDS,
    _SERVE_HEALTHY_RUN_SECONDS,
    _SERVE_INITIAL_BACKOFF_SECONDS,
)
from cw.events import record_event
from cw.exceptions import DispatchServeError
from cw.models import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from cw.dispatch_loop_identity import CodeIdentity, LoopAssessment

# Child poll cadence, and how often the loop's code identity is assessed.
_POLL_SECONDS = 1.0
_DRIFT_POLL_SECONDS = 10.0
# Consecutive fast failures of a child started after a re-exec before giving up.
_START_FAILURE_LIMIT = 3
# SIGTERM: how long the child gets after SIGINT before it is killed.
_STOP_GRACE_SECONDS = 120.0
_PROBE_TIMEOUT_SECONDS = 120.0
# Re-probe schedule while the import probe fails: 30s doubling to 600s.
_PROBE_BACKOFF_INITIAL_SECONDS = 30.0
_PROBE_BACKOFF_CAP_SECONDS = 600.0
# A child still running this long after the restart SIGHUP is reported once.
_RESTART_STALL_SECONDS = 600.0
# No automatic restart sooner than this after a child start; a child that
# lives this long is healthy.
_MIN_RESTART_INTERVAL_SECONDS = _SERVE_HEALTHY_RUN_SECONDS
# The bounded detail kept from a failed probe's stderr.
_PROBE_DETAIL_MAX = 200

_PROBE_CODE = "import cw.cli.dev_queue.run, cw.dispatch_serve"
# Spawned through when the supervisor's own SIGHUP is ignored (nohup): an
# ignored SIGHUP survives exec, so the child is reset to SIG_DFL first and
# exec'd in place (same pid) as ``python -m cw ...``.
_SIGHUP_DEFAULT_TRAMPOLINE = (
    "import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_DFL);"
    " os.execv(sys.argv[1], sys.argv[1:])"
)

_PROBE_FAILED_LINE = (
    "supervisor: not restarting the dispatch loop onto {label}: the new install"
    " failed its import check ({detail}); keeping the old loop (pid {pid}) and"
    " re-checking in {delay:g}s"
)
_GAVE_UP_LINE = (
    "supervisor: giving up after {failures} consecutive failed starts of the"
    " dispatch loop (last exit status {status}); the loop is NOT running. Fix"
    " the install, then start `cw dev-queue serve --supervised` again"
)
_KILLED_LINE = (
    "supervisor: dispatch loop (pid {pid}) was killed by {signal_name}; not"
    " restarting. The loop is NOT running; restart `cw dev-queue serve"
    " --supervised`"
)
_CHILD_FAILED_LINE = (
    "supervisor: dispatch loop (pid {pid}) exited with status {status}; not"
    " restarting. The loop is NOT running; restart `cw dev-queue serve"
    " --supervised` after fixing the cause"
)
_STALLED_LINE = (
    "supervisor: dispatch loop (pid {pid}) has not exited {minutes:g} minutes"
    " after the restart SIGHUP; a tick may be stuck on an external call. Not"
    " forcing the exit (never mid-tick); inspect or stop pid {pid} yourself"
)
_SAME_CHECKOUT_LINE = (
    "supervisor: the dispatch loop is still stale against {label} after an"
    " automatic restart; not restarting again for this checkout. Send SIGHUP"
    " to the supervisor (pid {supervisor_pid}) to force one"
)


@dataclass(frozen=True)
class ServeOptions:
    """The ``cw dev-queue serve`` options forwarded to every child."""

    max_parallel: int | None
    use_plan: bool
    parent: str | None
    quiet: bool
    auto_ff: bool
    client: str | None
    max_restarts: int


class _Child(Protocol):
    """The part of :class:`subprocess.Popen` the supervisor uses."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


def build_child_argv(options: ServeOptions, *, reset_sighup: bool) -> list[str]:
    """The child's argv: ``python -m cw dev-queue serve`` with *options*.

    ``--supervised`` is never forwarded (no nesting) and ``--force`` cannot
    reach here. *reset_sighup* prefixes the SIG_DFL trampoline.
    """
    argv = [sys.executable, "-m", "cw", "dev-queue", "serve"]
    if options.max_parallel is not None:
        argv += ["--max-parallel", str(options.max_parallel)]
    if options.use_plan:
        argv.append("--use-plan")
    if options.parent is not None:
        argv += ["--parent", options.parent]
    if options.quiet:
        argv.append("--quiet")
    argv.append("--auto-ff" if options.auto_ff else "--no-auto-ff")
    if options.client is not None:
        argv += ["--client", options.client]
    argv += ["--max-restarts", str(options.max_restarts)]
    if reset_sighup:
        return [sys.executable, "-c", _SIGHUP_DEFAULT_TRAMPOLINE, *argv]
    return argv


def probe_install(timeout: float) -> tuple[bool, str]:
    """Import the child's entry modules in a fresh interpreter.

    Returns ``(True, "")`` on success, else ``(False, detail)`` where detail
    is the exit status and the last stderr line (the exception), bounded.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g}s"
    except OSError as exc:
        return False, f"could not start: {type(exc).__name__}"
    if result.returncode == 0:
        return True, ""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    last = lines[-1].strip() if lines else "no output"
    return False, f"exit {result.returncode}: {last[:_PROBE_DETAIL_MAX]}"


def _spawn_child(argv: list[str], env: dict[str, str]) -> _Child:
    """Start the child in the supervisor's process group (Ctrl-C reaches it)."""
    return subprocess.Popen(argv, env=env)


def _label(identity: CodeIdentity | None) -> str:
    return identity.label() if identity is not None else "unknown"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


class _Supervisor:
    """Runs one child at a time and decides every restart.

    Signal handlers only set the bool flags below; all decisions happen in
    :meth:`run`'s poll loop.
    """

    _child: _Child

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv
        self._env = {**os.environ, SUPERVISOR_PID_ENV: str(os.getpid())}
        self._started_at = 0.0
        self._after_reexec = False
        self._failures = 0
        self._memo: CodeIdentity | None = None
        self._memo_noted = False
        self._probe_target: CodeIdentity | None = None
        self._probe_delay = _PROBE_BACKOFF_INITIAL_SECONDS
        self._probe_next_at = 0.0
        self._forwarded_at: float | None = None
        self._forward_label = ""
        self._stall_reported = False
        self._next_drift_at = 0.0
        self.stopping = False
        self.terminate = False
        self.operator_hup = False

    def handlers(
        self,
    ) -> dict[signal.Signals, Callable[[int, FrameType | None], None]]:
        return {
            signal.SIGTERM: self._on_sigterm,
            signal.SIGINT: self._on_sigint,
            signal.SIGHUP: self._on_sighup,
        }

    def _on_sigterm(self, _signum: int, _frame: FrameType | None) -> None:
        self.stopping = True
        self.terminate = True

    def _on_sigint(self, _signum: int, _frame: FrameType | None) -> None:
        # A terminal Ctrl-C already reached the child (same process group).
        self.stopping = True

    def _on_sighup(self, _signum: int, _frame: FrameType | None) -> None:
        self.operator_hup = True

    def run(self) -> None:
        """Supervise until a clean stop; raise when the loop is down for good."""
        self._start_child(after_reexec=False)
        while True:
            returncode = self._child.poll()
            if returncode is not None:
                if not self._on_child_exit(returncode):
                    return
                continue
            if self.terminate:
                self._stop_child()
                return
            now = time.monotonic()
            self._check_stall(now)
            self._check_restart(now)
            time.sleep(_POLL_SECONDS)

    def _start_child(self, *, after_reexec: bool) -> None:
        self._child = _spawn_child(self._argv, self._env)
        self._started_at = time.monotonic()
        self._after_reexec = after_reexec

    def _stop_child(self) -> None:
        """SIGTERM: ask the child to stop, then kill it after the grace."""
        self._child.send_signal(signal.SIGINT)
        try:
            self._child.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._child.kill()
            self._child.wait()

    def _on_child_exit(self, returncode: int) -> bool:
        """Handle a child exit; True when a new child was started."""
        self._forwarded_at = None
        # An operator SIGHUP asked about the child that just exited; it must not
        # carry over and restart the replacement.
        self.operator_hup = False
        if self.stopping or returncode == 0:
            return False
        if returncode < 0:
            self._stop_loudly(
                _KILLED_LINE.format(
                    pid=self._child.pid, signal_name=_signal_name(-returncode)
                ),
                returncode,
            )
        healthy = time.monotonic() - self._started_at >= _SERVE_HEALTHY_RUN_SECONDS
        if healthy:
            self._failures = 0
        if returncode == REEXEC_EXIT_CODE and (healthy or not self._after_reexec):
            self._start_child(after_reexec=True)
            return True
        if healthy or not self._after_reexec:
            self._stop_loudly(
                _CHILD_FAILED_LINE.format(pid=self._child.pid, status=returncode),
                returncode,
            )
        return self._record_start_failure(returncode)

    def _record_start_failure(self, returncode: int) -> bool:
        """A child started after a re-exec failed fast: back off or give up.

        True when a new child was started; False when a stop was requested
        during the back-off (nothing is spawned).
        """
        self._failures += 1
        if self._failures >= _START_FAILURE_LIMIT:
            self._stop_loudly(
                _GAVE_UP_LINE.format(failures=self._failures, status=returncode),
                returncode,
            )
        time.sleep(
            min(
                _SERVE_INITIAL_BACKOFF_SECONDS * 2 ** (self._failures - 1),
                _SERVE_BACKOFF_CAP_SECONDS,
            )
        )
        if self.stopping:
            return False
        self._start_child(after_reexec=True)
        return True

    def _stop_loudly(self, line: str, returncode: int) -> NoReturn:
        click.echo(line, err=True)
        with contextlib.suppress(Exception):
            record_event(
                OrchestratorEventType.DISPATCH_LOOP_EXITED,
                payload={
                    "normal": False,
                    "exception_type": None,
                    "codex_threads_still_running": 0,
                    "reason": "supervisor_gave_up",
                    "child_returncode": returncode,
                    "failed_starts": self._failures,
                },
            )
        msg = "dispatch supervisor stopped: the dispatch loop is not running"
        raise DispatchServeError(msg)

    def _check_stall(self, now: float) -> None:
        """Report, once per forwarded SIGHUP, a child that has not exited."""
        if self._forwarded_at is None or self._stall_reported:
            return
        stalled = now - self._forwarded_at
        if stalled < _RESTART_STALL_SECONDS:
            return
        self._stall_reported = True
        pid = self._child.pid
        click.echo(
            _STALLED_LINE.format(pid=pid, minutes=_RESTART_STALL_SECONDS / 60),
            err=True,
        )
        with contextlib.suppress(Exception):
            record_event(
                OrchestratorEventType.DISPATCH_RESTART_STALLED,
                payload={
                    "child_pid": pid,
                    "stalled_seconds": stalled,
                    "checkout": self._forward_label,
                },
            )

    def _check_restart(self, now: float) -> None:
        """Forward a restart when the operator asked or the loop went STALE."""
        if self.stopping or self._forwarded_at is not None:
            return
        if not self.operator_hup and now < self._next_drift_at:
            return
        self._next_drift_at = now + _DRIFT_POLL_SECONDS
        assessment = assess_dispatch_loop()
        if assessment.pid != self._child.pid:
            # Not registered yet (still importing): never signal it.
            return
        if self.operator_hup:
            self.operator_hup = False
            self._probe_then_forward(assessment)
        elif self._auto_restart_due(assessment, now):
            self._probe_then_forward(assessment)

    def _auto_restart_due(self, assessment: LoopAssessment, now: float) -> bool:
        if assessment.status is not LoopStatus.STALE:
            return False
        if now - self._started_at < _MIN_RESTART_INTERVAL_SECONDS:
            return False
        target = assessment.current
        if target == self._memo:
            if not self._memo_noted:
                self._memo_noted = True
                click.echo(
                    _SAME_CHECKOUT_LINE.format(
                        label=_label(target), supervisor_pid=os.getpid()
                    ),
                    err=True,
                )
            return False
        if target != self._probe_target:
            # A new checkout is probed at once, with a fresh backoff.
            self._probe_target = target
            self._probe_delay = _PROBE_BACKOFF_INITIAL_SECONDS
            self._probe_next_at = now
        return now >= self._probe_next_at

    def _probe_then_forward(self, assessment: LoopAssessment) -> None:
        target = assessment.current
        ok, detail = probe_install(_PROBE_TIMEOUT_SECONDS)
        now = time.monotonic()
        if not ok:
            delay = self._probe_delay
            click.echo(
                _PROBE_FAILED_LINE.format(
                    label=_label(target),
                    detail=detail,
                    pid=self._child.pid,
                    delay=delay,
                ),
                err=True,
            )
            self._probe_target = target
            self._probe_next_at = now + delay
            self._probe_delay = min(delay * 2, _PROBE_BACKOFF_CAP_SECONDS)
            return
        self._probe_delay = _PROBE_BACKOFF_INITIAL_SECONDS
        self._probe_next_at = now
        self._memo = target
        self._memo_noted = False
        self._forward_label = _label(target)
        self._child.send_signal(signal.SIGHUP)
        self._forwarded_at = now
        self._stall_reported = False


def run_supervised_serve(
    *,
    max_parallel: int | None,
    use_plan: bool,
    parent: str | None,
    quiet: bool,
    auto_ff: bool,
    client: str | None,
    max_restarts: int,
) -> None:
    """Run ``cw dev-queue serve`` as a supervised, re-execable child.

    SIGHUP is handled only when it is not inherited as ignored (``nohup``);
    then the child is started through the SIG_DFL trampoline so its own
    handler still installs.

    Raises:
        DispatchServeError: When the child cannot be kept running (killed, a
            non-restartable exit, or repeated failed starts).
    """
    options = ServeOptions(
        max_parallel=max_parallel,
        use_plan=use_plan,
        parent=parent,
        quiet=quiet,
        auto_ff=auto_ff,
        client=client,
        max_restarts=max_restarts,
    )
    argv = build_child_argv(options, reset_sighup=sighup_ignored())
    supervisor = _Supervisor(argv)
    with handling_signals(
        supervisor.handlers(), respect_ignored=frozenset({signal.SIGHUP})
    ):
        supervisor.run()
