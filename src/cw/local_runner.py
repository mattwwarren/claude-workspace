"""RFC 0005 F3 — aider subprocess runner and git-based sentinel synthesis.

Parallel to native_daemon.py (which backs ClaudeNativeExecutor). LocalExecutor
delegates file edits and commits to aider; this module owns spawn, supervise,
harvest, and AutoDevResult synthesis from git facts. The local model never emits
a sentinel — cw synthesizes it from git state after aider commits.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, runtime_checkable

from cw.auto_dev_result import (
    AutoDevResult,
    Blocker,
    Health,
    Review,
    Scope,
    ScopeTier,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_SCHEMA_VERSION: Literal[4] = 4

# --- Reason-string constants (exported for tests and callers) ---
_NUMSTAT_MIN_COLS = 3  # git diff --numstat lines: <added> \t <removed> \t <file>

ENDPOINT_NOT_CONFIGURED = "endpoint_not_configured"
AIDER_NOT_FOUND = "aider_not_found"
PLAN_MISSING = "plan_missing"
AIDER_NO_OUTPUT = "aider_no_output"
AIDER_ERROR = "aider_error"
BUDGET_EXCEEDED = "budget_exceeded"

# --- Shared fixed constants for ALL blocked paths ---
# scope.tier="small" and lines_actual=0 satisfy the stage2_impl post-impl
# invariants (scope.tier and lines_actual must both be non-null after impl).
_blocked_scope = Scope(
    tier="small",
    files=0,
    lines_estimate=0,
    lines_actual=0,
    # TODO: forbidden-area config for local backend is a follow-on (no model today)
    forbidden_touched=False,
)
_FIXED_HEALTH = Health(
    lowest_agent_confidence="LOW",
    any_incomplete_risk=True,
    recommendation="EXIT_FOR_HUMAN_REVIEW",
)
_FIXED_REVIEW = Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0)
_FIXED_NEXT_ACTIONS: list[str] = ["user_resolve_local_executor_failure"]


@dataclasses.dataclass
class AiderRunResult:
    """Outcome of a single aider subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class AiderRunner(Protocol):
    """Testability seam for the aider subprocess invocation."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int | None,
    ) -> AiderRunResult:
        """Spawn the aider process and return its outcome."""
        ...


class RealAiderRunner:
    """Production implementation: spawns aider as a real subprocess."""

    def run(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
        timeout_seconds: int | None,
    ) -> AiderRunResult:
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return AiderRunResult(
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
            return AiderRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return AiderRunResult(
            returncode=proc.returncode,
            stdout=stdout,
            # Bound stderr to avoid bloating the persisted state file.
            stderr=stderr[-4000:],
            timed_out=False,
        )


class FakeAiderRunner:
    """Test double: records invocation details; returns configurable results.

    Mirrors FakeNativeDaemonClient in native_daemon.py.
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
        env: dict[str, str],
        timeout_seconds: int | None,
    ) -> AiderRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "env": dict(env),
                "timeout": timeout_seconds,
            }
        )
        if self.simulate_timeout:
            return AiderRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        return AiderRunResult(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


def build_task_message(worktree: Path) -> str | None:
    """Build the aider task prompt from .cw/plan.md and optional .cw/context.json.

    Returns None when .cw/plan.md is absent (triggers plan_missing blocker in
    spawn()). Uses the same context source the claude-native IMPL stage uses:
    the approved plan posted by the PLAN stage plus the ticket body from the
    intake context.
    """
    plan_path = worktree / ".cw" / "plan.md"
    if not plan_path.exists():
        return None
    plan = plan_path.read_text(encoding="utf-8")

    header = ""
    ctx_path = worktree / ".cw" / "context.json"
    if ctx_path.exists():
        try:
            ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
            title: str = ctx.get("title", "")
            body: str = ctx.get("body", "")
            if title or body:
                header = f"## Ticket: {title}\n\n{body}\n\n"
        except (OSError, json.JSONDecodeError):
            pass

    return f"{header}## Implementation Plan\n\n{plan}"


def build_argv(model: str, task_message: str) -> list[str]:
    """Return the aider argv for the given model and task message.

    Prepends 'openai/' to model when not already present, as required by
    aider's OpenAI-compatible endpoint routing.
    """
    qualified_model = model if model.startswith("openai/") else f"openai/{model}"
    return [
        "aider",
        "--model",
        qualified_model,
        "--message",
        task_message,
        "--yes",
        "--auto-commits",
        "--no-pretty",
        "--no-browser",
        "--no-auto-lint",
        "--no-auto-test",
        "--map-tokens",
        "0",
        "--no-stream",
    ]


def build_env(endpoint: str) -> dict[str, str]:
    """Return the subprocess env dict for aider pointing at a local endpoint.

    OPENAI_API_KEY must be set or aider refuses to start; LM Studio ignores its
    value, so "local" is the documented fallback.
    """
    return {
        **os.environ,
        "OPENAI_API_BASE": endpoint,
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "local"),
    }


class _GitFacts(TypedDict):
    branch: str
    fork_point: str
    commits: list[str]
    files: int
    lines_actual: int


def _git_facts(worktree: Path, default_branch: str) -> _GitFacts:
    """Collect git metadata needed to synthesize an AutoDevResult."""

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")

    fork_point = ""
    with contextlib.suppress(subprocess.CalledProcessError):
        fork_point = _git("merge-base", "HEAD", f"origin/{default_branch}")

    commits: list[str] = []
    files = 0
    lines_actual = 0

    if fork_point:
        commits_out = _git("log", f"{fork_point}..HEAD", "--format=%H")
        commits = [c for c in commits_out.splitlines() if c]

        numstat_out = _git("diff", "--numstat", f"{fork_point}..HEAD")
        for line in numstat_out.splitlines():
            parts = line.split("\t")
            if len(parts) >= _NUMSTAT_MIN_COLS:
                with contextlib.suppress(ValueError):  # binary files show '-'
                    lines_actual += int(parts[0]) + int(parts[1])
                    files += 1

    return _GitFacts(
        branch=branch,
        fork_point=fork_point,
        commits=commits,
        files=files,
        lines_actual=lines_actual,
    )


def _resolve_tier(scope_hint: str | None) -> ScopeTier:
    """Map task.scope_hint to a valid ScopeTier, defaulting to 'small'."""
    if scope_hint == "large":
        return "large"
    return "small"


def make_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str = "",
    retry_eligible: bool | None = None,
    retry_delay_seconds: int | None = None,
) -> AutoDevResult:
    """Return a typed blocked AutoDevResult for any LocalExecutor failure mode."""
    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=ticket_id,
        status="blocked",
        stage_reached="stage2_impl",
        scope=_blocked_scope,
        plan_source="none",
        review=_FIXED_REVIEW,
        health=_FIXED_HEALTH,
        blocker=Blocker(
            stage="stage2_impl",
            reason=reason,
            details=details,
            retry_eligible=retry_eligible,
            retry_delay_seconds=retry_delay_seconds,
        ),
        next_actions=_FIXED_NEXT_ACTIONS,
        worktree_path=str(worktree),
    )


def synthesize_result(
    *,
    task: TicketTask,
    worktree: Path,
    run_result: AiderRunResult,
    default_branch: str,
) -> AutoDevResult:
    """Map an AiderRunResult + git state to a typed AutoDevResult.

    Disposition table:
    - timed_out              → BUDGET_EXCEEDED (blocked, retry_eligible)
    - returncode != 0        → AIDER_ERROR (blocked, stderr tail in details)
    - exit 0, commits found  → stage_complete (synthesized from git facts)
    - exit 0, no commits     → AIDER_NO_OUTPUT (blocked, retry_eligible)
    """
    if run_result.timed_out:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=BUDGET_EXCEEDED,
            retry_eligible=True,
        )

    if run_result.returncode != 0:
        stderr_tail = run_result.stderr[-2000:] if run_result.stderr else ""
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=AIDER_ERROR,
            details=stderr_tail,
        )

    facts = _git_facts(worktree, default_branch)

    if not facts["commits"]:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=AIDER_NO_OUTPUT,
            retry_eligible=True,
            retry_delay_seconds=0,
        )

    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=task.ticket_id,
        status="stage_complete",
        stage_reached="stage2_impl",
        scope=Scope(
            tier=_resolve_tier(task.scope_hint),
            files=facts["files"],
            lines_estimate=0,  # plan/scope_hint line-count mapping is a follow-on
            lines_actual=facts["lines_actual"],
            # TODO: forbidden-area config for local backend is a follow-on
            forbidden_touched=False,
        ),
        plan_source="none",
        branch=facts["branch"],
        fork_point_sha=facts["fork_point"] or None,
        commits=facts["commits"],
        review=Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0),
        health=Health(
            lowest_agent_confidence="HIGH",
            any_incomplete_risk=False,
            recommendation="PROCEED",
        ),
        worktree_path=str(worktree),
    )
