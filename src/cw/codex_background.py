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
from typing import TYPE_CHECKING

from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_review import STAGE3_REVIEW, render_verdict_comment
from cw.config import load_effective_config, sessions_lock
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event as _record_orchestrator_event
from cw.local_runner import UNEXPECTED_ERROR, make_blocked
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask

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
    """
    # Function-level imports break the cw.executor <-> cw.codex_background
    # cycle: executor.py imports this module at its top, and all three names
    # below are defined *after* executor.py's own import block, so a
    # module-level import here would hit a partially initialized module.
    # Mirrors claim.py's deferred cw.dispatch.gating import (#1310).
    from cw.executor import (
        _complete_session_via_door,
        _post_review_comment,
        _resolve_codex_fix_loop_enabled,
    )

    try:
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
        with sessions_lock():
            _complete_session_via_door(
                sid=sid,
                payload=make_blocked(
                    ticket_id=task.ticket_id,
                    worktree=worktree,
                    reason=UNEXPECTED_ERROR,
                    stage_reached=STAGE3_REVIEW,
                ).model_dump(mode="json"),
                guard_already_completed=True,
            )
        # Deferred for the same cycle reason as the imports above: claim.py
        # imports cw.executor at module level, which imports this module.
        from cw.dispatch.claim import _revert_claimed_task_to_pending

        _revert_claimed_task_to_pending(client.name, task.ticket_id, stamp_backoff=True)
