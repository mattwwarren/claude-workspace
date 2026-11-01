"""The settle-payload section of a blocking verdict comment (#2210).

The one section that is not purely a report: it hands the orchestrator a
ready-to-paste ``cw review settle`` payload per blocking finding, which is the
producer half of the cross-round adjudication ledger's first real write path.

Tests that shrink the section budget patch ``_SETTLE_SECTION_BUDGET_CHARS`` on
THIS module: :func:`_render_settle_payloads` reads its own module's global, so
it is deliberately not re-exported from the package.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from cw.codex_review._verdict._render._common import _safe, _truncate
from cw.review_debt import fingerprint_v1
from cw.review_markers import SETTLE_SECTION_HEADING, escape_angle_brackets_in_json

if TYPE_CHECKING:
    from cw.review_findings import Finding, ReviewVerdict

# The fence the settle payloads render in, widened past any backtick run in
# the body so a summary containing ``` cannot break out of its own block.
_MIN_FENCE = 3
_BACKTICK_RUN_RE = re.compile(r"`+")

_SETTLE_INTRO = (
    "Each payload below records one blocking finding as settled. Save one to "
    "a file and run `cw review settle <file> --reason '<why>' --ticket "
    "<ticket> -c <client> --out settle.md` from the orchestrator session "
    "(anywhere outside a headless dispatch worker). That records the "
    "decision on the cw dev-queue row, which the next review pass reads "
    "whatever the ticket's tracker is. `settle.md` is the human-readable "
    "mirror of it: post it as its own comment, unedited, with your tracker's "
    "own tool (on a GitHub-tracked ticket the codex lane also reads it back, "
    "and only when it opens the comment body). "
    "`file`, `summary` and `reviewed_sha` are the finding's identity and "
    "provenance, copied verbatim, so nothing needs editing; put your "
    "reasoning in `--reason` (or a per-entry `rationale`), or set `outcome` "
    "to `ACCEPTED` if you uphold the finding. The command refuses to run "
    "inside a headless dispatch worker — a settled finding is never "
    "re-raised, so the pipeline must not be able to settle its own "
    "reviewer's findings."
)

# Hard caps on the settle section. A blocking pass with many MUST_FIX findings
# would otherwise carry one JSON payload each, and GitHub rejects a comment
# body over 65,536 characters -- the rest of this comment (findings, debt,
# rejected sections) needs the remainder. Past either cap the remaining
# findings are named compactly and the operator is pointed at `cw review
# settle`; a JSON block is NEVER truncated mid-structure, because half a
# payload pastes into something that half-parses.
_SETTLE_MAX_PAYLOADS = 10
_SETTLE_SECTION_BUDGET_CHARS = 12_000
# The compact overflow list is bounded independently of the byte budget above
# (it is what the budget overflows INTO, so it cannot be charged to it): at
# most this many rows, each summary trimmed, then a counted residue line.
_SETTLE_MAX_COMPACT_ROWS = 40
_SETTLE_COMPACT_SUMMARY_MAX = 120
_SETTLE_OVERFLOW_NOTE = (
    "Not enough room for the remaining findings' payloads. Settle any of "
    "these by writing a payload by hand and passing it to `cw review settle "
    "<file> --reason '<why>' --ticket <ticket> -c <client>` — the identity "
    "is the file and the verbatim summary from its MUST_FIX line above:"
)


def _settle_fence(body: str) -> str:
    """A code fence guaranteed to be longer than any backtick run in *body*."""
    runs = _BACKTICK_RUN_RE.findall(body)
    longest = max((len(run) for run in runs), default=0)
    return "`" * max(_MIN_FENCE, longest + 1)


def _settleable_findings(verdict: ReviewVerdict) -> list[Finding]:
    """The blocking findings that can be keyed, one per ledger key (#2210).

    Deduplicated on the VERBATIM ``(file, summary)`` because that is what the
    ledger key now binds (round 3): two findings whose summaries only
    normalize alike are two records, and collapsing them here would leave the
    second with no payload to settle it by.
    """
    seen: set[tuple[str, str]] = set()
    keyable: list[Finding] = []
    for finding in verdict.must_fix:
        # #1817's no-diff-anchor case: there is no path to key on, so this
        # finding gets no cross-round memory and no payload.
        identity = (finding.file, finding.summary)
        if fingerprint_v1(*identity) is None or identity in seen:
            continue
        seen.add(identity)
        keyable.append(finding)
    return keyable


def _settle_payload_block(index: int, finding: Finding, reviewed_sha: str) -> list[str]:
    """One labelled, fenced payload for *finding* (#2210).

    The one place in this module that does NOT use ``_safe``. ``file`` and
    ``summary`` here are the ledger key itself — the operator pastes this block
    unedited and ``cw review settle`` mints a key from it that a later re-raise
    must hit byte for byte — so a visible escape would silently produce a
    record for a finding that does not exist.

    It is made inert losslessly instead (#2210 round 4):
    :func:`~cw.review_markers.escape_angle_brackets_in_json` rewrites ``<`` and
    ``>`` as their ``\\uXXXX`` JSON escapes, so the rendered text carries no
    literal ``<!--`` or ``-->`` while ``json.loads`` still yields the identical
    string. Two further properties make this sufficient rather than merely
    convenient: ``json.dumps`` escapes the quotes of any JSON an injected
    summary carries, so an embedded payload cannot parse even if a delimiter
    survived; and the reader honours a sentinel only at the marker's own
    structural position, which a fenced payload inside a verdict comment is
    not.

    The label line outside the fence is ordinary rendered text and IS escaped.
    """
    body = escape_angle_brackets_in_json(
        json.dumps(
            {
                "entries": [
                    {
                        "file": finding.file,
                        "summary": finding.summary,
                        "outcome": "REJECTED",
                        "rationale": "",
                        "reviewed_sha": reviewed_sha,
                    }
                ]
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    fence = _settle_fence(body)
    return [f"**{index}. {_safe(finding.file)}**", "", f"{fence}json", body, fence, ""]


def _settle_overflow_lines(overflow: list[Finding]) -> list[str]:
    """Name the findings that did not fit, compactly and with no JSON (#2210).

    The whole point of the caps above is that a payload is never cut in half,
    so what lands here must not look like one: identity in prose, nothing
    fenced, nothing that could half-parse if an operator copied it.
    """
    if not overflow:
        return []
    rows = [
        f"- **{_safe(finding.file)}** — "
        f"{_truncate(finding.summary, _SETTLE_COMPACT_SUMMARY_MAX)}"
        for finding in overflow[:_SETTLE_MAX_COMPACT_ROWS]
    ]
    residue = len(overflow) - len(rows)
    if residue:
        rows.append(f"- …and {residue} more MUST_FIX finding(s) listed above.")
    return [_SETTLE_OVERFLOW_NOTE, "", *rows, ""]


def _render_settle_payloads(verdict: ReviewVerdict) -> list[str]:
    """Hand the operator a ready-to-paste settle payload per finding (#2210).

    The cross-round ledger (#1838) had a renderer, a parser and a backstop but
    no production writer, and its runbook told operators to compute the key by
    hand from a `python -c` one-liner. This section is the other end of
    ``cw review settle``: each payload carries the finding's VERBATIM ``file``
    and ``summary`` (the ledger's whole identity) plus the verdict's
    ``reviewed_sha`` (the provenance the record needs to say what code it
    silenced), so pasting it needs no editing and reproduces exactly the key a
    later re-raise will hit.

    Empty-returns-``[]`` like its siblings, and renders nothing at all unless
    the pass actually blocks — there is nothing to settle otherwise.

    **Bounded.** At most ``_SETTLE_MAX_PAYLOADS`` payloads, and at most
    ``_SETTLE_SECTION_BUDGET_CHARS`` characters of them; the remainder is
    listed compactly by :func:`_settle_overflow_lines`. Accounting is over the
    joined text actually emitted, and a block is tested WHOLE before it is
    kept, so the section can never end on a half-written JSON object.

    Deliberately NOT the postable ``REVIEW-FINDING-DISPOSITIONS`` marker
    itself: the dispositions reader ingests every comment body on the ticket,
    including this one, so printing the marker here would auto-settle every
    finding as REJECTED on the next pass. Per finding rather than one combined
    block, so an operator cannot settle the genuinely actionable finding by
    pasting everything at once.
    """
    if not verdict.blocking:
        return []
    keyable = _settleable_findings(verdict)
    if not keyable:
        return []
    head = [SETTLE_SECTION_HEADING, "", _SETTLE_INTRO, ""]
    blocks: list[list[str]] = []
    overflow: list[Finding] = []
    for index, finding in enumerate(keyable, start=1):
        candidate = [
            *blocks,
            _settle_payload_block(index, finding, verdict.reviewed_sha),
        ]
        joined = "\n".join([*head, *(line for block in candidate for line in block)])
        if (
            len(candidate) > _SETTLE_MAX_PAYLOADS
            or len(joined) > _SETTLE_SECTION_BUDGET_CHARS
        ):
            overflow = keyable[index - 1 :]
            break
        blocks = candidate
    body = [line for block in blocks for line in block]
    return [*head, *body, *_settle_overflow_lines(overflow)]
