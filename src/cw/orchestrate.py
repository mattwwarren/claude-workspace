"""PR retirement and orchestrator status snapshot.

This module closes the loop on the orchestrator pipeline:

* :func:`retire_merged_prs` consumes ``pr.merged`` events, removes the PR
  from review-monitor's persisted state, marks correlated sessions as
  ``COMPLETED`` (reason ``HANDOFF``), and emits ``session.completed`` events.

* :func:`orchestrator_status` returns a snapshot of the orchestrator's
  current state -- pending dev-queue tickets, running sessions, monitored
  PRs, and recent events -- shared between the TUI dashboard and the
  ``cw orchestrate status [--json]`` CLI.

* :func:`orchestrator_workers` returns a list of worker sessions for an
  orchestrator session, with missing-session sentinels for drift cases.

* :func:`orchestrator_parent` resolves a worker session to its parent
  orchestrator session.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from cw.atomic import atomic_write_text
from cw.config import (
    load_state,
    review_monitor_dir,
    save_state,
    sessions_lock,
    state_dir,
)
from cw.dev_queue import load_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    CompletionReason,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionStatus,
    TicketTask,
)
from cw.reconcile._shared import _transcript_age_seconds

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cw.models import CwState

logger = logging.getLogger(__name__)

_RETIREMENT_CONSUMER = "orchestrate_retire"
_RECENT_EVENTS_LIMIT = 20
# Attention digest only replays SESSION_NEEDS_ATTENTION events from the last
# _ATTENTION_WINDOW *and* only for sessions still present in state. Without
# both bounds the panel replays the entire event history (no resolution
# semantics exist), so resolved/ancient flags accumulate forever. See #854.
_ATTENTION_WINDOW = timedelta(hours=24)
_REVIEW_MONITOR_SCRIPT = Path.home() / ".claude" / "scripts" / "review_monitor.py"
_SECONDS_PER_MINUTE = 60


class _FeedEvent(Protocol):
    """Structural-typing seam for :func:`_aggregate_feed`.

    Lets the aggregator operate on both :class:`~cw.models.OrchestratorEvent`
    (board's windowed feed) and :class:`EventSummary`
    (``OrchestratorStatus.recent_events``) without a bulk conversion between
    the two on the render hot path. Read-only ``@property`` members (not
    plain attributes) are required so the covariant ``str`` return accepts
    ``OrchestratorEventType`` (a ``StrEnum`` subtype of ``str``) -- a plain
    mutable protocol attribute would be invariant and fail ``mypy --strict``.
    """

    @property
    def id(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...

    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True)
class _FeedEntry:
    """One display row in the (possibly aggregated) event-feed panel.

    ``id`` is preserved for passthrough (non-tick) entries so raw-mode
    callers can still surface it; synthetic tick-summary entries have no
    single originating event id, so ``id`` is ``None``.
    """

    text: str
    created_at: datetime
    id: str | None = None


def _aggregate_feed(events: Sequence[_FeedEvent]) -> list[_FeedEntry]:
    """Collapse consecutive dispatch.tick events into one summary entry.

    Any other event type breaks the run and is emitted verbatim. Pure --
    operates over whatever event sequence it is given (no truncation here;
    callers own the aggregate-then-tail truncation order).
    """
    entries: list[_FeedEntry] = []
    run: list[_FeedEvent] = []

    def _flush_tick_run() -> None:
        if not run:
            return
        span_seconds = (run[-1].created_at - run[0].created_at).total_seconds()
        span_minutes = int(span_seconds // _SECONDS_PER_MINUTE)
        entries.append(
            _FeedEntry(
                text=f"dispatch.tick x{len(run)} over {span_minutes}m",
                created_at=run[-1].created_at,
            )
        )
        run.clear()

    for event in events:
        if event.type == OrchestratorEventType.DISPATCH_TICK.value:
            run.append(event)
            continue
        _flush_tick_run()
        ticket_id = event.payload.get("ticket_id")
        label = f"{event.type} ({ticket_id})" if ticket_id else f"{event.type}"
        entries.append(_FeedEntry(text=label, created_at=event.created_at, id=event.id))
    _flush_tick_run()
    return entries


# ---------------------------------------------------------------------------
# PR dispatch record (migrated from pr_responder)
# ---------------------------------------------------------------------------

_DISPATCH_FILE_NAME = "pr_dispatch.json"


class PRDispatchRecord(BaseModel):
    """Tracks in-flight PR response sessions."""

    # key: "repo#pr_number|role"  (role = "fix-ci" or "address-review")
    active: dict[str, str] = Field(default_factory=dict)


def load_dispatch_record() -> PRDispatchRecord:
    """Load PRDispatchRecord from the state directory, or return an empty one."""
    path = state_dir() / _DISPATCH_FILE_NAME
    if not path.exists():
        return PRDispatchRecord()
    return PRDispatchRecord.model_validate_json(path.read_text())


def save_dispatch_record(record: PRDispatchRecord) -> None:
    """Persist PRDispatchRecord to the state directory atomically."""
    state_dir().mkdir(parents=True, exist_ok=True)
    path = state_dir() / _DISPATCH_FILE_NAME
    atomic_write_text(path, record.model_dump_json(indent=2))


def clear_completed_pr_sessions(state: CwState) -> None:
    """Remove PRDispatchRecord entries whose sessions are completed.

    Args:
        state: Current CwState used to determine completed session IDs.
    """
    dispatch_record = load_dispatch_record()
    completed_ids = {
        s.id for s in state.sessions if s.status == SessionStatus.COMPLETED
    }
    dispatch_record.active = {
        key: sid
        for key, sid in dispatch_record.active.items()
        if sid not in completed_ids
    }
    save_dispatch_record(dispatch_record)


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
    *,
    pr_number: int,
    repo: str,
) -> None:
    """Close a session: mark COMPLETED and emit event.

    Multiplexer surface close is intentionally omitted: the native daemon
    manages session lifetime.  Legacy ``surface_ref`` values on existing
    records are logged and skipped rather than passed to a now-removed adapter.
    """
    if sess.surface_ref is not None:
        logger.warning(
            "Session %s has legacy surface_ref %r; skipping surface close",
            sess.id,
            sess.surface_ref,
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
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Process ``pr.merged`` events and retire correlated sessions.

    For each unprocessed ``pr.merged`` event:

    1. Invoke ``review_monitor.py complete`` to remove the PR from
       monitoring state.
    2. Look up sessions correlated to the PR via :class:`PRDispatchRecord`.
    3. Mark each session ``COMPLETED`` with reason ``HANDOFF`` and emit a
       ``session.completed`` event.
    4. Drop the dispatch record entries so subsequent ticks see no
       lingering correlation.

    Idempotency comes from the consumer cursor: a second invocation with
    no new ``pr.merged`` events is a no-op.

    Args:
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

    # Why not mutate_state: _invoke_review_monitor_complete (subprocess) runs
    # inside the lock window (criterion 1: no subprocess in lock).
    with sessions_lock():
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
    # GH check rollup: pending, success, failure, error, action_required, stale
    ci_status: str | None = None
    mergeable: bool | None = None


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
    last_stage: str | None = None
    transcript_age_seconds: float | None = None
    paused_status: str | None = None


class TicketSummary(BaseModel):
    """Lightweight view of a dev-queue ticket."""

    ticket_id: str
    client: str
    priority: int
    status: str
    created_at: datetime
    scope_hint: str | None = None
    lane: str = DEFAULT_LANE


class EventSummary(BaseModel):
    """Lightweight view of a single orchestrator event."""

    id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime


class TickSummary(BaseModel):
    """Most recent dispatch.tick summary for one client."""

    claimed: int
    pending: int
    running: int
    cap: int
    skip_reason: str
    tick_at: datetime
    lanes: dict[str, dict[str, int]] = Field(default_factory=dict)
    lane_occupants: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    occupied: int = 0
    freshness_detail: str | None = None
    blocked_branch: str | None = None


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
    attention_events: list[EventSummary] = Field(default_factory=list)
    total_cost_by_client: dict[str, float] = Field(default_factory=dict)
    last_tick_by_client: dict[str, TickSummary] = Field(default_factory=dict)


def summarise_ticket(task: TicketTask) -> TicketSummary:
    return TicketSummary(
        ticket_id=task.ticket_id,
        client=task.client,
        priority=task.priority,
        status=task.status.value,
        created_at=task.created_at,
        scope_hint=task.scope_hint,
        lane=task.lane,
    )


def summarise_session(
    sess: Session,
    *,
    last_stage: str | None = None,
    now: datetime | None = None,
) -> SessionSummary:
    _now = now if now is not None else datetime.now(UTC)
    paused_status = (
        sess.last_result.get("paused_status")
        if isinstance(sess.last_result, dict)
        else None
    )
    return SessionSummary(
        id=sess.id,
        name=sess.name,
        client=sess.client,
        status=sess.status.value,
        purpose=sess.purpose.value,
        started_at=sess.started_at,
        surface_ref=sess.surface_ref,
        worktree_path=sess.worktree_path,
        last_stage=last_stage,
        transcript_age_seconds=_transcript_age_seconds(sess, _now),
        paused_status=paused_status if isinstance(paused_status, str) else None,
    )


def _derive_last_stage_by_session(
    events: list[OrchestratorEvent],
) -> dict[str, str]:
    """Map session_id -> most recent STAGE_ENTERED.stage.

    Iterates events in chronological order; later events overwrite
    earlier ones. STAGE_ERRORED events are deliberately ignored — they
    remain visible in recent_events but do not redefine the "current
    stage" of a session.
    """
    result: dict[str, str] = {}
    for ev in events:
        if ev.type is not OrchestratorEventType.STAGE_ENTERED:
            continue
        session_id = ev.payload.get("session_id")
        stage = ev.payload.get("stage")
        if isinstance(session_id, str) and isinstance(stage, str):
            result[session_id] = stage
    return result


def _summarise_event(event: OrchestratorEvent) -> EventSummary:
    return EventSummary(
        id=event.id,
        type=event.type.value,
        payload=event.payload,
        correlation_id=event.correlation_id,
        created_at=event.created_at,
    )


def _extract_lanes(raw: object) -> dict[str, dict[str, int]]:
    """Safely extract lanes dict from DISPATCH_TICK payload.

    Tolerates pre-#558 events where the key is absent or malformed.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for lane_name, stats in raw.items():
        if not isinstance(lane_name, str) or not isinstance(stats, dict):
            continue
        result[lane_name] = {
            k: int(v)
            for k, v in stats.items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
    return result


def _extract_lane_occupants(raw: object) -> dict[str, list[dict[str, str]]]:
    """Safely extract lane_occupants dict from a DISPATCH_TICK payload.

    Sibling of :func:`_extract_lanes` -- deliberately NOT reused, since the
    value shape differs (list[{"ticket_id","status"}] vs dict[str,int]).
    Tolerates events emitted before this field existed (#1243) or
    malformed entries.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for lane_name, occupant_list in raw.items():
        if not isinstance(lane_name, str) or not isinstance(occupant_list, list):
            continue
        occupants: list[dict[str, str]] = []
        for item in occupant_list:
            if not isinstance(item, dict):
                continue
            ticket_id = item.get("ticket_id")
            status = item.get("status")
            if isinstance(ticket_id, str) and isinstance(status, str):
                occupants.append({"ticket_id": ticket_id, "status": status})
        result[lane_name] = occupants
    return result


def _latest_tick_by_client(
    events: list[OrchestratorEvent],
) -> dict[str, TickSummary]:
    """Derive the most recent dispatch.tick per client from an event list.

    Each client's "most recent" tick is resolved independently (#1346): a
    client's dispatch.tick cadence is its own, so two clients can legitimately
    show a ``skip_reason`` sourced from two different ticks (even different
    ticks of the SAME dispatch loop run) at the same point in wall-clock time.
    That skew is expected per-client cadence, not a bug.
    """
    result: dict[str, TickSummary] = {}
    for ev in events:
        if ev.type is not OrchestratorEventType.DISPATCH_TICK:
            continue
        client = ev.payload.get("client")
        if not isinstance(client, str):
            continue
        existing = result.get(client)
        if existing is None or ev.created_at > existing.tick_at:
            try:
                fd = ev.payload.get("freshness_detail")
                bb = ev.payload.get("blocked_branch")
                result[client] = TickSummary(
                    claimed=int(ev.payload.get("claimed", 0)),
                    pending=int(ev.payload.get("pending", 0)),
                    running=int(ev.payload.get("running", 0)),
                    cap=int(ev.payload.get("cap", 0)),
                    skip_reason=str(ev.payload.get("skip_reason", "none")),
                    tick_at=ev.created_at,
                    lanes=_extract_lanes(ev.payload.get("lanes")),
                    lane_occupants=_extract_lane_occupants(
                        ev.payload.get("lane_occupants")
                    ),
                    occupied=int(ev.payload.get("occupied", 0)),
                    freshness_detail=fd if isinstance(fd, str) else None,
                    blocked_branch=bb if isinstance(bb, str) else None,
                )
            except (TypeError, ValueError):
                continue
    return result


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
        active: dict[str, Any] = raw.get("monitored", {})
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
                    ci_status=pr_data.get("ci_status"),
                    mergeable=pr_data.get("mergeable"),
                )
            )
    return monitored


def latest_tick_summary_by_client() -> dict[str, TickSummary]:
    """Return the most recent dispatch.tick summary per client.

    Uses only DISPATCH_TICK events — no full orchestrator_status() overhead.

    Per-client independence is intentional (#1346, see
    :func:`_latest_tick_by_client`): an operator viewing ``cw dev-queue
    status --json`` may legitimately see different ``skip_reason`` values
    across clients even though they were captured at the same instant — each
    client's tick cadence is independent, this is not a synchronization bug.
    """
    events = read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])
    return _latest_tick_by_client(events)


def orchestrator_status() -> OrchestratorStatus:
    """Build a snapshot of pending tickets, running sessions, PRs, events."""
    queue = load_dev_queue()
    pending = [
        summarise_ticket(t) for t in queue.tasks if t.status == QueueItemStatus.PENDING
    ]

    # Read all events first so we can derive last_stage per session before
    # summarising the running list.
    all_events = read_events()
    last_stage_by_session = _derive_last_stage_by_session(all_events)
    last_tick = _latest_tick_by_client(all_events)

    now = datetime.now(UTC)
    state = load_state()
    running = [
        summarise_session(s, last_stage=last_stage_by_session.get(s.id), now=now)
        for s in state.sessions
        if s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
    ]

    monitored = _load_monitored_prs()

    tail = all_events[-_RECENT_EVENTS_LIMIT:]
    recent = [_summarise_event(e) for e in tail]

    # Bound the attention digest: recent window + sessions still in state.
    # There is no "attention resolved" event, so an unbounded replay surfaces
    # every flag ever emitted (resolved or for long-reaped sessions). See #854.
    attention_since = now - _ATTENTION_WINDOW
    live_session_ids = {s.id for s in state.sessions}
    attention_raw = [
        e
        for e in all_events
        if e.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        and e.created_at >= attention_since
        and e.payload.get("session_id") in live_session_ids
    ]
    attention = [_summarise_event(e) for e in attention_raw]

    total_cost: dict[str, float] = {}
    for task in queue.tasks:
        if task.status == QueueItemStatus.COMPLETED and task.total_cost_usd is not None:
            prior = total_cost.get(task.client, 0.0)
            total_cost[task.client] = prior + task.total_cost_usd

    return OrchestratorStatus(
        generated_at=datetime.now(UTC),
        pending_tickets=pending,
        running_sessions=running,
        monitored_prs=monitored,
        recent_events=recent,
        attention_events=attention,
        total_cost_by_client=total_cost,
        last_tick_by_client=last_tick,
    )


# ---------------------------------------------------------------------------
# Worker / parent inspection
# ---------------------------------------------------------------------------


def _last_activity(sess: Session) -> datetime:
    """Return the most recent non-None timestamp from a session's lifecycle fields."""
    optional = (sess.idle_at, sess.backgrounded_at, sess.resumed_at, sess.completed_at)
    extra = [ts for ts in optional if ts is not None]
    return max([sess.started_at, *extra])


class WorkerEntry(BaseModel):
    """Lightweight view of one worker session for the workers subcommand."""

    id: str
    status: str
    branch: str | None = None
    last_activity: datetime


class MissingWorkerEntry(BaseModel):
    """Sentinel for a worker whose session record has been removed from state.

    Returned in the second element of ``orchestrator_workers`` so callers can
    discriminate by type rather than inspecting a shared field. ``missing``
    is fixed True to give JSON consumers an explicit drift marker.
    """

    id: str
    missing: bool = True


class ParentEntry(BaseModel):
    """Lightweight view of the parent session for the parent subcommand."""

    id: str
    status: str
    surface_ref: str | None = None


def orchestrator_workers(
    orchestrator_id: str,
) -> tuple[list[WorkerEntry], list[MissingWorkerEntry]]:
    """Return (present_workers, missing_workers) for an orchestrator session.

    Looks up the orchestrator by ID or name, iterates its
    ``worker_session_ids``, and classifies each as present (in state) or
    missing (dropped from state — drift case for ``cw doctor``).

    Args:
        orchestrator_id: Session ID or name of the orchestrator.

    Returns:
        A tuple of (present_workers, missing_workers).

    Raises:
        CwError: If ``orchestrator_id`` does not match any session.
    """
    state = load_state()
    orch = state.find_by_name_or_id(orchestrator_id)
    if orch is None:
        msg = f"No session found matching {orchestrator_id!r}"
        raise CwError(msg)

    present: list[WorkerEntry] = []
    missing: list[MissingWorkerEntry] = []

    for worker_id in orch.worker_session_ids:
        worker = state.find_by_name_or_id(worker_id)
        if worker is None:
            missing.append(MissingWorkerEntry(id=worker_id))
        else:
            present.append(
                WorkerEntry(
                    id=worker.id,
                    status=worker.status.value,
                    branch=worker.branch,
                    last_activity=_last_activity(worker),
                )
            )

    return present, missing


def orchestrator_parent(worker_id: str) -> ParentEntry | None:
    """Return the parent session of a worker, or None if there is no parent.

    Args:
        worker_id: Session ID or name of the worker session.

    Returns:
        A :class:`ParentEntry` if a parent exists and is found in state,
        or ``None`` if the worker has no ``parent_session_id``.

    Raises:
        CwError: If ``worker_id`` does not match any session, or if the
            parent session ID is set but its record is missing from state.
    """
    state = load_state()
    worker = state.find_by_name_or_id(worker_id)
    if worker is None:
        msg = f"No session found matching {worker_id!r}"
        raise CwError(msg)

    if worker.parent_session_id is None:
        return None

    parent = state.find_by_name_or_id(worker.parent_session_id)
    if parent is None:
        msg = (
            f"Parent session {worker.parent_session_id!r} not found in state"
            " (drift — run 'cw doctor' to inspect)"
        )
        raise CwError(msg)

    return ParentEntry(
        id=parent.id,
        status=parent.status.value,
        surface_ref=parent.surface_ref,
    )
