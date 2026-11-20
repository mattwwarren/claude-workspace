"""The one trust evaluator for ticket-snapshot comments (RFC 0019 T2 R3, #3712).

Every comment-binding reader on the codex lanes (operator comment text, voided
findings and settle markers, constraints, the approved plan) asks
:func:`bound_comments` which comments of a snapshot it may read for its
:class:`Purpose`. The answer is returned in the legacy GitHub comment-dict
shape ``{id, body, author: {login}, createdAt}`` so the existing parsers run
unchanged; ``author.login`` carries the display ``author``, never ``author_id``.

Two rule sets, chosen by :attr:`SnapshotContext.github` (``resolve_tracker``
equals ``github-issues``; an unset tracker is NOT GitHub):

- GitHub: today's rules, byte-compatible. ``operator_ids`` are ignored, and
  login compares are exact.
- Every other tracker ("MCP"): a comment binds only when its ``author_id`` is
  non-blank, is one of ``operator_ids`` (casefolded at evaluation), and the
  comment is not ``author_conflict``-flagged. Empty ``operator_ids`` binds
  nothing (fail closed).

Snapshot order is oldest first, as the tracker returned it (the stdin
document is documented oldest-first). On MCP, "newest" means the LAST
qualifying comment in that order, never a string ``max`` over ``created_at``.
The ``minimized`` field is never consulted.

``agent_authored`` is computed by cw from each body. The model-reported
``author_id`` is trusted on MCP trackers by design (D-D3); a consistently
wrong id on a new comment is not detected here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.gh import _PLAN_MARKER
from cw.operator_identity import resolve_operator_login
from cw.ticket_snapshot._refresh import is_github, read_for_stage, snapshot_for_stage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot._models import SnapshotComment, TicketSnapshot
    from cw.ticket_snapshot._refresh import CourierFetch

    #: ``resolve(task, stage)``: the snapshot a stage binds against, or None.
    SnapshotResolver = Callable[[TicketTask, str], TicketSnapshot | None]


class Purpose(StrEnum):
    """What a reader binds a comment for; each has its own row in the table."""

    OPERATOR_TEXT = "operator_text"
    VOIDED_AND_SETTLE = "voided_and_settle"
    CONSTRAINTS = "constraints"
    APPROVED_PLAN = "approved_plan"


@dataclass(frozen=True)
class SnapshotContext:
    """Per-client trust inputs plus how a stage obtains its snapshot.

    The default is fail-closed: not GitHub, no operator ids, no login, and a
    resolver that reads the current row's snapshot without refreshing.
    """

    operator_ids: tuple[str, ...] = ()
    operator_login: str | None = None
    github: bool = False
    resolve: SnapshotResolver = read_for_stage

    @property
    def comments_readable(self) -> bool:
        """True when a present snapshot's comments can bind at all."""
        return self.github or bool(self.operator_ids)


DEFAULT_SNAPSHOT_CONTEXT = SnapshotContext()


def context_for_client(
    client: ClientConfig,
    worktree: Path,
    *,
    resolve_login: bool,
    refresh: bool = False,
    courier: CourierFetch | None = None,
) -> SnapshotContext:
    """Build *client*'s context; ``refresh`` makes ``resolve`` refresh per call.

    *courier* (#3760) is the MCP refresh turn a non-GitHub refresh uses; it is
    consulted only inside the refresh, so ``refresh=False`` ignores it.

    ``operator_login`` is resolved only when asked (``gh api user`` stays
    lazy). The GitHub test is the exact ``resolve_tracker`` equality.
    """
    tickets = client.bindings.tickets
    ids = tuple(tickets.operator_ids) if tickets is not None else ()
    resolve: SnapshotResolver = read_for_stage
    if refresh:

        def resolve(task: TicketTask, stage: str) -> TicketSnapshot | None:
            return snapshot_for_stage(
                task, client, worktree, stage=stage, courier=courier
            )

    return SnapshotContext(
        operator_ids=ids,
        operator_login=resolve_operator_login(client) if resolve_login else None,
        github=is_github(client),
        resolve=resolve,
    )


def _legacy(comment: SnapshotComment) -> dict[str, object]:
    author = {"login": comment.author} if comment.author is not None else None
    return {
        "id": comment.id,
        "body": comment.body,
        "author": author,
        "createdAt": comment.created_at,
    }


def _is_operator(comment: SnapshotComment, ctx: SnapshotContext) -> bool:
    """Predicate M: a non-blank ``author_id`` in ``operator_ids``, not conflicted."""
    author_id = comment.author_id
    if author_id is None or not author_id.strip() or comment.author_conflict:
        return False
    return author_id.casefold() in {entry.casefold() for entry in ctx.operator_ids}


def _github_binds(
    comment: SnapshotComment, purpose: Purpose, gh_login: str | None
) -> bool:
    if purpose is Purpose.OPERATOR_TEXT:
        return bool(comment.body.strip()) and not comment.agent_authored
    if purpose is Purpose.APPROVED_PLAN:
        return (
            gh_login is not None
            and _PLAN_MARKER in comment.body
            and comment.author_id == gh_login
        )
    # Voided/settle bodies and constraints: every comment; the constraint
    # selector applies today's login rule itself.
    return True


def _mcp_binds(
    comment: SnapshotComment, purpose: Purpose, ctx: SnapshotContext
) -> bool:
    if not _is_operator(comment, ctx):
        return False
    if purpose is Purpose.APPROVED_PLAN:
        # The agent marker is ignored for a plan of record (B8).
        return _PLAN_MARKER in comment.body
    if comment.agent_authored:
        return False
    return purpose is not Purpose.OPERATOR_TEXT or bool(comment.body.strip())


def bound_comments(
    snapshot: TicketSnapshot | None,
    ctx: SnapshotContext,
    purpose: Purpose,
    *,
    gh_login: str | None = None,
) -> list[dict[str, object]]:
    """The comments of *snapshot* that may bind for *purpose*, oldest first.

    *gh_login* is the current gh identity, consulted only for the GitHub
    approved-plan rule. A None snapshot binds nothing.
    """
    if snapshot is None:
        return []
    if ctx.github:
        kept = [c for c in snapshot.comments if _github_binds(c, purpose, gh_login)]
    else:
        kept = [c for c in snapshot.comments if _mcp_binds(c, purpose, ctx)]
    return [_legacy(comment) for comment in kept]


def approved_plan_text(
    snapshot: TicketSnapshot | None,
    ctx: SnapshotContext,
    *,
    gh_login: str | None = None,
) -> str | None:
    """Return the last trusted approved-plan body, or ``None``.

    This is the single body-selection seam for plan recovery and local plan
    fetching. ``gh_login`` is supplied by callers that already have the
    current GitHub identity; MCP binding never needs it.
    """
    comments = bound_comments(snapshot, ctx, Purpose.APPROVED_PLAN, gh_login=gh_login)
    body = comments[-1]["body"] if comments else None
    return body if isinstance(body, str) else None
