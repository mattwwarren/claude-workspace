"""Seed cycle 0 with findings from outside the codex review (#2807).

On a clean-mode codex lane a dirty Claude confirm round re-enters the fix loop
(:mod:`cw.codex_confirm_feedback`). Its accepted MUST_FIX findings are merged
into the cycle-0 verdict here, before the loop's entry check, so they flow
through the existing machinery: the cycle-0 open-findings tracker admits them
(disposition ``fixed``), the first fix prompt carries them, the codex
re-review verifies them as ``prior_open_findings``, and every fence, growth,
divergence and constraint guard applies unchanged.

Pure: no I/O, and the input verdict is never mutated. Follows the
``model_copy`` style of :func:`cw.codex_fix_loop.convergence._survivors_only_verdict`,
which rebuilds the same three fields to narrow a verdict where this widens it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_fix_loop.convergence import _open_finding_key

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.review_findings import AcceptedFinding, ReviewVerdict


def _new_seeds(
    verdict: ReviewVerdict, seeds: Sequence[AcceptedFinding]
) -> list[AcceptedFinding]:
    """*seeds* the verdict does not already carry, each stamped ``fixed``.

    Dedupe runs against every accepted entry whatever its disposition, so a
    seed never re-opens a finding the codex pass already settled (an
    operator void, an adjudicated reject). Duplicate seeds collapse to one.
    """
    known: set[_OpenFindingKey] = {
        _open_finding_key(item.finding) for item in verdict.accepted
    }
    added: list[AcceptedFinding] = []
    for seed in seeds:
        key = _open_finding_key(seed.finding)
        if key in known:
            continue
        known.add(key)
        added.append(seed.model_copy(update={"disposition": "fixed"}))
    return added


def seed_verdict(
    verdict: ReviewVerdict, seeds: Sequence[AcceptedFinding]
) -> ReviewVerdict:
    """*verdict* with *seeds* merged in as open MUST_FIX work.

    New seeds are appended to ``accepted`` (disposition ``fixed``) and to
    ``must_fix``, ``blocking`` becomes ``True`` and
    ``review.must_fix_initial`` counts them, so the loop's terminal ``Review``
    reports what it was asked to fix. Returns *verdict* itself when nothing
    new is seeded (no seeds, or every seed already present).
    """
    added = _new_seeds(verdict, seeds)
    if not added:
        return verdict
    review = verdict.review.model_copy(
        update={"must_fix_initial": verdict.review.must_fix_initial + len(added)}
    )
    return verdict.model_copy(
        update={
            "accepted": [*verdict.accepted, *added],
            "must_fix": [*verdict.must_fix, *(item.finding for item in added)],
            "blocking": True,
            "review": review,
        }
    )
