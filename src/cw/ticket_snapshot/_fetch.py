"""Build a :class:`TicketSnapshot` from GitHub or from a model-reported stdin document.

GitHub (``github-issues`` clients) is fetched here, one ``gh`` call, with no
lock held (ADR-0019). The stdin path is for every other tracker: the document
is cw-defined but its content is model-reported by RFC design (RFC 0019
trust-cost mitigation 4), so ``agent_authored`` is computed by cw from each
body and is never accepted from the caller.

Caps are enforced by the store writer (``enforce_caps``), never truncated here.
A pydantic ``ValidationError`` becomes :class:`SnapshotRefusedError` naming
only field locations and error types, never the offending input value, so no
ticket text reaches an exception message, the CLI output or a log.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cw.gh.issues import fetch_issue_snapshot, is_agent_authored
from cw.gh.runner import FETCH_COMMENTS_TIMEOUT
from cw.ticket_snapshot._models import (
    SnapshotComment,
    SnapshotRefusedError,
    TicketSnapshot,
    normalize_ticket_id,
)


class StdinComment(BaseModel):
    """One comment in the stdin document. ``agent_authored`` is never accepted."""

    model_config = ConfigDict(extra="forbid")

    id: str
    author_id: str | None
    author: str | None
    created_at: str
    body: str
    minimized: bool = False


class StdinDocument(BaseModel):
    """The cw-owned stdin shape for non-GitHub trackers (RFC 0019 R4, R16)."""

    model_config = ConfigDict(extra="forbid")

    ticket_url: str | None = None
    title: str
    body: str
    comments: list[StdinComment] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _gh_comment(raw: Any) -> SnapshotComment:
    if not isinstance(raw, dict):
        msg = "gh comment is not an object"
        raise SnapshotRefusedError(msg)
    comment_id = raw.get("id")
    body = raw.get("body")
    created_at = raw.get("createdAt")
    if not isinstance(comment_id, str) or not comment_id:
        msg = "gh comment has no id"
        raise SnapshotRefusedError(msg)
    if not isinstance(body, str):
        msg = "gh comment has no body text"
        raise SnapshotRefusedError(msg)
    if not isinstance(created_at, str):
        msg = "gh comment has no createdAt"
        raise SnapshotRefusedError(msg)
    author = raw.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    author_login = login if isinstance(login, str) else None
    return SnapshotComment(
        id=comment_id,
        author_id=author_login,
        author=author_login,
        created_at=created_at,
        body=body,
        agent_authored=is_agent_authored(body),
        minimized=raw.get("isMinimized") is True,
    )


def snapshot_from_github(ticket_id: str, client: str, cwd: Path) -> TicketSnapshot:
    """Fetch the issue live (one ``gh`` call, no lock) and map it to a snapshot."""
    normalized = normalize_ticket_id(ticket_id)
    data = fetch_issue_snapshot(normalized, FETCH_COMMENTS_TIMEOUT, cwd=cwd)
    if data is None:
        msg = "gh issue view failed or returned a malformed document"
        raise SnapshotRefusedError(msg)
    raw_comments: list[Any] = list(data.get("comments") or [])
    comments = [_gh_comment(raw) for raw in raw_comments]
    url = data.get("url")
    return TicketSnapshot(
        ticket_id=normalized,
        client=client,
        ticket_url=url if isinstance(url, str) else None,
        fetched_at=_now(),
        title=str(data.get("title", "")),
        body=str(data.get("body", "")),
        comments=comments,
    )


def snapshot_from_stdin(text: str, ticket_id: str, client: str) -> TicketSnapshot:
    """Parse the cw-owned stdin document into a snapshot; refuse anything else."""
    normalized = normalize_ticket_id(ticket_id)
    try:
        payload: Any = json.loads(text)
    except ValueError as exc:
        msg = "stdin document is not valid JSON"
        raise SnapshotRefusedError(msg) from exc
    try:
        document = StdinDocument.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['type']}"
            for err in exc.errors()
        )
        msg = f"stdin document refused ({problems})"
        raise SnapshotRefusedError(msg) from None
    comments = [
        SnapshotComment(
            id=comment.id,
            author_id=comment.author_id,
            author=comment.author,
            created_at=comment.created_at,
            body=comment.body,
            agent_authored=is_agent_authored(comment.body),
            minimized=comment.minimized,
        )
        for comment in document.comments
    ]
    return TicketSnapshot(
        ticket_id=normalized,
        client=client,
        ticket_url=document.ticket_url,
        fetched_at=_now(),
        title=document.title,
        body=document.body,
        comments=comments,
    )
