"""Evidence and remedy text for the liveness dead-session page (#2153).

The liveness sweep's ``SESSION_NEEDS_ATTENTION`` page (``cw.reconcile.
liveness``) fires once per *evidence key*, not once per re-evaluation
interval. This leaf module owns everything that page carries beyond its
historical breadcrumb, so ``liveness.py`` only wires call sites and doctor
class-8 (``cw.doctor.wedge``) reuses the same text builder:

* :func:`summarize_last_transcript_record` — the final record of the
  session's own transcript file (type, timestamp, and whether the last
  content-bearing record is an ``API Error``).
* :func:`compute_evidence_key` — the dedup key: ``paused_status``, the
  staleness-basis timestamp (:func:`_widened_transcript_timestamp`, the max
  over the primary and sibling transcripts, so a trailing metadata record
  cannot move it), and the owned queue row's status.
* :func:`close_command` / :func:`requeue_command` — the two operator commands.
* :func:`format_evidence_suffix` / :func:`evidence_payload_fields` — the
  breadcrumb suffix and the additive payload keys.

Wording is "evidence suggests dead; confirm first": cw never asserts
``--confirmed-dead`` itself and nothing here closes, requeues, or reaps
(ADR-0014). Read-only: file reads only, no subprocess, no lock (ADR-0019).
Imports only the ``cw.reconcile._shared`` package root, never ``liveness`` or
``routed_result_sessions``, so both can import it without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.reconcile._shared import (
    _iter_transcript_records,
    _locate_session_transcript,
    _redact_and_truncate,
    _widened_transcript_timestamp,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session, TicketTask

_KEY_SEP = "|"
_KEY_NONE = "none"
_API_ERROR_PREFIX = "API Error"
_ASSISTANT_RECORD_TYPE = "assistant"
_MINUTES_PER_HOUR = 60
_UNKNOWN = "unknown"
_DEAD_EVIDENCE_LEAD = (
    "; evidence suggests this session is dead (confirm before closing)"
)


class LastRecordSummary(NamedTuple):
    """The final record of a session's own transcript file (#2153).

    ``record_type``/``timestamp`` describe the final record of any type (often
    trailing metadata such as ``cost-state``). ``is_error`` is true iff the
    last *content-bearing* record is an assistant record whose text starts
    with ``API Error``; ``snippet`` is that text, redacted and truncated, and
    is set only when ``is_error``.
    """

    record_type: str | None
    timestamp: datetime | None
    is_error: bool
    snippet: str | None


def summarize_last_transcript_record(session: Session) -> LastRecordSummary | None:
    """Summarize the final record of *session*'s own transcript, or None.

    Fails open to None when the transcript cannot be located (including an
    ``OSError`` from the project-dir check) or yields no records; the shared
    iterator already swallows open/read errors and skips malformed lines.
    """
    try:
        path = _locate_session_transcript(session)
    except OSError:
        return None
    if path is None:
        return None
    found = False
    last_type: str | None = None
    last_ts: datetime | None = None
    content_type: str | None = None
    content_text: str | None = None
    for record in _iter_transcript_records(path):
        found = True
        last_type = record.record_type
        last_ts = record.timestamp
        if record.content_bearing:
            content_type = record.record_type
            content_text = record.text
    if not found:
        return None
    error_text = (
        content_text
        if content_type == _ASSISTANT_RECORD_TYPE
        and content_text is not None
        and content_text.startswith(_API_ERROR_PREFIX)
        else None
    )
    return LastRecordSummary(
        record_type=last_type,
        timestamp=last_ts,
        is_error=error_text is not None,
        snippet=_redact_and_truncate(error_text) if error_text is not None else None,
    )


def _staleness_basis_leg(session: Session) -> str:
    """The widened transcript timestamp as ISO text, or ``none`` (fail-open)."""
    try:
        ts = _widened_transcript_timestamp(session)
    except OSError:
        return _KEY_NONE
    return ts.isoformat() if ts is not None else _KEY_NONE


def compute_evidence_key(
    session: Session,
    *,
    paused_status: str,
    task: TicketTask | None,
    session_age: bool,
) -> str:
    """Return the dead-session page's dedup key for *session*.

    ``paused_status|<staleness-basis ts or none>|<owned row status or none>``.
    For a ``session_age`` candidate the key is still computed, but
    :func:`_widened_transcript_timestamp` is not called (the ts leg is
    ``none``), matching #2417's never-consult-the-transcript rule. The row
    leg counts only a row whose ``session_id`` is this session, the same
    guard the #2135 carve-out uses.
    """
    ts_leg = _KEY_NONE if session_age else _staleness_basis_leg(session)
    task_leg = (
        task.status.value
        if task is not None and task.session_id == session.id
        else _KEY_NONE
    )
    return _KEY_SEP.join((paused_status, ts_leg, task_leg))


def close_command(session_id: str) -> str:
    """The exact operator command that closes a session confirmed dead."""
    return f"cw spawn close --confirmed-dead {session_id}"


def requeue_command(ticket_id: str | None, client: str) -> str | None:
    """The operator command that requeues *ticket_id* after the close, or None."""
    if ticket_id is None:
        return None
    return f"cw dev-queue requeue {ticket_id} -c {client} --from-cancelled"


def _last_record_clause(summary: LastRecordSummary) -> str:
    record_type = summary.record_type or _UNKNOWN
    ts = summary.timestamp.isoformat() if summary.timestamp is not None else _UNKNOWN
    error = f" (API error: {summary.snippet})" if summary.is_error else ""
    return f"; last record {record_type} at {ts}{error}"


def format_evidence_suffix(
    summary: LastRecordSummary | None,
    *,
    session_id: str,
    ticket_id: str | None,
    client: str,
    stale_minutes: float,
    session_age: bool = False,
) -> str:
    """Return the breadcrumb suffix shared by the liveness page and doctor class-8.

    Three forms, each opening with the "evidence suggests ... confirm" lead
    and closing with the close command (plus ``then:`` the requeue command
    when a ticket exists): the last-record form, the flat-only form when no
    summary exists, and the ``session_age`` form for an unobservable session.
    """
    hours = stale_minutes / _MINUTES_PER_HOUR
    if session_age:
        evidence = f"; unobserved for {hours:.1f}h (session age, no transcript)"
    elif summary is None:
        evidence = f"; flat {hours:.1f}h"
    else:
        evidence = f"{_last_record_clause(summary)}; flat {hours:.1f}h"
    remedy = (
        f"; if you have confirmed the session is dead, run: {close_command(session_id)}"
    )
    requeue = requeue_command(ticket_id, client)
    if requeue is not None:
        remedy = f"{remedy} then: {requeue}"
    return f"{_DEAD_EVIDENCE_LEAD}{evidence}{remedy}"


def evidence_payload_fields(
    summary: LastRecordSummary | None,
    *,
    session_id: str,
    ticket_id: str | None,
    client: str,
    evidence_key: str,
) -> dict[str, object]:
    """Return the six additive ``session.needs_attention`` payload keys.

    ``last_record_*`` are null when *summary* is None (always so for a
    ``session_age`` page); ``requeue_command`` is always present and null
    without a ticket.
    """
    return {
        "last_record_type": summary.record_type if summary is not None else None,
        "last_record_ts": (
            summary.timestamp.isoformat()
            if summary is not None and summary.timestamp is not None
            else None
        ),
        "last_record_is_error": summary.is_error if summary is not None else None,
        "close_command": close_command(session_id),
        "requeue_command": requeue_command(ticket_id, client),
        "evidence_key": evidence_key,
    }
