"""Boot-time pass over codex sessions orphaned by a crash (GitHub #1727).

Since ``CodexExecutor.spawn()`` hands its review to a background thread, an
ordinary process exit can land mid-review. ``run_dispatch_loop``'s shutdown
path covers the exits we control by bounded-joining those threads
(``cw.codex_background.join_outstanding_codex_threads``). A crash or ``SIGKILL``
is the case a join cannot reach at all: the process that owned the thread is
already gone, so there is nothing left to join and nothing recorded a failure.
What survives is a session still marked ``ACTIVE``, a task still ``RUNNING``,
and possibly a half-committed worktree.

This module is the other half of that pair: run once per process before the
first dispatch tick, it treats any live codex-origin headless ``DAEMON``
session as evidence of exactly that and dispositions it one of two ways
(#2285):

- **Requeue** (RUNNING -> PENDING, same stage) only when the lane's
  ``reap_policy`` resolves to ``auto`` (ADR-0006: a revert is a destructive
  act) AND the orphan is provably clean — the lane's codex fix loop is off,
  the worktree carries nothing uncommitted beyond ``.claude/review-verdict.md``,
  HEAD still matches the review's recorded baseline, and no codex process is
  still running in the worktree. Emits ``TICKET_REQUEUED``.
- **Park** for operator inspection in every other case, exactly as before —
  any uncertainty (a git error, an unresolvable ref) parks.

In both branches the orphaned ``Session`` record itself is closed to
``COMPLETED``/``CRASHED``. Before #2285 it stayed ``ACTIVE`` forever: it held a
client ceiling slot, and its stale ``cw-context.json`` made the next
DAEMON-origin ``_write_hook_context`` into the same worktree raise
``HookContextConflictError``.

Two existing primitives carry the task transition rather than a new path:

- ``cw.dispatch.claim._park_running_task_blocked_on_user`` — the shared
  "park this task for operator inspection and emit SESSION_NEEDS_ATTENTION"
  primitive already used by the dirty-worktree guard and the codex capability
  gate (#1238, #1257).
- ``cw.dispatch.claim._revert_claimed_task_to_pending`` — the shared
  RUNNING -> PENDING revert, which charges an unproductive attempt so a serve
  crash loop stays bounded by the global attempt ceiling.

The codex-origin test, ``cw.executor.resolve_executor_config(...).backend !=
CODEX_BACKEND``, is lifted from ``claim.py``'s capability gate.

This is a blast-radius bound, not a liveness handle: it does not make codex
sessions crash-recoverable in the RFC 0005 F3 sense (no PID/surface_ref is
persisted for external harvest). See the ``StageExecutor`` Protocol invariant
comment in ``cw.executor`` for the accepted gap this bounds.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from cw._git import capture_head_sha, git_clean_env
from cw.codex_background import (
    REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
    _resolve_codex_fix_loop_enabled,
)
from cw.config import (
    load_clients,
    load_effective_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue
from cw.events import record_event
from cw.models import (
    CODEX_BACKEND,
    CompletionReason,
    OrchestratorEventType,
    ReapPolicy,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import _LIVE_STATUSES, _is_headless, ticket_id_for_session
from cw.reconcile.tasks import _resolve_task_policy
from cw.worktree import _checked_out_branch, fetch_feature_branch

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, Stage, TicketTask

_log = logging.getLogger(__name__)

# Short reason code stamped as the task's disposition and carried as the
# SESSION_NEEDS_ATTENTION payload's ``paused_status``.
CODEX_ORPHANED_AT_BOOT_DISPOSITION = "codex_review_orphaned_at_boot"

# TICKET_REQUEUED ``reason`` for a provably-clean orphan put back to PENDING.
CODEX_ORPHAN_CLEAN_REQUEUE_REASON = "codex_orphan_clean_requeue_at_boot"

_ORPHAN_BREADCRUMBS = (
    "ACTIVE codex-origin session found at process start; its background review"
    " thread did not survive the prior process exit (crash/SIGKILL) — inspect"
    " the worktree for a partial commit or orphaned scratch dir before"
    " reclaiming."
)

# Why a park, not a requeue — appended to _ORPHAN_BREADCRUMBS.
_PARK_REASON_REAP_POLICY_NOT_AUTO = (
    "the lane's reap_policy does not authorize automatic requeue"
)
_PARK_REASON_FIX_LOOP_ENABLED = "the lane's codex fix loop is enabled"
_PARK_REASON_DIRTY_WORKTREE = (
    "the worktree carries uncommitted changes beyond the review verdict"
)
_PARK_REASON_GIT_ERROR = "the worktree's git state could not be established"
_PARK_REASON_HEAD_MOVED = "HEAD has moved since the review's recorded baseline"
_PARK_REASON_CODEX_PROCESS_RUNNING = "a codex process is still running in the worktree"

# Bounds every git call below: this pass blocks process start, so one hung
# git must not wedge the dispatch loop before its first tick.
_GIT_SUBPROCESS_TIMEOUT_SECONDS: float = 10.0
# Porcelain v1: two status characters and a space precede the path.
_GIT_PORCELAIN_PATH_OFFSET = 3
_GIT_PORCELAIN_RENAME_SEPARATOR = " -> "
# psutil name() of the exec'd codex binary (codex_runner spawns it directly,
# no shell or interpreter wrapper).
_CODEX_PROCESS_NAME = "codex"


def _worktree_porcelain_clean_except_verdict(worktree: Path) -> bool | None:
    """Return whether *worktree* is clean apart from the review verdict file.

    Tri-state: ``True`` clean (nothing, or only
    ``REVIEW_VERDICT_COMMENT_RELATIVE_PATH``, is pending), ``False`` dirty,
    ``None`` when git could not answer — kept distinct from ``False`` so the
    park reason says ``git_error`` rather than misreporting dirt. Parse shape
    mirrors ``codex_fix_loop._porcelain_changed_paths``, except a rename
    contributes BOTH sides: ``git mv tracked.md .claude/review-verdict.md``
    touches a tracked file, which must never read as clean.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            env=git_clean_env(),
            timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    allowed = str(REVIEW_VERDICT_COMMENT_RELATIVE_PATH)
    for line in completed.stdout.splitlines():
        if not line:
            continue
        entry = line[_GIT_PORCELAIN_PATH_OFFSET:]
        paths = entry.split(_GIT_PORCELAIN_RENAME_SEPARATOR, 1)
        if any(path != allowed for path in paths):
            return False
    return True


def _head_matches_pre_review_ref(
    worktree: Path, task: TicketTask, client: ClientConfig
) -> bool | None:
    """Return whether *worktree*'s HEAD still equals the review's baseline.

    The baseline is ``task.stage_base_ref`` (HEAD as stamped when the review
    spawn succeeded). When that was never stamped, fall back to the remote
    branch tip: best-effort fetch, then compare against ``origin/<branch>``.
    ``None`` when neither can be established — HEAD unreadable, detached, or
    no remote-tracking ref — which the caller parks as a git error.
    """
    head = capture_head_sha(worktree, strict=False)
    if not head:
        return None
    if task.stage_base_ref:
        return head == task.stage_base_ref
    branch = _checked_out_branch(worktree)
    if branch is None:
        return None
    # Best effort: a failed fetch leaves the existing tracking ref, and a
    # missing one resolves to "" below, which parks.
    fetch_feature_branch(client, branch)
    origin_sha = capture_head_sha(worktree, ref=f"origin/{branch}", strict=False)
    if not origin_sha:
        return None
    return head == origin_sha


def _codex_process_running_in(worktree: Path) -> bool:
    """Return whether any process named ``codex`` has *worktree* as its cwd.

    A cwd scan is the only signal available: no PID is persisted for the
    codex child (see the module docstring), so this cannot pin identity by
    start time the way ``LocalLivenessHandle`` does. Never raises. A process
    that vanishes or denies access mid-scan is skipped; a scan that cannot run
    at all reads as ``True`` so the caller parks rather than races an
    unobserved writer.
    """
    target = worktree.resolve()
    try:
        processes = list(psutil.process_iter(["name", "cwd"]))
    except (psutil.Error, OSError):
        return True
    for process in processes:
        try:
            info = process.info
            cwd = info.get("cwd")
            if (
                info.get("name") == _CODEX_PROCESS_NAME
                and cwd
                and Path(cwd).resolve() == target
            ):
                return True
        except (psutil.Error, OSError):
            continue
    return False


def _git_park_reason(
    worktree: Path, task: TicketTask, client: ClientConfig
) -> str | None:
    """Return the park reason the worktree's git state implies, or None."""
    clean = _worktree_porcelain_clean_except_verdict(worktree)
    if clean is None:
        return _PARK_REASON_GIT_ERROR
    if not clean:
        return _PARK_REASON_DIRTY_WORKTREE
    head_matches = _head_matches_pre_review_ref(worktree, task, client)
    if head_matches is None:
        return _PARK_REASON_GIT_ERROR
    if not head_matches:
        return _PARK_REASON_HEAD_MOVED
    return None


def _resolve_orphan_action(
    worktree: Path | None,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> tuple[bool, str]:
    """Decide requeue vs. park for one orphaned codex session.

    Returns ``(should_requeue, reason)``. Gates run cheapest and most decisive
    first; the first failing gate parks with its own reason. Gate 0 is the
    ADR-0006 authority check: the requeue is a RUNNING -> PENDING revert, so
    anything but ``reap_policy: auto`` for the task's lane (resolved with the
    same resolver ``reconcile.tasks`` gates its reverts on) parks before any
    git or psutil work runs.
    """
    policy = _resolve_task_policy(task.client, task.lane, clients, config)
    if policy is not ReapPolicy.AUTO:
        return False, _PARK_REASON_REAP_POLICY_NOT_AUTO
    if worktree is None:
        return False, _PARK_REASON_GIT_ERROR
    if _resolve_codex_fix_loop_enabled(client, task, config):
        return False, _PARK_REASON_FIX_LOOP_ENABLED
    git_reason = _git_park_reason(worktree, task, client)
    if git_reason is not None:
        return False, git_reason
    if _codex_process_running_in(worktree):
        return False, _PARK_REASON_CODEX_PROCESS_RUNNING
    return True, CODEX_ORPHAN_CLEAN_REQUEUE_REASON


def _close_orphaned_session_and_dispose(
    *,
    session_id: str,
    ticket_id: str,
    client_name: str,
    stage: Stage,
    should_requeue: bool,
    park_reason: str,
) -> None:
    """Close the orphaned Session record, then requeue or park its task.

    The session is closed in both branches: a record left ACTIVE holds a
    ceiling slot and trips the next spawn's hook-context conflict guard. The
    task transition re-verifies ``expected_session_id`` under the dev-queue
    lock, so a row re-claimed since the caller's snapshot is left alone.
    """
    # Deferred for the same import-cycle reason as in
    # reap_orphaned_codex_sessions_at_boot below.
    from cw.dispatch.claim import (
        _park_running_task_blocked_on_user,
        _revert_claimed_task_to_pending,
    )

    # Why not mutate_state: dev_queue_lock is nested inside this sessions_lock
    # window (mirrors cli/spawn.py:_spawn_complete_impl's identical nesting) so
    # the session close and the task transition land under one lock scope.
    with sessions_lock():
        state = load_state()
        for session in state.sessions:
            if session.id == session_id:
                session.status = SessionStatus.COMPLETED
                session.completed_at = datetime.now(UTC)
                session.completed_reason = CompletionReason.CRASHED
                break
        save_state(state)
        if should_requeue:
            _revert_claimed_task_to_pending(
                client_name, ticket_id, expected_session_id=session_id
            )
            # Same-stage PENDING revert, so the payload mirrors
            # dispatch/routing's provider_overload_retry shape (from_stage ==
            # to_stage, no ``regressed`` key) rather than crud.py's
            # always-regressed one.
            record_event(
                OrchestratorEventType.TICKET_REQUEUED,
                {
                    "ticket_id": ticket_id,
                    "client": client_name,
                    "from_stage": stage,
                    "to_stage": stage,
                    "reason": CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
                    "session_id": session_id,
                },
            )
        else:
            _park_running_task_blocked_on_user(
                ticket_id=ticket_id,
                client_name=client_name,
                expected_session_id=session_id,
                disposition=CODEX_ORPHANED_AT_BOOT_DISPOSITION,
                breadcrumbs=f"{_ORPHAN_BREADCRUMBS} ({park_reason}).",
            )


def reap_orphaned_codex_sessions_at_boot() -> int:
    """Requeue or park every live codex-origin session found at process start.

    Returns the number of orphans acted on (requeued or parked). Never raises
    on an unresolvable session (unknown client, no matching dev-queue row,
    unparseable name) — this runs on the boot path, where refusing to start is
    strictly worse than skipping one ambiguous session.
    """
    # Deferred for import-cycle reasons: cw.executor imports cw.reconcile at
    # module level, so this module (inside the cw.reconcile package) must not
    # reach into it at import time. Mirrors _shared.py's own deferred
    # cw.dispatch import (#698).
    from cw.executor import resolve_executor_config

    state = load_state()
    # Keyed by (ticket_id, client), not ticket_id alone: ticket numbering is
    # per-client, so a claude-workspace ticket 21 and another client's ticket 21
    # are different tasks. Keying on ticket_id alone would let one client's row
    # shadow the other's and park the wrong client's live session. Matches
    # _park_running_task_blocked_on_user's own (ticket_id, client) key exactly.
    task_by_ticket = {
        (task.ticket_id, task.client): task for task in load_dev_queue().tasks
    }
    clients = load_clients()
    config = load_effective_config()

    # Counts every orphan acted on -- requeued or parked.
    parked = 0
    for session in state.sessions:
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
        ):
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        task = task_by_ticket.get((ticket_id, session.client))
        client = clients.get(session.client)
        if task is None or client is None:
            continue
        # Identity, not coincidence of (ticket_id, client): an earlier boot's
        # orphan can linger in state as an ACTIVE record long after its task was
        # parked, recovered, and re-dispatched onto a fresh session. Without this
        # check that zombie re-matches on every later boot and disposes of
        # whatever healthy review now owns the row. This is a cheap early-exit
        # against the snapshot read above, not the safety guarantee itself — the
        # row could still be re-claimed between here and the transition below,
        # so the same identity is re-verified atomically under the lock via
        # expected_session_id (#1727 round 5, #2285).
        if task.session_id != session.id:
            continue
        if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
            continue
        should_requeue, reason = _resolve_orphan_action(
            session.worktree_path, task, client, clients, config
        )
        if should_requeue:
            _log.warning(
                "codex_boot: session %s (%s/%s) was still ACTIVE at process start;"
                " worktree and HEAD are unchanged since the review began --"
                " requeuing the task for a fresh attempt",
                session.id,
                session.client,
                ticket_id,
            )
        else:
            _log.warning(
                "codex_boot: session %s (%s/%s) was still ACTIVE at process start;"
                " parking the task for operator inspection (%s)",
                session.id,
                session.client,
                ticket_id,
                reason,
            )
        _close_orphaned_session_and_dispose(
            session_id=session.id,
            ticket_id=ticket_id,
            client_name=session.client,
            stage=task.stage,
            should_requeue=should_requeue,
            park_reason=reason,
        )
        parked += 1
    return parked
