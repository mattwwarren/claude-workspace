"""RFC 0005 A2/E1/F3 — StageExecutor seam + ClaudeNativeExecutor + LocalExecutor."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from cw.auto_dev_result import (
    AutoDevResult,
    Health,
    PlanSource,
    Review,
    Scope,
)
from cw.codex_runner import CodexRunner, CodexRunResult, RealCodexRunner
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event as _record_orchestrator_event
from cw.local_runner import (
    _SCHEMA_VERSION,
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    PLAN_MISSING,
    UNEXPECTED_ERROR,
    AiderRunner,
    GithubIssuePlanFetcher,
    PlanFetcher,
    RealAiderRunner,
    build_argv,
    build_env,
    build_task_message,
    make_blocked,
    resolve_tier,
    synthesize_result,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    ClientConfig,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.spawn import spawn_create_impl
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient

# CodexExecutor blocker reason codes (RFC 0005 F1).
CODEX_NOT_FOUND = "codex_not_found"
CODEX_REVIEW_ONLY = "codex_review_only"
CODEX_TIMEOUT = "codex_timeout"
CODEX_ERROR = "codex_error"


def resolve_executor_config(
    stage: Stage,
    task: TicketTask,
    client: ClientConfig,
) -> StageExecutorConfig:
    """Return the effective StageExecutorConfig for a stage, with lane override (E1).

    Three-level priority: lane stage config > client stage config > default.
    """
    if task.lane:
        for lane_cfg in client.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.pipeline is not None:
                lane_stage_config = lane_cfg.pipeline.executors.get(stage)
                if lane_stage_config is not None:
                    return lane_stage_config
                break
    return client.pipeline.executors.get(stage, StageExecutorConfig())


def resolve_executor(
    task: TicketTask,
    client: ClientConfig,
    *,
    native_daemon: NativeDaemonClient | None = None,
) -> StageExecutor:
    """Return the executor for task.stage, selected by backend (RFC 0005 E1)."""
    config = resolve_executor_config(task.stage, task, client)
    if config.backend == LOCAL_BACKEND:
        return LocalExecutor(config=config)
    if config.backend == CODEX_BACKEND:
        return CodexExecutor(config=config)
    if config.backend == CLAUDE_NATIVE_BACKEND:
        return ClaudeNativeExecutor(native_daemon=native_daemon)
    msg = f"unknown executor backend: {config.backend!r}"
    raise ValueError(msg)


@runtime_checkable
class StageExecutor(Protocol):
    """Protocol for executing a single pipeline stage (RFC 0005 A2)."""

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
        """Spawn the stage session; return the cw session id."""
        ...

    def stage_sentinel_schema(self, stage: Stage) -> dict[str, Any]:
        """Return the expected JSON schema for this stage's result sentinel."""
        ...


class ClaudeNativeExecutor:
    """StageExecutor backed by claude --bg via spawn_create_impl (RFC 0005 A2).

    # Why: A2 seam — wraps spawn_create_impl without modifying it.
    # --model forwarding uses client.model_copy to avoid double-flag.
    # stage_sentinel_schema bridges to AutoDevResult until A3 per-stage schemas.
    """

    def __init__(self, *, native_daemon: NativeDaemonClient | None = None) -> None:
        self._native_daemon = native_daemon

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
        stage_config = resolve_executor_config(stage, task, client)
        effective_model = stage_config.model or client.worker_model
        effective_client = client.model_copy(update={"worker_model": effective_model})
        return spawn_create_impl(
            client=effective_client,
            worktree=worktree,
            prompt=f"/auto-dev-{stage.value} {task.ticket_id} --headless",
            label=f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            ticket_id=task.ticket_id,
            lane=task.lane,
            headless=True,
            purpose=SessionPurpose.IMPL,
            permission_mode=None,
            parent=parent,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            native_daemon=self._native_daemon,
            task=task,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        # Why: per-stage result models do not exist until A3 (#614).
        # Bridge via the current monolith sentinel until then.
        return AutoDevResult.model_json_schema()


class LocalExecutor:
    """StageExecutor backed by aider subprocess + git synthesis (RFC 0005 F3).

    spawn() is synchronous: blocks for the full aider run, synthesizes an
    AutoDevResult from git facts, persists it to Session.last_result, and emits
    SESSION_COMPLETED before returning. Appropriate only for max_parallel=1 lanes.

    Result delivery bypasses persist_last_result (no sentinel framing). The
    SESSION_COMPLETED event carries no 'stdout' key, so dispatch.py:~1513's
    isinstance(stdout, str) guard is False and persist_last_result is skipped;
    the last_result written in Step 4 is consumed as-is by consume_completed_sessions.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: AiderRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: AiderRunner = runner if runner is not None else RealAiderRunner()

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
        # parent is intentionally unused; aider runs have no parent-session concept.
        del parent
        # Step 1: Create Session with all required fields.
        sess = Session(
            name=f"{client.name}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=worktree,
            stage=stage,
            lane=task.lane,
        )
        sid = sess.id
        with sessions_lock():
            state = load_state()
            state.sessions.append(sess)
            save_state(state)

        # Step 2: Pre-flight checks (first match assigns result).
        result: AutoDevResult | None = None
        task_message: str | None = None
        plan_source: PlanSource = "none"

        if self._config.endpoint is None:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=ENDPOINT_NOT_CONFIGURED,
            )
        elif shutil.which("aider") is None:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=AIDER_NOT_FOUND,
                retry_eligible=True,
                retry_delay_seconds=0,
            )
        else:
            plan_fetcher: PlanFetcher | None = None
            if resolve_tracker(client.workspace_path) == TRACKER_GITHUB_ISSUES:
                plan_fetcher = GithubIssuePlanFetcher()
            plan_was_on_disk = (worktree / ".cw" / "plan.md").exists()
            task_message = build_task_message(
                worktree,
                ticket_id=task.ticket_id,
                plan_fetcher=plan_fetcher,
            )
            if task_message is None:
                result = make_blocked(
                    ticket_id=task.ticket_id,
                    worktree=worktree,
                    reason=PLAN_MISSING,
                )
            elif not plan_was_on_disk and plan_fetcher is not None:
                plan_source = "github_issue_existing"

        try:
            if result is None:
                # Step 3: Run aider (only reached when all pre-flight checks pass).
                model = self._config.model or ""
                argv = build_argv(model, task_message or "")
                env = build_env(self._config.endpoint or "")
                run_result = self._runner.run(
                    worktree, argv, env, wall_clock_budget_seconds
                )
                result = synthesize_result(
                    task=task,
                    worktree=worktree,
                    run_result=run_result,
                    default_branch=client.default_branch,
                    plan_source=plan_source,
                )

            # Step 4: Persist result under sessions_lock.
            with sessions_lock():
                state = load_state()
                target = next((s for s in state.sessions if s.id == sid), None)
                if target is not None:
                    target.last_result = result.model_dump(mode="json")
                    target.status = SessionStatus.COMPLETED
                    save_state(state)

            # Step 5: Emit SESSION_COMPLETED — no "stdout" key so dispatch.py skips
            # persist_last_result and uses the last_result written in Step 4 as-is.
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            # Ensure session is never left ACTIVE on unexpected errors (e.g. git
            # CalledProcessError in _git_facts, OSError from Popen). Mark it
            # COMPLETED with a blocked result so reconcile can clean it up.
            # SESSION_COMPLETED is NOT emitted; dispatch's exception handler reverts
            # the task to PENDING, which is the correct recovery path.
            with sessions_lock():
                state = load_state()
                target = next((s for s in state.sessions if s.id == sid), None)
                if target is not None and target.status != SessionStatus.COMPLETED:
                    target.last_result = make_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                    ).model_dump(mode="json")
                    target.status = SessionStatus.COMPLETED
                    save_state(state)
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


class CodexExecutor:
    """StageExecutor backed by ``codex exec review`` subprocess (RFC 0005 F1).

    REVIEW-only: spawn() returns make_blocked(reason=CODEX_REVIEW_ONLY) if
    called on any stage other than REVIEW. Synthesizes an AutoDevResult directly
    from the codex exit code — codex stdout is raw markdown, not a sentinel, so
    on a clean exit the raw findings are posted as a GitHub issue comment.

    Like LocalExecutor, spawn() is synchronous and bypasses persist_last_result:
    the SESSION_COMPLETED event carries no 'stdout' key, so dispatch consumes the
    last_result written here as-is. Appropriate only for max_parallel=1 lanes.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: CodexRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: CodexRunner = runner if runner is not None else RealCodexRunner()

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
        # parent is intentionally unused; codex has no parent-session concept.
        del parent
        # Step 1: Create Session with all required fields.
        sess = Session(
            name=f"{client.name}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=worktree,
            stage=stage,
            lane=task.lane,
        )
        sid = sess.id
        with sessions_lock():
            state = load_state()
            state.sessions.append(sess)
            save_state(state)

        # Step 2: Pre-flight checks (first match assigns result).
        result: AutoDevResult | None = None
        if stage != Stage.REVIEW:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_REVIEW_ONLY,
                stage_reached="stage3_review",
            )
        elif shutil.which("codex") is None:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_NOT_FOUND,
                stage_reached="stage3_review",
            )

        try:
            if result is None:
                # Step 3: Run codex (only reached when pre-flight checks pass).
                argv = _build_codex_argv(self._config.model, client.default_branch)
                run_result = self._runner.run(worktree, argv, wall_clock_budget_seconds)
                result = _synthesize_codex_result(
                    task=task,
                    worktree=worktree,
                    run_result=run_result,
                    ticket_id=task.ticket_id,
                )
                # Step 3b: Post raw review findings as an issue comment (best-effort).
                if (
                    not run_result.timed_out
                    and run_result.returncode == 0
                    and run_result.stdout
                ):
                    _post_review_comment(task.ticket_id, run_result.stdout)

            # Step 4: Persist result under sessions_lock.
            with sessions_lock():
                state = load_state()
                target = next((s for s in state.sessions if s.id == sid), None)
                if target is not None:
                    target.last_result = result.model_dump(mode="json")
                    target.status = SessionStatus.COMPLETED
                    save_state(state)

            # Step 5: Emit SESSION_COMPLETED — no "stdout" key so dispatch skips
            # persist_last_result and uses the last_result written in Step 4.
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            # Ensure the session is never left ACTIVE on unexpected errors (e.g.
            # git CalledProcessError in _synthesize_codex_result). Mark it
            # COMPLETED with a blocked result so reconcile can clean it up.
            # SESSION_COMPLETED is NOT emitted; dispatch's exception handler
            # reverts the task to PENDING, which is the correct recovery path.
            with sessions_lock():
                state = load_state()
                target = next((s for s in state.sessions if s.id == sid), None)
                if target is not None and target.status != SessionStatus.COMPLETED:
                    target.last_result = make_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                        stage_reached="stage3_review",
                    ).model_dump(mode="json")
                    target.status = SessionStatus.COMPLETED
                    save_state(state)
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


def _build_codex_argv(model: str | None, default_branch: str) -> list[str]:
    """Return the ``codex exec review`` argv for the given model and base branch."""
    argv = ["codex", "exec", "review", "--base", default_branch]
    if model:
        argv += ["-m", model]
    return argv


def _synthesize_codex_result(
    *,
    task: TicketTask,
    worktree: Path,
    run_result: CodexRunResult,
    ticket_id: str,
) -> AutoDevResult:
    """Map a CodexRunResult to a typed AutoDevResult at stage3_review.

    Disposition table:
    - timed_out         → CODEX_TIMEOUT (blocked, retry_eligible)
    - returncode != 0   → CODEX_ERROR (blocked, stderr tail in details)
    - exit 0            → stage_complete (raw findings posted by the caller)
    """
    if run_result.timed_out:
        return make_blocked(
            ticket_id=ticket_id,
            worktree=worktree,
            reason=CODEX_TIMEOUT,
            retry_eligible=True,
            stage_reached="stage3_review",
        )
    if run_result.returncode != 0:
        return make_blocked(
            ticket_id=ticket_id,
            worktree=worktree,
            reason=CODEX_ERROR,
            details=run_result.stderr[-2000:],
            stage_reached="stage3_review",
        )
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree,
        text=True,
    ).strip()
    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=ticket_id,
        status="stage_complete",
        stage_reached="stage3_review",
        scope=Scope(
            tier=resolve_tier(task.scope_hint),
            files=0,
            lines_estimate=0,
            lines_actual=0,
            forbidden_touched=False,
        ),
        plan_source="none",
        branch=branch,
        fork_point_sha=None,
        commits=[],
        review=Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0),
        health=Health(
            lowest_agent_confidence="HIGH",
            any_incomplete_risk=False,
            recommendation="PROCEED",
        ),
        worktree_path=str(worktree),
    )


def _post_review_comment(ticket_id: str, review_text: str) -> None:
    """Post codex review findings as a GitHub issue comment (best-effort)."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["gh", "issue", "comment", ticket_id, "--body", review_text],
            capture_output=True,
            timeout=30,
            check=False,
        )
