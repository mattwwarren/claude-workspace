"""The per-queue-ticket review-debt roll-up issue (one issue, many findings).

A queue ticket's deferred findings share one ``review-debt`` issue instead of
one issue each. The body keeps the legacy first line (``Deferred during review
of #N on branch ...``) so the scope check of :func:`_marker.body_matches` and
:func:`_marker.queue_ticket_ref` read a roll-up exactly as they read a legacy
per-finding issue. Each finding is one checklist item carrying its own
``<!-- cw-review-debt: <key> -->`` marker line, so a key is found in a roll-up
or a legacy issue by the same match, and a checked-off item stays listed (its
marker is still there), so it is never re-added.

A body stays at or under :data:`ROLLUP_BODY_MAX_CHARS`; :func:`plan_groups`
packs findings into the last open part and then into new continuation parts,
and never splits or drops a finding. Free text passes through the shared post
redaction; marker lines are added after redaction so it cannot alter them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.gh import AGENT_COMMENT_MARKER
from cw.review_debt_filing._constants import (
    EVIDENCE_PLACEHOLDER,
    MUST_FIX_SEVERITY,
    PR_MARKER_PREFIX,
    ROLLUP_BODY_MAX_CHARS,
    ROLLUP_EVIDENCE_MAX_CHARS,
    ROLLUP_FIELD_MAX_CHARS,
    ROLLUP_MARKER_PREFIX,
    ROLLUP_PART_KEY,
)
from cw.review_debt_filing._marker import (
    _marker_values,
    debt_marker,
    scope_prefix,
)
from cw.review_debt_filing._render import _fence, redact

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_debt_filing._models import DebtEntry, FilingContext

_INDENT = "  "
_ELLIPSIS = "..."
_EVIDENCE_TRUNCATED = "(evidence truncated)"
_FIRST_PART = 1


class RollupItem(NamedTuple):
    """One rendered checklist item and the entry it lists."""

    entry: DebtEntry
    text: str


class RollupGroup(NamedTuple):
    """The items bound for one roll-up issue.

    ``number`` is the existing issue to append to, or None for a new part.
    ``body`` is the existing body (empty for a new part) and ``part`` the part
    number the issue carries.
    """

    number: int | None
    part: int
    body: str
    items: list[RollupItem]


def _one_line(text: str, limit: int) -> str:
    """*text* on one line, capped at *limit* characters."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _neutral(text: str) -> str:
    """*text* with every HTML comment opener broken.

    Free text is quoted from a review and must never be able to forge a marker
    line (a forged key marker would hide a real finding from dedup).
    """
    return text.replace("<!--", "<! --")


def _evidence_block(entry: DebtEntry, ctx: FilingContext) -> list[str]:
    """The fenced, indented evidence lines of one item."""
    evidence = entry.evidence if entry.evidence.strip() else EVIDENCE_PLACEHOLDER
    truncated = len(evidence) > ROLLUP_EVIDENCE_MAX_CHARS
    evidence = _neutral(redact(evidence[:ROLLUP_EVIDENCE_MAX_CHARS], ctx)).rstrip("\n")
    fence = _fence(evidence)
    body = [f"{_INDENT}{fence}text"]
    body += [f"{_INDENT}{line}" if line else "" for line in evidence.splitlines()]
    if truncated:
        body.append(f"{_INDENT}{_EVIDENCE_TRUNCATED}")
    body.append(f"{_INDENT}{fence}")
    return body


def render_item(entry: DebtEntry, ctx: FilingContext) -> RollupItem:
    """The checklist item for *entry*: head line, detail, key marker, evidence."""
    location = f"`{entry.file}`" + (f":{entry.line}" if entry.line is not None else "")
    summary = _one_line(entry.summary, ROLLUP_FIELD_MAX_CHARS)
    visible = [
        f"- [ ] **{entry.severity}** {location} - {summary}",
        f"{_INDENT}- **Why deferred:** "
        f"{_one_line(entry.rationale, ROLLUP_FIELD_MAX_CHARS)}",
    ]
    if entry.rule_id:
        visible.append(f"{_INDENT}- **Rule:** {_one_line(entry.rule_id, 200)}")
    head = _neutral(redact("\n".join(visible), ctx))
    lines = [head, f"{_INDENT}{debt_marker(entry.key)}", *_evidence_block(entry, ctx)]
    return RollupItem(entry, "\n".join(lines) + "\n")


def rollup_marker(part: int) -> str:
    """``<!-- cw-review-debt-rollup: part=<n> -->``."""
    return f"{ROLLUP_MARKER_PREFIX}{ROLLUP_PART_KEY}{part} -->"


def rollup_part(body: str) -> int | None:
    """The part number *body*'s roll-up marker names, or None for a legacy issue."""
    for value in _marker_values(body, ROLLUP_MARKER_PREFIX):
        number = value.removeprefix(ROLLUP_PART_KEY)
        if value.startswith(ROLLUP_PART_KEY) and number.isascii() and number.isdigit():
            return int(number) or None
    return None


def is_rollup(body: str) -> bool:
    """True when *body* is a roll-up issue body."""
    return rollup_part(body) is not None


def scoped_rollup(body: str, ticket_ref: str) -> int | None:
    """The part number when *body* is a roll-up for *ticket_ref*, else None."""
    lines = body.splitlines()
    if not lines or not lines[0].startswith(scope_prefix(ticket_ref)):
        return None
    return rollup_part(body)


def _header(ctx: FilingContext, part: int) -> str:
    lines = [
        f"{scope_prefix(ctx.ticket_ref)} `{ctx.branch}`.",
        "",
        rollup_marker(part),
        "",
        f"Deferred review findings for {ctx.ticket_ref} (part {part}). Each item is "
        "one finding; check it off when it is fixed or dismissed. cw never "
        "re-adds an item, checked or not, and appends later findings for "
        f"{ctx.ticket_ref} below.",
        "",
    ]
    return redact("\n".join(lines), ctx) + "\n"


def _tail() -> str:
    return f"{AGENT_COMMENT_MARKER}\n"


def render_body(items: Sequence[RollupItem], ctx: FilingContext, part: int) -> str:
    """A new roll-up body: header, every item, the agent marker."""
    added = "".join(item.text for item in items)
    return f"{_header(ctx, part)}{added}\n{_tail()}"


def _is_tail_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped == AGENT_COMMENT_MARKER
        or stripped.startswith(PR_MARKER_PREFIX)
    )


def append_items(body: str, items: Sequence[RollupItem]) -> str:
    """*body* with *items* inserted before its trailing marker block.

    The trailing block is the run of blank, PR-link and agent-marker lines at
    the end of the body, kept verbatim after the new items.
    """
    lines = body.splitlines()
    cut = len(lines)
    while cut > 0 and _is_tail_line(lines[cut - 1]):
        cut -= 1
    head = "\n".join(lines[:cut]).rstrip("\n")
    tail = "\n".join(lines[cut:]).strip("\n")
    added = "".join(item.text for item in items)
    tail_text = f"\n{tail}\n" if tail else ""
    return f"{head}\n\n{added}{tail_text}" if head else f"{added}{tail_text}"


def plan_groups(
    items: Sequence[RollupItem],
    ctx: FilingContext,
    target: tuple[int, int, str] | None,
) -> list[RollupGroup]:
    """Pack *items* into the target roll-up, then continuation parts.

    *target* is ``(number, part, body)`` of the newest open roll-up for the
    queue ticket, or None. Every group gets at least one item when it is new,
    so packing always terminates, and no item is split or dropped. An existing
    target that cannot take the next item is left alone and a new part opens.
    """
    groups: list[RollupGroup] = []
    if target is not None:
        number, part, body = target
        current = RollupGroup(number, part, body, [])
        size = len(body)
    else:
        part = _FIRST_PART
        current = RollupGroup(None, part, "", [])
        size = len(_header(ctx, part)) + len(_tail())
    for item in items:
        fits = size + len(item.text) <= ROLLUP_BODY_MAX_CHARS
        if not fits and (current.items or current.number is not None):
            if current.items:
                groups.append(current)
            part = current.part + 1
            current = RollupGroup(None, part, "", [])
            size = len(_header(ctx, part)) + len(_tail())
        current.items.append(item)
        size += len(item.text)
    if current.items:
        groups.append(current)
    return groups


def group_title(group: RollupGroup, ctx: FilingContext) -> str:
    """The title of a new roll-up issue, counting *group*'s findings.

    ``review-debt: #3060 \u2014 52 deferred findings (3 MUST_FIX)``; a continuation
    adds ``part <n>``. The count is the number of findings the issue was
    created with: appends do not rewrite the title.
    """
    total = len(group.items)
    must = sum(1 for item in group.items if item.entry.severity == MUST_FIX_SEVERITY)
    noun = "deferred finding" if total == 1 else "deferred findings"
    label = f"{total} {noun}" + (f" ({must} {MUST_FIX_SEVERITY})" if must else "")
    part = f"part {group.part}, " if group.part > _FIRST_PART else ""
    return redact(f"review-debt: {ctx.ticket_ref} \u2014 {part}{label}", ctx)
