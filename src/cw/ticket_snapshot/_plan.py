"""Plan-of-record recovery for codex IMPL (RFC 0019 T2 R13, #3712).

When ``.cw/plan.md`` is absent on a non-repair row, IMPL recovers the approved
plan before its guards run, from two sources in order:

1. The outbox entry codex PLAN enqueued (``codex-plan`` producer, ``plan-``
   marker, any state including PENDING), newest first. Only the newest entry
   is read: a later round that is unqueued, or posted directly on the ticket,
   is not seen (bounded staleness, accepted). Its parts are unwrapped with
   :func:`cw.tracker_post.unwrap_part`; the text is the redacted one the
   producer enqueued. Any read or unwrap failure means "no entry".
2. The snapshot: the last approved-plan comment per the trust evaluator.

Recovered text must carry the ``<!-- plan-spec-reviewed`` marker. The file is
written as ``local_runner.build_task_message`` writes it.

Not re-exported from :mod:`cw.ticket_snapshot`: this module imports
``cw.codex_plan._post``, whose package imports ``cw.ticket_snapshot``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from cw.codex_plan._post import PLAN_MARKER_PREFIX, PLAN_PRODUCER
from cw.exceptions import CwError
from cw.gh import _PLAN_MARKER
from cw.models.repair_entry import is_adopted_repair_entry
from cw.operator_identity import cached_gh_login
from cw.outbox import OutboxRole, list_entries, read_parts
from cw.ticket_snapshot._binding import SnapshotContext, approved_plan_text
from cw.tracker_post import unwrap_part

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.outbox import OutboxEntry
    from cw.ticket_snapshot._models import TicketSnapshot

_log = logging.getLogger("cw.ticket_snapshot")

PlanSource = Literal["outbox", "snapshot"]
#: The friction entry prefix IMPL records for a recovered plan.
PLAN_RECOVERED_FRICTION = "plan_recovered"
PLAN_RELATIVE_PATH = (".cw", "plan.md")


def _is_plan_entry(entry: OutboxEntry, ticket: str) -> bool:
    return (
        entry.role is OutboxRole.TICKETS
        and entry.ticket == ticket
        and entry.producer == PLAN_PRODUCER
        and entry.marker.startswith(f"{PLAN_MARKER_PREFIX}-")
    )


def _unwrap(parts: list[str]) -> str | None:
    total = len(parts)
    pieces = [unwrap_part(part, index, total) for index, part in enumerate(parts, 1)]
    if any(piece is None for piece in pieces):
        return None
    return "".join(piece for piece in pieces if piece is not None)


def _from_outbox(task: TicketTask, client: ClientConfig) -> str | None:
    """The newest ``codex-plan`` entry's text, or None ("no entry")."""
    ticket = task.ticket_id.lstrip("#")
    try:
        entries = list_entries(client.name)
        entry = next((e for e in entries if _is_plan_entry(e, ticket)), None)
        parts = read_parts(entry) if entry is not None else None
    except (CwError, OSError) as exc:
        _log.warning(
            "plan recovery: outbox unreadable ticket_id=%s error_class=%s",
            task.ticket_id,
            type(exc).__name__,
        )
        return None
    return _unwrap(parts) if parts else None


def _from_snapshot(snapshot: TicketSnapshot | None, ctx: SnapshotContext) -> str | None:
    """The last approved-plan comment the evaluator binds, or None."""
    login = cached_gh_login() if ctx.github else None
    return approved_plan_text(snapshot, ctx, gh_login=login)


def _reviewed(text: str | None) -> bool:
    return text is not None and _PLAN_MARKER in text


def recover_plan(
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    snapshot: TicketSnapshot | None,
    ctx: SnapshotContext,
) -> PlanSource | None:
    """Materialize ``.cw/plan.md`` from the outbox, else the snapshot.

    Returns the source used, or None when the file already exists, the row is
    an adopted repair entry, or neither source holds a reviewed plan.
    """
    plan_path = worktree.joinpath(*PLAN_RELATIVE_PATH)
    if plan_path.exists() or is_adopted_repair_entry(task):
        return None
    source: PlanSource = "outbox"
    text = _from_outbox(task, client)
    if not _reviewed(text):
        source = "snapshot"
        text = _from_snapshot(snapshot, ctx)
    if text is None or not _reviewed(text):
        return None
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(text, encoding="utf-8")
    _log.warning(
        "plan recovery: .cw/plan.md recovered ticket_id=%s source=%s",
        task.ticket_id,
        source,
    )
    return source
