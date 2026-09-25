"""CodexExecutor — prompt-driven ``codex exec`` REVIEW backend (#1236, #1727)."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.codex_background import (
    _complete_session_as_unexpected_error,
    _default_background,
    _run_codex_review_and_complete,
    _stamp_session_id_on_running_task,
)
from cw.codex_review import (
    make_codex_blocked,
)
from cw.codex_runner import CodexRunner, RealCodexRunner
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event as _record_orchestrator_event
from cw.executor.core import CODEX_NOT_FOUND, _complete_session_via_door
from cw.models import (
    ClientConfig,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.spawn import _write_hook_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Session-level CodexExecutor pre-flight blocker reason code (RFC 0005 F1);
# its sibling CODEX_NOT_FOUND lives in cw.executor.core, shared with the
# capability probe.
CODEX_REVIEW_ONLY = "codex_review_only"


class CodexExecutor:
    """StageExecutor backed by prompt-driven ``codex exec`` reviewers (#1236).

    REVIEW-only: spawn() returns make_codex_blocked(reason=CODEX_REVIEW_ONLY) if
    called on any stage other than REVIEW. Step 3 delegates to
    ``codex_review.run_review``, which runs a per-reviewer-role loop of generic
    ``codex exec`` calls (each fed a materialized prompt over stdin), validates
    every reviewer's structured output through the ``review_findings`` library,
    and synthesizes a typed AutoDevResult from the consolidated verdict. The
    consolidated verdict is posted as a GitHub issue comment on a clean run.

    spawn() is NOT synchronous (#1727). Pre-flight (Steps 1-2) runs on the
    caller's thread; once it passes, the review is handed to a
    ``cw.codex_background`` daemon thread and spawn() returns the session id
    immediately. Blocking here would freeze the shared ``dispatch_tick`` stack
    for the whole review, stalling every other client and lane — see the
    StageExecutor Protocol invariant in ``cw.executor.core``.

    Like LocalExecutor, it bypasses stdout-sentinel parsing: the
    SESSION_COMPLETED event carries no result payload, so dispatch consumes the
    last_result written via the door (``emit_result_locked``,
    source=EXECUTOR_DIRECT — RFC 0012 A2, #1458) as-is.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: CodexRunner | None = None,
        background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._config = config
        self._runner: CodexRunner = runner if runner is not None else RealCodexRunner()
        # Testability seam for the threading handoff: tests inject
        # ``lambda fn: fn()`` to run the review inline and keep their
        # assertions deterministic.
        self._background = background if background is not None else _default_background

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

        # #2280: CodexExecutor never reaches either of _write_hook_context's
        # other two call sites (both are Claude-session spawns), so a
        # codex-review attempt never got a cw-context.json — and with it,
        # never got a prior_attempts_summary on retry. write_stop_hook=False:
        # there is no Claude turn loop here to signal-stop. Called ahead of
        # the pre-flight branch below so even a CODEX_REVIEW_ONLY/
        # CODEX_NOT_FOUND park is covered.
        try:
            _write_hook_context(
                worktree,
                session_id=sid,
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
                write_stop_hook=False,
            )
        except Exception:
            # #2280: sess is already persisted ACTIVE above -- a raise here
            # would otherwise leak it permanently ACTIVE (the same class of
            # leak that held a client-ceiling slot for ~2h, #2285). Mirrors
            # the pre-flight-result-write branch below: dispatch is still on
            # this stack, so re-raising lets its own handler revert the
            # claimed task to PENDING.
            _complete_session_as_unexpected_error(sid, task, worktree)
            raise

        # Step 2: Pre-flight checks (first match assigns result).
        result: AutoDevResult | None = None
        if stage != Stage.REVIEW:
            result = make_codex_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_REVIEW_ONLY,
            )
        elif shutil.which("codex") is None:
            result = make_codex_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_NOT_FOUND,
            )

        if result is not None:
            # Pre-flight failed: nothing to review, so persist + emit inline.
            # This branch is cheap and has no subprocess in it, so keeping it
            # on the caller's thread costs dispatch nothing.
            try:
                with sessions_lock():
                    _complete_session_via_door(
                        sid=sid, payload=result.model_dump(mode="json")
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
                # Never leave the session ACTIVE on an unexpected error. The
                # re-raise is correct *here* (unlike on the background path):
                # dispatch is still on this stack and its own handler reverts
                # the claimed task to PENDING.
                _complete_session_as_unexpected_error(sid, task, worktree)
                raise
            return sid

        # Pre-flight passed. Stamp session_id onto the still-RUNNING dev-queue
        # row *before* backgrounding (#1727 R1): dispatch stamps it too, but
        # only after spawn() returns, so a crash in that window would otherwise
        # leave a live codex session with no queue row pointing at it and no
        # way to attribute the failure. Deliberately narrower than dispatch's
        # own post-spawn stamp — session_id only, not the error-counter reset
        # or stage_base_ref — so backoff semantics keep a single owner.
        _stamp_session_id_on_running_task(
            client_name=client.name,
            ticket_id=task.ticket_id,
            session_id=sid,
            created_at=task.created_at,
        )

        # Steps 3/4/4b/5 run off the dispatch_tick call stack (#1727).
        self._background(
            lambda: _run_codex_review_and_complete(
                runner=self._runner,
                task=task,
                worktree=worktree,
                client=client,
                wall_clock_budget_seconds=wall_clock_budget_seconds,
                sid=sid,
                sess_name=sess.name,
                config_model=self._config.model,
                config_reasoning_effort=self._config.reasoning_effort,
            )
        )
        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
