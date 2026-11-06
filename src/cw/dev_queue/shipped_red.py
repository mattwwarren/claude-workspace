"""Row-layer helpers for the red-shipped-PR signal (GitHub #2945, RFC 0016 B2).

A leaf module (no ``gh`` I/O): the candidate predicate, the per-episode latch
rules (:func:`is_due`, :func:`is_alerted`, :func:`episode_marker`), the poll
pacing constants and the injection-safe check-name rendering shared by the
sweep (``cw.shipped_pr_signal``), ``cw queue peek`` and the next-command rule.
It imports only models and the text helpers, never ``next_commands`` (which
imports this module for its rule).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw._text import redact
from cw.models import QueueItemStatus
from cw.pr_feedback_text import escape_controls

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.models import TicketTask

SHIPPED_PR_CHECKS_FAILED_REASON = "shipped_pr_checks_failed"
RECOVER_RED_PR_VERB = "recover-red-pr"
SHIPPED_DISPOSITION = "shipped"

# An alerted row gets a head-only continuity read, and a stamped (unarmed or
# optional-only) row a re-check, once per window.
RECHECK_SECONDS = 900
UNREADABLE_BACKOFF_BASE_SECONDS = 60
# At most this many rows are evaluated (gh reads) per tick.
MAX_EVALUATIONS_PER_TICK = 8

_MAX_BREADCRUMB_NAMES = 5
_MAX_NAME_CHARS = 80
_BACKOFF_MAX_DOUBLINGS = 4
_FINGERPRINT_HEX_CHARS = 16
_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")
_NEVER_EVALUATED = datetime.min.replace(tzinfo=UTC)


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """``(owner/repo, number)`` from a GitHub PR URL, or None."""
    match = _PR_URL_RE.search(pr_url)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def is_shipped_red_candidate(task: TicketTask) -> bool:
    """True for a COMPLETED, cw-shipped row whose OPEN PR hydrates ``ci_ok`` False.

    ``ci_ok`` is the genuine-failure flag (``summarize_status_checks``): it can
    only over-include (the rollup covers optional checks), so the sweep still
    runs the ``--required`` read after this pre-filter.
    """
    state = task.pr_state
    return (
        task.status is QueueItemStatus.COMPLETED
        and task.disposition == SHIPPED_DISPOSITION
        and bool(task.pr_url)
        and parse_pr_url(task.pr_url or "") is not None
        and state is not None
        and state.state == "OPEN"
        and not state.ci_ok
    )


def red_fingerprint(names: Iterable[str]) -> str:
    """Order-insensitive, stable hash of a set of check names."""
    digest = hashlib.sha256("\n".join(sorted(set(names))).encode()).hexdigest()
    return digest[:_FINGERPRINT_HEX_CHARS]


def is_alerted(task: TicketTask) -> bool:
    """True iff an episode is open: a latch exists and a page was emitted."""
    return task.shipped_red is not None and task.shipped_red.alerted_at is not None


def unreadable_backoff_seconds(failures: int) -> int:
    """60 s doubling per consecutive unknown lookup, capped at ``RECHECK_SECONDS``."""
    doublings = min(max(failures - 1, 0), _BACKOFF_MAX_DOUBLINGS)
    return min(UNREADABLE_BACKOFF_BASE_SECONDS << doublings, RECHECK_SECONDS)


def is_due(task: TicketTask, now: datetime) -> bool:
    """Whether the sweep should spend ``gh`` reads on *task* this tick.

    Due when: there is no latch; the hydrated failing-name set changed and the
    latch is not in unreadable backoff; the latch is unalerted and its
    not-before has passed; or the latch is alerted and ``RECHECK_SECONDS`` have
    passed since its last evaluation (the head-only continuity read).
    """
    latch = task.shipped_red
    if latch is None:
        return True
    not_before_passed = latch.retry_not_before is None or latch.retry_not_before <= now
    names = task.pr_state.failing_checks if task.pr_state is not None else []
    if red_fingerprint(names) != latch.hydrated_fingerprint and (
        latch.failures == 0 or not_before_passed
    ):
        return True
    if latch.alerted_at is None:
        return not_before_passed
    return latch.evaluated_at + timedelta(seconds=RECHECK_SECONDS) <= now


def episode_marker(head_sha: str, alerted_at: datetime) -> str:
    """The page's ``renotify_marker``: an episode id, never the check fingerprint."""
    return f"{head_sha}@{alerted_at.isoformat()}"


def eval_order_key(task: TicketTask) -> tuple[bool, datetime, str]:
    """Fair order: never-evaluated first, then oldest evaluation, then ticket id."""
    latch = task.shipped_red
    evaluated = latch.evaluated_at if latch is not None else _NEVER_EVALUATED
    return (latch is not None, evaluated, task.ticket_id)


def red_breadcrumbs(names: Iterable[str]) -> str:
    """Render check names as one safe line: JSON-quoted, redacted, bounded.

    Check names are untrusted (a hostile name must not start a new line or
    carry an instruction into an event or a peek row): each is escaped,
    redacted, truncated to 80 characters and ``json.dumps``-quoted; at most 5
    are listed and the rest become ``+N more``.
    """
    listed = list(names)
    shown = [
        json.dumps(redact(escape_controls(name))[:_MAX_NAME_CHARS])
        for name in listed[:_MAX_BREADCRUMB_NAMES]
    ]
    extra = len(listed) - _MAX_BREADCRUMB_NAMES
    if extra > 0:
        shown.append(f"+{extra} more")
    return ", ".join(shown)
