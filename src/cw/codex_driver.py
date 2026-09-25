"""``cw codex run`` subprocess entry point (RFC 0014 S1, ADR-0018).

Owns the process-boundary driver that ``CodexExecutor.spawn()`` hands the
codex review off to as a detached subprocess, in place of the in-process
daemon thread ``cw.codex_background`` runs today (D-2: both paths remain
live until B2 retires the thread path). This module is invoked ONLY as a
subprocess via ``cw codex run`` (``cw.cli.codex``) — never imported by
``cw.executor`` (D-1): the whole point of the process boundary is that a
crash here cannot take the dispatch loop down with it, which an in-process
import would silently reintroduce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_background import _run_codex_review_and_complete
from cw.codex_runner import RealCodexRunner
from cw.config import get_client, load_state
from cw.exceptions import CwError
from cw.executor import resolve_executor_config
from cw.models import Stage
from cw.reconcile import find_running_task_for_session

if TYPE_CHECKING:
    from cw.codex_runner import CodexRunner
    from cw.models import Session, TicketTask

STAGE_REVIEW = "review"
STAGE_IMPL = "impl"

_IMPL_STAGE_NOT_IMPLEMENTED = (
    "cw codex run --stage impl is not implemented yet — see GitHub #1550."
)


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
    (``_stamp_session_id_on_running_task``, called synchronously inside
    ``spawn()`` before the handoff, RFC 0014 A2) is the only source of truth.
    """
    session = _load_session(session_id)
    client = get_client(session.client)
    task = _load_running_task(ticket_id=ticket_id, session_id=session_id)
    worktree = session.worktree_path
    if worktree is None:
        msg = f"session {session_id!r} has no worktree_path"
        raise CwError(msg)
    executor_config = resolve_executor_config(Stage.REVIEW, task, client)
    _run_codex_review_and_complete(
        runner=runner or RealCodexRunner(),
        task=task,
        worktree=worktree,
        client=client,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        sid=session.id,
        sess_name=session.name,
        config_model=executor_config.model,
        config_reasoning_effort=executor_config.reasoning_effort,
    )
