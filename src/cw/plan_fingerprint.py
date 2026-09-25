"""Shared validation and computation for plan-draft content fingerprints.

The *Plan-draft fingerprint rule* in ``.claude/commands/auto-dev-plan.md`` is
the single definition of how a ``.cw/plan-draft.md`` fingerprint is computed
(#2102). :func:`compute_plan_draft_fingerprint` is that rule's only
implementation inside ``cw`` -- ``cw result emit`` (#2382) and the approve-time
promotion gate (#2342) both call it, so a producer's self-reported value can
always be checked against, or replaced by, a value cw computed itself.
"""

from __future__ import annotations

import hashlib
import re

# A plan-draft fingerprint is a full SHA-256 digest in lowercase hex.
PLAN_DRAFT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

# The closed leading bookkeeping-line grammar of `.cw/plan-draft.md`, in the
# fixed order auto-dev-plan.md's "Leading bookkeeping-line grammar and order"
# rule (#2154, #2376) prescribes. Each pattern is anchored by `re.match` at a
# running offset, so a matching HTML comment *inside* the plan body is plan
# content and stays hash material.
_ROUND_LINE = re.compile(r"<!-- plan-stage-scan-round: [0-9]+ -->\n?")
_LAST_EVALUATED_LINE = re.compile(
    r"<!-- plan-stage-last-evaluated: "
    r"operator_comment=[^|\n]+\|body_sha=[0-9a-f]{64} -->\n?"
)
_SETTLED_LINE = re.compile(
    r"<!-- plan-stage-settled: "
    r"(?:A[0-9]+: (?:ADOPTED|ALT-[a-z])|"
    r"P[0-9]+: (?:CONFIRMED|REFUTED|DEFERRED)) -->\n?"
)
_RESOLUTIONS_ATTEMPTED_LINE = re.compile(
    r"<!-- plan-stage-resolutions-attempted: source=[^;\n]+; "
    r"outcome=(?:started; lease_until=[^\n]+?|failed|succeeded) -->\n?"
)
_APPROVAL_REVOKED_LINE = re.compile(
    r"<!-- plan-stage-approval-revoked: at=[^;\n]+; fingerprint=[0-9a-f]{64} -->\n?"
)
_RESOLUTIONS_APPLIED_LINE = re.compile(
    r"<!-- plan-stage-resolutions-applied: "
    r"source=(?:comment:[^\n]+?|body:[0-9a-f]{64}|none) -->\n?"
)
# Single-occurrence lines that follow the settled block, in grammar order.
_TRAILING_BOOKKEEPING_LINES = (
    _RESOLUTIONS_ATTEMPTED_LINE,
    _APPROVAL_REVOKED_LINE,
    _RESOLUTIONS_APPLIED_LINE,
)


def is_plan_draft_fingerprint(value: str) -> bool:
    """Return whether *value* is a 64-character lowercase hex digest."""
    return PLAN_DRAFT_FINGERPRINT_RE.fullmatch(value) is not None


def compute_plan_draft_fingerprint(text: str) -> str:
    """Apply the named *Plan-draft fingerprint rule* to draft *text* (#2102).

    Strips the leading bookkeeping block -- the round-counter line, the
    ``plan-stage-last-evaluated`` line, every ``plan-stage-settled`` marker,
    and the ``resolutions-attempted`` / ``approval-revoked`` /
    ``resolutions-applied`` lines -- and hashes what remains with SHA-256,
    rendered as full lowercase hex. The bookkeeping grammar is a *leading*
    block: without the round-counter line nothing is stripped, and any of
    these markers appearing later in the body is content.
    """
    round_match = _ROUND_LINE.match(text)
    if round_match is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    offset = round_match.end()
    last_evaluated_match = _LAST_EVALUATED_LINE.match(text, offset)
    if last_evaluated_match is not None:
        offset = last_evaluated_match.end()
    while settled_match := _SETTLED_LINE.match(text, offset):
        offset = settled_match.end()
    for pattern in _TRAILING_BOOKKEEPING_LINES:
        trailing_match = pattern.match(text, offset)
        if trailing_match is not None:
            offset = trailing_match.end()
    stripped = text[offset:]
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()
