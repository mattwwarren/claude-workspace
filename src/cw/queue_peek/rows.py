"""Report rows for ``cw queue peek``.

Enumerates the RUNNING and BLOCKED_ON_USER tasks and builds one report dict
per task from its transcript (via ``locate`` and ``parse``), its PR state
(via ``gh``) and the ``ladder`` recommendation.

The names below are imported into this module's namespace on purpose:
``build_peek_rows`` / ``format_row`` / ``gh_pr_state`` look them up here, so
tests patch them as ``cw.queue_peek.rows.<name>``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from cw.dev_queue import list_tickets
from cw.gh import _fetch_pr_state
from cw.models import QueueItemStatus, TicketTask
from cw.opencode_runner import OPENCODE_LOG_RELATIVE_PATH
from cw.queue_peek.ladder import IDLE_PEEK_MIN, recommend
from cw.queue_peek.locate import (
    _compute_jsonl_idle_min,
    _is_known_idless_session,
    _load_session_refs,
    find_transcript_for_ticket,
)
from cw.queue_peek.parse import (
    _newest_subagent_ts,
    parse_opencode_transcript,
    parse_transcript,
)
from cw.queue_peek_convergence import convergence_peek_override

RECOMMEND_BLIND = "PEEK-BLIND"
# A row parked awaiting an operator answer (#2212). Like RECOMMEND_BLIND this
# is produced outside the WAIT/PEEK/STOP ladder -- a dedicated branch that
# returns before recommend() is called at all, with no numeric thresholds.
RECOMMEND_AWAITING_OPERATOR = "AWAITING_OPERATOR"
_AWAITING_OPERATOR_FALLBACK_REASON = "parked, no answer yet"
# Blind reasons for a matched Session with no Claude ids (#2417); only the
# handle-less one may claim "never started".
_BLIND_REASON_NEVER_STARTED = "blind — never started"
_BLIND_REASON_LOCAL_HANDLE = (
    "blind — no Claude transcript (local process handle present)"
)
_SIGNAL_SOURCE_BLIND = "blind"
_SIGNAL_SOURCE_TRANSCRIPT = "transcript"
_SIGNAL_SOURCE_PARKED = "parked"


# The statuses `cw queue peek` reports on. BLOCKED_ON_USER joined RUNNING in
# #2212: the park path already recorded "awaiting operator," but peek only
# ever queried RUNNING, so an operator had to know to check `cw dev-queue
# tasks` instead. AWAITING_OPERATOR_SIGNOFF is deliberately NOT here -- that
# is RFC 0007's distinct ship-signoff park (#990), not an unanswered question.
_PEEK_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER}
)


def load_attention_tasks(client: str | None) -> list[TicketTask]:
    """Return RUNNING and BLOCKED_ON_USER tasks, optionally filtered by client."""
    return [t for t in list_tickets(client) if t.status in _PEEK_STATUSES]


def gh_pr_state(pr_number: int) -> str:
    """Return OPEN | MERGED | CLOSED | UNKNOWN for the given PR number."""
    try:
        return _fetch_pr_state(pr_number, timeout=10) or "UNKNOWN"
    except FileNotFoundError:
        return "UNKNOWN"


def minutes_since(iso_ts: str | None, now: dt.datetime) -> float | None:
    """Return minutes elapsed since *iso_ts*, or None if unparseable.

    A naive (tz-less) *iso_ts* is coerced to UTC, mirroring
    ``_parse_started_at``'s coercion — CW_STATE's ``started_at`` can be a
    naive ISO string in practice.
    """
    if not iso_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return (now - ts).total_seconds() / 60.0


def _awaiting_operator_reason(t: TicketTask) -> str:
    """Return the reason a parked row is waiting, in source-priority order.

    ``disposition`` first because the primary park path -- the worker's own
    ``cw signal-park`` marker routed through ``_route_stopped_without_sentinel``
    -- stamps only that field: ``transition_task_status`` is called there
    without a ``blocked_reason=`` kwarg and unconditionally clears
    ``advisory_note`` on every call. ``blocked_reason`` and ``advisory_note``
    are stamped by the dispatch claim/routing call sites instead, so they stay
    in the chain but cannot lead it.
    """
    return (
        t.disposition
        or t.blocked_reason
        or t.advisory_note
        or _AWAITING_OPERATOR_FALLBACK_REASON
    )


def _awaiting_operator_row(t: TicketTask) -> dict[str, Any]:
    """Build the report dict for a BLOCKED_ON_USER row (#2212).

    Returns before the age/idle ladder runs: a parked row is *supposed* to be
    idle, so scoring it would report false precision and could recommend a
    STOP for a session that is behaving correctly.
    """
    return {
        "ticket": t.ticket_id,
        "session": (t.session_id or "-")[:12],
        "client": t.client,
        "attempts": t.attempts,
        "unproductive_attempts": t.unproductive_attempts,
        "age_min": None,
        "idle_min": None,
        "stage": None,
        "status": None,
        "pr": None,
        "pr_state": None,
        "recommend": RECOMMEND_AWAITING_OPERATOR,
        "reason": _awaiting_operator_reason(t),
        "signal_source": _SIGNAL_SOURCE_PARKED,
        "jsonl_idle_min": None,
        "stage_high_water": t.stage_high_water,
        "pipeline_stage": t.stage,
    }


def format_row(t: TicketTask, info: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Build a report dict for one RUNNING or BLOCKED_ON_USER task."""
    if t.status is QueueItemStatus.BLOCKED_ON_USER:
        return _awaiting_operator_row(t)

    signal_source: str = info.get("signal_source", _SIGNAL_SOURCE_TRANSCRIPT)
    jsonl_idle_min: float | None = info.get("jsonl_idle_min")

    if signal_source == _SIGNAL_SOURCE_BLIND:
        blind_reason: str | None = info.get("blind_reason")
        if blind_reason is not None:
            reason = blind_reason
        elif jsonl_idle_min is not None:
            reason = f"no resolvable transcript; newest jsonl {jsonl_idle_min:.0f}m ago"
        else:
            reason = "no resolvable transcript; none found"
        return {
            "ticket": t.ticket_id,
            "session": (t.session_id or "-")[:12],
            "client": t.client,
            "attempts": t.attempts,
            "unproductive_attempts": t.unproductive_attempts,
            "age_min": None,
            "idle_min": None,
            "stage": None,
            "status": None,
            "pr": None,
            "pr_state": None,
            "recommend": RECOMMEND_BLIND,
            "reason": reason,
            "signal_source": signal_source,
            "jsonl_idle_min": jsonl_idle_min,
            "stage_high_water": t.stage_high_water,
            "pipeline_stage": t.stage,
        }

    claim_age = minutes_since(info.get("claim_started_at"), now)
    age = (
        claim_age
        if claim_age is not None
        else minutes_since(info.get("first_user_ts"), now)
    )
    idle = minutes_since(info.get("last_asst_ts"), now)
    if claim_age is not None and idle is not None and age is not None and idle > age:
        # The transcript's last-assistant timestamp predates the session's own
        # claim time — a logical contradiction proving it belongs to a stale,
        # reused-worktree transcript rather than this session.
        idle = None
    child_idle = minutes_since(info.get("subagent_last_asst_ts"), now)
    child_active = child_idle is not None and (idle is None or child_idle < idle)
    if child_active:
        idle = child_idle
    pr_state = None
    if info.get("last_pr_number"):
        pr_state = gh_pr_state(info["last_pr_number"])
    rec, reason = recommend(
        age_min=age,
        idle_min=idle,
        pr_state=pr_state,
        sentinel_status=info.get("last_sentinel_status"),
        unproductive_attempts=t.unproductive_attempts,
        stage_high_water=t.stage_high_water,
        usage_limit_detected=info.get("usage_limit_detected", False),
        child_active=child_active,
    )
    # Clean-mode convergence exemption (#2787): sees the bare ladder reason,
    # before the child suffix below is appended to whatever results.
    override = convergence_peek_override(
        t, rec, reason, idle_min=idle, idle_live_max_min=IDLE_PEEK_MIN
    )
    if override is not None:
        rec, reason = override
    if child_active:
        reason = (
            f"{reason}; child subagent active {child_idle:.1f}m ago — "
            "waiting on it, not idle"
        )
    return {
        "ticket": t.ticket_id,
        "session": (t.session_id or "-")[:12],
        "client": t.client,
        "attempts": t.attempts,
        "unproductive_attempts": t.unproductive_attempts,
        "age_min": round(age, 1) if age is not None else None,
        "idle_min": round(idle, 1) if idle is not None else None,
        "stage": info.get("last_sentinel_stage"),
        "status": info.get("last_sentinel_status"),
        "pr": info.get("last_pr_number"),
        "pr_state": pr_state,
        "recommend": rec,
        "reason": reason,
        "signal_source": signal_source,
        "jsonl_idle_min": None,
        "stage_high_water": t.stage_high_water,
        "pipeline_stage": t.stage,
    }


def build_peek_rows(client: str | None, now: dt.datetime) -> list[dict[str, Any]]:
    """Enumerate RUNNING and BLOCKED_ON_USER tasks, one report row per task."""
    rows = []
    for t in load_attention_tasks(client):
        if t.status is QueueItemStatus.BLOCKED_ON_USER:
            # No transcript lookup: the row's verdict comes from the queue
            # row itself, and a parked session's transcript timestamps carry
            # no signal the AWAITING_OPERATOR branch would read.
            rows.append(_awaiting_operator_row(t))
            continue
        transcript = find_transcript_for_ticket(
            str(t.ticket_id), t.session_id, t.worktree_path
        )
        refs = _load_session_refs(t.session_id)
        if transcript is not None:
            if transcript.name == OPENCODE_LOG_RELATIVE_PATH.name:
                info: dict[str, Any] = parse_opencode_transcript(
                    transcript, str(t.ticket_id)
                )
            else:
                info = parse_transcript(transcript)
                info["subagent_last_asst_ts"] = _newest_subagent_ts(
                    transcript.parent, refs.get("claude_session_id")
                )
            info["signal_source"] = _SIGNAL_SOURCE_TRANSCRIPT
            info["jsonl_idle_min"] = None
            info["claim_started_at"] = refs.get("started_at")
        elif _is_known_idless_session(refs):
            # No mtime scan either: it would report an earlier stage's idle age.
            info = {
                "signal_source": _SIGNAL_SOURCE_BLIND,
                "jsonl_idle_min": None,
                "blind_reason": (
                    _BLIND_REASON_LOCAL_HANDLE
                    if refs.get("local_liveness")
                    else _BLIND_REASON_NEVER_STARTED
                ),
            }
        else:
            info = {
                "signal_source": _SIGNAL_SOURCE_BLIND,
                "jsonl_idle_min": _compute_jsonl_idle_min(t, now),
            }
        rows.append(format_row(t, info, now))
    return rows
