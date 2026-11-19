"""Best-effort outbox enqueue of a codex stage comment (#3062, RFC 0019 O3 #3707).

Extracted from ``cw.codex_plan._post`` so the codex PLAN lane (the plan and
park comments) and the codex FINALIZE lane (the PR-link comment) enqueue through
one implementation.

The enqueue is best-effort and never retried. A refused plan, an oversize
body, or a failed or duplicate enqueue is one ``review.verdict_comment_skipped``
event, never a row hold. The text is redacted and split by
:func:`cw.outbox_prose_plan.plan_prose_parts`; the parts are stored verbatim
(post-redaction, without the agent provenance marker, which the drain appends).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.codex_review_signals import (
    VerdictCommentKind,
    emit_verdict_comment_skipped,
    tracker_label,
)
from cw.codex_stage_completion import STAGE_FAULTS
from cw.outbox_producer import (
    SKIP_ENQUEUE_FAILED,
    enqueue_prose,
    try_content_marker,
)
from cw.outbox_prose_plan import plan_prose_parts

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

#: The skipped reason when the planner refuses the text (or it yields no parts).
PLAN_REFUSED: str = "plan_refused"


class StagePost:
    """One stage comment's identity, shared by its skipped event and log line."""

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

    def skipped(self, reason: str, entry_id: str | None = None) -> None:
        """Record a comment that enqueued nothing (producer-side non-enqueue)."""
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
            entry_id=entry_id,
        )


def _plan_or_skip(
    post: StagePost, text: str, client: ClientConfig, worktree: Path
) -> tuple[tuple[str, ...], bool] | None:
    """The redacted parts and their ``redacted`` flag, or ``None`` after a skip."""
    try:
        plan = plan_prose_parts(text, client, worktree)
    except ValueError:
        _log.warning("%s_comment_plan_refused ticket=%s", post.kind, post.ticket_id)
        post.skipped(PLAN_REFUSED)
        return None
    if not plan.bodies:
        post.skipped(PLAN_REFUSED)
        return None
    return plan.bodies, plan.redacted


def _enqueue_or_skip(
    post: StagePost,
    *,
    client: ClientConfig,
    task: TicketTask,
    producer: str,
    marker: str,
    parts: tuple[str, ...],
    redacted: bool,
) -> None:
    outcome = enqueue_prose(
        client.name,
        task.ticket_id,
        producer,
        marker,
        parts,
        redacted=redacted,
    )
    if outcome.skipped_reason is not None:
        post.skipped(outcome.skipped_reason, outcome.entry_id)


def post_stage_comment(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    text: str,
    artifact_path: Path | None,
    kind: VerdictCommentKind,
    producer: str,
    marker_prefix: str,
    marker_source: str | None = None,
) -> None:
    """Enqueue *text* as a ticket comment, best-effort; never raises a stage fault.

    *artifact_path* is the durable copy the events name (``None`` when the
    file is absent). The marker is ``<marker_prefix>-<sha256(marker_source)[:16]>``,
    where *marker_source* defaults to the joined parts. A fault here must not
    escape: the result has already persisted through the door.
    """
    try:
        post = StagePost(
            task=task,
            client=client,
            tracker=tracker_label(client),
            artifact_path=artifact_path
            if artifact_path is not None and artifact_path.is_file()
            else None,
            sid=sid,
            kind=kind,
        )
        planned = _plan_or_skip(post, text, client, worktree)
        if planned is None:
            return
        parts, redacted = planned
        source = marker_source if marker_source is not None else "".join(parts)
        marker = try_content_marker(marker_prefix, source)
        if marker is None:
            post.skipped(SKIP_ENQUEUE_FAILED)
            return
        _enqueue_or_skip(
            post,
            client=client,
            task=task,
            producer=producer,
            marker=marker,
            parts=parts,
            redacted=redacted,
        )
    except STAGE_FAULTS:
        _log.exception(
            "%s comment enqueue failed ticket=%s session=%s", kind, task.ticket_id, sid
        )
