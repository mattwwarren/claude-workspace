"""OpencodeExecutor — fire-and-forget opencode backend (#1669)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import (
    REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
    AutoDevResult,
)
from cw.executor._engine_guard import engine_block_details
from cw.executor._finalize_guard import (
    FINALIZE_UNSUPPORTED_BACKEND,
    finalize_block_details,
)
from cw.executor._repair_entry import (
    REPAIR_ENTRY_UNSUPPORTED_BACKEND,
    repair_entry_block_details,
)
from cw.executor.core import FireAndForgetRunner, _PreflightOK, _spawn_fire_and_forget
from cw.models import OPENCODE_BACKEND, SessionOrigin, SessionPurpose
from cw.native_daemon import get_native_daemon_client
from cw.opencode_runner import (
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    SUPPORTED_STAGES,
    RealOpencodeRunner,
    build_stage_prompt,
    opencode_available,
    stage_entry_marker,
)
from cw.opencode_runner import (
    build_argv as build_opencode_argv,
)
from cw.opencode_runner import (
    build_env as build_opencode_env,
)
from cw.opencode_runner import (
    make_blocked as make_opencode_blocked,
)
from cw.spawn import _write_hook_context
from cw.tracker import tracker_kind

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        Session,
        Stage,
        StageExecutorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient


def _opencode_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    stage: Stage,
    sess: Session,
    daemon: NativeDaemonClient | None = None,
) -> AutoDevResult | _PreflightOK:
    """Run OpencodeExecutor pre-flight checks for any supported stage.

    #2653: refreshes ``<worktree>/.claude/cw-context.json`` first, on every
    spawn. Without it a reused worktree keeps the previous session's file, so
    a worker's bare ``cw result emit`` resolves the stale session id (refused
    as "already recorded") and ``check_must_fix_override.py`` never sees the
    operator's ``queue_metadata.must_fix_override``. ``write_stop_hook=False``:
    opencode has no Claude turn loop to signal-stop. Written before the
    binary / stage checks so even a blocked park carries a fresh context.

    Returns a blocked ``AutoDevResult`` on binary-missing or unsupported
    stage; returns ``_PreflightOK`` with the resolved argv + env (stage
    prompt) when the binary is available and the stage is supported. A
    REVIEW spawn for a ``review.convergence: clean`` client gets the
    converge-to-clean prompt (``clean_review=client.review.is_clean``, #2806),
    which drives the convergence ledger itself; the schema-15 ``review``
    object stamped into the context below is what its ``cw review converge``
    commands read.
    FINALIZE is accepted by ``SUPPORTED_STAGES`` but refused by
    ``finalize_block_details`` before the binary probe (RFC 0018 B1, #3059);
    its prompt (the ``auto-dev-finalize.md`` command file, worktree copy first,
    home fallback — #1670 R6) is kept consistent with the doc for when the
    guard lifts. PLAN/IMPL/REVIEW get self-contained prompts, and every
    stage's prompt carries *session_id* so the worker can push its result via
    ``cw result emit --session-id`` (#2430; see ``build_stage_prompt``). Every
    blocked result carries the dispatched stage's own entry marker so dispatch
    never walks ``task.stage`` forward on a failure sentinel.
    """
    _write_hook_context(
        worktree,
        session_id=sess.id,
        session_name=sess.name,
        client=client.name,
        purpose=SessionPurpose.IMPL.value,
        ticket_id=task.ticket_id,
        origin=SessionOrigin.DAEMON,
        headless=True,
        task=task,
        default_branch=client.default_branch,
        workspace_path=client.workspace_path,
        lane=task.lane,
        merge_gate_ignore_paths=client.merge_gate_ignore_paths,
        merge_gate_enabled=client.merge_gate_enabled,
        review_config=client.review,
        write_stop_hook=False,
        daemon=daemon,
    )
    if stage.value not in SUPPORTED_STAGES:
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=f"opencode_{stage.value}_not_implemented",
            stage_reached=STAGE4A_MERGE_GATE,
        )
    # RFC 0018 B1 (#3059): FINALIZE fails closed; the claude-native doc's
    # Claude-only steps are unverified on opencode.
    finalize_details = finalize_block_details(stage, OPENCODE_BACKEND)
    if finalize_details is not None:
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=FINALIZE_UNSUPPORTED_BACKEND,
            details=finalize_details,
            retry_eligible=False,
            stage_reached=stage_entry_marker(stage.value),
        )
    # #3216: an engine-configured REVIEW never runs the built-in roster here.
    engine_details = engine_block_details(client, stage, OPENCODE_BACKEND)
    if engine_details is not None:
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
            details=engine_details,
            retry_eligible=False,
            stage_reached=stage_entry_marker(stage.value),
        )
    # #2944 repair rows, extended to any row carrying review_feedback: #2983
    # decided fail-closed for opencode (RFC 0018 B1, #3059).
    repair_details = repair_entry_block_details(
        task, stage, OPENCODE_BACKEND, block_any_feedback=True
    )
    if repair_details is not None:
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=REPAIR_ENTRY_UNSUPPORTED_BACKEND,
            details=repair_details,
            stage_reached=stage_entry_marker(stage.value),
        )
    if not opencode_available():
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=OPENCODE_NOT_FOUND,
            retry_eligible=True,
            retry_delay_seconds=0,
            stage_reached=stage_entry_marker(stage.value),
        )
    prompt = build_stage_prompt(
        stage.value,
        task.ticket_id,
        worktree,
        session_id=sess.id,
        clean_review=client.review.is_clean,
        tracker=tracker_kind(client.workspace_path),
        tracker_server=client.tracker_mcp_server(OPENCODE_BACKEND),
    )
    return _PreflightOK(
        argv=build_opencode_argv(config.model, worktree, prompt),
        env=build_opencode_env(worktree),
    )


class OpencodeExecutor:
    """StageExecutor backed by a fire-and-forget opencode subprocess (#1669).

    FINALIZE is refused in pre-flight with
    ``reason=finalize_unsupported_backend`` (RFC 0018 B1, #3059; no real
    opencode FINALIZE run has verified the claude-native doc); its prompt is
    kept consistent with the doc for when the guard lifts. PLAN/IMPL/REVIEW
    carry self-contained stage contracts because their command files require
    Claude Code-only machinery (see ``opencode_runner.build_stage_prompt``).
    Every prompt instructs opencode to emit the sentinel with the correct
    ``stage_reached`` marker (R1). Unsupported stages (e.g. HARDEN) are blocked
    with ``reason=opencode_<stage>_not_implemented``.

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches opencode via ``OpencodeRunner.launch`` (Popen, no wait),
    records a ``Session.local_liveness`` handle (PID + start-time), leaves the
    session ACTIVE, and returns the sid immediately. The opencode run completes
    asynchronously; reconcile/local harvest later detects the dead process,
    parses the JSONL log for the sentinel, and completes the session.

    Pre-flight failures (binary missing, FINALIZE or repair-entry refused) stay
    synchronous: they persist a
    blocked result to Session.last_result via the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT — RFC 0012 A2), mark the
    session COMPLETED, and emit SESSION_COMPLETED before returning. The shared
    skeleton lives in ``cw.executor.core._spawn_fire_and_forget`` (#2369).

    opencode has no ``--output-schema`` (probe-confirmed, #1669 R3); the result
    travels as free-form text in ``text`` event payloads, harvested via the
    ``<<<AUTO_DEV_RESULT>>>`` sentinel pattern. Appropriate only for
    max_parallel=1 lanes (mirrors CodexExecutor).
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: FireAndForgetRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: FireAndForgetRunner = (
            runner if runner is not None else RealOpencodeRunner()
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
        del parent, wall_clock_budget_seconds
        daemon = get_native_daemon_client()

        def _blocked(*, reason: str, details: str) -> AutoDevResult:
            return make_opencode_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=reason,
                details=details,
                stage_reached=stage_entry_marker(stage.value),
            )

        return _spawn_fire_and_forget(
            task=task,
            worktree=worktree,
            client=client,
            stage=stage,
            executor_name="opencode",
            preflight_fn=lambda sess: _opencode_preflight(
                self._config, task, worktree, client, stage, sess, daemon
            ),
            blocked_ctor=_blocked,
            runner=self._runner,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
