"""Post the codex PLAN comment after the result persists (#3060 R9).

A completing exit posts the stamped ``.cw/plan.md`` (both sign-off markers on
its first lines) as the plan of record; a park exit posts the consolidated
park comment (:func:`render_park_comment`), never the draft as a plan of
record. A blocked exit and ``no_op`` post nothing in C1.

The post is best-effort and never retried. A refusal or an unconfirmed post is
an event, never a row hold: ``.cw/plan.md`` (or ``.cw/plan-draft.md``) is the
durable copy. The events are the three existing verdict-comment events with
``kind: "plan"`` and ``reviewed_sha: null``.

* GitHub (and an unset tracker): the text is redacted and split with
  ``plan_turn_parts(marked=False)`` and each part posted with
  ``post_issue_comment``, which appends the provenance marker once. A failed
  part stops the post, so the parts that did post stay in order.
* Any other tracker: one A2 tracker-IO turn (``post_parts_via_turn``).

Tracker-IO outcome classification is shared with the REVIEW post through the
neutral signal helper; PLAN supplies its own comment identity and artifact.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.codex_plan._result import PARK_STATUSES
from cw.codex_review_signals import (
    VerdictCommentIdentity,
    emit_verdict_comment_posted,
    emit_verdict_comment_skipped,
    surface_tracker_io_outcome,
)
from cw.codex_stage_completion import STAGE_FAULTS
from cw.gh import post_issue_comment
from cw.tracker import TRACKER_GITHUB_ISSUES, TrackerClass, tracker_kind
from cw.tracker_io import (
    TrackerIoContext,
    TrackerIoReason,
    plan_turn_parts,
    post_parts_via_turn,
)
from cw.tracker_post import parse_comment_url

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

#: The skipped reason when a GitHub ``gh issue comment`` part fails.
GITHUB_POST_FAILED: Final = "github_post_failed"

PARK_HEADER: Final = "## Pending Verification Scan"
_COMMENT_ID_RE: Final = re.compile(r"issuecomment-([0-9]+)")


def _item_lines(number: int, item: dict[str, Any], lead_keys: Sequence[str]) -> str:
    lead = next(
        (str(item[key]) for key in lead_keys if isinstance(item.get(key), str)),
        "(no text)",
    )
    lines = [f"{number}. {lead}"]
    lines.extend(
        f"   - {key}: {value}"
        for key, value in item.items()
        if key not in lead_keys and value not in (None, "", [], {})
    )
    return "\n".join(lines)


def _scan_items(result: AutoDevResult) -> str:
    items = [
        _item_lines(i, a, ("question",)) for i, a in enumerate(result.ambiguities, 1)
    ]
    offset = len(items)
    items.extend(
        _item_lines(offset + i, p, ("claim", "premise"))
        for i, p in enumerate(result.premises, 1)
    )
    return "\n".join(items) if items else "No open ambiguities or premises."


def _how_to_answer(ticket_id: str, client_name: str) -> str:
    return (
        "### How to answer\n\n"
        f"Run `cw dev-queue resolve {ticket_id} -c {client_name}` to answer each "
        "numbered item above on the dev-queue row (no tracker comment needed). "
        "An operator Pre-flight Resolutions comment is still read by the next "
        "plan turn for the numbered items, but on the codex PLAN lane it "
        "carries no resolutions authority."
    )


def _approval_requested(
    ticket_id: str,
    client_name: str,
    current: str | None,
    approved: str | None,
    *,
    mismatch: bool,
) -> str:
    revoked = (
        "The earlier approval was revoked by a **fingerprint mismatch**: the "
        "draft changed after it was approved.\n\n"
        if mismatch
        else ""
    )
    return (
        "### Approval requested\n\n"
        f"{revoked}This plan is classified Large. Run "
        f"`cw dev-queue approve {ticket_id} -c {client_name}`: it stamps "
        "`plan_approved_at` and `plan_approved_fingerprint` on the dev-queue "
        "row, and the approval is bound to this draft. If the draft changes "
        "before the next dispatch, the approval no longer matches and the "
        "plan parks again. Comment-path approval is not available on the "
        "codex PLAN lane.\n\n"
        f"- current draft fingerprint: `{current or 'none'}`\n"
        f"- approved fingerprint: `{approved or 'none'}`"
    )


def render_park_comment(
    *,
    ticket_id: str,
    client_name: str,
    result: AutoDevResult,
    draft_body: str,
    approved_fingerprint: str | None,
    advisory_findings: Sequence[str],
    mismatch: bool = False,
) -> str:
    """The consolidated park comment for a park exit.

    Sections, in order: the numbered open items, ``### How to answer`` (when
    an item is open), the advisory station findings (when any), ``###
    Approval requested`` (Large tier) and the draft. It never prints the
    pre-flight resolutions marker literal.
    """
    sections = [PARK_HEADER, _scan_items(result)]
    if result.ambiguities or result.premises:
        sections.append(_how_to_answer(ticket_id, client_name))
    if advisory_findings:
        sections.append(
            "### Advisory plan-review findings (address in the same round)\n\n"
            + "\n".join(advisory_findings)
        )
    if result.scope.tier == SCOPE_TIER_LARGE or result.status in PARK_STATUSES:
        sections.append(
            _approval_requested(
                ticket_id,
                client_name,
                result.plan_draft_fingerprint,
                approved_fingerprint,
                mismatch=mismatch,
            )
        )
    sections.append(
        "### Draft plan (unreviewed — context only)\n\n" + draft_body.rstrip()
    )
    return "\n\n".join(sections) + "\n"


def _tracker_label(client: ClientConfig) -> tuple[str, bool]:
    """The tracker string the events carry, and whether the GitHub path applies."""
    kind = tracker_kind(client.workspace_path)
    if kind.tracker_class is TrackerClass.OTHER and kind.system is not None:
        return kind.system, False
    return kind.system or TRACKER_GITHUB_ISSUES, True


def _posted_entry(index: int, url: str) -> dict[str, object]:
    match = _COMMENT_ID_RE.search(url)
    return {"index": index, "comment_id": match.group(1) if match else "", "url": url}


class _Post:
    """One plan post's identity, shared by its events."""

    def __init__(
        self,
        *,
        task: TicketTask,
        client: ClientConfig,
        tracker: str,
        artifact_path: Path | None,
        sid: str,
    ) -> None:
        self.ticket_id = task.ticket_id
        self.client = client.name
        self.tracker = tracker
        self.artifact_path = artifact_path
        self.sid = sid

    def skipped(self, reason: str) -> None:
        _log.warning(
            "plan_comment_skipped ticket=%s tracker=%s reason=%s: durable copy at %s",
            self.ticket_id,
            self.tracker,
            reason,
            self.artifact_path,
        )
        emit_verdict_comment_skipped(
            ticket_id=self.ticket_id,
            client=self.client,
            tracker=self.tracker,
            kind="plan",
            artifact_path=self.artifact_path,
            reviewed_sha=None,
            session_id=self.sid,
            reason=reason,
        )

    def posted(self, posted: list[dict[str, object]], content: str) -> None:
        emit_verdict_comment_posted(
            ticket_id=self.ticket_id,
            client=self.client,
            tracker=self.tracker,
            kind="plan",
            artifact_path=self.artifact_path,
            reviewed_sha=None,
            session_id=self.sid,
            posted=posted,
            content=content,
        )

    @property
    def identity(self) -> VerdictCommentIdentity:
        return VerdictCommentIdentity(
            client=self.client,
            session_id=self.sid,
            kind="plan",
            reviewed_sha=None,
            artifact_path=self.artifact_path,
        )


def _post_github(post: _Post, text: str, context: TrackerIoContext) -> None:
    try:
        parts = plan_turn_parts(text, context, marked=False)
    except ValueError:
        _log.warning("plan_comment_plan_refused ticket=%s", post.ticket_id)
        parts = ()
    if not parts:
        post.skipped(TrackerIoReason.PLAN_REFUSED.value)
        return
    posted: list[dict[str, object]] = []
    for index, part in enumerate(parts, 1):
        proc = post_issue_comment(
            post.ticket_id, part, cwd=context.client.workspace_path
        )
        if proc is None or proc.returncode != 0:
            post.skipped(GITHUB_POST_FAILED)
            return
        url = parse_comment_url(proc.stdout)
        if url is not None:
            posted.append(_posted_entry(index, url))
    post.posted(posted, text)


def _post_other(post: _Post, text: str, context: TrackerIoContext) -> None:
    outcome = post_parts_via_turn(
        post.ticket_id, text, tracker=post.tracker, context=context, session_id=post.sid
    )
    surface_tracker_io_outcome(
        post.ticket_id,
        text,
        tracker=post.tracker,
        outcome=outcome,
        identity=post.identity,
    )


def post_plan_comment(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    runner: CodexRunner,
    text: str,
    artifact_path: Path | None,
) -> None:
    """Post *text* to the ticket, best-effort; never raises a stage fault.

    *artifact_path* is the durable copy the events name (``None`` when the
    file is absent). A fault here must not escape: the result has already
    persisted through the door, so an escape would revert the claimed row.
    """
    try:
        tracker, github = _tracker_label(client)
        post = _Post(
            task=task,
            client=client,
            tracker=tracker,
            artifact_path=artifact_path
            if artifact_path is not None and artifact_path.is_file()
            else None,
            sid=sid,
        )
        context = TrackerIoContext(runner=runner, client=client, worktree=worktree)
        if github:
            _post_github(post, text, context)
        else:
            _post_other(post, text, context)
    except STAGE_FAULTS:
        _log.exception(
            "plan comment post failed ticket=%s session=%s", task.ticket_id, sid
        )
