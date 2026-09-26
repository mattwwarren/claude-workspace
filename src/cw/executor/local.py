"""LocalExecutor — fire-and-forget aider backend (RFC 0005 F3)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.executor.core import FireAndForgetRunner, _PreflightOK, _spawn_fire_and_forget
from cw.local_runner import (
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    PLAN_MISSING,
    TASK_CONTEXT_RELATIVE_PATH,
    GithubIssuePlanFetcher,
    PlanFetcher,
    RealAiderRunner,
    aider_available,
    build_aiderignore,
    build_argv,
    build_env,
    build_task_message,
    make_blocked,
)
from cw.plan_files import parse_plan_files_modified
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        Stage,
        StageExecutorConfig,
        TicketTask,
    )


def _local_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
) -> AutoDevResult | _PreflightOK:
    """Run LocalExecutor pre-flight checks (endpoint/aider/plan availability).

    Returns a blocked ``AutoDevResult`` on the first failing check; returns
    ``_PreflightOK`` with the resolved aider argv + env when all checks pass.
    The discriminated return lets callers use ``isinstance(_PreflightOK)`` to
    narrow without ``or ""`` guards on the resolved values. Kept synchronous
    (Addendum 1 Alt A): pre-flight failures complete the session inline, before
    any fire-and-forget launch.
    """
    if config.endpoint is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=ENDPOINT_NOT_CONFIGURED,
        )
    if not aider_available():
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=AIDER_NOT_FOUND,
            retry_eligible=True,
            retry_delay_seconds=0,
        )
    plan_fetcher: PlanFetcher | None = None
    if resolve_tracker(client.workspace_path) == TRACKER_GITHUB_ISSUES:
        plan_fetcher = GithubIssuePlanFetcher()
    task_message = build_task_message(
        worktree,
        ticket_id=task.ticket_id,
        plan_fetcher=plan_fetcher,
    )
    if task_message is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=PLAN_MISSING,
        )
    # Both files below are guaranteed on disk by build_task_message's own
    # materialise-before-return contract (it writes .cw/plan.md on a tracker
    # fetch, and the task-context file unconditionally on the success path).
    # The re-read is still suppressed: an unreadable plan degrades to "no
    # manifest" (zero --file flags, aider's own heuristic), never to a crash.
    plan_text = ""
    with contextlib.suppress(OSError):
        plan_text = (worktree / ".cw" / "plan.md").read_text(encoding="utf-8")
    # The plan's ``## Files Modified`` manifest is aider's explicit edit set
    # (#1905); a non-empty one also materialises an --aiderignore blocking
    # every tracked file outside it (#1915).
    files = parse_plan_files_modified(plan_text)
    return _PreflightOK(
        argv=build_argv(
            config.model or "",
            task_message,
            files,
            worktree / TASK_CONTEXT_RELATIVE_PATH,
            build_aiderignore(worktree, files),
        ),
        env=build_env(config.endpoint),  # narrowed: is-None check above
    )


class LocalExecutor:
    """StageExecutor backed by a fire-and-forget aider subprocess (RFC 0005 F3).

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches aider via ``AiderRunner.launch`` (Popen, no wait),
    records a ``Session.local_liveness`` handle (PID + /proc start-time), leaves
    the session ACTIVE, and returns the sid immediately. The aider run completes
    asynchronously; reconcile/local harvest later detects the dead process,
    synthesizes an AutoDevResult from git facts, and completes the session. This
    keeps the shared ``dispatch_tick`` thread from blocking for the full run.

    Pre-flight failures (endpoint/aider/plan missing) stay synchronous: they
    persist a blocked result to Session.last_result via the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT — RFC 0012 A2, #1458), mark
    the session COMPLETED, and emit SESSION_COMPLETED before returning — the
    launch never happens. The shared skeleton lives in
    ``cw.executor.core._spawn_fire_and_forget`` (#2369).

    Result delivery bypasses stdout-sentinel parsing entirely. Every
    SESSION_COMPLETED event this class or the harvest path emits carries no
    result payload; the last_result written onto the session by the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT) is consumed as-is by
    consume_completed_sessions.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: FireAndForgetRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: FireAndForgetRunner = (
            runner if runner is not None else RealAiderRunner()
        )

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        # parent is intentionally unused; aider runs have no parent-session
        # concept. wall_clock_budget_seconds is unused on the fire-and-forget
        # launch path — the harvest sweep, not a blocking timeout, bounds the run.
        del parent, wall_clock_budget_seconds

        def _blocked(*, reason: str, details: str) -> AutoDevResult:
            return make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=reason,
                details=details,
            )

        return _spawn_fire_and_forget(
            task=task,
            worktree=worktree,
            client=client,
            stage=stage,
            executor_name="aider",
            preflight_fn=lambda _sid: _local_preflight(
                self._config, task, worktree, client
            ),
            blocked_ctor=_blocked,
            runner=self._runner,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
