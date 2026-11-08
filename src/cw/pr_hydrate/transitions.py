"""PR-state transition diffing and hydration candidate selection.

Part of the ``cw.pr_hydrate`` package (#2999).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.gh import _GH_PR_STATE_CLOSED, _GH_PR_STATE_MERGED
from cw.models import OrchestratorEventType

if TYPE_CHECKING:
    from cw.models import PrState, TicketTask

# pr.mergeable fires on ENTERING one of GitHub's genuinely-mergeable statuses
# from outside the set — not on merely leaving a blocking status into
# UNKNOWN/DRAFT (operator resolution, #929 premise round 2026-07-05).
_MERGEABLE_STATES: frozenset[str] = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})
_TERMINAL_PR_STATES: frozenset[str] = frozenset(
    {_GH_PR_STATE_MERGED, _GH_PR_STATE_CLOSED}
)


def _diff_transitions(
    *,
    old: PrState | None,
    new: PrState,
    base: dict[str, object],
) -> list[tuple[OrchestratorEventType, dict[str, object]]]:
    """Return ``(event_type, payload)`` pairs for each old->new transition (R5).

    ``old``/``new`` are keyword-only: both are ``PrState``-domain values with no
    structural difference at the call site, so an accidental positional swap
    would silently invert every transition's direction with no type error.

    Dedup rule: value-change events (ci_failed/review_received/mergeable) require
    a prior persisted baseline (``old is not None``); ``pr.merged`` fires on the
    first observation of a MERGED state so a re-discovered merge still retires.
    """
    events: list[tuple[OrchestratorEventType, dict[str, object]]] = []
    old_state = old.state if old is not None else ""
    if new.state == _GH_PR_STATE_MERGED and old_state != _GH_PR_STATE_MERGED:
        events.append((OrchestratorEventType.PR_MERGED, dict(base)))
    if old is not None and old.ci_ok and not new.ci_ok:
        events.append(
            (
                OrchestratorEventType.PR_CI_FAILED,
                {**base, "failing_checks": list(new.failing_checks)},
            )
        )
    if old is not None and old.review_decision != new.review_decision:
        events.append(
            (
                OrchestratorEventType.PR_REVIEW_RECEIVED,
                {**base, "review_decision": new.review_decision},
            )
        )
    if (
        old is not None
        and old.merge_state_status not in _MERGEABLE_STATES
        and new.merge_state_status in _MERGEABLE_STATES
    ):
        # mergeStateStatus is a deliberate passthrough (not snake_cased) — it is
        # the raw gh field name, unlike the derived failing_checks/review_decision.
        events.append(
            (
                OrchestratorEventType.PR_MERGEABLE,
                {**base, "mergeStateStatus": new.merge_state_status},
            )
        )
    return events


def _is_candidate(task: TicketTask) -> bool:
    """A task is hydratable when it has a PR URL and a non-terminal PR state.

    Why the terminal-state exclusion: ported review-monitor lesson
    (session:94a665a5, "Abandoned PR auto-completion") — a PR MERGED or CLOSED on
    GitHub needs no further operator attention. Excluding terminal states here
    (the same predicate the review-recipe detect phase reuses) means no review
    recipe ever fires on a merged/abandoned PR, cw's analogue of review_monitor
    auto-completing such PRs out of the monitored queue. See
    tests/test_pr_hydrate.py::TestCandidateSelection::test_skips_closed_pr_state
    and tests/test_reconcile_review_recipes.py::test_closed_pr_never_a_candidate.
    """
    if not task.pr_url:
        return False
    return task.pr_state is None or task.pr_state.state not in _TERMINAL_PR_STATES
