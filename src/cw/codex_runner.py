"""RFC 0005 F1 — codex subprocess runner for CodexExecutor.

Parallel to local_runner.py's AiderRunner seam. The codex review delegates each
per-role ``codex exec`` invocation to a CodexRunner so tests can drive every
disposition (exit code, timeout, stderr) without spawning a real subprocess.

A second, distinct seam lives here too (RFC 0014 A2, #2388): the job launcher
``RealCodexJobRunner`` plus ``build_codex_run_argv``/``build_codex_run_env``,
which ``CodexExecutor.spawn()`` uses to start the whole review as a detached
``cw codex run`` subprocess. That subprocess then drives the per-role
``CodexRunner`` calls itself. This module must never import ``cw.codex_driver``:
``cw.executor`` imports it, and ``cw.executor`` must never reach the driver
(D-1, the process boundary).
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from cw.executor_launch import _launch_logged_subprocess
from cw.worktree import apply_worker_tmpdir

# Per-run output of the detached ``cw codex run`` job. Private: harvest never
# parses it — reconcile/local's codex branch (A1) decides from git and audit
# facts, not from a log.
_CODEX_DRIVER_LOG_RELATIVE_PATH: Path = Path(".cw", "codex_driver.log")

# Mirrors ``cw.codex_driver.STAGE_REVIEW``; not imported, to keep this module
# (imported by ``cw.executor.codex``) from ever reaching ``cw.codex_driver``.
_CODEX_RUN_STAGE_REVIEW = "review"


def build_codex_run_argv(
    *,
    ticket_id: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
) -> list[str]:
    """Return the argv that runs the codex review stage as ``cw codex run``.

    ``sys.executable -m cw`` rather than resolving a ``cw`` binary on PATH
    (contrast ``cw.watchdog._resolve_cw_executable_path``, which serves a
    systemd/launchd unit written ahead of time with no inherited PATH): the
    parent here IS a running ``cw`` process, so its own interpreter already
    has the package importable.
    """
    argv = [
        sys.executable,
        "-m",
        "cw",
        "codex",
        "run",
        ticket_id,
        "--stage",
        _CODEX_RUN_STAGE_REVIEW,
        "--session-id",
        session_id,
    ]
    if wall_clock_budget_seconds is not None:
        argv.extend(["--wall-clock-budget-seconds", str(wall_clock_budget_seconds)])
    return argv


def build_codex_run_env(worktree: Path) -> dict[str, str]:
    """Return the full parent environment for the ``cw codex run`` job.

    Deliberately NOT aider/opencode's narrow allowlist. The subprocess is
    another ``cw`` process: it must resolve the same ``CONFIG_DIR``/
    ``STATE_DIR`` (``cw.config`` derives them from ``XDG_CONFIG_HOME``/
    ``XDG_DATA_HOME`` at import time) as the parent ``cw dev-queue serve``.
    The in-process review's ``codex exec`` calls already inherited the full
    serve environment unfiltered; narrowing it here would silently change what
    ``codex exec`` can see.

    The one exception: TMPDIR/TMP/TEMP are force-set to *worktree*'s own
    scratch dir via :func:`cw.worktree.apply_worker_tmpdir` (#2470).
    ``RealCodexRunner.run``'s ``Popen`` passes no ``env=``, so every per-role
    ``codex exec`` the driver starts inherits this value transitively.
    """
    env = dict(os.environ)
    apply_worker_tmpdir(env, worktree)
    return env


class RealCodexJobRunner:
    """Launches the detached ``cw codex run`` job (RFC 0014 A2, #2388).

    Satisfies ``cw.executor.core.FireAndForgetRunner`` structurally; not
    imported, so this module stays free of the executor package. Output goes
    to ``.cw/codex_driver.log`` in the worktree, and the child gets its own
    session (``start_new_session=True``) so it outlives a serve restart.
    """

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        return _launch_logged_subprocess(
            worktree, argv, env, _CODEX_DRIVER_LOG_RELATIVE_PATH
        )


@dataclasses.dataclass
class CodexRunResult:
    """Outcome of a single codex subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Contents of the file at the argv "-o" path, read after the process
    # exits. None when the flag was absent, the file was never written, or
    # it could not be read (issue #1203).
    output_file_content: str | None = None


def _read_output_file(argv: list[str]) -> str | None:
    """Return the contents of the file following "-o" in *argv*, or None.

    Returns None when "-o" is absent from argv, has no following element, the
    target file cannot be read (missing, permissions, etc.), or its bytes
    aren't valid UTF-8 — the caller treats None as "no structured output
    available". The content originates from an external process (codex), so
    a decode failure is a real possibility, not just a missing-file case.
    """
    if "-o" not in argv:
        return None
    idx = argv.index("-o")
    if idx + 1 >= len(argv):
        return None
    output_path = Path(argv[idx + 1])
    try:
        return output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@runtime_checkable
class CodexRunner(Protocol):
    """Testability seam for the codex subprocess invocation."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        """Spawn the codex process and return its outcome.

        *stdin*, when set, is written to the process's standard input (used to
        feed a materialized reviewer prompt to ``codex exec``); when ``None``,
        the process gets ``/dev/null`` on stdin (the pre-#1236 behavior).
        """
        ...


class RealCodexRunner:
    """Production implementation: spawns codex as a real subprocess."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=worktree,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return CodexRunResult(
                returncode=127,
                stdout="",
                stderr=f"{argv[0]}: command not found",
                timed_out=False,
            )
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return CodexRunResult(
            returncode=proc.returncode,
            stdout=stdout,
            # Cap in-memory allocation; caller applies a tighter cap before persisting.
            stderr=stderr[-4000:],
            timed_out=False,
            output_file_content=_read_output_file(argv),
        )


class FakeCodexRunner:
    """Test double: records invocation details; returns configurable results.

    Mirrors FakeFireAndForgetRunner in cw.executor.core.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        simulate_timeout: bool = False,
        output_file_content: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.simulate_timeout = simulate_timeout
        self.output_file_content = output_file_content
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "timeout": timeout_seconds,
                "stdin": stdin,
            }
        )
        if self.simulate_timeout:
            return CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return CodexRunResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            output_file_content=self.output_file_content,
        )
