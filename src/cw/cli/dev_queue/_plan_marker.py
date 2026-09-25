"""Builders and matchers for the ``approve --post-marker`` audit comment.

``cw dev-queue approve --post-marker`` posts an operator-authored comment
recording that a PLAN-stage approval gate was released (#1419). #2194 binds
that comment to the draft it approved by embedding the approval's plan-draft
fingerprint; the *Plan-draft fingerprint rule* in
``.claude/commands/auto-dev-plan.md`` is the single definition of how that
value is computed, and this module deliberately does not restate it — it only
shapes the marker string and matches it.

The marker is audit-only: nothing reads it back as plan-approval evidence,
which lives on the dev-queue row (``plan_approved_at`` /
``plan_approved_fingerprint``) and travels to the next worker through
``queue_metadata``. There is deliberately no parse-back helper here — a
function that recovered the fingerprint from a comment body is the first step
toward some future stage adopting the comment as an evidence source, which is
exactly what #2102/#2194 exist to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.plan_fingerprint import is_plan_draft_fingerprint

_is_plan_draft_fingerprint = is_plan_draft_fingerprint

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Operator-facing plan-approved marker the auto-dev runbooks document
# (README.md, docs/dispatch-runbook.md, docs/session-disposition.md) --
# distinct from lifecycle.py's `_PLAN_SPEC_MARKER`/`_PLAN_SOUNDNESS_MARKER`
# pair (the coded plan-quality-review gate). See GitHub #1419. The unbound
# form is what an approval that binds no draft still posts.
_PLAN_APPROVED_MARKER = "<!-- auto-dev-plan-approved -->"

# The draft-bound form (#2194). Space after the colon, matching the
# `<!-- plan-spec-reviewed: D vN -->` convention. The bare marker is NOT a
# substring of this one (it needs `approved -->`, this has `approved: <sha>
# -->`), which is what makes exact-string containment a sufficient dedup.
_PLAN_APPROVED_MARKER_BOUND = "<!-- auto-dev-plan-approved: {fingerprint} -->"


def _plan_approved_marker(fingerprint: str | None) -> str:
    """The marker to post (and match) for an approval bound to *fingerprint*.

    Total by construction: no fingerprint, or one that is not a well-formed
    digest, yields the unbound bare marker -- the pre-#2194 behavior, so a
    malformed agent-produced value degrades instead of leaking into a comment
    body where a ``-->`` fragment could break out of the HTML comment.
    """
    if fingerprint is None or not _is_plan_draft_fingerprint(fingerprint):
        return _PLAN_APPROVED_MARKER
    return _PLAN_APPROVED_MARKER_BOUND.format(fingerprint=fingerprint)


def _marker_present(comments: Sequence[Mapping[str, object]], marker: str) -> bool:
    """True iff some comment body contains *marker* verbatim.

    Exact-string containment, never a parse: a marker bound to a different
    draft, and the bare marker, are both misses. Bodies that are absent or
    not strings are skipped -- the value is bound before the ``isinstance``
    check so the narrowing carries to the containment test (a separate
    ``c["body"]`` subscript is typed ``object`` and fails ``mypy --strict``).
    """
    return any(
        isinstance(body := comment.get("body"), str) and marker in body
        for comment in comments
    )
