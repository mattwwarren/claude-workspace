"""Operator allow of a repo review engine round (GitHub #3039, RFC 0017 I1).

``cw review engine allow`` records one allow on a row parked
``review_engine_modified``, bound to the exact HEAD the operator inspected.
Modeled on :mod:`cw.dev_queue.must_fix_override`: event first, then save. The
row keeps its status; the operator then runs ``cw dev-queue requeue``. A later
commit needs a new allow, because :func:`review_engine_head_allowed` matches the
recorded ``head_sha`` exactly.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result import REVIEW_ENGINE_MODIFIED_BLOCKER_REASON
from cw.config import get_client
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus, ReviewEngineAllow
from cw.operator_identity import resolve_operator_login

if TYPE_CHECKING:
    from cw.models import TicketTask

_log = logging.getLogger(__name__)

_HEAD_RE: Final = re.compile(r"^[0-9a-f]{40}$")


def _require_parked(task: TicketTask) -> None:
    """Refuse unless the row is parked by the engine preflight park."""
    if (
        task.status != QueueItemStatus.BLOCKED_ON_USER
        or task.blocked_reason != REVIEW_ENGINE_MODIFIED_BLOCKER_REASON
    ):
        msg = (
            "cw review engine allow needs a row parked BLOCKED_ON_USER with "
            f"blocked_reason={REVIEW_ENGINE_MODIFIED_BLOCKER_REASON!r}"
        )
        raise CwError(msg)


def record_review_engine_allow(
    ticket_id: str,
    client_name: str,
    head_sha: str,
    reason: str,
    *,
    bounded: bool = False,
) -> dict[str, str]:
    """Record an allow of *head_sha* on the parked row, or raise :class:`CwError`.

    Every refusal happens before the first write. An allow with no resolvable
    operator login is refused: an allow must not be anonymous.
    """
    stripped = reason.strip()
    if not stripped:
        msg = "cw review engine allow needs a non-blank --reason"
        raise CwError(msg)
    if _HEAD_RE.fullmatch(head_sha) is None:
        msg = f"--head-sha must be a full 40-hex commit id (got {head_sha!r})"
        raise CwError(msg)
    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        _require_parked(task)
        actor = resolve_operator_login(get_client(client_name))
        if actor is None:
            msg = "cannot resolve the operator login; an engine allow must be named"
            raise CwError(msg)
        now = datetime.now(UTC)
        record_event(
            OrchestratorEventType.REVIEW_ENGINE_ALLOWED,
            {
                "ticket_id": ticket_id,
                "client": client_name,
                "head_sha": head_sha,
                "reason": stripped,
                "actor": actor,
                "recorded_at": now.isoformat(),
            },
            correlation_id=ticket_id,
        )
        allow = ReviewEngineAllow(
            head_sha=head_sha, actor=actor, reason=stripped, recorded_at=now
        )
        task.review_engine_allows = [
            entry for entry in task.review_engine_allows if entry.head_sha != head_sha
        ] + [allow]
        try:
            save_dev_queue(store)
        except Exception:
            _log.exception(
                "review engine allow for %s/%s was recorded as an event but the "
                "queue save failed; the row is unchanged",
                client_name,
                ticket_id,
            )
            raise
    return {
        "ticket_id": ticket_id,
        "client": client_name,
        "head_sha": head_sha,
        "actor": actor,
    }


def review_engine_head_allowed(ticket_id: str, client_name: str, head_sha: str) -> bool:
    """Whether the row carries an allow for exactly *head_sha* (read-only)."""
    try:
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
    except CwError:
        return False
    return any(entry.head_sha == head_sha for entry in task.review_engine_allows)
