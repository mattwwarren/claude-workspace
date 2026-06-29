"""RFC 0005 F1 — codex subprocess runner for CodexExecutor.

Parallel to local_runner.py's AiderRunner seam. CodexExecutor delegates the
``codex exec review`` invocation to a CodexRunner so tests can drive every
disposition (exit code, timeout, stderr) without spawning a real subprocess.
"""

from __future__ import annotations

import dataclasses
import subprocess
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path


@dataclasses.dataclass
class CodexRunResult:
    """Outcome of a single codex subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class CodexRunner(Protocol):
    """Testability seam for the codex subprocess invocation."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
    ) -> CodexRunResult:
        """Spawn the codex process and return its outcome."""
        ...


class RealCodexRunner:
    """Production implementation: spawns codex as a real subprocess."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
    ) -> CodexRunResult:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=worktree,
                stdin=subprocess.DEVNULL,
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
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
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
        )


class FakeCodexRunner:
    """Test double: records invocation details; returns configurable results.

    Mirrors FakeAiderRunner in local_runner.py.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        simulate_timeout: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.simulate_timeout = simulate_timeout
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
    ) -> CodexRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "timeout": timeout_seconds,
            }
        )
        if self.simulate_timeout:
            return CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return CodexRunResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )
