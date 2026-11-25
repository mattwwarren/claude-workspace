"""Builders for the codex PLAN comment-approval tests (#3465).

One concern: the comments, snapshots and parked-run setup an approval-evidence
test needs, built on :func:`tests._shared.ticket_context.make_comment` and
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
from cw.codex_plan._result import write_plan_draft
from cw.gh import BLOCKING_FINDINGS_HEADER, agent_marked_body
from cw.plan_fingerprint import LeadingBlock
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.bindings_builders import bindings_github_cli
from tests._shared.ticket_context import (
    issue_envelope_json,
    make_comment,
    make_snapshot_from,
)
from tests.test_codex_plan_run import TICKET

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import ClientConfig
    from cw.ticket_snapshot import SnapshotComment, TicketSnapshot
    from tests.test_codex_plan_run import Harness

#: The operator identity a comment approval needs (an ``operator_ids`` entry).
OPERATOR: Final = "operator-login"
#: The identity the park comment is posted under.
PARK_AUTHOR: Final = "cw-bot"
#: Default instants: the park precedes the approval token.
PARK_AT: Final = "2026-11-01T12:00:00Z"
APPROVAL_AT: Final = "2026-11-18T12:00:00Z"

#: The ``gh`` argv the PLAN run's tracker read issues for the run's ticket.
VIEW: Final = ("issue", "view", TICKET, "--json", "title,body,url,comments")

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


def blocking_comment(
    comment_id: str = "b1", *, created_at: str = PARK_AT
) -> SnapshotComment:
    """An agent-authored blocking-findings header comment (a non-park header)."""
    return make_comment(
        comment_id,
        body=f"{BLOCKING_FINDINGS_HEADER}\n\nfix it",
        author_id=PARK_AUTHOR,
        created_at=created_at,
        agent_authored=True,
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


def parked_with_token(
    h: Harness,
    plan: str,
    *,
    with_token: bool = True,
    operator_ids: Sequence[str] | None = (OPERATOR,),
) -> Harness:
    """*h* resumed from a parked draft of *plan*, the token optionally posted.

    The ticket carries a park comment, then (with *with_token*) the approval
    token. *operator_ids* ``None`` binds no ``operator_ids``; the captured
    comments are authored by ``OPERATOR``, so ids that omit it make the token a
    stranger's.
    """
    write_plan_draft(h.worktree, plan, book=LeadingBlock(applied="none"))
    comments = [("p1", park_body(), PARK_AT)]
    if with_token:
        comments.append(("c9", token_body(), APPROVAL_AT))
    h.gh.reply(*VIEW, stdout=issue_envelope_json(comments))
    client = codex_tracker_client(h.worktree)
    h.client = (
        client if operator_ids is None else with_operator_ids(client, operator_ids)
    )
    return h
