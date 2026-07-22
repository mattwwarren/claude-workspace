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
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
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
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _FINALIZE_BLOCKED_REASON,
    _NEEDS_SALVAGE_REASON,
    _RESCUE_PR_BODY_TEMPLATE,
    _RESCUE_PR_CLOSES_TRAILER_TEMPLATE,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_PR_BODY_TEMPLATE,
    _SALVAGE_PR_TITLE_TEMPLATE,
    _SalvageCandidate,
    ticket_id_for_session,
)
from cw.worktree import _git_dir, _has_commits_beyond_base

if TYPE_CHECKING:
    from cw.models import Session

_log = logging.getLogger(__name__)


def salvage_committed_no_pr_sessions(
    candidates: list[_SalvageCandidate],
    *,
    merged_client_ticket_ids: frozenset[tuple[str, str]] = frozenset(),
) -> list[str]:
    """Post-pass: git-state salvage for committed-but-no-PR reaped sessions.

    Called from reconcile() AFTER sessions_lock releases — git and gh subprocesses
    run here, never under the session lock. See GitHub issue #497.

    candidates: list of (session_id, ticket_id, branch, worktree_path_str,
    post_review_clean) collected by flag_silently_idle_daemon_sessions under lock.

    merged_client_ticket_ids: (client, ticket_id) pairs whose PR is already
    confirmed merged (#1054 pre-pass). A merged ticket is shipped ground
    truth, not a salvage candidate; completion for it is owned by the idle
    merged-REVERT_TASK path, so it is skipped here as a defensive guard
    against a duplicate PR. In practice the FINALIZE guard + merged-routing
    in idle.py make this branch unreachable for a real merged session, but
    it costs nothing to check before the gh subprocess call. Keyed by
    (client, ticket_id) rather than a bare ticket_id -- ticket_id strings
    are not globally unique across clients (see dev_queue.py's
    (ticket_id, client)-keyed lookups), so a bare-string check would let one
    client's merged ticket wrongly skip a *different* client's unmerged,
    same-numbered candidate.

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

        # Resolve client config; skip session if client no longer configured
        # (removed between session creation and now). Capture the ClientConfig
        # (not just default_branch) so gh calls can be scoped to its repo cwd
        # (GitHub #1269/#1279).
        try:
            client_cfg = get_client(session.client)
        except CwError:
            _log.warning(
                "salvage: unknown client %r for session %s — skipping",
                session.client,
                session_id,
            )
            continue
        default_branch = client_cfg.default_branch
        cwd = _git_dir(client_cfg)

        # Confirm git-state trigger: commits beyond base AND no open PR.
        has_commits = _has_commits_beyond_base(wt_path, default_branch)
        if not has_commits:
            # No commits beyond base — not a salvage candidate; fall through to
            # existing recover/park on the next reconcile tick.
            continue

        # PR merged — shipped, not a salvage candidate. Completion is owned
        # by the idle merged-REVERT_TASK path; skip before the OPEN-only gh
        # check below. See GitHub #1054.
        if ticket_id and (session.client, ticket_id) in merged_client_ticket_ids:
            continue

        pr_result, gh_available = pr_exists_for_branch(branch, cwd=cwd)
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
                cwd=cwd,
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
    *,
    cwd: Path | None = None,
) -> None:
    """Execute the HIGH-confidence automated draft PR path.

    *cwd* scopes the idempotency-recheck gh call to the client's repo
    (GitHub #1269/#1279); its sole production caller always supplies it.
    """
    # Idempotency re-check immediately before creating the PR.
    pr_result, gh_available = pr_exists_for_branch(branch, cwd=cwd)
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
                    transition_task_status(task, QueueItemStatus.COMPLETED)
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


def _stamp_low_path_session_state(
    session: Session, *, usage_limit_detected: bool, now: datetime
) -> bool:
    """Stamp session disposition under sessions_lock.

    usage_limit_detected=True stamps TIMED_OUT/TIMED_OUT/USAGE_LIMIT_CUTOFF.
    Otherwise stamps the pre-#1336 COMPLETED/CRASHED/needs_salvage
    disposition, unless already flagged on a prior tick.

    Returns already_flagged (always False when usage_limit_detected=True,
    since that path carries no idempotency marker of its own — it relies on
    the existing _LIVE_STATUSES gate elsewhere to prevent reclassification).
    """
    already_flagged = False
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id != session.id:
                continue
            if usage_limit_detected:
                s.status = SessionStatus.TIMED_OUT
                s.completed_at = now
                s.completed_reason = CompletionReason.TIMED_OUT
                s.reap_reason = ReapReason.USAGE_LIMIT_CUTOFF
                break
            already_flagged = (
                isinstance(s.last_result, dict)
                and s.last_result.get("paused_status") == _NEEDS_SALVAGE_REASON
            )
            if not already_flagged:
                if isinstance(s.last_result, dict):
                    s.last_result = {
                        **s.last_result,
                        "paused_status": _NEEDS_SALVAGE_REASON,
                    }
                else:
                    s.last_result = {"paused_status": _NEEDS_SALVAGE_REASON}
                s.reap_reason = ReapReason.SALVAGE_PARKED
                s.status = SessionStatus.COMPLETED
                s.completed_at = now
                s.completed_reason = CompletionReason.CRASHED
            break
        save_state(fresh_state)
    return already_flagged


def _notify_needs_salvage(
    session: Session, ticket_id: str | None, breadcrumbs: str
) -> None:
    """Route queue task to BLOCKED_ON_USER and emit the needs_salvage alert."""
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    transition_task_status(
                        task,
                        QueueItemStatus.BLOCKED_ON_USER,
                        disposition=_NEEDS_SALVAGE_REASON,
                    )
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
            "lane": session.lane,
        },
    )
    _deps.fire_push_notification(session.name, session.client)


def _salvage_low_path(
    session: Session,
    ticket_id: str | None,
    branch: str,
    worktree_path_str: str,
) -> None:
    """Execute the LOW-confidence flag-only path.

    When the timeout was caused by the operator hitting their Claude usage
    limit (detected via :func:`_shared.detect_usage_limit`) rather than an
    agent crash, stamps TIMED_OUT/TIMED_OUT/USAGE_LIMIT_CUTOFF instead of the
    COMPLETED/CRASHED/needs_salvage disposition, and skips the BLOCKED_ON_USER
    routing and SESSION_NEEDS_ATTENTION push notification — the existing
    tasks.py:revert_timed_out_tasks backstop already knows how to preserve
    the worktree and route a dirty worktree to BLOCKED_ON_USER on its own
    (GitHub #1336).
    """
    breadcrumbs = f"branch={branch} worktree={worktree_path_str}"
    # Fail CLOSED with a tight 60s window here (#1345): the salvage low-path
    # stamps a terminal USAGE_LIMIT_CUTOFF disposition and (#1336) preserves the
    # worktree, so a stale or unanchored limit message must NOT be mislabeled as
    # a live rate-limit cutoff — that inverse-mislabel would suppress the normal
    # auto-retry of an ordinary crash. Unlike the backoff sites, this one does
    # not fail open.
    usage_limit_detected = _shared.usage_limit_is_recent(
        _shared.detect_usage_limit(session),
        window_seconds=_shared.USAGE_LIMIT_SALVAGE_WINDOW_SECONDS,
        fail_open=False,
    )
    now = datetime.now(UTC)

    # Capture already_flagged so the early-return below can suppress
    # duplicate queue mutation, event, and push notification (#418).
    already_flagged = _stamp_low_path_session_state(
        session, usage_limit_detected=usage_limit_detected, now=now
    )

    if not usage_limit_detected:
        # Already dispositioned on a prior tick — suppress duplicate queue
        # mutation, event, and push notification so the idle watchdog
        # re-collecting this parked session does not re-fire every reconcile
        # tick (#418 removed the upstream _has_terminal_sentinel skip this
        # relied on).
        if already_flagged:
            return
        _notify_needs_salvage(session, ticket_id, breadcrumbs)

    # Stop the surface if still running — daemon cleanup is orthogonal to the
    # disposition label above (GitHub #1249, #1336).
    if session.surface_ref is not None:
        with contextlib.suppress(Exception):
            _deps.get_native_daemon_client().stop(session.surface_ref)


def _rescue_mark_attempted(session_id: str) -> None:
    """Write rescue_attempted=True so the session is skipped on future ticks."""
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session_id:
                if isinstance(s.last_result, dict):
                    s.last_result = {**s.last_result, "rescue_attempted": True}
                else:
                    s.last_result = {"rescue_attempted": True}
                break
        save_state(fresh_state)


def _rescue_open_pr(branch: str, default_branch: str, ticket_id: str | None) -> bool:
    """Create a PR from branch → default_branch. Returns True on success."""
    body = _RESCUE_PR_BODY_TEMPLATE.format(ticket_id=ticket_id or "unknown")
    if ticket_id is not None and ticket_id.isdigit():
        body += _RESCUE_PR_CLOSES_TRAILER_TEMPLATE.format(ticket_id=ticket_id)
    try:
        subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                default_branch,
                "--head",
                branch,
                "--title",
                f"auto-dev: finalize for {ticket_id or 'unknown'}",
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def _rescue_complete(
    session: Session,
    ticket_id: str | None,
    branch: str,
    rescued_ticket_ids: list[str],
    *,
    skip_merge: bool = False,
) -> None:
    """Mark session + task COMPLETED and emit SESSION_COMPLETED event.

    skip_merge=True (merged-ticket path, #1054): the PR is already merged, so
    the ``gh pr merge`` call below would be redundant (and could even fail
    against an already-merged/closed branch); skip straight to marking the
    session/task COMPLETED.
    """
    now = datetime.now(UTC)
    mutated = False
    with sessions_lock():
        fresh_state = load_state()
        for s in fresh_state.sessions:
            if s.id == session.id:
                if s.status == SessionStatus.TIMED_OUT:
                    s.status = SessionStatus.COMPLETED
                    s.completed_at = now
                    s.completed_reason = CompletionReason.NORMAL
                    mutated = True
                break
        save_state(fresh_state)

    if not mutated:
        # Session already advanced past TIMED_OUT by a concurrent path; skip
        # duplicate event and daemon stop to keep the audit log clean.
        return

    # Why: gh pr merge runs AFTER the mutated guard so a concurrent-completion
    # race does not issue a redundant merge-enable (#816). The PR was opened
    # by _rescue_open_pr before this function runs, so the branch is valid.
    # Non-fatal if merge fails — PR is open, human can merge.
    if not skip_merge:
        try:
            subprocess.run(
                ["gh", "pr", "merge", "--auto", "--squash", branch],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            _log.warning(
                "rescue_finalize_blocked: gh pr merge failed for branch %r session %s",
                branch,
                session.id,
            )

    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                # Why (client, ticket_id): ticket_id strings are not globally
                # unique across clients -- a bare ticket_id match here could
                # complete a different client's BLOCKED_ON_USER task that
                # happens to share this ticket_id. See GitHub #1054.
                if (
                    task.client == session.client
                    and task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.BLOCKED_ON_USER
                ):
                    transition_task_status(task, QueueItemStatus.COMPLETED)
                    save_dev_queue(store)
                    rescued_ticket_ids.append(ticket_id)
                    break

    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "crashed": False,
            "salvaged": True,
            "salvage_kind": "finalize_blocked_rescue",
            "branch": branch,
            # skip_merge=True means completion was driven by the merged-ticket
            # ground-truth check (#1054) rather than by cw opening/merging this
            # PR itself -- kept distinct in the audit trail since the two paths
            # have different failure signatures. See GitHub #1054.
            "skip_merge": skip_merge,
        },
    )
    if session.surface_ref is not None:
        with contextlib.suppress(Exception):
            _deps.get_native_daemon_client().stop(session.surface_ref)


def rescue_finalize_blocked_sessions(
    *,
    merged_client_ticket_ids: frozenset[tuple[str, str]] = frozenset(),
) -> list[str]:
    """Post-pass: open PRs for sessions blocked at the finalize stage (GitHub #812).

    Called from reconcile() AFTER sessions_lock releases. Finds TIMED_OUT sessions
    with last_result["paused_status"] == _FINALIZE_BLOCKED_REASON, opens a PR for
    the pushed branch, then marks the session and its task COMPLETED.

    Idempotency: after a gh failure, writes last_result["rescue_attempted"] = True
    so the session is not retried on every subsequent reconcile tick.

    merged_client_ticket_ids: (client, ticket_id) pairs whose PR is already
    confirmed merged (#1054 pre-pass). A merged ticket completes directly
    (skip_merge=True) without the OPEN-only pr_exists_for_branch check or a
    duplicate _rescue_open_pr — avoids the Mode B deadlock where a merged,
    park-marker-blocked session would otherwise never reach completion.
    Keyed by (client, ticket_id), not a bare ticket_id — see
    salvage_committed_no_pr_sessions's docstring for why.

    Returns list of rescued ticket_ids (placed in ReconcileReport.rescued_ticket_ids).
    """
    state = load_state()
    rescued_ticket_ids: list[str] = []

    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if not isinstance(session.last_result, dict):
            continue
        if session.last_result.get("paused_status") != _FINALIZE_BLOCKED_REASON:
            continue
        if session.last_result.get("rescue_attempted"):
            continue
        branch = session.last_result.get("branch")
        if not isinstance(branch, str) or not branch:
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id and (session.client, ticket_id) in merged_client_ticket_ids:
            _rescue_complete(
                session,
                ticket_id=ticket_id,
                branch=branch,
                rescued_ticket_ids=rescued_ticket_ids,
                skip_merge=True,
            )
            continue
        try:
            client_cfg = get_client(session.client)
        except CwError:
            _log.warning(
                "rescue_finalize_blocked: unknown client %r for session %s — skipping",
                session.client,
                session.id,
            )
            continue
        default_branch = client_cfg.default_branch
        pr_result, gh_available = pr_exists_for_branch(branch, cwd=_git_dir(client_cfg))
        # Why: gh-unavailable and transient errors (pr_result=None) are not
        # tombstoned with rescue_attempted. The intent is to retry on the next
        # tick — these conditions are expected to be transient. Only definitive
        # gh pr create failures are tombstoned (below) because they indicate the
        # branch or repo state is incompatible, not a transient availability issue.
        if not gh_available or pr_result is None:
            continue
        pr_created = pr_result is not False or _rescue_open_pr(
            branch, default_branch, ticket_id
        )
        if not pr_created:
            _rescue_mark_attempted(session.id)
            continue
        _rescue_complete(
            session,
            ticket_id=ticket_id,
            branch=branch,
            rescued_ticket_ids=rescued_ticket_ids,
        )

    return rescued_ticket_ids
