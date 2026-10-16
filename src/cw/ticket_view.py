"""Build and render the per-ticket view behind ``cw dev-queue show`` (#2691).

``build_ticket_view`` turns one dev-queue row into a ``TicketView``: the row's
own fields plus what an operator needs to act on it -- the bound session and
its daemon liveness, the resolved attempt ceiling, the last hydrated PR state,
why a PENDING row is not dispatching, the approvals recorded on the row, and
the legal next commands (``cw.dev_queue.next_commands``).
``render_ticket_view`` prints it as one ``key: value`` per line.

Read-only by construction: no dev-queue or sessions lock, no subprocess, no
event emitted. The reads are the unlocked ``load_dev_queue``/``load_state``,
the roster file, the orchestrator and client config, the concurrency-overrides
file, and one dispatch.tick read of the event inbox (the same read
``cw dev-queue status`` makes, under the inbox's own short read lock).

A top-level module, beside ``queue_rows``/``queue_peek``, because it reads
``cw.reconcile._shared``, ``cw.orchestrate`` and ``cw.dispatch``, all of which
sit above ``cw.dev_queue``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cached_property
from typing import TYPE_CHECKING

from cw.config import (
    _load_concurrency_overrides,
    get_effective_client,
    load_orchestrator_config,
    load_state,
)
from cw.dev_queue import _find_ticket, load_dev_queue, session_attention_state
from cw.dev_queue.next_commands import next_commands
from cw.dispatch import TICK_STALE_SECONDS, resolve_hold_finalize, resolve_signoff
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import DispatchSkipReason, OrchestratorEventType, QueueItemStatus
from cw.models.ticket_view import (
    ApprovalView,
    DispatchSource,
    DispatchView,
    LastResultView,
    MustFixOverrideView,
    PrView,
    SessionView,
    TicketView,
)
from cw.native_daemon import get_native_daemon_client
from cw.orchestrate import client_tick_snapshots
from cw.pr_hydrate import _parse_pr_url
from cw.reconcile._shared import (
    resolve_attempt_ceiling,
    resolve_session_for_task,
    session_daemon_liveness,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        ConcurrencyOverrides,
        DevQueueStore,
        OrchestratorConfig,
        OrchestratorEvent,
        Session,
        TicketTask,
    )
    from cw.orchestrate import TickSummary

# How much of a SHA (draft fingerprint, scope-drift head) the text shows.
_SHA_DISPLAY_LEN = 12
_ABSENT = "-"
_NO_APPROVAL = (
    "- (no row-side approval recorded; tracker-comment approvals are not mirrored)"
)
_SESSION_NOT_FOUND = "(session not found)"
_LANE_STAT_KEYS = ("pending", "running", "blocked", "signoff")
_ROW_SKIP_DETAILS: dict[str, str] = {
    DispatchSkipReason.ATTEMPT_CAP_BLOCKED.value: "unproductive attempts at ceiling",
    DispatchSkipReason.LANE_CIRCUIT_PAUSED.value: "lane paused by its circuit breaker",
}


class ViewSources:
    """The reads every row of one command shares, each made at most once.

    A multi-ticket ``add``/``cancel`` builds one view per ticket; passing one
    ``ViewSources`` to every build keeps the roster, config and event-inbox
    reads to one each. Every read is lazy: a non-PENDING row never reads the
    event inbox, and a row with no bound session never reads the roster.
    """

    def __init__(self) -> None:
        self._clients: dict[str, ClientConfig | None] = {}

    @cached_property
    def roster_ids(self) -> set[str] | None:
        """Live daemon short ids; None when the roster is unreadable."""
        return get_native_daemon_client().list_live_session_short_ids_fail_closed()

    @cached_property
    def orchestrator_config(self) -> OrchestratorConfig:
        return load_orchestrator_config()

    @cached_property
    def concurrency_overrides(self) -> ConcurrencyOverrides:
        return _load_concurrency_overrides()

    @cached_property
    def dispatch_ticks(self) -> list[OrchestratorEvent]:
        return read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])

    @cached_property
    def client_snapshots(self) -> dict[str, TickSummary]:
        return client_tick_snapshots(self.dispatch_ticks)

    def effective_client(self, name: str) -> ClientConfig | None:
        """*name*'s config with runtime lane overrides; None if unconfigured."""
        if name not in self._clients:
            try:
                self._clients[name] = get_effective_client(name)
            except CwError:
                self._clients[name] = None
        return self._clients[name]


def find_ticket_clients(store: DevQueueStore, ticket_id: str) -> list[str]:
    """Every client whose queue holds a row for *ticket_id*, sorted."""
    return sorted({t.client for t in store.tasks if t.ticket_id == ticket_id})


def resolve_view_client(
    store: DevQueueStore, ticket_id: str, client_override: str | None
) -> str:
    """The client to show *ticket_id* for: the override, else the only match.

    Raises:
        CwError: no client queues *ticket_id*, or several do and no override
            picks one.
    """
    if client_override is not None:
        return client_override
    clients = find_ticket_clients(store, ticket_id)
    if not clients:
        msg = f"No dev-queue task found for ticket '{ticket_id}' in any client."
        raise CwError(msg)
    if len(clients) > 1:
        msg = (
            f"Ticket '{ticket_id}' is queued under {len(clients)} clients"
            f" ({', '.join(clients)}); pass -c <client> to pick one."
        )
        raise CwError(msg)
    return clients[0]


def resolve_human_opt_in(
    task: TicketTask, client_cfg: ClientConfig | None, config: OrchestratorConfig
) -> bool:
    """True iff the ticket, its lane or the global default arms a human gate.

    A finalize hold (``hold_finalize`` / lane ``finalize_gate`` /
    ``default_finalize_gate``) or operator signoff (``signoff`` / lane
    ``signoff`` / ``default_signoff``), resolved exactly as dispatch resolves
    them. ``next_commands`` passes it as ``operator_opt_in`` so rules 6/6a do
    not advertise the ``requeue --stage finalize`` jump past REVIEW, where
    those gates are evaluated (#2692 R5). An unconfigured client falls
    through to the global default, as in dispatch.
    """
    clients = {} if client_cfg is None else {task.client: client_cfg}
    return (
        resolve_hold_finalize(task, clients, config) is not None
        or resolve_signoff(task, clients, config) is not None
    )


def build_ticket_view(
    ticket_id: str, client: str, *, now: datetime, sources: ViewSources
) -> TicketView:
    """Build the view of *ticket_id*'s row in *client*'s queue.

    The queue and session state are re-read on every call (a mutating verb's
    echo must see the row it just wrote); everything else comes from
    *sources*. The row is the one ``_find_ticket`` selects, without its
    duplicate-row warning: ``other_rows`` carries that count instead.

    Raises:
        CwError: no row for (*ticket_id*, *client*).
    """
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client, warn=False)
    session = resolve_session_for_task(task, load_state())
    client_cfg = sources.effective_client(client)
    ceiling = resolve_attempt_ceiling(client_cfg, task, sources.orchestrator_config)
    opt_in = resolve_human_opt_in(task, client_cfg, sources.orchestrator_config)
    return TicketView(
        ticket_id=task.ticket_id,
        client=task.client,
        status=task.status,
        stage=task.stage,
        disposition=task.disposition,
        blocked_reason=task.blocked_reason,
        blocker_details=_blocker_details(session),
        advisory_note=task.advisory_note,
        lane=task.lane,
        priority=task.priority,
        scope_hint=task.scope_hint,
        attempts=task.attempts,
        unproductive_attempts=task.unproductive_attempts,
        attempt_ceiling=ceiling,
        session=_session_view(task, session, sources),
        last_result=_last_result_view(task, session),
        pr=_pr_view(task),
        stage_base_ref=task.stage_base_ref,
        dispatch=_dispatch_view(task, ceiling, client_cfg, now=now, sources=sources),
        approval=_approval_view(task),
        other_rows=sum(
            1 for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        )
        - 1,
        created_at=task.created_at,
        completed_at=task.completed_at,
        next=next_commands(task, session=session, operator_opt_in=opt_in),
    )


def _blocker_details(session: Session | None) -> str | None:
    if session is None or session.last_result is None:
        return None
    blocker = session.last_result.get("blocker")
    if not isinstance(blocker, dict):
        return None
    details = blocker.get("details")
    return details if isinstance(details, str) and details else None


def _session_view(
    task: TicketTask, session: Session | None, sources: ViewSources
) -> SessionView | None:
    if session is None:
        if task.session_id is None:
            return None
        return SessionView(
            id=task.session_id,
            status=None,
            liveness_bucket=None,
            in_roster=None,
            attention_state=None,
        )
    roster = sources.roster_ids
    in_roster = (
        None if roster is None else session_daemon_liveness(session, roster).in_roster
    )
    return SessionView(
        id=session.id,
        status=session.status.value,
        liveness_bucket=session.liveness_bucket.value,
        in_roster=in_roster,
        attention_state=session_attention_state(task, session),
    )


def _last_result_view(
    task: TicketTask, session: Session | None
) -> LastResultView | None:
    if session is None or session.last_result is None:
        return None
    status = session.last_result.get("status")
    source = session.last_result_source
    return LastResultView(
        status=status if isinstance(status, str) else None,
        source=source.value if source is not None else None,
        at=session.completed_at or task.completed_at,
    )


def _pr_view(task: TicketTask) -> PrView | None:
    if task.pr_url is None:
        return None
    parsed = _parse_pr_url(task.pr_url)
    number = parsed[1] if parsed is not None else None
    state = task.pr_state
    if state is None:
        return PrView(
            url=task.pr_url,
            number=number,
            state=None,
            mergeable=None,
            merge_state_status=None,
            ci_ok=None,
            review_decision=None,
            attention_state=None,
            is_draft=None,
            hydrated_at=None,
        )
    return PrView(
        url=task.pr_url,
        number=number,
        state=state.state,
        mergeable=state.mergeable,
        merge_state_status=state.merge_state_status,
        ci_ok=state.ci_ok,
        review_decision=state.review_decision,
        attention_state=state.attention_state,
        is_draft=state.is_draft,
        hydrated_at=state.hydrated_at,
    )


def _approval_view(task: TicketTask) -> ApprovalView | None:
    override = task.must_fix_override
    recorded = (
        task.plan_approved_at,
        task.plan_approved_fingerprint,
        task.scope_drift_approved_extra_files,
        task.scope_drift_approved_head,
        override,
    )
    if all(value is None for value in recorded):
        return None
    return ApprovalView(
        plan_approved_at=task.plan_approved_at,
        plan_approved_fingerprint=task.plan_approved_fingerprint,
        scope_drift_approved_extra_files=task.scope_drift_approved_extra_files,
        scope_drift_approved_head=task.scope_drift_approved_head,
        must_fix_override=(
            None
            if override is None
            else MustFixOverrideView(
                actor=override.actor,
                reason=override.reason,
                reviewed_sha=override.reviewed_sha,
                recorded_at=override.recorded_at,
            )
        ),
    )


def _lane_paused(task: TicketTask, client_cfg: ClientConfig | None) -> bool | None:
    if client_cfg is None:
        return None
    lane = next((ln for ln in client_cfg.effective_lanes if ln.name == task.lane), None)
    return None if lane is None else lane.paused


def _breaker_tripped(task: TicketTask, sources: ViewSources) -> bool:
    """The lane's spawn-error counter is at the circuit-breaker threshold.

    Mirrors ``cw.dispatch.lanes._check_lane_circuit_paused``. Its third
    condition, pending work in the lane, always holds: this row is PENDING in
    that lane.
    """
    override = sources.concurrency_overrides.lanes.get(f"{task.client}/{task.lane}")
    threshold = sources.orchestrator_config.lane_circuit_breaker_threshold
    return override is not None and override.consecutive_spawn_errors >= threshold


def _row_skip_reason(
    task: TicketTask,
    ceiling: int | None,
    lane_paused: bool | None,
    *,
    now: datetime,
    sources: ViewSources,
) -> str | None:
    """The skip reason derivable live from the row and lane, or None."""
    if task.next_eligible_at is not None and task.next_eligible_at > now:
        return DispatchSkipReason.SPAWN_ERROR_BACKOFF.value
    if ceiling is not None and task.unproductive_attempts >= ceiling:
        return DispatchSkipReason.ATTEMPT_CAP_BLOCKED.value
    if lane_paused and _breaker_tripped(task, sources):
        return DispatchSkipReason.LANE_CIRCUIT_PAUSED.value
    return None


def _latest_ticket_event(
    events: list[OrchestratorEvent], task: TicketTask
) -> OrchestratorEvent | None:
    """The newest per-ticket dispatch.tick for this (client, ticket)."""
    matches = [
        e
        for e in events
        if e.payload.get("client") == task.client
        and e.payload.get("ticket_id") == task.ticket_id
    ]
    return max(matches, key=lambda e: e.created_at, default=None)


def _is_stale(at: datetime, now: datetime) -> bool:
    return (now - at).total_seconds() > TICK_STALE_SECONDS


def _dispatch_view(
    task: TicketTask,
    ceiling: int | None,
    client_cfg: ClientConfig | None,
    *,
    now: datetime,
    sources: ViewSources,
) -> DispatchView | None:
    """Why a PENDING row is not dispatching, first source that knows.

    Precedence: row-derived facts, then the latest per-ticket dispatch.tick
    event, then the client's latest client-wide tick snapshot. Event and
    snapshot answers carry their age and a stale flag; neither is live.
    """
    if task.status != QueueItemStatus.PENDING:
        return None
    lane_paused = _lane_paused(task, client_cfg)
    view = DispatchView(
        skip_reason=None,
        source=DispatchSource.NONE,
        as_of=None,
        stale=False,
        backoff_until=task.next_eligible_at,
        lane_paused=lane_paused,
        lane_stats=None,
        lane_occupants=None,
    )
    row_reason = _row_skip_reason(task, ceiling, lane_paused, now=now, sources=sources)
    if row_reason is not None:
        return view.model_copy(
            update={
                "skip_reason": row_reason,
                "source": DispatchSource.ROW,
                "as_of": now,
            }
        )
    event = _latest_ticket_event(sources.dispatch_ticks, task)
    if event is not None:
        return view.model_copy(
            update={
                "skip_reason": str(event.payload.get("skip_reason", "none")),
                "source": DispatchSource.EVENT,
                "as_of": event.created_at,
                "stale": _is_stale(event.created_at, now),
            }
        )
    snapshot = sources.client_snapshots.get(task.client)
    if snapshot is None:
        return view
    return view.model_copy(
        update={
            "skip_reason": snapshot.skip_reason,
            "source": DispatchSource.LAST_TICK,
            "as_of": snapshot.tick_at,
            "stale": _is_stale(snapshot.tick_at, now),
            "lane_stats": snapshot.lanes.get(task.lane),
            "lane_occupants": snapshot.lane_occupants.get(task.lane),
        }
    )


# --- text rendering ----------------------------------------------------------


def _ts(value: datetime | None) -> str:
    """ISO-8601 UTC, second precision, ``Z`` suffix; ``-`` when absent."""
    if value is None:
        return _ABSENT
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _or_absent(value: str | None) -> str:
    return value or _ABSENT


def _kv(key: str, value: str) -> str:
    """``key: value``; continuation lines of a multi-line value indent 2."""
    first, *rest = value.split("\n")
    return "\n".join([f"{key}: {first}", *(f"  {line}" for line in rest)])


def _reason_text(view: TicketView) -> str:
    if view.blocked_reason:
        return view.blocked_reason
    if view.advisory_note:
        return f"{view.advisory_note} (advisory)"
    return _ABSENT


def _details_text(view: TicketView) -> str:
    if view.blocker_details:
        return view.blocker_details
    if view.session is not None and view.session.status is None:
        return f"{_ABSENT} {_SESSION_NOT_FOUND}"
    return _ABSENT


def _session_text(session: SessionView | None) -> str:
    if session is None:
        return _ABSENT
    if session.status is None:
        return f"{session.id} {_SESSION_NOT_FOUND}"
    if session.in_roster is None:
        roster = "in_roster=unknown; roster: unreadable"
    else:
        roster = f"in_roster={'yes' if session.in_roster else 'no'}"
    return (
        f"{session.id} ({session.status}, liveness={session.liveness_bucket}, {roster})"
    )


def _last_result_text(result: LastResultView | None) -> str:
    if result is None:
        return _ABSENT
    return (
        f"{_or_absent(result.status)} (source={_or_absent(result.source)})"
        f" at {_ts(result.at)}"
    )


def _pr_text(pr: PrView | None) -> str:
    if pr is None:
        return _ABSENT
    number = f"#{pr.number}" if pr.number is not None else _ABSENT
    if pr.hydrated_at is None:
        return f"{number} {pr.url} hydrated: no"
    return (
        f"{number} {pr.url} state={_or_absent(pr.state)}"
        f" mergeable={_or_absent(pr.mergeable)} hydrated={_ts(pr.hydrated_at)}"
    )


def _lane_snapshot_text(lane: str, dispatch: DispatchView) -> str:
    parts = ["client-wide"]
    if dispatch.lane_stats is not None:
        stats = " ".join(
            f"{key}={dispatch.lane_stats.get(key, 0)}" for key in _LANE_STAT_KEYS
        )
        parts.append(f"lane {lane}: {stats}")
    if dispatch.lane_occupants:
        occupants = ", ".join(
            f"{o.get('ticket_id', _ABSENT)} ({o.get('status', _ABSENT)})"
            for o in dispatch.lane_occupants
        )
        parts.append(f"occupants: {occupants}")
    return "; ".join(parts)


def _skip_detail(lane: str, dispatch: DispatchView, now: datetime) -> str:
    if dispatch.source == DispatchSource.ROW:
        if dispatch.skip_reason == DispatchSkipReason.SPAWN_ERROR_BACKOFF.value:
            until = _ts(dispatch.backoff_until)
            return f"source={DispatchSource.ROW.value}, until {until}"
        detail = _ROW_SKIP_DETAILS.get(dispatch.skip_reason or "", "")
        return (
            f"source={DispatchSource.ROW.value}, {detail}"
            if detail
            else f"source={DispatchSource.ROW.value}"
        )
    age = int((now - dispatch.as_of).total_seconds()) if dispatch.as_of else 0
    stale = ", stale" if dispatch.stale else ""
    head = f"source={dispatch.source.value}, {age}s ago{stale}"
    if dispatch.source == DispatchSource.LAST_TICK:
        return f"{head}, {_lane_snapshot_text(lane, dispatch)}"
    return head


def _skip_reason_text(view: TicketView, now: datetime) -> str:
    dispatch = view.dispatch
    if dispatch is None:
        return _ABSENT
    paused = (
        " [lane paused]"
        if dispatch.lane_paused
        and dispatch.skip_reason != DispatchSkipReason.LANE_CIRCUIT_PAUSED.value
        else ""
    )
    if dispatch.skip_reason is None:
        return f"{_ABSENT}{paused}"
    detail = _skip_detail(view.lane, dispatch, now)
    return f"{dispatch.skip_reason} ({detail}){paused}"


def _approval_text(approval: ApprovalView | None) -> str:
    if approval is None:
        return _NO_APPROVAL
    parts: list[str] = []
    fingerprint = approval.plan_approved_fingerprint
    if approval.plan_approved_at is not None or fingerprint is not None:
        plan = f"plan approved {_ts(approval.plan_approved_at)}; approver: not recorded"
        if fingerprint is not None:
            plan += f"; draft={fingerprint[:_SHA_DISPLAY_LEN]}"
        parts.append(plan)
    override = approval.must_fix_override
    if override is not None:
        parts.append(
            f"must-fix override by {override.actor} at {_ts(override.recorded_at)}:"
            f" {override.reason}"
        )
    files = approval.scope_drift_approved_extra_files
    head = approval.scope_drift_approved_head
    if files is not None or head is not None:
        shown_head = head[:_SHA_DISPLAY_LEN] if head is not None else _ABSENT
        parts.append(
            f"scope-drift grant: files={','.join(files or [])} head={shown_head}"
        )
    return "; ".join(parts)


def render_ticket_view(view: TicketView, *, now: datetime | None = None) -> str:
    """The view as text: one ``key: value`` per line, fixed key order.

    Every key is always printed (``-`` when absent); nothing is truncated;
    timestamps are ISO-8601 UTC. *now* (default: the current time) dates the
    ``Ns ago`` ages on ``skip_reason``.
    """
    at = now if now is not None else datetime.now(UTC)
    session = view.session
    pairs = [
        ("ticket", view.ticket_id),
        ("client", view.client),
        ("status", view.status.value),
        ("stage", view.stage.value),
        ("disposition", _or_absent(view.disposition)),
        ("reason", _reason_text(view)),
        ("details", _details_text(view)),
        ("lane", view.lane),
        ("priority", str(view.priority)),
        ("attempts", str(view.attempts)),
        ("unproductive_attempts", str(view.unproductive_attempts)),
        (
            "attempt_ceiling",
            "none" if view.attempt_ceiling is None else str(view.attempt_ceiling),
        ),
        ("session", _session_text(session)),
        ("attention", _or_absent(session.attention_state if session else None)),
        ("last_result", _last_result_text(view.last_result)),
        ("pr", _pr_text(view.pr)),
        ("stage_base_ref", _or_absent(view.stage_base_ref)),
        ("skip_reason", _skip_reason_text(view, at)),
        ("approval", _approval_text(view.approval)),
        ("other_rows", str(view.other_rows)),
    ]
    lines = [_kv(key, value) for key, value in pairs]
    lines.append("next:" if view.next else f"next: {_ABSENT}")
    lines.extend(f"  {cmd.command}   # {cmd.purpose}" for cmd in view.next)
    return "\n".join(lines)
