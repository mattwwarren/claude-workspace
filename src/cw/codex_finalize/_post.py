"""The PR-link ticket comment after the result persists (#3062 R11).

Best-effort and never a hold (C1's rule): the post runs after the door, and a
refusal or an unconfirmed post is an event. The events are the three
verdict-comment events with ``kind: "pr_link"``, ``reviewed_sha: null`` and
``artifact_path: null``. GitHub tickets post through the shared poster; other
trackers through the A2 tracker-IO turn (both in :mod:`cw.codex_stage_post`).

A free-text ticket (``plan_source == "free_text"``) has no ticket to comment
on and skips with a reason. A requeue that reaches an already-shipped PR does
not re-post on GitHub: when the ticket's comments already carry the PR URL the
post is skipped as ``already_posted``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from cw.codex_review_signals import emit_verdict_comment_skipped
from cw.codex_stage_completion import STAGE_FAULTS
from cw.codex_stage_post import post_stage_comment, tracker_label
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_comments

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

PR_LINK_KIND: Final = "pr_link"
FREE_TEXT_PLAN_SOURCE: Final = "free_text"
SKIP_FREE_TEXT: Final = "free_text_ticket"
SKIP_ALREADY_POSTED: Final = "already_posted"


def compose_pr_link_comment(pr_url: str, *, parent_pr: int | None = None) -> str:
    """The comment text: the PR link, plus the stacked-draft note when known."""
    text = f"PR: {pr_url}\n"
    if parent_pr is not None:
        text += f"\nCreated as draft — stacked behind PR #{parent_pr}\n"
    return text


def _already_posted(task: TicketTask, client: ClientConfig, pr_url: str) -> bool:
    comments = fetch_issue_comments(
        task.ticket_id, FETCH_COMMENTS_TIMEOUT, cwd=client.workspace_path
    )
    if not comments:
        return False
    return any(pr_url in str(comment.get("body") or "") for comment in comments)


def _skip(task: TicketTask, client: ClientConfig, sid: str, reason: str) -> None:
    tracker, _github = tracker_label(client)
    emit_verdict_comment_skipped(
        ticket_id=task.ticket_id,
        client=client.name,
        tracker=tracker,
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
    runner: CodexRunner,
    pr_url: str,
    plan_source: str,
) -> None:
    """Post the PR link to the ticket, best-effort; never raises a stage fault."""
    try:
        if plan_source == FREE_TEXT_PLAN_SOURCE:
            _skip(task, client, sid, SKIP_FREE_TEXT)
            return
        _tracker, github = tracker_label(client)
        if github and _already_posted(task, client, pr_url):
            _skip(task, client, sid, SKIP_ALREADY_POSTED)
            return
        post_stage_comment(
            task=task,
            client=client,
            worktree=worktree,
            sid=sid,
            runner=runner,
            text=compose_pr_link_comment(pr_url),
            artifact_path=None,
            kind=PR_LINK_KIND,
        )
    except STAGE_FAULTS:
        _log.exception(
            "pr_link comment post failed ticket=%s session=%s", task.ticket_id, sid
        )
