"""Background execution of ``CodexExecutor``'s blocking review (GitHub #1727).

``CodexExecutor.spawn()`` used to run the whole review — per-role ``codex exec``
subprocesses plus the bounded fix loop, up to the full REVIEW budget — inside
the shared ``dispatch_tick`` call stack, so one client's review stalled the
dispatch loop for every client. This module owns the unit of work that was
moved off that stack (:func:`_run_codex_review_and_complete`, the old Step
3/4/4b/5) plus the daemon thread it runs on.

Threading precedent: ``cw.notify.fire_push_notification`` already fires a
``threading.Thread(daemon=True)`` for the same reason (do not make the caller
wait). This is the second such occurrence in the codebase and deliberately
carries no new abstraction — no thread pool, no executor service. A *third*
occurrence is the trigger to stop and write an ADR for a shared background-work
primitive rather than growing a third bespoke launcher.

This module also owns the registry of still-running review threads that
``run_dispatch_loop``'s shutdown path drains via
:func:`join_outstanding_codex_threads`, so a deploy/restart/``--once`` exit
gets a short, bounded chance to let an almost-done review finish rather than
vanishing mid-``git commit``. Threads still alive at that deadline are
``cw.reconcile.codex_boot``'s problem on the next boot, not the join's.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_review import make_codex_blocked, render_verdict_comment
from cw.config import load_effective_config, sessions_lock
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.dispatch_state import (
    ExecutorBlockedMarker,
    clear_executor_blocked_marker,
    save_executor_blocked_marker,
)
from cw.events import record_event as _record_orchestrator_event
from cw.gh import post_issue_comment
from cw.local_runner import UNEXPECTED_ERROR
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

_log = logging.getLogger(__name__)

# Shared budget for draining outstanding review threads on a graceful exit.
#
# Why 10s: long enough for a thread that has already finished its codex
# subprocesses and its git work and is only doing fast cleanup (the door write,
# the GitHub verdict comment, the SESSION_COMPLETED event) to land. Far too
# short to "wait out" a review still mid-subprocess, which can run to the full
# REVIEW budget — so a thread still alive at this deadline is, by construction,
# nowhere near done, and waiting longer would only delay the deploy/restart this
# join exists to unblock. That thread is cw.reconcile.codex_boot's problem on
# the next process start.
_CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS: float = 10.0

# Still-running review threads, newest last. Entries are appended before
# ``Thread.start()`` and removed by the thread itself on completion (success or
# exception), so this only ever holds genuinely in-flight work rather than
# accumulating over the process lifetime.
_outstanding: list[threading.Thread] = []
_outstanding_lock = threading.Lock()


def _start_daemon_thread(fn: Callable[[], None], *, name: str) -> None:
    """Run *fn* on a registered daemon thread and return immediately.

    The thread deregisters itself in a ``finally:`` so a raising *fn* cannot
    leave a dead entry behind for :func:`join_outstanding_codex_threads` to
    trip over.
    """
    holder: list[threading.Thread] = []

    def _wrapped() -> None:
        try:
            fn()
        finally:
            with _outstanding_lock:
                if holder and holder[0] in _outstanding:
                    _outstanding.remove(holder[0])

    thread = threading.Thread(target=_wrapped, name=name, daemon=True)
    holder.append(thread)
    with _outstanding_lock:
        _outstanding.append(thread)
    thread.start()


def _stamp_session_id_on_running_task(
    *, client_name: str, ticket_id: str, session_id: str
) -> None:
    """Stamp *session_id* onto the matching RUNNING dev-queue row (#1727 R1).

    Called by ``CodexExecutor.spawn()`` immediately before it hands the review
    off, so the queue row already points at the live session by the time this
    module's thread starts. Dispatch stamps session_id too, but only *after*
    spawn() returns — a crash in that window would otherwise leave a running
    codex session with no row attributing it.

    Keyword-only because ``client_name``/``ticket_id``/``session_id`` are all
    plain ``str`` with no type-system distinction between them — mirrors
    ``_park_running_task_blocked_on_user``'s reasoning.

    Deliberately narrower than dispatch's own post-spawn stamp: session_id
    only, no error-counter reset and no stage_base_ref, so backoff semantics
    keep a single owner in claim.py.

    A no-op when no RUNNING row matches: CodexExecutor is reachable outside
    dispatch (direct construction in tests, a future one-off invocation), and
    there is nothing to attribute in that case.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        for stored_task in store.tasks:
            if (
                stored_task.ticket_id == ticket_id
                and stored_task.client == client_name
                and stored_task.status == QueueItemStatus.RUNNING
            ):
                stored_task.session_id = session_id
                break
        save_dev_queue(store)


def _default_background(fn: Callable[[], None]) -> None:
    """Production ``background`` seam for :class:`~cw.executor.CodexExecutor`."""
    _start_daemon_thread(fn, name="codex-review")


def join_outstanding_codex_threads(timeout_seconds: float | None = None) -> int:
    """Join every in-flight review thread against one shared deadline.

    Returns the number still alive after the deadline — reported on the
    ``DISPATCH_LOOP_EXITED`` event so "the loop exited while N reviews were
    still running" is visible from outside the process.

    *timeout_seconds* is the budget for the whole drain, not per thread. It
    defaults to :data:`_CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS`, read at call
    time rather than bound as a default argument so the constant stays
    patchable.
    """
    budget = (
        _CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    with _outstanding_lock:
        snapshot = list(_outstanding)
    deadline = time.monotonic() + budget
    for thread in snapshot:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    with _outstanding_lock:
        return sum(1 for thread in _outstanding if thread.is_alive())


def _resolve_codex_fix_loop_enabled(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> bool:
    """Resolve the effective codex_fix_loop_enabled gate for *task* (#1553).

    Precedence (highest to lowest):
      1. Lane-level LaneConfig.codex_fix_loop_enabled in *client*'s config.
      2. Global OrchestratorConfig.default_codex_fix_loop_enabled.

    A task whose lane name is not declared in the client's lanes falls
    through to the global default. Mirrors resolve_reap_policy's lane-then-
    global fallthrough shape (cw.reconcile._shared).
    """
    for lane_cfg in client.effective_lanes:
        if lane_cfg.name == task.lane and lane_cfg.codex_fix_loop_enabled is not None:
            return lane_cfg.codex_fix_loop_enabled
    return config.default_codex_fix_loop_enabled


def _post_review_comment(
    ticket_id: str, review_text: str, *, cwd: Path | None = None
) -> None:
    """Post codex review findings as a GitHub issue comment (best-effort, logged).

    Delegates to the shared ``cw.gh.post_issue_comment`` primitive. A failed
    post is logged at warning (ticket_id, returncode, stderr) rather than
    swallowed silently — for the CODEX_MUST_FIX_FINDINGS path this comment is
    the only destination for the finding text (GitHub #1391).

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    result = post_issue_comment(ticket_id, review_text, cwd=cwd)
    if result is None:
        _log.warning("review_comment_post_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "review_comment_post_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


def _complete_session_as_unexpected_error(
    sid: str, task: TicketTask, worktree: Path
) -> None:
    """Complete a session with a blocked/unexpected-error payload via the door.

    Shared by ``CodexExecutor.spawn()``'s synchronous pre-flight-failure branch
    (``cw.executor``) and this module's own background-thread except branch
    below — both built the identical ``make_codex_blocked(reason=
    UNEXPECTED_ERROR)`` payload independently before this extraction (#1727
    round 5 DRY fix). ``guard_already_completed=True`` since either caller may
    be racing the completion door's own success path.
    """
    from cw.executor import _complete_session_via_door

    with sessions_lock():
        _complete_session_via_door(
            sid=sid,
            payload=make_codex_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=UNEXPECTED_ERROR,
            ).model_dump(mode="json"),
            guard_already_completed=True,
        )


def _run_codex_review_and_complete(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    wall_clock_budget_seconds: int | None,
    sid: str,
    sess_name: str,
    config_model: str | None,
) -> None:
    """Run the review, persist its result, post the verdict, emit completion.

    This is ``CodexExecutor.spawn()``'s former Step 3/4/4b/5, verbatim in
    behavior, relocated onto a background thread. The only substantive change
    is the failure branch: there is no longer a caller on this stack to catch a
    re-raised exception, so instead of re-raising, this reverts the claimed
    task itself — the recovery dispatch's own ``except`` handler used to
    perform.

    Marker lifecycle (#1742): an :class:`~cw.dispatch_state.ExecutorBlockedMarker`
    is written on entry and cleared in a ``finally:``, so an operator reading
    ``cw dev-queue status``/``cw doctor`` during a long review sees
    ``[BLOCKED — codex review, ...]`` rather than ``[STALE]`` and knows not to
    restart the loop out from under this thread.
    """
    # Function-level import breaks the cw.executor <-> cw.codex_background
    # cycle: executor.py imports this module at its top, and the name below is
    # defined *after* executor.py's own import block, so a module-level import
    # here would hit a partially initialized module. Mirrors claim.py's
    # deferred cw.dispatch.gating import (#1310).
    # ``_complete_session_via_door`` stays in executor.py because it is shared
    # with LocalExecutor, OpencodeExecutor, and CodexExecutor's own synchronous
    # pre-flight-failure branch.
    from cw.executor import _complete_session_via_door

    try:
        # #1742: mark the loop as legitimately busy for the whole review. Set
        # outside any sessions_lock() block, honouring dispatch_state_lock()'s
        # documented ordering (sessions_lock -> dispatch_state_lock only).
        save_executor_blocked_marker(
            ExecutorBlockedMarker(
                client=client.name,
                ticket_id=task.ticket_id,
                executor="codex",
                reviewer_role=None,
                started_at=datetime.now(UTC),
                session_id=sid,
            )
        )

        # Step 3: per-role review pass + bounded fix loop (#1392). The
        # lane-scoped fix_loop_enabled gate (#1553) resolves here rather than
        # being threaded through the StageExecutor Protocol.
        config = load_effective_config()
        fix_loop_enabled = _resolve_codex_fix_loop_enabled(client, task, config)
        result, verdict = run_review_with_fix_loop(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=client.default_branch,
            model=config_model,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            session_id=sid,
            fix_loop_enabled=fix_loop_enabled,
        )

        # Step 4: persist result under sessions_lock.
        with sessions_lock():
            _complete_session_via_door(sid=sid, payload=result.model_dump(mode="json"))

        # Step 4b: post the consolidated verdict (best-effort). After Step 4 so
        # a retry on a persist failure cannot post a duplicate comment. verdict
        # is None only when every reviewer failed (no documents to render).
        if verdict is not None:
            _post_review_comment(
                task.ticket_id,
                render_verdict_comment(verdict, fix_loop_enabled=fix_loop_enabled),
                cwd=_git_dir(client),
            )

        # Step 5: emit SESSION_COMPLETED — no result payload; dispatch reads
        # the last_result written through the door in Step 4.
        _record_orchestrator_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": sid,
                "ticket_id": task.ticket_id,
                "session_name": sess_name,
            },
        )
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. This is the top frame of a daemon thread. Any escaping exception
        #    (git CalledProcessError from the diff capture, a codex subprocess
        #    fault, an I/O error on the door write) would be printed by the
        #    threading excepthook and then silently discarded, leaving the
        #    session ACTIVE forever and the task RUNNING forever.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical to the caller: dispatch_tick returned long ago; no
        #    other work depends on this thread's outcome.
        # 4. Paired test: tests/test_codex_background.py
        #    test_run_codex_review_and_complete_exception_path.
        _log.exception(
            "codex review thread failed session=%s ticket=%s", sid, task.ticket_id
        )
        _complete_session_as_unexpected_error(sid, task, worktree)
        # Deferred for the same cycle reason as the imports above: claim.py
        # imports cw.executor at module level, which imports this module.
        from cw.dispatch.claim import _revert_claimed_task_to_pending

        _revert_claimed_task_to_pending(client.name, task.ticket_id, stamp_backoff=True)
    finally:
        # #1742: fires on every exit path — the return out of ``try``, the
        # return out of ``except``, and a BaseException the except clause does
        # not catch. The review is over either way, so the marker must go.
        clear_executor_blocked_marker(client.name, task.ticket_id)
