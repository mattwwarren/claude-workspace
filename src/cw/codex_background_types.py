"""Pure types of the codex background review run (split out for #3216).

Moved out of :mod:`cw.codex_background` so that module stays within its line
budget (R4) while it gains the repo review engine startup check. Types only:
no logic and no import of ``cw.codex_background``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypedDict

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.codex_fix_loop import CycleObserver, RunGrowth
    from cw.review_findings import AcceptedFinding, ReviewVerdict


class ReviewOutcome(NamedTuple):
    """Step 3's answer: what to persist, its verdict, its label and footer.

    ``fix_loop_footer`` is whether the posted verdict may carry the codex fix
    loop's "available" wording: the lane's fix loop is enabled AND the verdict
    is the codex-built one. ``reviewer_label`` is presentation only.
    """

    result: AutoDevResult
    verdict: ReviewVerdict | None
    fix_loop_footer: bool
    reviewer_label: str


class LoopKwargs(TypedDict, total=False):
    """The optional per-iteration arguments of the bound codex loop."""

    seed_findings: tuple[AcceptedFinding, ...]
    cycle_observer: CycleObserver
    run_growth: RunGrowth
