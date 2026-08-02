"""RFC 0005 F3 — aider subprocess runner and git-based sentinel synthesis.

Parallel to native_daemon.py (which backs ClaudeNativeExecutor). LocalExecutor
delegates file edits and commits to aider; this module owns spawn, supervise,
harvest, and AutoDevResult synthesis from git facts. The local model never emits
a sentinel — cw synthesizes it from git state after aider commits.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, runtime_checkable

import psutil

from cw.auto_dev_result import (
    AutoDevResult,
    Blocker,
    Health,
    PlanSource,
    Review,
    Scope,
    ScopeTier,
    StageReached,
)
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.gh import fetch_approved_plan_comment
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.worktree import _parse_numstat_totals

if TYPE_CHECKING:
    from cw.models import TicketTask

_SCHEMA_VERSION: Literal[4] = 4

# --- Reason-string constants (exported for tests and callers) ---
_AIDER_LOG_RELATIVE_PATH: Path = Path(".cw", "aider.log")
_AIDER_LOG_TAIL_CHARS = 4000  # matches codex_runner.py's stderr[-4000:] convention

ENDPOINT_NOT_CONFIGURED = "endpoint_not_configured"
AIDER_NOT_FOUND = "aider_not_found"
PLAN_MISSING = "plan_missing"
AIDER_NO_OUTPUT = "aider_no_output"
UNEXPECTED_ERROR = "unexpected_error"
LIVENESS_UNAVAILABLE = "liveness_unavailable"

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


def aider_available() -> bool:
    """Return True if the aider binary is on PATH."""
    return shutil.which("aider") is not None


@runtime_checkable
class AiderRunner(Protocol):
    """Testability seam for the aider subprocess launch (RFC 0005 F3, #888)."""

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        """Fire-and-forget spawn of the aider process; return the live Popen.

        The caller does NOT wait — it captures the PID + start-time as a
        liveness handle and returns immediately. reconcile/local harvest later
        detects the dead process and synthesizes the git-based completion.
        """
        ...


class RealAiderRunner:
    """Production implementation: launches aider as a detached subprocess."""

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        # Redirect to a per-run log file (never PIPE — nothing reads the pipe on
        # this fire-and-forget path, and an unread full pipe buffer deadlocks the
        # child; a file has no such backpressure). Truncated ("w") on every call
        # so a retry into the same worktree does not bleed a prior attempt's
        # output into the next harvest read.
        log_path = worktree / _AIDER_LOG_RELATIVE_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as log_file:
            return subprocess.Popen(
                argv,
                env=env,
                cwd=worktree,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )


class FakeAiderRunner:
    """Test double: records the launch call; returns a real live subprocess.

    Returns ``Popen(["sleep", "60"])`` rather than a fast-exiting process so the
    caller's ``read_process_start_time_ns`` lookup does not race a just-exited
    PID. Mirrors FakeNativeDaemonClient in native_daemon.py.
    Spawned processes are tracked in ``self.procs`` so tests can kill them.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.procs: list[subprocess.Popen[bytes]] = []

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "env": dict(env),
            }
        )
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc


def read_process_start_time_ns(pid: int) -> int | None:
    """Return the process start-time in ns, or None if unreadable.

    Uses ``psutil.Process(pid).create_time()`` (epoch seconds as a float) on all
    platforms — no ``/proc`` field parsing or ``sys.platform`` branch — so macOS
    and Linux share one code path (issue #921). The float is converted to integer
    nanoseconds to preserve the historical ``int | None`` contract. Returns None
    when the PID is gone or inaccessible — the caller treats None as "process not
    alive".
    """
    try:
        return int(psutil.Process(pid).create_time() * 1_000_000_000)
    except (psutil.Error, ValueError):
        return None


@runtime_checkable
class PlanFetcher(Protocol):
    """Testability seam for fetching the approved plan from an external tracker."""

    def fetch(self, ticket_id: str) -> str | None:
        """Return the approved plan body for *ticket_id*, or None if unavailable."""
        ...


class GithubIssuePlanFetcher:
    """Fetches the approved plan from a GitHub issue's comments (production)."""

    def fetch(self, ticket_id: str) -> str | None:
        return fetch_approved_plan_comment(ticket_id)


class FakePlanFetcher:
    """Test double for PlanFetcher.

    Returns a configurable plan body and records all ticket_id arguments
    passed to fetch(). Mirrors FakeAiderRunner in test-double style.
    """

    def __init__(self, plan: str | None = None) -> None:
        self.plan = plan
        self.calls: list[str] = []

    def fetch(self, ticket_id: str) -> str | None:
        self.calls.append(ticket_id)
        return self.plan


def build_task_message(
    worktree: Path,
    *,
    ticket_id: str | None = None,
    plan_fetcher: PlanFetcher | None = None,
) -> str | None:
    """Build the aider task prompt from .cw/plan.md and optional .cw/context.json.

    When .cw/plan.md is absent and both *ticket_id* and *plan_fetcher* are
    provided, fetches the approved plan from the tracker (GitHub issue comment
    carrying the ``<!-- plan-spec-reviewed`` marker). On a successful fetch the
    plan is materialised to .cw/plan.md so subsequent retries do not need to
    re-fetch.

    Returns None when no plan is available — either .cw/plan.md is absent and
    the tracker also has no approved plan, or no fetcher/ticket_id was provided.
    This triggers the plan_missing blocker in spawn().
    """
    plan_path = worktree / ".cw" / "plan.md"
    if not plan_path.exists():
        if ticket_id is None or plan_fetcher is None:
            return None
        fetched = plan_fetcher.fetch(ticket_id)
        if fetched is None:
            return None
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(fetched, encoding="utf-8")

    plan = plan_path.read_text(encoding="utf-8")

    header = ""
    ctx_path = worktree / CONTEXT_JSON_RELATIVE_PATH
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


# Git identity and core vars aider needs for commits. The subprocess receives
# only these (plus AIDER_* and the OPENAI_* overrides) — operator shell secrets
# (AWS_*, tokens, etc.) are excluded by default.
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "SHELL",
        # Git identity — required for aider to commit
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
    }
)


def build_env(endpoint: str) -> dict[str, str]:
    """Return the subprocess env dict for aider pointing at a local endpoint.

    Passes only an explicit allowlist of env vars plus OPENAI_* overrides.
    All operator shell secrets (AWS_*, tokens, etc.) are excluded by default.

    OPENAI_API_KEY must be set or aider refuses to start; LM Studio ignores its
    value, so "local" is the documented fallback.
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    # Forward all AIDER_* vars (dynamic; not enumerated in the static allowlist)
    env.update({k: v for k, v in os.environ.items() if k.startswith("AIDER_")})
    env["OPENAI_API_BASE"] = endpoint
    env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "local")
    return env


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
        # Shared with worktree.compute_branch_diff_scope so every producer of
        # scope.files / scope.lines_actual counts a diff the same way (#1487).
        files, lines_actual = _parse_numstat_totals(numstat_out)

    return _GitFacts(
        branch=branch,
        fork_point=fork_point,
        commits=commits,
        files=files,
        lines_actual=lines_actual,
    )


def resolve_tier(scope_hint: str | None) -> ScopeTier:
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
    stage_reached: StageReached = "stage2_impl",
) -> AutoDevResult:
    """Return a typed blocked AutoDevResult for any LocalExecutor failure mode."""
    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=ticket_id,
        status="blocked",
        stage_reached=stage_reached,
        scope=_blocked_scope,
        plan_source="none",
        review=_FIXED_REVIEW,
        health=_FIXED_HEALTH,
        blocker=Blocker(
            stage=stage_reached,
            reason=reason,
            details=details,
            retry_eligible=retry_eligible,
            retry_delay_seconds=retry_delay_seconds,
        ),
        next_actions=_FIXED_NEXT_ACTIONS,
        worktree_path=str(worktree),
    )


def _persist_aider_no_output_diagnostics(*, session_id: str, log_tail: str) -> None:
    """Write a ``missing_output`` diagnostics bundle for an AIDER_NO_OUTPUT harvest.

    No aider exit code / argv is available on the harvest path, so those fields
    are left None/empty; the .cw/aider.log tail (already bounded upstream) is
    reused as the stdout excerpt. Never raises (persist swallows OSError).
    """
    failure = build_executor_failure(
        category="missing_output",
        executor_name="aider",
        session_id=session_id,
        argv=[],
        stdout_excerpt=log_tail,
        stderr_excerpt="",
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="aider",
        failure=failure,
    )


def synthesize_git_result(
    *,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    plan_source: PlanSource = "none",
    session_id: str | None = None,
) -> AutoDevResult:
    """Map the worktree's git state to a typed AutoDevResult (RFC 0005 F3, #888).

    Called by reconcile/local harvest AFTER the fire-and-forget aider process
    has exited. The local model never emits a sentinel — cw synthesizes it from
    git facts, plus a best-effort tail of the process's captured log on the
    no-commits path (no aider exit code is available in either case):

    - commits since fork point  → stage_complete (synthesized from git facts)
    - no commits                → AIDER_NO_OUTPUT (blocked, retry_eligible,
      details populated from the .cw/aider.log tail when readable)

    *session_id* is optional (defaulted for the 8 existing test call sites that
    do not exercise the diagnostics path): when set, the AIDER_NO_OUTPUT branch
    also persists a typed ``missing_output`` diagnostics bundle under that
    session's diagnostics dir, and ``details`` gets a trailing
    ``[diagnostics: <bundle path>]`` pointer appended (#1239). None makes both
    of those a no-op, leaving ``details`` as the bare log tail (or empty).
    """
    facts = _git_facts(worktree, default_branch)

    if not facts["commits"]:
        details = ""
        log_path = worktree / _AIDER_LOG_RELATIVE_PATH
        with contextlib.suppress(OSError):
            details = log_path.read_text(encoding="utf-8", errors="replace")[
                -_AIDER_LOG_TAIL_CHARS:
            ]
        if session_id is not None:
            _persist_aider_no_output_diagnostics(
                session_id=session_id, log_tail=details
            )
            details = append_diagnostics_pointer(details, session_id=session_id)
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=AIDER_NO_OUTPUT,
            details=details,
            retry_eligible=True,
            retry_delay_seconds=0,
        )

    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=task.ticket_id,
        status="stage_complete",
        stage_reached="stage2_impl",
        scope=Scope(
            tier=resolve_tier(task.scope_hint),
            files=facts["files"],
            lines_estimate=0,  # plan/scope_hint line-count mapping is a follow-on
            lines_actual=facts["lines_actual"],
            # TODO: forbidden-area config for local backend is a follow-on
            forbidden_touched=False,
        ),
        plan_source=plan_source,
        branch=facts["branch"],
        fork_point_sha=facts["fork_point"] or None,
        commits=facts["commits"],
        review=Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0),
        # Why: at this point synthesize_git_result knows only that commits exist
        # and how many files/lines changed — it has no reviewer, no test run, no
        # vetting of any kind. Claiming HIGH/PROCEED asserted a review that never
        # happened (#1580, sibling of #1551's codex-producer fix). Mirrors
        # _FIXED_HEALTH's pessimistic-default posture (`:67-71`): a producer that
        # cannot vouch for the work should not claim it can.
        health=Health(
            lowest_agent_confidence="MEDIUM",
            any_incomplete_risk=True,
            recommendation="EXIT_FOR_HUMAN_REVIEW",
        ),
        worktree_path=str(worktree),
    )
