"""OpencodeExecutor — fire-and-forget opencode backend (#1669)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.executor.core import FireAndForgetRunner, _PreflightOK, _spawn_fire_and_forget
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

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        Stage,
        StageExecutorConfig,
        TicketTask,
    )


def _opencode_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    stage: Stage,
) -> AutoDevResult | _PreflightOK:
    """Run OpencodeExecutor pre-flight checks for any supported stage.

    Returns a blocked ``AutoDevResult`` on binary-missing or unsupported
    stage; returns ``_PreflightOK`` with the resolved argv + env
    (stage prompt) when the binary is available and the stage is supported.
    FINALIZE's prompt points at the ``auto-dev-finalize.md`` command file
    (worktree copy first, home fallback — #1670 R6); PLAN/IMPL/REVIEW get
    self-contained prompts (see ``build_stage_prompt``). Every blocked result
    carries the dispatched stage's own entry marker so dispatch never walks
    ``task.stage`` forward on a failure sentinel.
    """
    del client  # unused: opencode builds its prompt from ticket_id + stage
    if stage.value not in SUPPORTED_STAGES:
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=f"opencode_{stage.value}_not_implemented",
            stage_reached=STAGE4A_MERGE_GATE,
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
    prompt = build_stage_prompt(stage.value, task.ticket_id, worktree)
    return _PreflightOK(
        argv=build_opencode_argv(config.model, worktree, prompt),
        env=build_opencode_env(),
    )


class OpencodeExecutor:
    """StageExecutor backed by a fire-and-forget opencode subprocess (#1669).

    Supports PLAN, IMPL, REVIEW, and FINALIZE stages. Unsupported stages
    (e.g. HARDEN) are blocked in pre-flight with
    ``reason=opencode_<stage>_not_implemented``. FINALIZE's prompt points at
    the ``auto-dev-finalize.md`` command file (R6, worktree copy first);
    PLAN/IMPL/REVIEW carry self-contained stage contracts because their
    command files require Claude Code-only machinery (see
    ``opencode_runner.build_stage_prompt``). Every prompt instructs opencode
    to emit the sentinel with the correct ``stage_reached`` marker (R1).

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches opencode via ``OpencodeRunner.launch`` (Popen, no wait),
    records a ``Session.local_liveness`` handle (PID + start-time), leaves the
    session ACTIVE, and returns the sid immediately. The opencode run completes
    asynchronously; reconcile/local harvest later detects the dead process,
    parses the JSONL log for the sentinel, and completes the session.

    Pre-flight failures (binary missing) stay synchronous: they persist a
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
            preflight_fn=lambda: _opencode_preflight(
                self._config, task, worktree, client, stage
            ),
            blocked_ctor=_blocked,
            runner=self._runner,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
