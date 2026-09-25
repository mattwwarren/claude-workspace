"""LocalExecutor — fire-and-forget aider backend (RFC 0005 F3)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, NamedTuple

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event as _record_orchestrator_event
from cw.executor.core import _complete_session_via_door
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.local_runner import (
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    LIVENESS_UNAVAILABLE,
    PLAN_MISSING,
    TASK_CONTEXT_RELATIVE_PATH,
    UNEXPECTED_ERROR,
    AiderRunner,
    GithubIssuePlanFetcher,
    PlanFetcher,
    RealAiderRunner,
    aider_available,
    build_aiderignore,
    build_argv,
    build_env,
    build_task_message,
    make_blocked,
    read_process_start_time_ns,
)
from cw.models import (
    ClientConfig,
    LocalLivenessHandle,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.plan_files import parse_plan_files_modified
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path


class _PreflightOK(NamedTuple):
    """Resolved launch parameters returned by _local_preflight on success."""

    endpoint: str
    model: str
    task_message: str
    # The plan's ``## Files Modified`` manifest — aider's explicit edit set
    # (#1905). Empty when the plan has no manifest section.
    files: list[str]
    # The materialised read-only task-context file passed to aider as --read.
    read_only_path: Path
    # The materialised --aiderignore file blocking every tracked file outside
    # the manifest (#1915); None when the manifest is empty (no restriction).
    aiderignore_path: Path | None


def _local_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
) -> AutoDevResult | _PreflightOK:
    """Run LocalExecutor pre-flight checks (endpoint/aider/plan availability).

    Returns a blocked ``AutoDevResult`` on the first failing check; returns
    ``_PreflightOK`` with the resolved launch parameters when all checks pass.
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
    files = parse_plan_files_modified(plan_text)
    return _PreflightOK(
        endpoint=config.endpoint,  # narrowed: is-None check above
        model=config.model or "",
        task_message=task_message,
        files=files,
        read_only_path=worktree / TASK_CONTEXT_RELATIVE_PATH,
        aiderignore_path=build_aiderignore(worktree, files),
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
    launch never happens.

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
        # parent is intentionally unused; aider runs have no parent-session
        # concept. wall_clock_budget_seconds is unused on the fire-and-forget
        # launch path — the harvest sweep, not a blocking timeout, bounds the run.
        del parent, wall_clock_budget_seconds
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

        # Step 2: Pre-flight checks (synchronous, Addendum 1 Alt A).
        # _PreflightOK → all checks passed; AutoDevResult → blocked.
        preflight = _local_preflight(self._config, task, worktree, client)

        # Captured for diagnostics: empty until the launch path builds it, so
        # the generic-except branch (which may fire before build_argv) can still
        # persist an argv-less bundle.
        argv: list[str] = []
        try:
            if isinstance(preflight, _PreflightOK):
                # Step 3: Launch aider fire-and-forget (pre-flight all passed).
                # Capture the PID + start-time as a liveness handle, leave the
                # session ACTIVE, and return — reconcile/local harvest completes
                # it once the process exits. NEVER block on the run here.
                argv = build_argv(
                    preflight.model,
                    preflight.task_message,
                    preflight.files,
                    preflight.read_only_path,
                    preflight.aiderignore_path,
                )
                env = build_env(preflight.endpoint)
                proc = self._runner.launch(worktree, argv, env)
                start_time_ns = read_process_start_time_ns(proc.pid)
                if start_time_ns is not None:
                    with sessions_lock():
                        state = load_state()
                        target = next((s for s in state.sessions if s.id == sid), None)
                        if target is not None:
                            target.local_liveness = LocalLivenessHandle(
                                pid=proc.pid,
                                start_time_ns=start_time_ns,
                            )
                            save_state(state)
                    return sid
                # /proc/<pid>/stat unreadable immediately after launch — process
                # may have exited before exec or /proc is unavailable. Storing 0
                # would make every liveness check return False, triggering
                # premature harvest while aider is still running. Kill any orphan
                # and fall through to the blocked completion path so the dispatch
                # retry path requeues the task (no stale liveness handle stored).
                with contextlib.suppress(OSError):
                    proc.kill()
                    proc.wait()
                liveness_detail = f"process {proc.pid} start-time unavailable"
                # Post-spawn failure (process exited before exec / /proc gone),
                # classified runtime_error per the inline comment above — not a
                # spawn_error, the launch itself succeeded.
                _persist_aider_runtime_error_diagnostics(
                    session_id=sid, argv=argv, details=liveness_detail
                )
                completion_result: AutoDevResult = make_blocked(
                    ticket_id=task.ticket_id,
                    worktree=worktree,
                    reason=LIVENESS_UNAVAILABLE,
                    details=append_diagnostics_pointer(liveness_detail, session_id=sid),
                )
            else:
                completion_result = preflight

            # Pre-flight blocked OR proc stat unreadable: complete synchronously.
            # Write the blocked result through the door (_complete_session_via_door,
            # RFC 0012 A2), mark COMPLETED, and emit SESSION_COMPLETED — dispatch
            # reads last_result from the session directly, no payload needed.
            with sessions_lock():
                _complete_session_via_door(
                    sid=sid, payload=completion_result.model_dump(mode="json")
                )
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            # Ensure the session is never left ACTIVE on unexpected errors during
            # launch (FileNotFoundError/OSError from Popen despite the
            # aider_available check, or a save_state failure). Mark it COMPLETED
            # with a blocked result so reconcile can clean it up. SESSION_COMPLETED
            # is NOT emitted; dispatch's exception handler reverts the task to
            # PENDING, which is the correct recovery path.
            unexpected_error_detail = "unexpected error during aider launch"
            _persist_aider_runtime_error_diagnostics(
                session_id=sid,
                argv=argv,
                details=unexpected_error_detail,
            )
            with sessions_lock():
                _complete_session_via_door(
                    sid=sid,
                    payload=make_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                        details=append_diagnostics_pointer(
                            unexpected_error_detail, session_id=sid
                        ),
                    ).model_dump(mode="json"),
                    guard_already_completed=True,
                )
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


def _persist_aider_runtime_error_diagnostics(
    *, session_id: str, argv: list[str], details: str
) -> None:
    """Write a ``runtime_error`` diagnostics bundle for a LocalExecutor failure.

    Covers both post-spawn LocalExecutor.spawn failure branches
    (LIVENESS_UNAVAILABLE and the generic ``except``). *argv* is passed
    through raw — ``ExecutorFailure``'s own ``argv_sanitized`` field_validator
    redacts aider's ``--message`` value (full ticket+plan text) wholesale
    (#1330 item 4). Never raises (persist swallows OSError).
    """
    failure = build_executor_failure(
        category="runtime_error",
        executor_name="aider",
        session_id=session_id,
        argv=argv,
        stdout_excerpt="",
        stderr_excerpt=details,
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="aider",
        failure=failure,
    )
