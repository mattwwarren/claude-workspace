"""Git-state salvage post-pass for reconcile.

Recovers committed-but-no-PR sessions that were reaped post-review: opens an
automated draft PR (HIGH path) or flags for human salvage (LOW path). Runs
AFTER sessions_lock releases — git and gh subprocesses never run under the
session lock. See GitHub issue #497.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw.config import get_client, load_state, save_state, sessions_lock
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import pr_exists_for_branch
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionStatus,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _NEEDS_SALVAGE_REASON,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_PR_BODY_TEMPLATE,
    _SALVAGE_PR_TITLE_TEMPLATE,
    _SalvageCandidate,
)
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from cw.models import Session

_log = logging.getLogger(__name__)


def salvage_committed_no_pr_sessions(
    candidates: list[_SalvageCandidate],
) -> list[str]:
    """Post-pass: git-state salvage for committed-but-no-PR reaped sessions.

    Called from reconcile() AFTER sessions_lock releases — git and gh subprocesses
    run here, never under the session lock. See GitHub issue #497.

    candidates: list of (session_id, ticket_id, branch, worktree_path_str,
    post_review_clean) collected by flag_silently_idle_daemon_sessions under lock.

    Returns list of ticket_ids that were auto-completed (HIGH path).
    """
    if not candidates:
        return []

    completed_ticket_ids: list[str] = []
    state = load_state()

    for (
        session_id,
        ticket_id,
        branch,
        worktree_path_str,
        post_review_clean,
    ) in candidates:
        session = next((s for s in state.sessions if s.id == session_id), None)
        if session is None:
            continue

        wt_path = Path(worktree_path_str)

        # Resolve default_branch from client config; skip session if client no
        # longer configured (removed between session creation and now).
        try:
            default_branch = get_client(session.client).default_branch
        except CwError:
            _log.warning(
                "salvage: unknown client %r for session %s — skipping",
                session.client,
                session_id,
            )
            continue

        # Confirm git-state trigger: commits beyond base AND no open PR.
        has_commits = _has_commits_beyond_base(wt_path, default_branch)
        if not has_commits:
            # No commits beyond base — not a salvage candidate; fall through to
            # existing recover/park on the next reconcile tick.
            continue

        pr_result, gh_available = pr_exists_for_branch(branch)
        if not gh_available:
            # gh absent — cannot confirm PR absence; treat as non-candidate.
            continue
        if pr_result is None:
            # Transient error — cannot confirm; treat as non-candidate.
            continue
        if pr_result is True:
            # PR already exists — not our case.
            continue

        # Confirmed: commits beyond base AND no open PR.
        if post_review_clean:
            # HIGH path: automated draft PR.
            _salvage_high_path(
                session,
                ticket_id,
                branch,
                wt_path,
                completed_ticket_ids,
                default_branch,
            )
        else:
            # LOW path: flag for human salvage.
            _salvage_low_path(session, ticket_id, branch, worktree_path_str)

    return completed_ticket_ids


def _salvage_high_path(
    session: Session,
    ticket_id: str | None,
    branch: str,
    wt_path: Path,
    completed_ticket_ids: list[str],
    default_branch: str,
) -> None:
    """Execute the HIGH-confidence automated draft PR path."""
    # Idempotency re-check immediately before creating the PR.
    pr_result, gh_available = pr_exists_for_branch(branch)
    if not gh_available or pr_result is True or pr_result is None:
        # Cannot confirm or PR now exists — downgrade to LOW.
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Push branch to origin (no-op if already pushed).
    try:
        subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=wt_path,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Create draft PR.
    title = _SALVAGE_PR_TITLE_TEMPLATE.format(ticket_id=ticket_id or "unknown")
    body = _SALVAGE_PR_BODY_TEMPLATE.format(ticket_id=ticket_id or "unknown")
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                default_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        pr_url = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        _salvage_low_path(session, ticket_id, branch, str(wt_path))
        return

    # Mark session completed — under sessions_lock to prevent concurrent clobber.
    now = datetime.now(UTC)
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session.id:
                if s.status not in (SessionStatus.COMPLETED, SessionStatus.TIMED_OUT):
                    s.status = SessionStatus.COMPLETED
                    s.completed_at = now
                    s.completed_reason = CompletionReason.NORMAL
                    s.reap_reason = ReapReason.SALVAGE_COMPLETED
                break
        save_state(fresh_state)

    # Update queue task to COMPLETED.
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.COMPLETED
                    save_dev_queue(store)
                    completed_ticket_ids.append(ticket_id)
                    break

    # Emit SESSION_COMPLETED event.
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "crashed": False,
            "salvaged": True,
            "salvage_kind": _SALVAGE_KIND_GIT_STATE,
            "draft": True,
            "pr": pr_url,
        },
    )

    # Stop the surface if still running.
    if session.surface_ref is not None:
        with contextlib.suppress(Exception):
            _deps.get_native_daemon_client().stop(session.surface_ref)


def _salvage_low_path(
    session: Session,
    ticket_id: str | None,
    branch: str,
    worktree_path_str: str,
) -> None:
    """Execute the LOW-confidence flag-only path."""
    breadcrumbs = f"branch={branch} worktree={worktree_path_str}"
    already_flagged = False

    # Update session last_result under sessions_lock. Capture already_flagged
    # before the conditional write so the early-return below can suppress
    # duplicate queue mutation, event, and push notification (#418).
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session.id:
                already_flagged = (
                    isinstance(s.last_result, dict)
                    and s.last_result.get("paused_status") == _NEEDS_SALVAGE_REASON
                )
                if not already_flagged:
                    s.last_result = {"paused_status": _NEEDS_SALVAGE_REASON}
                    s.reap_reason = ReapReason.SALVAGE_PARKED
                break
        save_state(fresh_state)

    # Already dispositioned on a prior tick — suppress duplicate queue mutation,
    # event, and push notification so the idle watchdog re-collecting this parked
    # session does not re-fire every reconcile tick (#418 removed the upstream
    # _has_terminal_sentinel skip this relied on).
    if already_flagged:
        return

    # Route queue task to BLOCKED_ON_USER.
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.BLOCKED_ON_USER
                    save_dev_queue(store)
                    break

    # Emit SESSION_NEEDS_ATTENTION with breadcrumbs for human salvage.
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _NEEDS_SALVAGE_REASON,
            "breadcrumbs": breadcrumbs,
            "crashed": False,
        },
    )
    _deps.fire_push_notification(session.name, session.client)
