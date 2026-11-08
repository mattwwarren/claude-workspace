"""Title, body and emit payload for one review-debt ticket (#3041, R8).

Every piece of free text (title, summary, rationale, evidence, file) passes
through :func:`cw.redact_post_text.redact_post_text` with the run's anchors.
The two marker lines are appended after redaction so it can never alter them.
The evidence is always fenced, because III3 (#3045) makes this body a worker
spec: a fence one tick longer than any backtick run inside the evidence keeps
the evidence from closing it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cw.gh import AGENT_COMMENT_MARKER
from cw.redact_post_text import redact_post_text
from cw.review_debt_filing._constants import (
    DETAIL_MAX_CHARS,
    EVIDENCE_PLACEHOLDER,
    MIN_FENCE_TICKS,
    STDERR_SUMMARY_MAX_CHARS,
    TITLE_SUMMARY_MAX_CHARS,
)
from cw.review_debt_filing._marker import debt_marker, scope_prefix

if TYPE_CHECKING:
    from cw.review_debt_filing._models import DebtEntry, FilingContext

_BACKTICK_RUN_RE = re.compile(r"`+")


def redact(text: str, ctx: FilingContext) -> str:
    """*text* through the shared post redaction, with *ctx*'s anchors."""
    return redact_post_text(text, home=ctx.home, worktrees=ctx.worktrees)


def redact_detail(text: str, ctx: FilingContext) -> str:
    """A redacted failure detail, capped at :data:`DETAIL_MAX_CHARS`."""
    return redact(text, ctx)[:DETAIL_MAX_CHARS]


def render_title(entry: DebtEntry, ctx: FilingContext) -> str:
    """``review-debt: <severity> <summary truncated to 80>``, redacted."""
    summary = " ".join(entry.summary.split())[:TITLE_SUMMARY_MAX_CHARS]
    return redact(f"review-debt: {entry.severity} {summary}", ctx)


def _fence(evidence: str) -> str:
    """A backtick fence longer than any backtick run in *evidence*."""
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(evidence)), default=0)
    return "`" * max(MIN_FENCE_TICKS, longest + 1)


def render_body(entry: DebtEntry, ctx: FilingContext) -> str:
    """The R8 body template, redacted, ending with the two marker lines."""
    location = f"`{entry.file}`" + (f":{entry.line}" if entry.line is not None else "")
    lines = [
        f"{scope_prefix(ctx.ticket_ref)} `{ctx.branch}`.",
        "",
        f"**Severity:** {entry.severity}",
        f"**File:** {location}",
    ]
    if entry.rule_id:
        lines.append(f"**Rule:** {entry.rule_id}")
    evidence = entry.evidence if entry.evidence.strip() else EVIDENCE_PLACEHOLDER
    fence = _fence(evidence)
    lines += [
        f"**Why deferred:** {entry.rationale}",
        "",
        entry.summary,
        "",
        f"{fence}text",
        evidence.rstrip("\n"),
        fence,
    ]
    content = redact("\n".join(lines), ctx)
    return f"{content}\n\n{debt_marker(entry.key)}\n{AGENT_COMMENT_MARKER}\n"


def render_payload(entry: DebtEntry, ctx: FilingContext) -> dict[str, str]:
    """The ``cw review debt emit`` payload for *entry*."""
    return {
        "key": entry.key,
        "marker": debt_marker(entry.key),
        "title": render_title(entry, ctx),
        "body": render_body(entry, ctx),
    }


def stderr_summary(entry: DebtEntry, ctx: FilingContext) -> str:
    """The redacted summary shown in a per-failure stderr line, capped."""
    return redact(" ".join(entry.summary.split()), ctx)[:STDERR_SUMMARY_MAX_CHARS]
