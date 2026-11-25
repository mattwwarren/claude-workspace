"""Builders for the codex PLAN comment-approval tests (#3465).

One concern: the comments and snapshots an approval-evidence test needs, built
on :func:`tests._shared.ticket_context.make_comment` and
:func:`~tests._shared.ticket_context.make_snapshot_from`. The park comment is
composed from the production :data:`~cw.codex_plan._post.PARK_HEADER` and
:func:`~cw.gh.agent_marked_body`, and the token comment from
:data:`~cw.codex_plan._approval.APPROVAL_LINE`, so no test keeps its own copy of
either literal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.codex_plan._approval import APPROVAL_LINE
from cw.codex_plan._post import PARK_HEADER
from cw.gh import agent_marked_body
from tests._shared.bindings_builders import bindings_github_cli
from tests._shared.ticket_context import make_comment, make_snapshot_from

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import ClientConfig
    from cw.ticket_snapshot import SnapshotComment, TicketSnapshot

#: The operator identity a comment approval needs (an ``operator_ids`` entry).
OPERATOR: Final = "operator-login"
#: The identity the park comment is posted under.
PARK_AUTHOR: Final = "cw-bot"
#: Default instants: the park precedes the approval token.
PARK_AT: Final = "2026-11-01T12:00:00Z"
APPROVAL_AT: Final = "2026-11-18T12:00:00Z"

_PARK_ITEMS: Final = "1. Which store?"


def park_body(items: str = _PARK_ITEMS) -> str:
    """A park comment's text exactly as cw posts it: header, items, agent marker."""
    return agent_marked_body(f"{PARK_HEADER}\n\n{items}")


def token_body() -> str:
    """An approval comment's text: a sentence, then the token as its own line."""
    return f"Approved.\n\n{APPROVAL_LINE}\n"


def park_comment(
    comment_id: str = "p1", *, created_at: str = PARK_AT, items: str = _PARK_ITEMS
) -> SnapshotComment:
    """The agent-authored park comment *comment_id* at *created_at*."""
    return make_comment(
        comment_id,
        body=park_body(items),
        author_id=PARK_AUTHOR,
        created_at=created_at,
        agent_authored=True,
    )


def token_comment(
    comment_id: str = "c9",
    *,
    author: str | None = OPERATOR,
    created_at: str = APPROVAL_AT,
) -> SnapshotComment:
    """A comment by *author* carrying the approval token as its own line."""
    return make_comment(
        comment_id, body=token_body(), author_id=author, created_at=created_at
    )


def stream_snapshot(
    comments: Sequence[SnapshotComment], *, body: str | None = None
) -> TicketSnapshot:
    """A snapshot whose comment stream is exactly *comments*, in the order given.

    The order is the authority stream's order; ``created_at`` is data the
    staleness and delta rules read, so callers keep the two consistent.
    """
    if body is None:
        return make_snapshot_from(comments)
    return make_snapshot_from(comments, body=body)


def with_operator_ids(
    client: ClientConfig, operator_ids: Sequence[str] = (OPERATOR,)
) -> ClientConfig:
    """*client* bound to GitHub ``gh`` with ``tickets.operator_ids`` set."""
    bindings = bindings_github_cli(operator_ids=operator_ids)
    return client.model_copy(update={"bindings": bindings})
