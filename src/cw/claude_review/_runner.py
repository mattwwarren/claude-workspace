"""``claude -p`` subprocess seam for the Claude confirm round (#2791).

The sibling of :mod:`cw.codex_runner`'s ``CodexRunner`` seam, kept separate
because the wire format differs: ``codex exec`` writes its document to an
``-o`` file beside a JSONL event stream, while ``claude -p --output-format
json`` prints one JSON envelope on stdout. Tests drive every disposition
through :class:`FakeClaudeRunner`; nothing here is reached by a test that does
not patch :func:`_which_claude` or construct the fake.

The reviewer's posture is fixed by :func:`build_claude_review_argv` and pinned
by a test: read-only file tools, ``dontAsk``, empty settings sources (no
operator or project hooks), no MCP servers, no slash commands, no session file,
and no ``--add-dir``. The prompt always travels over stdin.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from cw.codex_review._const import _COMMAND_NOT_FOUND_RETURNCODE

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

CLAUDE_BINARY = "claude"

#: The reviewer's whole tool set: read-only file tools, nothing that writes,
#: runs a shell, or reaches the network.
CLAUDE_REVIEW_TOOLS = "Read,Grep,Glob"

# In-memory cap on captured stderr; the diagnostics bundle applies its own,
# tighter cap before persisting (same figure as RealCodexRunner).
_STDERR_CAP = 4000


def _which_claude() -> str | None:
    """Resolve the ``claude`` binary's path.

    A module-level seam (like ``cw.codex_review._capability._which_codex``):
    ``claude`` is not in the test suite's optional-binary denylist, so every
    test that reaches a binary check patches this name instead.
    """
    return shutil.which(CLAUDE_BINARY)


def build_claude_review_argv(*, model: str | None, schema: str) -> list[str]:
    """Return the ``claude -p`` argv for one reviewer role.

    *schema* is the ``ReviewerFindingsDocument`` JSON schema text, passed
    inline to ``--json-schema`` (the envelope then carries the document as
    ``structured_output``). The posture flags, each verified against
    ``claude --help`` 2.1.312 and the deferred-premise runs:

    - ``--tools Read,Grep,Glob`` and ``--permission-mode dontAsk``: the file
      tools work inside the cwd (the worktree) and an out-of-cwd read is
      denied (DP1, DP2).
    - ``--setting-sources ""`` (one empty argv element): neither the
      operator's user settings nor the reviewed repo's project settings load,
      so no hook runs in the reviewer subprocess. Login still resolves (DP3).
    - ``--strict-mcp-config`` (with no ``--mcp-config``): no MCP servers.
    - ``--disable-slash-commands`` and ``--no-session-persistence``.

    ``--model`` is appended only when set; otherwise Claude's own default
    model runs.
    """
    argv = [
        CLAUDE_BINARY,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--tools",
        CLAUDE_REVIEW_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        argv += ["--model", model]
    return argv


@dataclasses.dataclass
class ClaudeRunResult:
    """Outcome of a single ``claude -p`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class ClaudeRunner(Protocol):
    """Testability seam for the ``claude -p`` subprocess invocation."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str,
    ) -> ClaudeRunResult:
        """Run ``claude`` in *worktree* with *stdin* as the prompt."""
        ...


class RealClaudeRunner:
    """Production implementation: spawns ``claude`` as a real subprocess.

    ``cwd`` is the task worktree, which is what confines the file tools.
    No ``env=`` is passed: the child inherits the driver's environment
    (TMPDIR already rewritten into the worktree, ``build_codex_run_env``).
    """

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str,
    ) -> ClaudeRunResult:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=worktree,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return ClaudeRunResult(
                returncode=_COMMAND_NOT_FOUND_RETURNCODE,
                stdout="",
                stderr=f"{argv[0]}: command not found",
            )
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return ClaudeRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return ClaudeRunResult(
            returncode=proc.returncode, stdout=stdout, stderr=stderr[-_STDERR_CAP:]
        )


class FakeClaudeRunner:
    """Test double: records each call and replays configured results.

    *results* are returned in call order; once exhausted (or when none are
    given) every call returns the single-result defaults. *side_effect*, when
    set, is called with the worktree during each call, before the result is
    returned: it stands in for whatever the real subprocess might do to the
    worktree (the G4 mutation cases).
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        simulate_timeout: bool = False,
        results: Sequence[ClaudeRunResult] = (),
        side_effect: Callable[[Path], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.simulate_timeout = simulate_timeout
        self._results = list(results)
        self.side_effect = side_effect
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str,
    ) -> ClaudeRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "timeout": timeout_seconds,
                "stdin": stdin,
            }
        )
        if self.side_effect is not None:
            self.side_effect(worktree)
        if self._results:
            return self._results.pop(0)
        if self.simulate_timeout:
            return ClaudeRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return ClaudeRunResult(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )
