"""RUNNING-task wedge detectors for ``cw doctor`` (classes 2, 3 and 4).

RUNNING queue tasks with no live session (class-2), with an already
COMPLETED session (class-3), and whose branch is pushed while the queue still
says RUNNING (class-4, ``wedge/repo-ahead-of-queue``, advisory only). The only
submodule that reads ``run_git``; imports :func:`_gh_pr_states` from
``loop_health`` at top level. Imports no sibling. Split out of the flat
``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

import subprocess as _sp
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw._git import run_git
from cw.config import state_file
from cw.doctor import _deps
from cw.doctor._shared import WedgeFinding
from cw.doctor.loop_health import _gh_pr_states
from cw.exceptions import CwError
from cw.models import QueueItemStatus, SessionStatus
from cw.reconcile import SPAWN_GRACE_SECONDS, feature_branch_key

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session, TicketTask


def _check_wedge_task_running_no_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks with no associated live session.

    Skips tasks within SPAWN_GRACE_SECONDS of creation — newly spawned
    tasks have not yet registered their session_id.
    """
    findings: list[WedgeFinding] = []
    _live = {SessionStatus.ACTIVE, SessionStatus.IDLE}
    live_session_ids = {s.id for s in state.sessions if s.status in _live}
    now = datetime.now(UTC)
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.session_id is None:
            created = task.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).total_seconds() < SPAWN_GRACE_SECONDS:
                continue
            findings.append(
                WedgeFinding(
                    wedge_class="wedge/task-running-no-session",
                    session_id=None,
                    ticket_id=task.ticket_id,
                    recipe=(
                        "Queue task RUNNING with no matching session. "
                        "Run: cw doctor --reap to revert task to PENDING."
                    ),
                    state_file=str(state_file()),
                )
            )
        elif task.session_id not in live_session_ids:
            # session_id set but points to a non-live session (missing from
            # state, TIMED_OUT, etc.).
            # _check_wedge_task_running_completed_session handles COMPLETED.
            # BACKGROUNDED is excluded: the session is intentionally paused
            # and will resume — flagging it as a wedge is a false positive.
            session_by_id_local = {s.id: s for s in state.sessions}
            sess = session_by_id_local.get(task.session_id)
            _non_wedge = {SessionStatus.COMPLETED, SessionStatus.BACKGROUNDED}
            if sess is None or sess.status not in _non_wedge:
                findings.append(
                    WedgeFinding(
                        wedge_class="wedge/task-running-no-session",
                        session_id=task.session_id,
                        ticket_id=task.ticket_id,
                        recipe=(
                            "Queue task RUNNING with no matching session. "
                            "Run: cw doctor --reap to revert task to PENDING."
                        ),
                        state_file=str(state_file()),
                    )
                )
    return findings


def _check_wedge_task_running_completed_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks whose session is already COMPLETED."""
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING or task.session_id is None:
            continue
        session = session_by_id.get(task.session_id)
        if session is None or session.status != SessionStatus.COMPLETED:
            continue
        findings.append(
            WedgeFinding(
                wedge_class="wedge/task-running-completed-session",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "Queue task RUNNING but its session is already COMPLETED. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _resolve_wedge_branch(
    task: TicketTask,
    session_by_id: dict[str, Session],
    clients: dict[str, ClientConfig],
) -> str:
    """Branch for a wedge check: the session's branch, else the feature prefix.

    Falls back to ``<feature_branch_prefix>/<ticket>`` (``dev`` when the client
    is unknown), mirroring what the staged pipeline provisions and pushes (#712).
    """
    if task.session_id is not None:
        session = session_by_id.get(task.session_id)
        if session is not None and session.branch:
            return session.branch
    return feature_branch_key(task.client, task.ticket_id, clients)


def _check_wedge_repo_ahead(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING tasks whose branch is pushed to remote but queue not updated.

    Uses ``git ls-remote`` to check if the branch exists on the remote and
    ``gh pr list`` to determine whether a PR is open. Advisory only — no
    automatic reap.
    """
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients and fall back to the default feature-branch prefix below
    # (mirrors the guard around load_clients in run_doctor).
    try:
        clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.worktree_path is None:
            continue
        # Branch resolution: prefer session branch, fallback to the client's
        # configured feature branch (<feature_branch_prefix>/<ticket>, e.g.
        # dev/662 — what the staged pipeline provisions and pushes, #712).
        branch = _resolve_wedge_branch(task, session_by_id, clients)
        # Get remote URL from worktree
        try:
            remote_result = run_git(
                ["-C", str(task.worktree_path), "remote", "get-url", "origin"],
                capture_output=True,
                check=False,
            )
            if remote_result.returncode != 0:
                continue
            remote_url = remote_result.stdout.strip()
        except OSError:
            continue
        # Check if branch exists on remote
        try:
            ls_result = run_git(
                ["ls-remote", remote_url, f"refs/heads/{branch}"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if ls_result.returncode != 0 or not ls_result.stdout.strip():
                continue
        except (OSError, _sp.TimeoutExpired):
            continue
        # Check PR status via gh CLI
        recipe: str
        prs, _ = _gh_pr_states(branch)
        if not prs:
            recipe = (
                f"Branch {branch} is ahead of main with no open PR. "
                f"Suggested: cw spawn-complete {task.ticket_id} or open PR manually."
            )
        else:
            pr_state = prs[0].get("state", "OPEN")
            recipe = (
                f"Branch {branch} has {pr_state} PR but queue still RUNNING. "
                f"Suggested: cw spawn-complete {task.ticket_id} --status shipped."
            )
        findings.append(
            WedgeFinding(
                wedge_class="wedge/repo-ahead-of-queue",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=recipe,
                state_file=str(state_file()),
            )
        )
    return findings
