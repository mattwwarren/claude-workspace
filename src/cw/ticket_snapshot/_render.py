"""Render ``.cw/context.json`` from the snapshot store.

The rendered file keeps every key the readers consume by name: the seven
#2700 keys (``ticket_id``, ``ticket_title``, ``ticket_body``, ``comments``,
``scope_hint``, ``prior_decisions``, ``materialized_by_session``), with
comment entries carrying ``author``, ``created_at`` and ``body`` plus the
T1 additions. Readers ignore extra keys (RFC 0019 R19).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cw.atomic import atomic_write_text
from cw.context_schema import CW_TICKET_CONTEXT_SCHEMA_VERSION
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from cw.ticket_snapshot._store import read

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.ticket_snapshot._models import TicketSnapshot

_FETCHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def render_context(
    snapshot: TicketSnapshot,
    digest: str,
    *,
    materialized_by_session: str | None,
) -> dict[str, Any]:
    """Return the ``.cw/context.json`` payload for *snapshot*."""
    comments: list[dict[str, Any]] = [
        {
            "author": comment.author,
            "created_at": comment.created_at,
            "body": comment.body,
            "id": comment.id,
            "author_id": comment.author_id,
            "agent_authored": comment.agent_authored,
            "minimized": comment.minimized,
        }
        for comment in snapshot.comments
    ]
    return {
        "schema_version": CW_TICKET_CONTEXT_SCHEMA_VERSION,
        "ticket_id": snapshot.ticket_id,
        "ticket_title": snapshot.title,
        "ticket_body": snapshot.body,
        "ticket_url": snapshot.ticket_url,
        "comments": comments,
        "scope_hint": None,
        "prior_decisions": [],
        "materialized_by_session": materialized_by_session,
        "snapshot_digest": digest,
        "snapshot_fetched_at": snapshot.fetched_at.strftime(_FETCHED_AT_FORMAT),
    }


def write_context_json(worktree: Path, rendered: dict[str, Any]) -> Path:
    """Write the rendered payload atomically to ``<worktree>/.cw/context.json``."""
    path = worktree / CONTEXT_JSON_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(rendered, indent=2) + "\n")
    return path


def render_for_task(
    task: TicketTask,
    worktree: Path,
    *,
    materialized_by_session: str | None = None,
) -> bool:
    """Render the row's snapshot into *worktree*. False when the row has no digest.

    Store-only: no tracker call and no lock. Raises what :func:`read` raises.
    """
    snapshot = read(task)
    if snapshot is None or task.snapshot_digest is None:
        return False
    rendered = render_context(
        snapshot,
        task.snapshot_digest,
        materialized_by_session=materialized_by_session,
    )
    write_context_json(worktree, rendered)
    return True
