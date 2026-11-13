"""Best-effort ticket-comment post shared by the codex stage executors (#3062).

Extracted from ``cw.codex_plan._post`` so the codex PLAN lane (the plan and
park comments, ``kind="plan"``) and the codex FINALIZE lane (the PR-link
comment, ``kind="pr_link"``) post through one implementation.

The post is best-effort and never retried. A refusal or an unconfirmed post
is an event, never a row hold. The events are the three verdict-comment
events with the caller's ``kind`` and ``reviewed_sha: null``.

* GitHub (and an unset tracker): the text is redacted and split with
  ``plan_turn_parts(marked=False)`` and each part posted with
  ``post_parts``, which appends the provenance marker once. A failed part
  stops the post, so the parts that did post stay in order.
* Any other tracker: one A2 tracker-IO turn (``post_parts_via_turn``),
  classified through the shared ``surface_tracker_io_outcome``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from cw.codex_review_signals import (
    VerdictCommentIdentity,
    VerdictCommentKind,
    emit_verdict_comment_posted,
    emit_verdict_comment_skipped,
    emit_verdict_comment_unverified,
    surface_tracker_io_outcome,
)
from cw.codex_runner import relabeled
from cw.codex_stage_completion import STAGE_FAULTS
from cw.gh import agent_marked_body
from cw.models.usage import UsageStage
from cw.tracker import TRACKER_GITHUB_ISSUES, TrackerClass, tracker_kind
from cw.tracker_io import (
    PublicState,
    TrackerIoContext,
    TrackerIoReason,
    plan_turn_parts,
    post_parts_via_turn,
)
from cw.tracker_post import PostStatus, post_parts

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

#: The skipped reason when a GitHub ``gh issue comment`` part fails.
GITHUB_POST_FAILED: Final = "github_post_failed"


def tracker_label(client: ClientConfig) -> tuple[str, bool]:
    """The tracker string the events carry, and whether the GitHub path applies."""
    kind = tracker_kind(client.workspace_path)
    if kind.tracker_class is TrackerClass.OTHER and kind.system is not None:
        return kind.system, False
    return kind.system or TRACKER_GITHUB_ISSUES, True


class StagePost:
    """One stage comment post's identity, shared by its events."""

    def __init__(
        self,
        *,
        task: TicketTask,
        client: ClientConfig,
        tracker: str,
        artifact_path: Path | None,
        sid: str,
        kind: VerdictCommentKind,
    ) -> None:
        self.ticket_id = task.ticket_id
        self.client = client.name
        self.tracker = tracker
        self.artifact_path = artifact_path
        self.sid = sid
        self.kind: VerdictCommentKind = kind

    def skipped(self, reason: str) -> None:
        """Record a post that did not land (no part became public)."""
        _log.warning(
            "%s_comment_skipped ticket=%s tracker=%s reason=%s: durable copy at %s",
            self.kind,
            self.ticket_id,
            self.tracker,
            reason,
            self.artifact_path,
        )
        emit_verdict_comment_skipped(
            ticket_id=self.ticket_id,
            client=self.client,
            tracker=self.tracker,
            kind=self.kind,
            artifact_path=self.artifact_path,
            reviewed_sha=None,
            session_id=self.sid,
            reason=reason,
        )

    def posted(self, posted: list[dict[str, object]], bodies: Sequence[str]) -> None:
        """Record a confirmed post; *bodies* are the marked, redacted parts as sent."""
        emit_verdict_comment_posted(
            ticket_id=self.ticket_id,
            client=self.client,
            tracker=self.tracker,
            kind=self.kind,
            artifact_path=self.artifact_path,
            reviewed_sha=None,
            session_id=self.sid,
            posted=posted,
            bodies=bodies,
        )

    def unverified(self, posted: list[dict[str, object]]) -> None:
        """Record a partially public GitHub post without losing its parts."""
        emit_verdict_comment_unverified(
            ticket_id=self.ticket_id,
            client=self.client,
            tracker=self.tracker,
            kind=self.kind,
            reason=GITHUB_POST_FAILED,
            artifact_path=self.artifact_path,
            reviewed_sha=None,
            session_id=self.sid,
            public_state=PublicState.POSSIBLE.value,
            posted=posted,
            marker_survived=None,
        )

    @property
    def identity(self) -> VerdictCommentIdentity:
        """The identity the tracker-IO outcome classifier records."""
        return VerdictCommentIdentity(
            client=self.client,
            session_id=self.sid,
            kind=self.kind,
            reviewed_sha=None,
            artifact_path=self.artifact_path,
        )


def post_github(post: StagePost, text: str, context: TrackerIoContext) -> None:
    """Post *text* to a GitHub issue through ``post_parts``."""
    try:
        parts = plan_turn_parts(text, context, marked=False)
    except ValueError:
        _log.warning("%s_comment_plan_refused ticket=%s", post.kind, post.ticket_id)
        parts = ()
    if not parts:
        post.skipped(TrackerIoReason.PLAN_REFUSED.value)
        return
    result = post_parts(post.ticket_id, parts, cwd=context.client.workspace_path)
    posted = [
        {
            "index": part.index,
            "comment_id": part.url.rsplit("-", 1)[-1],
            "url": part.url,
        }
        for part in result.posted
    ]
    if result.status is PostStatus.REFUSED:
        post.skipped(GITHUB_POST_FAILED)
    elif result.status is PostStatus.PARTIAL:
        # Some parts are public, or delivery is otherwise ambiguous.  An
        # unknown first-part outcome remains ambiguous even when no URL was
        # returned, because the comment may have landed.
        post.unverified(posted)
    else:
        post.posted(posted, tuple(agent_marked_body(part) for part in parts))


def post_other(post: StagePost, text: str, context: TrackerIoContext) -> None:
    """Post *text* through one A2 tracker-IO turn (a non-GitHub tracker)."""
    outcome = post_parts_via_turn(
        post.ticket_id, text, tracker=post.tracker, context=context, session_id=post.sid
    )
    surface_tracker_io_outcome(
        post.ticket_id,
        tracker=post.tracker,
        outcome=outcome,
        identity=post.identity,
    )


def post_stage_comment(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    runner: CodexRunner,
    text: str,
    artifact_path: Path | None,
    kind: VerdictCommentKind,
) -> None:
    """Post *text* to the ticket, best-effort; never raises a stage fault.

    *artifact_path* is the durable copy the events name (``None`` when the
    file is absent). A fault here must not escape: the result has already
    persisted through the door, so an escape would revert the claimed row.
    """
    try:
        tracker, github = tracker_label(client)
        post = StagePost(
            task=task,
            client=client,
            tracker=tracker,
            artifact_path=artifact_path
            if artifact_path is not None and artifact_path.is_file()
            else None,
            sid=sid,
            kind=kind,
        )
        context = TrackerIoContext(
            runner=relabeled(runner, UsageStage.TRACKER_IO),
            client=client,
            worktree=worktree,
        )
        if github:
            post_github(post, text, context)
        else:
            post_other(post, text, context)
    except STAGE_FAULTS:
        _log.exception(
            "%s comment post failed ticket=%s session=%s", kind, task.ticket_id, sid
        )
