"""Mechanical disposition routing for the codex REVIEW path (#2464).

The Claude-native review adjudicates every accepted MUST_FIX/SHOULD_FIX into
one of four buckets (``auto-dev-review.md`` Checkpoint 3a): FIX NOW, REJECT,
DEFER, OPERATOR ACTIONABLE. Its invariant is that every finding ends *fixed*,
*rejected-with-reason*, *ticketed*, or *handed to the operator as an explicit
action*. The codex path has no coordinating session to exercise that judgment,
so before this module it left every accepted finding at the optimistic
``disposition="fixed"`` default: a SHOULD_FIX on a clean pass shipped as
"fixed" with nothing fixed, and a ``no_diff_anchor`` MUST_FIX was handed to the
autonomous fixer, which cannot edit a diff that does not exist.

:func:`route_codex_dispositions` applies the parts of that contract that are
mechanical — decidable from the finding's own fields and the plan text, with
no judgment about whether the finding is correct — and leaves the rest
exactly where it was:

- A finding whose ``plan_requirement_quote`` appears verbatim in the approved
  plan is a NON_DEFERRABLE plan deviation (Checkpoint 3a (4a)). Any severity
  becomes ``operator_actionable`` with a :data:`PLAN_DEVIATION_DETAIL_PREFIX`
  detail and the pass parks as ``plan_deviation``. The Claude path lets its
  fix loop try first; this path does not, because a stateless fixer must not
  decide how a plan clause is satisfied (#2464: "route to an operator, not an
  autonomous fixer"). A quote not found in the plan is logged and ignored, so
  a reviewer cannot park a pass by inventing a plan clause.
- A MUST_FIX carrying ``no_diff_anchor`` is ``operator_actionable`` with a
  :data:`NO_DIFF_ANCHOR_DETAIL_PREFIX` detail (bucket 4); the pass parks as
  ``review_operator_actionable``.
- Every other SHOULD_FIX — including a ``no_diff_anchor`` one, which the
  Claude contract also routes through DEFER — is ``deferred`` with a
  :data:`AUTO_DEFERRED_DETAIL_PREFIX` detail (operator decision, 2026-11-05).
  The codex REVIEW thread records it in ``.cw/deferred-findings.md``, so
  FINALIZE copies it into the PR body's ``DEFERRED-REVIEW-FINDINGS`` block and
  the merge-time sweep files it as a ticket.
- Every other MUST_FIX stays ``fixed`` and keeps blocking, exactly as before:
  it is the fix loop's (or the ``codex_must_fix_findings`` park's) input.

Only findings still at the ``"fixed"`` default are routed. Any other
disposition was decided upstream — an operator void, a ledger adjudication, a
settled-suppression — and re-routing it would overturn that decision.

``"deferred"`` here means what it means on the Claude path ("may ship
un-fixed, ticketed on merge"). The codex fix loop's own capped-survivor state
is ``"unresolved"`` since #2352, so the two meanings no longer collide; see
ADR-0022 for why this module is a second producer of that disposition rather
than a reuse of :func:`cw.review_adjudication.apply_adjudication`.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    Adjudication,
    write_deferred_findings,
)
from cw.review_findings import derive_review_counts

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

_log = logging.getLogger(__name__)

_MUST_FIX = "MUST_FIX"
_SHOULD_FIX = "SHOULD_FIX"
_UNDECIDED = "fixed"

#: ``disposition_detail`` prefixes this module stamps. The block-reason table
#: and the deferred-findings writer key on them, so they are the one literal
#: each, never re-typed.
PLAN_DEVIATION_DETAIL_PREFIX = "plan_deviation"
NO_DIFF_ANCHOR_DETAIL_PREFIX = "no_diff_anchor"
AUTO_DEFERRED_DETAIL_PREFIX = "auto_deferred"

# A quote shorter than this, once whitespace is collapsed, matches too much of
# any plan to identify a clause ("do", "the test") and is not treated as one.
_MIN_PLAN_QUOTE_CHARS = 8
_WHITESPACE = re.compile(r"\s+")

_PLAN_DEVIATION_DETAIL = (
    PLAN_DEVIATION_DETAIL_PREFIX
    + ": violates the approved plan clause {quote!r}; an operator decides "
    "whether the implementation or the plan changes (never sent to the fixer)"
)
_NO_DIFF_ANCHOR_DETAIL = (
    NO_DIFF_ANCHOR_DETAIL_PREFIX
    + ": the remedy lies outside this diff, so an operator must act on it "
    "(never sent to the fixer)"
)
_AUTO_DEFERRED_DETAIL = (
    AUTO_DEFERRED_DETAIL_PREFIX
    + ": a mechanically routed review pass never fixes a SHOULD_FIX; "
    "recorded in .cw/deferred-findings.md and filed as a ticket on merge"
)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def verified_plan_quote(finding: Finding, plan_text: str | None) -> str | None:
    """The finding's plan quote when it names a real plan clause, else ``None``.

    Verbatim up to whitespace: runs of whitespace (a wrapped plan line, an
    indented bullet) collapse to one space on both sides before the substring
    test. No plan, a blank quote or one under :data:`_MIN_PLAN_QUOTE_CHARS`
    verifies nothing.
    """
    quote = _collapse(finding.plan_requirement_quote)
    if not quote or plan_text is None:
        return None
    if len(quote) < _MIN_PLAN_QUOTE_CHARS or quote not in _collapse(plan_text):
        _log.warning(
            "codex_plan_quote_unverified: %s %s: plan_requirement_quote %r is "
            "not a clause of .cw/plan.md; finding routed as an ordinary %s",
            finding.file,
            finding.summary,
            finding.plan_requirement_quote,
            finding.severity,
        )
        return None
    return quote


def _route(accepted: AcceptedFinding, plan_text: str | None) -> AcceptedFinding:
    finding = accepted.finding
    if accepted.disposition != _UNDECIDED or finding.severity not in (
        _MUST_FIX,
        _SHOULD_FIX,
    ):
        return accepted
    quote = verified_plan_quote(finding, plan_text)
    if quote is not None:
        detail = _PLAN_DEVIATION_DETAIL.format(quote=quote)
        disposition = "operator_actionable"
    elif finding.severity == _MUST_FIX and finding.no_diff_anchor:
        detail = _NO_DIFF_ANCHOR_DETAIL
        disposition = "operator_actionable"
    elif finding.severity == _SHOULD_FIX:
        detail = _AUTO_DEFERRED_DETAIL
        disposition = "deferred"
    else:
        return accepted
    return accepted.model_copy(
        update={"disposition": disposition, "disposition_detail": detail}
    )


def route_codex_dispositions(
    verdict: ReviewVerdict, plan_text: str | None
) -> ReviewVerdict:
    """*verdict* with every undecided MUST_FIX/SHOULD_FIX routed (module doc).

    ``must_fix``/``blocking`` are recomputed so a routed MUST_FIX leaves them,
    which is what keeps it away from the fix loop: the loop's open set is
    MUST_FIX at ``"fixed"``, and its entry gate is ``blocking``.
    ``review.deferred`` is recomputed with :func:`derive_review_counts`' own
    rule; every other ``review`` count is the frozen cycle-0 baseline and is
    preserved, the same split ``apply_adjudication`` keeps.
    """
    routed = [_route(af, plan_text) for af in verdict.accepted]
    routed_away = [
        af.finding
        for before, af in zip(verdict.accepted, routed, strict=True)
        if before.disposition == _UNDECIDED and af.disposition != _UNDECIDED
    ]
    must_fix = [f for f in verdict.must_fix if f not in routed_away]
    return verdict.model_copy(
        update={
            "accepted": routed,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
            "review": verdict.review.model_copy(
                update={"deferred": derive_review_counts(routed).deferred}
            ),
        }
    )


def _with_detail_prefix(verdict: ReviewVerdict, prefix: str) -> list[AcceptedFinding]:
    return [
        af
        for af in verdict.accepted
        if af.disposition_detail.startswith(prefix)
        and af.disposition in ("operator_actionable", "deferred")
    ]


def plan_deviations(verdict: ReviewVerdict) -> list[AcceptedFinding]:
    """The findings :func:`route_codex_dispositions` routed as plan deviations."""
    return _with_detail_prefix(verdict, PLAN_DEVIATION_DETAIL_PREFIX)


def operator_actions(verdict: ReviewVerdict) -> list[AcceptedFinding]:
    """The ``no_diff_anchor`` MUST_FIX findings routed to the operator."""
    return _with_detail_prefix(verdict, NO_DIFF_ANCHOR_DETAIL_PREFIX)


def codex_deferrals(verdict: ReviewVerdict) -> list[AcceptedFinding]:
    """The SHOULD_FIX findings this module deferred (the writer's input)."""
    return _with_detail_prefix(verdict, AUTO_DEFERRED_DETAIL_PREFIX)


def record_codex_deferrals(worktree: Path, verdict: ReviewVerdict | None) -> int:
    """Merge *verdict*'s codex deferrals into ``.cw/deferred-findings.md``.

    The "ticketed" half of the invariant: a deferred SHOULD_FIX is only
    terminal once FINALIZE can copy it into the PR body for the merge-time
    sweep to file. Called after every codex pass — cycle 0, each fix-loop
    re-review, and the terminal verdict — because a re-review covers only its
    delta, so the terminal verdict alone would lose cycle 0's deferrals. The
    shared writer dedupes on content, so recording the same deferral again is
    a no-op. Returns the number of entries this call offered (0 writes
    nothing). A prior file that does not parse raises
    :class:`~cw.exceptions.CwError`: overwriting it would lose its records.
    """
    if verdict is None:
        return 0
    entries = [
        Adjudication(
            severity=af.finding.severity,
            file=af.finding.file,
            line_start=af.finding.line_start,
            line_end=af.finding.line_end,
            evidence=af.finding.evidence,
            summary=af.finding.summary,
            outcome="defer",
            rationale=af.disposition_detail,
        )
        for af in codex_deferrals(verdict)
    ]
    if entries:
        write_deferred_findings(worktree / DEFERRED_FINDINGS_RELATIVE_PATH, entries)
    return len(entries)
