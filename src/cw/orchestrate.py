"""PR retirement and orchestrator status snapshot.

This module closes the loop on the orchestrator pipeline:

* :func:`retire_merged_prs` consumes ``pr.merged`` events, removes the PR
  from review-monitor's persisted state, marks correlated sessions as
  ``COMPLETED`` (reason ``HANDOFF``), closes their cmux surfaces, and
  emits ``session.completed`` events.

* :func:`orchestrator_status` returns a snapshot of the orchestrator's
  current state -- pending dev-queue tickets, running sessions, monitored
  PRs, and recent events -- shared between the TUI dashboard and the
  ``cw orchestrate status [--json]`` CLI.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from cw.cmux import get_cmux_adapter
from cw.config import load_state, review_monitor_dir, save_state
from cw.dev_queue import load_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.models import (
    CompletionReason,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionStatus,
    TicketTask,
)
from cw.pr_responder import (
    PRDispatchRecord,
    load_dispatch_record,
    save_dispatch_record,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.cmux import CmuxAdapter

logger = logging.getLogger(__name__)

_RETIREMENT_CONSUMER = "orchestrate_retire"
_RECENT_EVENTS_LIMIT = 20
_REVIEW_MONITOR_SCRIPT = Path.home() / ".claude" / "scripts" / "review_monitor.py"


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run review_monitor.py via subprocess, capturing output."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _invoke_review_monitor_complete(
    repo: str,
    pr_number: int,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Invoke ``review_monitor.py complete`` for a merged PR.

    Returns True on success (exit code 0) and False otherwise.  Failures
    are logged and swallowed so retirement of correlated sessions still
    proceeds.
    """
    script = _REVIEW_MONITOR_SCRIPT
    # The script existence check is a production UX helper -- skip it when
    # a custom runner is injected so tests can stub the subprocess entirely.
    if runner is None and not script.exists():
        # Resolve from PATH as a fallback (e.g. when packaged differently).
        which = shutil.which("review_monitor.py")
        if which is None:
            logger.warning(
                "review_monitor.py not found at %s and not on PATH; "
                "skipping monitor cleanup for %s#%s",
                script,
                repo,
                pr_number,
            )
            return False
        script = Path(which)

    cmd = [
        str(script),
        "complete",
        str(pr_number),
        "--repo",
        repo,
        "--reason",
        "merged",
    ]
    invoke = runner or _default_runner
    result = invoke(cmd)
    if result.returncode != 0:
        logger.warning(
            "review_monitor complete failed for %s#%s: %s",
            repo,
            pr_number,
            result.stderr.strip() if result.stderr else "(no stderr)",
        )
        return False
    return True


def _sessions_for_pr(
    dispatch_record: PRDispatchRecord,
    repo: str,
    pr_number: int,
) -> list[tuple[str, str]]:
    """Return ``[(dispatch_key, session_id)]`` pairs for a given PR.

    Matches dispatch keys of the form ``{repo}#{pr_number}|<role>``.
    """
    prefix = f"{repo}#{pr_number}|"
    return [
        (key, sid)
        for key, sid in dispatch_record.active.items()
        if key.startswith(prefix)
    ]


def _close_session(
    sess: Session,
    adapter: CmuxAdapter,
    *,
    pr_number: int,
    repo: str,
) -> None:
    """Close a session: mark COMPLETED, close surface, emit event."""
    if sess.surface_ref is not None:
        try:
            adapter.close(sess.surface_ref)
        except Exception:
            logger.exception(
                "Failed to close cmux surface %s for session %s",
                sess.surface_ref,
                sess.id,
            )

    sess.status = SessionStatus.COMPLETED
    sess.completed_reason = CompletionReason.HANDOFF
    sess.completed_at = datetime.now(UTC)

    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": sess.id,
            "reason": CompletionReason.HANDOFF.value,
            "pr_number": pr_number,
            "repo": repo,
        },
    )


def retire_merged_prs(
    adapter: CmuxAdapter | None = None,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Process ``pr.merged`` events and retire correlated sessions.

    For each unprocessed ``pr.merged`` event:

    1. Invoke ``review_monitor.py complete`` to remove the PR from
       monitoring state.
    2. Look up sessions correlated to the PR via :class:`PRDispatchRecord`.
    3. Close each session's cmux surface, mark it ``COMPLETED`` with
       reason ``HANDOFF``, and emit a ``session.completed`` event.
    4. Drop the dispatch record entries so subsequent ticks see no
       lingering correlation.

    Idempotency comes from the consumer cursor: a second invocation with
    no new ``pr.merged`` events is a no-op.

    Args:
        adapter: CmuxAdapter for surface close calls.  Defaults to
            :func:`cw.cmux.get_cmux_adapter`.
        runner: Optional subprocess runner override (for tests).

    Returns:
        List of session IDs that were retired during this call.
    """
    events = read_events(
        consumer=_RETIREMENT_CONSUMER,
        event_types=[OrchestratorEventType.PR_MERGED],
    )
    if not events:
        return []

    resolved_adapter = adapter or get_cmux_adapter()
    state = load_state()
    dispatch_record = load_dispatch_record()
    retired: list[str] = []

    for event in events:
        payload = event.payload
        repo = str(payload.get("repo", ""))
        pr_number_raw = payload.get("pr_number", 0)
        try:
            pr_number = int(pr_number_raw)
        except (TypeError, ValueError):
            logger.warning(
                "pr.merged event %s missing valid pr_number: %r",
                event.id,
                pr_number_raw,
            )
            advance_cursor(_RETIREMENT_CONSUMER, event.id)
            continue

        if not repo:
            logger.warning("pr.merged event %s missing repo field", event.id)
            advance_cursor(_RETIREMENT_CONSUMER, event.id)
            continue

        # 1. Cleanup review-monitor state.
        _invoke_review_monitor_complete(repo, pr_number, runner=runner)

        # 2-4. Find and retire correlated sessions.
        matches = _sessions_for_pr(dispatch_record, repo, pr_number)
        for dispatch_key, session_id in matches:
            sess = next(
                (s for s in state.sessions if s.id == session_id),
                None,
            )
            if sess is None:
                logger.info(
                    "Dispatch key %s references unknown session %s; dropping",
                    dispatch_key,
                    session_id,
                )
                dispatch_record.active.pop(dispatch_key, None)
                continue

            if sess.status == SessionStatus.COMPLETED:
                # Already retired; drop the stale dispatch entry.
                dispatch_record.active.pop(dispatch_key, None)
                continue

            _close_session(
                sess,
                resolved_adapter,
                pr_number=pr_number,
                repo=repo,
            )
            dispatch_record.active.pop(dispatch_key, None)
            retired.append(session_id)

        advance_cursor(_RETIREMENT_CONSUMER, event.id)

    save_state(state)
    save_dispatch_record(dispatch_record)

    return retired


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


class MonitoredPR(BaseModel):
    """Lightweight view of a PR currently tracked by review-monitor."""

    repo: str
    pr_number: int
    role: str
    status: str
    unresolved_threads: int


class SessionSummary(BaseModel):
    """Lightweight view of a session for the status snapshot."""

    id: str
    name: str
    client: str
    status: str
    purpose: str
    started_at: datetime
    surface_ref: str | None = None
    worktree_path: Path | None = None


class TicketSummary(BaseModel):
    """Lightweight view of a dev-queue ticket."""

    ticket_id: str
    client: str
    priority: int
    status: str
    created_at: datetime
    scope_hint: str | None = None


class EventSummary(BaseModel):
    """Lightweight view of a single orchestrator event."""

    id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime


class OrchestratorStatus(BaseModel):
    """Snapshot of the orchestrator subsystem.

    Consumed by both the dashboard TUI and the ``cw orchestrate status``
    CLI; fields are intentionally simple so JSON serialisation is stable.
    """

    generated_at: datetime
    pending_tickets: list[TicketSummary] = Field(default_factory=list)
    running_sessions: list[SessionSummary] = Field(default_factory=list)
    monitored_prs: list[MonitoredPR] = Field(default_factory=list)
    recent_events: list[EventSummary] = Field(default_factory=list)


def _summarise_ticket(task: TicketTask) -> TicketSummary:
    return TicketSummary(
        ticket_id=task.ticket_id,
        client=task.client,
        priority=task.priority,
        status=task.status.value,
        created_at=task.created_at,
        scope_hint=task.scope_hint,
    )


def _summarise_session(sess: Session) -> SessionSummary:
    return SessionSummary(
        id=sess.id,
        name=sess.name,
        client=sess.client,
        status=sess.status.value,
        purpose=sess.purpose.value,
        started_at=sess.started_at,
        surface_ref=sess.surface_ref,
        worktree_path=sess.worktree_path,
    )


def _summarise_event(event: OrchestratorEvent) -> EventSummary:
    return EventSummary(
        id=event.id,
        type=event.type.value,
        payload=event.payload,
        correlation_id=event.correlation_id,
        created_at=event.created_at,
    )


def _count_unresolved(thread_status: dict[str, Any]) -> int:
    """Return the number of unresolved threads in a thread_status dict."""
    count = 0
    for value in thread_status.values():
        resolved = False
        if isinstance(value, dict):
            resolved = bool(value.get("resolved", False))
        if not resolved:
            count += 1
    return count


def _load_monitored_prs() -> list[MonitoredPR]:
    """Read review-monitor state files and summarise active PRs."""
    monitor_dir = review_monitor_dir()
    if not monitor_dir.exists():
        return []
    monitored: list[MonitoredPR] = []
    for path in sorted(monitor_dir.glob("*.json")):
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        active: dict[str, Any] = raw.get("active", {})
        for pr_data in active.values():
            if not isinstance(pr_data, dict):
                continue
            try:
                pr_number = int(pr_data.get("pr_number", 0))
            except (TypeError, ValueError):
                continue
            thread_status: dict[str, Any] = pr_data.get("thread_status", {})
            monitored.append(
                MonitoredPR(
                    repo=str(pr_data.get("repo", "")),
                    pr_number=pr_number,
                    role=str(pr_data.get("role", "author")),
                    status=str(pr_data.get("status", "watching")),
                    unresolved_threads=_count_unresolved(thread_status),
                )
            )
    return monitored


def orchestrator_status() -> OrchestratorStatus:
    """Build a snapshot of pending tickets, running sessions, PRs, events."""
    queue = load_dev_queue()
    pending = [
        _summarise_ticket(t) for t in queue.tasks if t.status == QueueItemStatus.PENDING
    ]

    state = load_state()
    running = [
        _summarise_session(s)
        for s in state.sessions
        if s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
    ]

    monitored = _load_monitored_prs()

    # Read all events, then take the last N (chronological order from the inbox).
    all_events = read_events()
    tail = all_events[-_RECENT_EVENTS_LIMIT:]
    recent = [_summarise_event(e) for e in tail]

    return OrchestratorStatus(
        generated_at=datetime.now(UTC),
        pending_tickets=pending,
        running_sessions=running,
        monitored_prs=monitored,
        recent_events=recent,
    )
