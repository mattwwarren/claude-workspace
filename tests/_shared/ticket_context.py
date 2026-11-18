"""Shared helpers for the ticket snapshot and ``.cw/context.json`` tests (RFC 0019 T1).

Pure helpers, no fixtures. ``write_context_json`` is the one shared writer of a
``.cw/context.json`` payload in tests. ``observed_issue_envelope`` wraps the
captured ``gh_issue_comments_2633.json`` comments array in the issue envelope
``gh issue view --json title,body,url,comments`` returns. The captured comment
entries are never tidied. The ``title``, ``body`` and ``url`` values are
AUTHORED, not observed: the fixture carries only the observed comments array.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cw.ticket_snapshot import SnapshotComment, TicketSnapshot, normalize_ticket_id

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "gh_issue_comments_2633.json"
_AUTHORED_TITLE = "Authored test ticket title"
_AUTHORED_BODY = "Authored test ticket body."
_AUTHORED_URL = "https://github.com/example/repo/issues/2633"
_FIXED_FETCHED_AT = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


def write_context_json(worktree: Path, payload: dict[str, Any]) -> Path:
    """Write *payload* to ``<worktree>/.cw/context.json`` and return the path."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(parents=True, exist_ok=True)
    path = cw_dir / "context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def observed_issue_envelope() -> dict[str, Any]:
    """A fresh copy of the issue envelope: captured comments plus authored fields."""
    data: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    envelope = copy.deepcopy(data)
    envelope["title"] = _AUTHORED_TITLE
    envelope["body"] = _AUTHORED_BODY
    envelope["url"] = _AUTHORED_URL
    return envelope


def make_snapshot(**overrides: Any) -> TicketSnapshot:
    """A snapshot from the observed envelope (client ``acme``, ticket ``ACM-1``)."""
    envelope = observed_issue_envelope()
    comments: list[SnapshotComment] = []
    for raw in envelope["comments"]:
        author = raw.get("author") or {}
        login = author.get("login") if isinstance(author, dict) else None
        comments.append(
            SnapshotComment(
                id=raw["id"],
                author_id=login,
                author=login,
                created_at=raw["createdAt"],
                body=raw["body"],
                agent_authored=False,
                minimized=bool(raw.get("isMinimized")),
            )
        )
    fields: dict[str, Any] = {
        "ticket_id": normalize_ticket_id("ACM-1"),
        "client": "acme",
        "ticket_url": envelope["url"],
        "fetched_at": _FIXED_FETCHED_AT,
        "title": envelope["title"],
        "body": envelope["body"],
        "comments": comments,
    }
    fields.update(overrides)
    return TicketSnapshot(**fields)
