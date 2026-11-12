"""Post the codex PLAN comment after the result persists (#3060 R9).

A completing exit posts the stamped ``.cw/plan.md`` (both sign-off markers on
its first lines) as the plan of record; a park exit posts the consolidated
park comment (:func:`render_park_comment`), never the draft as a plan of
record. A blocked exit and ``no_op`` post nothing in C1.

The post itself is the shared best-effort stage post in
:mod:`cw.codex_stage_post` (#3062), with ``kind: "plan"``: a refusal or an
unconfirmed post is an event, never a row hold, and ``.cw/plan.md`` (or
``.cw/plan-draft.md``) is the durable copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.codex_stage_post import GITHUB_POST_FAILED, post_stage_comment

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask

__all__ = [
    "GITHUB_POST_FAILED",
    "PARK_HEADER",
    "PLAN_COMMENT_KIND",
    "post_plan_comment",
    "render_park_comment",
]

#: The verdict-comment event kind used by the codex PLAN lane.
PLAN_COMMENT_KIND: Final = "plan"

PARK_HEADER: Final = "## Pending Verification Scan"


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
    if result.scope.tier == SCOPE_TIER_LARGE:
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
    file is absent). A thin wrapper over
    :func:`cw.codex_stage_post.post_stage_comment` with ``kind="plan"``.
    """
    post_stage_comment(
        task=task,
        client=client,
        worktree=worktree,
        sid=sid,
        runner=runner,
        text=text,
        artifact_path=artifact_path,
        kind=PLAN_COMMENT_KIND,
    )
