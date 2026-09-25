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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cw.exceptions import PlanDraftBindingError

# Where a plan-stage worker persists its draft, relative to the session
# worktree (auto-dev-plan.md's draft-persistence rule).
PLAN_DRAFT_RELATIVE_PATH = Path(".cw") / "plan-draft.md"
# The sentinel key this module binds. Mirrors
# ``cw.models.tasks.PLAN_DRAFT_FINGERPRINT_KEY`` -- duplicated as a literal so
# this module stays a leaf below ``cw.models`` (the schema package imports it).
PLAN_DRAFT_FINGERPRINT_KEY = "plan_draft_fingerprint"

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


@dataclass(frozen=True)
class FingerprintBinding:
    """What :func:`bind_claimed_fingerprint` recorded onto a payload.

    ``replaced`` is True when the payload's own value differed from the digest
    cw computed -- the #2382 truncation shape -- so the caller can surface
    that the producer's value was not trusted.
    """

    fingerprint: str
    draft_path: Path
    replaced: bool


def bind_claimed_fingerprint(
    payload: dict[str, Any], draft_path: Path
) -> FingerprintBinding | None:
    """Replace a producer-claimed ``plan_draft_fingerprint`` with cw's own (#2382).

    The field is agent-transcribed on the transcript path, and a 62-character
    copy of a 64-character digest once reached ``cw dev-queue approve`` and
    re-opened the approval gate on every round. Here the value is never taken
    from the payload: a non-null value is read only as the producer's claim
    that a draft is in hand, and the digest itself is recomputed from
    *draft_path* under the named *Plan-draft fingerprint rule*. A ``null`` (or
    absent) claim is left alone and returns None -- it is the contract's "no
    draft" value, emitted by every non-plan-stage sentinel, and a stale draft
    on disk must not turn it into a binding.

    Raises:
        PlanDraftBindingError: the payload claims a draft but *draft_path*
            does not exist or cannot be read as UTF-8. Nothing is mutated.
    """
    claimed = payload.get(PLAN_DRAFT_FINGERPRINT_KEY)
    if claimed is None:
        return None
    if not draft_path.is_file():
        msg = (
            f"{PLAN_DRAFT_FINGERPRINT_KEY}: payload claims a plan draft but none "
            f"exists at {draft_path}; emit null when no draft is in hand, or pass "
            "--plan-draft <path>."
        )
        raise PlanDraftBindingError(msg)
    try:
        draft_text = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{PLAN_DRAFT_FINGERPRINT_KEY}: cannot read {draft_path}: {exc}"
        raise PlanDraftBindingError(msg) from exc
    computed = compute_plan_draft_fingerprint(draft_text)
    payload[PLAN_DRAFT_FINGERPRINT_KEY] = computed
    return FingerprintBinding(
        fingerprint=computed, draft_path=draft_path, replaced=claimed != computed
    )


def sanitize_persisted_fingerprint(last_result: dict[str, Any]) -> dict[str, Any]:
    """Return *last_result* with a malformed ``plan_draft_fingerprint`` nulled.

    The schema validator that rejects a malformed digest (#2382) also runs
    when an already-persisted ``Session.last_result`` is re-validated on the
    reconstruction path, so a legacy record carrying the pre-validator
    truncation shape would otherwise become unreconstructable and be treated
    as never emitted. A malformed persisted value is recorded absence, never
    a binding -- the same treatment ``_stamp_plan_approval`` gives it -- so
    it is replaced by ``None`` on a *copy*; the stored dict is untouched. A
    well-formed or absent value returns the input unchanged.
    """
    raw = last_result.get(PLAN_DRAFT_FINGERPRINT_KEY)
    if not isinstance(raw, str) or is_plan_draft_fingerprint(raw):
        return last_result
    return {**last_result, PLAN_DRAFT_FINGERPRINT_KEY: None}
