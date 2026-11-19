"""Builders for the ``approve --post-marker`` audit comment.

``cw dev-queue approve --post-marker`` queues an operator-authored comment on
the ticket recording that a PLAN-stage approval gate was released (#1419).
#2194 binds that comment to the draft it approved by embedding the approval's
plan-draft fingerprint; the *Plan-draft fingerprint rule* in
``.claude/commands/auto-dev-plan.md`` is the single definition of how that
value is computed, and this module deliberately does not restate it — it only
shapes the marker string.

The comment is queued on the outbox (RFC 0019 O3b), which delivers it for any
tracker; the outbox marker below is the idempotency key that the enqueue
dedups on.

The marker is audit-only: nothing reads it back as plan-approval evidence,
which lives on the dev-queue row (``plan_approved_at`` /
``plan_approved_fingerprint``) and travels to the next worker through
``queue_metadata``. There is deliberately no parse-back helper here — a
function that recovered the fingerprint from a comment body is the first step
toward some future stage adopting the comment as an evidence source, which is
exactly what #2102/#2194 exist to prevent.
"""

from __future__ import annotations

from cw.plan_fingerprint import is_plan_draft_fingerprint

_is_plan_draft_fingerprint = is_plan_draft_fingerprint

# Operator-facing plan-approved marker the auto-dev runbooks document
# (README.md, docs/dispatch-runbook.md, docs/session-disposition.md) --
# distinct from lifecycle.py's `_PLAN_SPEC_MARKER`/`_PLAN_SOUNDNESS_MARKER`
# pair (the coded plan-quality-review gate). See GitHub #1419. The unbound
# form is what an approval that binds no draft still posts.
_PLAN_APPROVED_MARKER = "<!-- auto-dev-plan-approved -->"

# The draft-bound form (#2194). Space after the colon, matching the
# `<!-- plan-spec-reviewed: D vN -->` convention.
_PLAN_APPROVED_MARKER_BOUND = "<!-- auto-dev-plan-approved: {fingerprint} -->"

# The outbox idempotency key's bare form, used when no well-formed fingerprint
# binds the approval.
_PLAN_APPROVED_OUTBOX_BARE = "plan-approved-bare"


def _plan_approved_marker(fingerprint: str | None) -> str:
    """The comment body to queue for an approval bound to *fingerprint*.

    Total by construction: no fingerprint, or one that is not a well-formed
    digest, yields the unbound bare marker -- the pre-#2194 behavior, so a
    malformed agent-produced value degrades instead of leaking into a comment
    body where a ``-->`` fragment could break out of the HTML comment.
    """
    if fingerprint is None or not _is_plan_draft_fingerprint(fingerprint):
        return _PLAN_APPROVED_MARKER
    return _PLAN_APPROVED_MARKER_BOUND.format(fingerprint=fingerprint)


def _plan_approved_outbox_marker(fingerprint: str | None) -> str:
    """The outbox entry marker (idempotency key) for the same approval.

    ``plan-approved-<fingerprint>`` for a well-formed digest, else
    ``plan-approved-bare``. Matches the outbox marker grammar, so the enqueue
    never refuses it.
    """
    if fingerprint is None or not _is_plan_draft_fingerprint(fingerprint):
        return _PLAN_APPROVED_OUTBOX_BARE
    return f"plan-approved-{fingerprint}"
