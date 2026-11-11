"""Operator text and event payloads for the unowned-running sweep (#3328).

Pure builders, no I/O: the NO_OWNER park breadcrumb and ``blocked_reason``,
the UNCONFIRMABLE page text and its stored ``advisory_note`` latch, and the
``session.needs_attention`` / ``session.reap_proposed`` payloads. The
templates are the ticket's operator-facing text, copied verbatim; the stored
note clauses are static and carry no count, so a changed live count never
re-pages (binding R10).

Never imports :mod:`cw.reconcile.unowned_running` or
:mod:`cw.reconcile.unowned_running_acts`; both import this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import ReapReason
from cw.reconcile._shared import _UNOWNED_UNCONFIRMED_NOTE_PREFIX

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

# ``paused_status`` of the NO_OWNER park page (``signal_only`` only).
NO_OWNER_PAGE_REASON = ReapReason.UNOWNED_RUNNING_ROW.value
# ``paused_status`` of the UNCONFIRMABLE page (both policies).
UNCONFIRMED_PAGE_REASON = "unowned_running_unconfirmed"

# The cause tokens of an UNCONFIRMABLE signal; each is also the dedup key.
CAUSE_DUPLICATE = "duplicate"
CAUSE_BOUND_ELSEWHERE = "bound_elsewhere"
CAUSE_CONFLICTING_SESSION = "conflicting_session"
CAUSE_STALE_SESSION = "stale_session"
CAUSE_NAIVE_DATETIME = "naive_datetime"
CAUSE_UNREADABLE_CONTEXT = "unreadable_context"
CAUSE_NON_NATIVE = "non_native"
CAUSE_ROSTER_UNPROVEN = "roster_unproven"

# The ``context_state`` values of a claim context that does not tie the row
# to a session (``evidence.context_state`` of ``session.reap_proposed``).
CONTEXT_MISSING = "missing"
CONTEXT_NO_ATTEMPT = "no_attempt"
CONTEXT_ATTEMPT_MISMATCH = "attempt_mismatch"
CONTEXT_OTHER_CLAIM = "other_claim"
CONTEXT_SESSION_ABSENT = "session_absent"

_CLAIMED_UNKNOWN = "unknown (row predates schema v44)"

# The static, count-free clause after ``?unowned_unconfirmed: {cause}: ``.
_NOTE_CLAUSES: dict[str, str] = {
    CAUSE_DUPLICATE: "another RUNNING row exists for this ticket",
    CAUSE_BOUND_ELSEWHERE: "the claim context names a session another row owns",
    CAUSE_CONFLICTING_SESSION: "the claim context names another ticket's session",
    CAUSE_STALE_SESSION: "the claim context names a session that predates this claim",
    CAUSE_NAIVE_DATETIME: "a timestamp has no timezone, so order is unknown",
    CAUSE_UNREADABLE_CONTEXT: "cw-context.json cannot be read",
    CAUSE_NON_NATIVE: "a non-claude-native executor is configured",
    CAUSE_ROSTER_UNPROVEN: "the daemon roster cannot prove a worker absent",
}

# ``{evidence}`` of the UNCONFIRMABLE breadcrumb, one template per cause.
_EVIDENCE: dict[str, str] = {
    CAUSE_DUPLICATE: (
        "another RUNNING row exists for {client}/{ticket} (created "
        "{other_created}), so no session can be matched to this one"
    ),
    CAUSE_BOUND_ELSEWHERE: (
        "cw-context.json names session {sid}, which another row already owns"
    ),
    CAUSE_CONFLICTING_SESSION: (
        "cw-context.json names session {sid}, but that session belongs to "
        "{s_client}/{s_ticket}, not {client}/{ticket}"
    ),
    CAUSE_STALE_SESSION: (
        "cw-context.json names session {sid}, which started at {started} "
        "before this claim"
    ),
    CAUSE_NAIVE_DATETIME: (
        "a timestamp on session {sid} or on the claim carries no timezone, so "
        "their order cannot be checked"
    ),
    CAUSE_UNREADABLE_CONTEXT: (
        "{wt}/.claude/cw-context.json exists but cannot be read as JSON"
    ),
    CAUSE_NON_NATIVE: (
        "client {client} configures a non-claude-native executor ({names}), "
        "whose workers this check cannot see on the daemon roster"
    ),
    CAUSE_ROSTER_UNPROVEN: (
        "the daemon roster lists no workers while {reason}, so an absent "
        "worker is not evidence of a dead one"
    ),
}

ALL_CAUSES: tuple[str, ...] = tuple(_EVIDENCE)


def unconfirmed_note_head(cause: str) -> str:
    """The stored-note prefix for *cause*: detect skips a row that carries it."""
    return f"{_UNOWNED_UNCONFIRMED_NOTE_PREFIX}: {cause}: "


def unconfirmed_note(cause: str) -> str:
    """The full ``advisory_note`` latch for *cause*, with no live count."""
    return unconfirmed_note_head(cause) + _NOTE_CLAUSES[cause]


def context_clause(
    state: str,
    *,
    claim_attempt: int,
    context_attempt: int | None,
    session_id: str | None,
) -> str:
    """The NO_OWNER breadcrumb's ``{context}`` for one ``context_state``."""
    if state == CONTEXT_MISSING:
        return "cw-context.json is missing"
    if state == CONTEXT_ATTEMPT_MISMATCH:
        return (
            f"cw-context.json records attempt {context_attempt}, not this "
            f"claim's attempt {claim_attempt}"
        )
    if state == CONTEXT_OTHER_CLAIM:
        return "cw-context.json names another ticket or client"
    if state == CONTEXT_SESSION_ABSENT:
        return (
            f"cw-context.json names session {session_id}, which is not in sessions.json"
        )
    return "cw-context.json records no attempt"


def no_owner_breadcrumbs(
    *, client: str, ticket: str, wt: str, pid: int, context: str
) -> str:
    """The NO_OWNER park page text (``signal_only`` only)."""
    return (
        f"No worker owns this RUNNING row: the process that claimed it (pid "
        f"{pid}) is gone, the daemon roster is readable and lists no worker "
        f"homed on {wt}, and {context}. The row was parked BLOCKED_ON_USER; "
        "nothing was stopped, reverted or deleted. Release it with "
        f"`cw dev-queue requeue {ticket} -c {client}` (it refuses while a "
        "recorded live session exists or the daemon roster is unreadable) or "
        "with `cw doctor --reap`."
    )


def no_owner_blocked_reason(pid: int) -> str:
    """The park's ``blocked_reason``."""
    return f"no worker owns this RUNNING row (claiming process {pid} is gone)"


def unconfirmed_evidence(cause: str, **facts: str) -> str:
    """The UNCONFIRMABLE breadcrumb's ``{evidence}`` for *cause*.

    *facts* fills the cause's placeholders: ``client``, ``ticket``, ``sid``,
    ``wt``, ``other_created``, ``s_client``, ``s_ticket``, ``started``,
    ``names`` and ``reason``, as that template needs.
    """
    return _EVIDENCE[cause].format(**facts)


def backend_names_text(names: Iterable[str]) -> str:
    """``{names}``: sorted, deduplicated backend names."""
    return ", ".join(sorted(set(names)))


def claimed_text(claimed_at: datetime | None) -> str:
    """``{claimed}``: the claim instant, or the pre-v44 fallback."""
    if claimed_at is None:
        return _CLAIMED_UNKNOWN
    return claimed_at.isoformat()


def unconfirmed_breadcrumbs(
    *, client: str, ticket: str, claimed_at: datetime | None, evidence: str
) -> str:
    """The UNCONFIRMABLE page text. Never names ``cw doctor --reap``."""
    return (
        f"Ownership of RUNNING row {client}/{ticket} (claimed "
        f"{claimed_text(claimed_at)}) cannot be confirmed: {evidence}. cw "
        "changed nothing; the row stays RUNNING and keeps its lane slot. If "
        "you confirm no worker is running for it, release it with "
        f"`cw dev-queue cancel {ticket} -c {client}` and then "
        f"`cw dev-queue requeue {ticket} -c {client} --from-cancelled`; that "
        "requeue refuses while a recorded live session exists or the daemon "
        "roster is unreadable."
    )


def no_owner_page_session_id(client: str, ticket: str, attempts: int) -> str:
    """The NO_OWNER page's synthetic ``session_id``."""
    return f"no-owner:{client}/{ticket}#{attempts}"


def unconfirmed_page_session_id(
    client: str, ticket: str, attempts: int, cause: str
) -> str:
    """The UNCONFIRMABLE page's synthetic ``session_id``."""
    return f"unconfirmed:{client}/{ticket}#{attempts}:{cause}"


def needs_attention_payload(
    *,
    session_id: str,
    client: str,
    ticket_id: str,
    lane: str,
    paused_status: str,
    breadcrumbs: str,
) -> dict[str, object]:
    """The nine canonical ``session.needs_attention`` keys; no Session exists."""
    return {
        "session_id": session_id,
        "session_name": "",
        "client": client,
        "ticket_id": ticket_id,
        "claude_session_id": None,
        "paused_status": paused_status,
        "breadcrumbs": breadcrumbs,
        "crashed": False,
        "lane": lane,
    }


def reap_proposed_payload(
    *,
    client: str,
    ticket_id: str,
    lane: str,
    proposed_action: str,
    worktree_path: str,
    context_state: str,
    launch_owner_pid: int,
    claim_age_seconds: float | None,
) -> dict[str, object]:
    """The NO_OWNER ``session.reap_proposed`` payload.

    ``session_id`` is ``""`` and ``session_name`` None, as the
    ``terminal_sibling`` proposal: there is no Session, so the
    ``cw orchestrate run`` drain finds nothing to act on.
    """
    return {
        "session_id": "",
        "session_name": None,
        "client": client,
        "ticket_id": ticket_id,
        "lane": lane,
        "proposed_action": proposed_action,
        "reason": ReapReason.UNOWNED_RUNNING_ROW.value,
        "evidence": {
            "roster_readable": True,
            "worktree_path": worktree_path,
            "context_state": context_state,
            "launch_owner_pid": launch_owner_pid,
            "launch_owner_alive": False,
            "claim_age_seconds": claim_age_seconds,
        },
    }


def claim_age_seconds(claimed_at: datetime | None, now: datetime) -> float | None:
    """Seconds since the claim; None for no ``claimed_at`` or a naive one.

    Informational only (``evidence.claim_age_seconds``), never a trigger.
    """
    if claimed_at is None or claimed_at.utcoffset() is None:
        return None
    return (now - claimed_at).total_seconds()
