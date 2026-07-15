"""RFC 0005 F1 — codex subprocess runner for CodexExecutor.

Parallel to local_runner.py's AiderRunner seam. CodexExecutor delegates the
``codex exec review`` invocation to a CodexRunner so tests can drive every
disposition (exit code, timeout, stderr) without spawning a real subprocess.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable


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
            output_file_content=_read_output_file(argv),
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
            output_file_content=self.output_file_content,
        )
