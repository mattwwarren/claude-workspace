"""CodexExecutor — detached ``cw codex run`` REVIEW backend (#1236, RFC 0014 A2)."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.codex_background import _stamp_session_id_on_running_task
from cw.codex_review import make_codex_blocked
from cw.codex_runner import (
    RealCodexJobRunner,
    build_codex_run_argv,
    build_codex_run_env,
)
from cw.executor.core import (
    CODEX_NOT_FOUND,
    FireAndForgetRunner,
    _PreflightOK,
    _spawn_fire_and_forget,
)
from cw.models import SessionOrigin, SessionPurpose, Stage
from cw.native_daemon import get_native_daemon_client
from cw.spawn import _write_hook_context

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        Session,
        StageExecutorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient

# Session-level CodexExecutor pre-flight blocker reason code (RFC 0005 F1);
# its sibling CODEX_NOT_FOUND lives in cw.executor.core, shared with the
# capability probe.
CODEX_REVIEW_ONLY = "codex_review_only"


def _codex_preflight(
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    stage: Stage,
    sess: Session,
    wall_clock_budget_seconds: int | None,
    daemon: NativeDaemonClient,
) -> AutoDevResult | _PreflightOK:
    """Run CodexExecutor pre-flight; resolve the ``cw codex run`` launch.

    #2280: CodexExecutor never reaches either of ``_write_hook_context``'s
    other two call sites (both are Claude-session spawns), so without this call
    a codex-review attempt never gets a cw-context.json -- and with it, never
    gets a ``prior_attempts_summary`` on retry. ``write_stop_hook=False``:
    there is no Claude turn loop here to signal-stop. Called first so even a
    CODEX_REVIEW_ONLY/CODEX_NOT_FOUND park is covered.

    On success, stamps the session id onto the still-RUNNING dev-queue row
    *before* the launch (#1727 R1): ``cw codex run`` re-finds its task by
    ``(ticket_id, session_id)``, and dispatch's own stamp lands only after
    spawn() returns. Deliberately narrower than dispatch's post-spawn stamp --
    session_id only, not the error-counter reset or stage_base_ref -- so
    backoff semantics keep a single owner.
    """
    _write_hook_context(
        worktree,
        session_id=sess.id,
        session_name=sess.name,
        client=client.name,
        purpose=SessionPurpose.IMPL.value,
        ticket_id=task.ticket_id,
        origin=SessionOrigin.DAEMON,
        task=task,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        default_branch=client.default_branch,
        workspace_path=client.workspace_path,
        lane=task.lane,
        merge_gate_ignore_paths=client.merge_gate_ignore_paths,
        write_stop_hook=False,
        daemon=daemon,
    )
    if stage != Stage.REVIEW:
        return make_codex_blocked(
            ticket_id=task.ticket_id, worktree=worktree, reason=CODEX_REVIEW_ONLY
        )
    if shutil.which("codex") is None:
        return make_codex_blocked(
            ticket_id=task.ticket_id, worktree=worktree, reason=CODEX_NOT_FOUND
        )
    _stamp_session_id_on_running_task(
        client_name=client.name,
        ticket_id=task.ticket_id,
        session_id=sess.id,
        created_at=task.created_at,
    )
    return _PreflightOK(
        argv=build_codex_run_argv(
            ticket_id=task.ticket_id,
            session_id=sess.id,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
        ),
        env=build_codex_run_env(worktree),
    )


class CodexExecutor:
    """StageExecutor backed by a detached ``cw codex run`` subprocess (#2388).

    REVIEW-only: pre-flight blocks with ``CODEX_REVIEW_ONLY`` on any other
    stage, and with ``CODEX_NOT_FOUND`` when the codex binary is absent.

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches ``cw codex run`` via ``RealCodexJobRunner.launch``
    (Popen in its own session, no wait), records a ``Session.local_liveness``
    handle (PID + start-time, ``backend="codex"``), leaves the session ACTIVE,
    and returns the sid immediately. The subprocess runs the per-reviewer-role
    ``codex exec`` loop and fix loop (the ``cw codex run`` driver), posts the
    consolidated verdict, and completes the session itself through the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT — RFC 0012 A2, #1458) —
    the sole normal completion path, which survives a serve restart. If the
    process dies without completing, reconcile/local's codex branch (A1,
    #2387) applies its audited clean-requeue-or-park gate.

    Pre-flight failures stay synchronous: they persist a blocked result via
    the door, mark the session COMPLETED, and emit SESSION_COMPLETED before
    returning — the launch never happens. The shared skeleton lives in
    ``cw.executor.core._spawn_fire_and_forget`` (#2369).
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: FireAndForgetRunner | None = None,
        native_daemon: NativeDaemonClient | None = None,
    ) -> None:
        self._config = config
        # Mirrors ClaudeNativeExecutor.native_daemon (#2077): threaded into
        # _write_hook_context so its DAEMON-conflict branch can corroborate a
        # prior session's liveness against the caller's own daemon client.
        self._native_daemon = native_daemon
        self._runner: FireAndForgetRunner = (
            runner if runner is not None else RealCodexJobRunner()
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
        # parent is intentionally unused; codex has no parent-session concept.
        del parent
        daemon = self._native_daemon or get_native_daemon_client()

        def _blocked(*, reason: str, details: str) -> AutoDevResult:
            return make_codex_blocked(
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
            executor_name="codex",
            preflight_fn=lambda sess: _codex_preflight(
                task=task,
                worktree=worktree,
                client=client,
                stage=stage,
                sess=sess,
                wall_clock_budget_seconds=wall_clock_budget_seconds,
                daemon=daemon,
            ),
            blocked_ctor=_blocked,
            runner=self._runner,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
