"""``cw codex run`` subprocess entry point (RFC 0014 S1, ADR-0018).

Owns the process-boundary driver that ``CodexExecutor.spawn()`` hands the
codex review off to as a detached subprocess (RFC 0014 A2, #2388), in place
of the in-process daemon thread ``cw.codex_background`` used to run (D-2: the
thread plumbing stays in place, unused on this path, until B2 retires it).
This module is invoked ONLY as a subprocess via ``cw codex run``
(``cw.cli.codex``) — never imported by ``cw.executor`` (D-1): the whole point
of the process boundary is that a crash here cannot take the dispatch loop
down with it, which an in-process import would silently reintroduce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_background import _run_codex_review_and_complete
from cw.codex_finalize import run_finalize_stage_and_complete
from cw.codex_impl import run_impl_stage_and_complete
from cw.codex_plan import run_plan_stage_and_complete
from cw.codex_runner import RealCodexRunner
from cw.codex_usage import UsageAccumulator
from cw.config import get_client, load_state
from cw.exceptions import CwError
from cw.executor import resolve_executor_config
from cw.models import Stage
from cw.models.usage import UsageStage
from cw.reconcile import find_running_task_for_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, Session, StageExecutorConfig, TicketTask

STAGE_REVIEW = "review"
STAGE_PLAN = "plan"
STAGE_IMPL = "impl"
STAGE_FINALIZE = "finalize"


def _load_session(session_id: str) -> Session:
    session = load_state().find_by_name_or_id(session_id)
    if session is None:
        msg = f"no session found for session_id={session_id!r}"
        raise CwError(msg)
    return session


def _load_running_task(*, ticket_id: str, session_id: str) -> TicketTask:
    task = find_running_task_for_session(ticket_id, session_id)
    if task is None:
        msg = (
            f"no RUNNING dev-queue task for ticket_id={ticket_id!r} "
            f"session_id={session_id!r}"
        )
        raise CwError(msg)
    return task


def unsupported_codex_stage_message(stage: Stage) -> str:
    """Return the refusal for a ``cw codex run --stage`` no codex executor serves.

    Names the stages that do have an executor (#3054, #3061, #3062).
    """
    return (
        f"no codex executor for stage {stage.value!r} "
        f"(supported: {STAGE_PLAN}, {STAGE_IMPL}, {STAGE_REVIEW}, {STAGE_FINALIZE})"
    )


def _load_stage_run(
    *, ticket_id: str, session_id: str, stage: Stage
) -> tuple[Session, ClientConfig, TicketTask, Path, StageExecutorConfig]:
    """Re-derive what a stage run needs from the persisted queue row and session.

    Stage-neutral: the executor config is resolved for *stage*, so a lane can
    give each stage its own model.
    """
    session = _load_session(session_id)
    client = get_client(session.client)
    task = _load_running_task(ticket_id=ticket_id, session_id=session_id)
    worktree = session.worktree_path
    if worktree is None:
        msg = f"session {session_id!r} has no worktree_path"
        raise CwError(msg)
    return session, client, task, worktree, resolve_executor_config(stage, task, client)


def _accumulator(
    session: Session, task: TicketTask, executor_config: StageExecutorConfig
) -> UsageAccumulator:
    """The one place a stage run's usage accumulator is created (#3063)."""
    return UsageAccumulator(
        session_id=session.id,
        ticket_id=task.ticket_id,
        prices=executor_config.model_prices,
    )


def run_codex_review_stage(
    *,
    ticket_id: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
    runner: CodexRunner | None = None,
) -> None:
    """Run the codex review stage for *ticket_id*/*session_id* and complete it.

    The driver's testable core. Re-derives the session/task/client/executor
    config that ``CodexExecutor.spawn()``'s in-process thread path used to
    receive via Python closure — a detached subprocess has no closure, so the
    persisted, already-session_id-stamped RUNNING dev-queue row
    (``stamp_session_id_on_running_task``, called synchronously inside
    ``spawn()`` before the handoff, RFC 0014 A2) is the only source of truth.
    """
    session, client, task, worktree, executor_config = _load_stage_run(
        ticket_id=ticket_id, session_id=session_id, stage=Stage.REVIEW
    )
    usage = _accumulator(session, task, executor_config)
    _run_codex_review_and_complete(
        runner=usage.recording_runner(
            UsageStage.REVIEW_ROLE, runner or RealCodexRunner()
        ),
        task=task,
        worktree=worktree,
        client=client,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        sid=session.id,
        sess_name=session.name,
        config_model=executor_config.model,
        config_reasoning_effort=executor_config.reasoning_effort,
        config_reviewer_sandbox_mode=executor_config.reviewer_sandbox_mode,
        config_fix_sandbox_mode=executor_config.fix_sandbox_mode,
        usage=usage,
    )


def run_codex_plan_stage(
    *,
    ticket_id: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
    runner: CodexRunner | None = None,
) -> None:
    """Run the codex PLAN stage for *ticket_id*/*session_id* and complete it (#3060).

    Re-derives the run from the persisted RUNNING row and session, as
    :func:`run_codex_review_stage` does. *wall_clock_budget_seconds* is
    accepted for the shared CLI signature and ignored: no cw timer bounds the
    PLAN turn or its review stations (ADR-0014). Only the PLAN stage's model
    and reasoning effort are read from its executor config; the turn's
    read-only sandbox is fixed, not configured.
    """
    del wall_clock_budget_seconds
    session, client, task, worktree, executor_config = _load_stage_run(
        ticket_id=ticket_id, session_id=session_id, stage=Stage.PLAN
    )
    usage = _accumulator(session, task, executor_config)
    run_plan_stage_and_complete(
        runner=usage.recording_runner(UsageStage.PLAN, runner or RealCodexRunner()),
        task=task,
        worktree=worktree,
        client=client,
        sid=session.id,
        sess_name=session.name,
        model=executor_config.model,
        reasoning_effort=executor_config.reasoning_effort,
        usage=usage,
    )


def run_codex_impl_stage(
    *,
    ticket_id: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
    runner: CodexRunner | None = None,
) -> None:
    """Run the codex IMPL stage for *ticket_id*/*session_id* and complete it (#3061).

    Re-derives the run from the persisted RUNNING row and session, as
    :func:`run_codex_plan_stage` does. *wall_clock_budget_seconds* is accepted
    for the shared CLI signature and ignored: no cw timer bounds the IMPL turn
    (ADR-0014). The IMPL stage's model, reasoning effort and
    ``fix_sandbox_mode`` are read from its executor config: the turn edits
    files, so it runs under the lane's write-capable sandbox, while PLAN, the
    review stations and tracker IO stay read-only.
    """
    del wall_clock_budget_seconds
    session, client, task, worktree, executor_config = _load_stage_run(
        ticket_id=ticket_id, session_id=session_id, stage=Stage.IMPL
    )
    usage = _accumulator(session, task, executor_config)
    run_impl_stage_and_complete(
        runner=usage.recording_runner(UsageStage.IMPL, runner or RealCodexRunner()),
        task=task,
        worktree=worktree,
        client=client,
        sid=session.id,
        sess_name=session.name,
        model=executor_config.model,
        reasoning_effort=executor_config.reasoning_effort,
        sandbox_mode=executor_config.fix_sandbox_mode,
        usage=usage,
    )


def run_codex_finalize_stage(
    *,
    ticket_id: str,
    session_id: str,
    wall_clock_budget_seconds: int | None,
    runner: CodexRunner | None = None,
) -> None:
    """Run the codex FINALIZE stage for *ticket_id*/*session_id* and complete it.

    #3062 (RFC 0018 C3). Re-derives the run from the persisted RUNNING row and
    session, as :func:`run_codex_impl_stage` does. *wall_clock_budget_seconds*
    is accepted for the shared CLI signature and ignored (ADR-0014). Only the
    FINALIZE stage's model and reasoning effort are read: the turn is
    read-only, so ``fix_sandbox_mode`` does not apply.
    """
    del wall_clock_budget_seconds
    session, client, task, worktree, executor_config = _load_stage_run(
        ticket_id=ticket_id, session_id=session_id, stage=Stage.FINALIZE
    )
    usage = _accumulator(session, task, executor_config)
    run_finalize_stage_and_complete(
        runner=usage.recording_runner(UsageStage.FINALIZE, runner or RealCodexRunner()),
        task=task,
        worktree=worktree,
        client=client,
        sid=session.id,
        sess_name=session.name,
        model=executor_config.model,
        reasoning_effort=executor_config.reasoning_effort,
        usage=usage,
    )
