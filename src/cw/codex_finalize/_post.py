"""The PR-link ticket comment after the result persists (#3062 R11, RFC 0019 O3).

Best-effort and never a hold (C1's rule): the post runs after the door, and a
refusal, an oversize body or a failed enqueue is one skipped event. The comment
is enqueued under the ``codex-pr-link`` producer with marker
``pr-link-<sha256(pr_url)[:16]>``, so a requeue that reaches the same PR dedups
on the outbox instead of re-reading the ticket's comments (RFC 0019 O3 retired
the ``gh issue view`` read).

A free-text ticket (``plan_source == "free_text"``) has no ticket to comment
on and skips with a reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from cw.codex_review_signals import emit_verdict_comment_skipped, tracker_label
from cw.codex_stage_completion import STAGE_FAULTS
from cw.codex_stage_post import post_stage_comment
from cw.outbox_producer import SKIP_ALREADY_POSTED

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

PR_LINK_KIND: Final = "pr_link"
PR_LINK_PRODUCER: Final = "codex-pr-link"
PR_LINK_MARKER_PREFIX: Final = "pr-link"
FREE_TEXT_PLAN_SOURCE: Final = "free_text"
SKIP_FREE_TEXT: Final = "free_text_ticket"

__all__ = [
    "SKIP_ALREADY_POSTED",
    "SKIP_FREE_TEXT",
    "compose_pr_link_comment",
    "post_pr_link_comment",
]


def compose_pr_link_comment(pr_url: str, *, parent_pr: int | None = None) -> str:
    """The comment text: the PR link, plus the stacked-draft note when known."""
    text = f"PR: {pr_url}\n"
    if parent_pr is not None:
        text += f"\nCreated as draft — stacked behind PR #{parent_pr}\n"
    return text


def _skip(task: TicketTask, client: ClientConfig, sid: str, reason: str) -> None:
    emit_verdict_comment_skipped(
        ticket_id=task.ticket_id,
        client=client.name,
        tracker=tracker_label(client),
        kind=PR_LINK_KIND,
        artifact_path=None,
        reviewed_sha=None,
        session_id=sid,
        reason=reason,
    )


def post_pr_link_comment(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    pr_url: str,
    plan_source: str,
) -> None:
    """Enqueue the PR link to the ticket, best-effort; never raises a stage fault."""
    try:
        if plan_source == FREE_TEXT_PLAN_SOURCE:
            _skip(task, client, sid, SKIP_FREE_TEXT)
            return
        post_stage_comment(
            task=task,
            client=client,
            worktree=worktree,
            sid=sid,
            text=compose_pr_link_comment(pr_url),
            artifact_path=None,
            kind=PR_LINK_KIND,
            producer=PR_LINK_PRODUCER,
            marker_prefix=PR_LINK_MARKER_PREFIX,
            marker_source=pr_url,
        )
    except STAGE_FAULTS:
        _log.exception(
            "pr_link comment enqueue failed ticket=%s session=%s", task.ticket_id, sid
        )
