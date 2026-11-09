"""GitHub CheckRun / StatusContext vocabulary, shared by adoption and hydration (#2958).

Two consumers read the same GitHub values with different contracts:

- ``cw.adopt_pr`` checks membership in the full allow-lists
  (``CHECK_RUN_STATUSES``, ``CHECK_RUN_CONCLUSIONS``,
  ``STATUS_CONTEXT_STATES``). Those are fail-closed: an unknown value refuses
  the adoption as malformed.
- ``cw.pr_hydrate`` classifies with the subsets (``FAILED_*``, ``INFRA_ONLY_*``,
  ``PENDING_*``). Those are lenient: an unknown value is neither failing nor
  pending, and hydration never raises on it.

A new GitHub value is added in this one file. The review-monitor script
(``.claude/scripts/review_monitor_lib/attention.py``) keeps a hand copy of two
subsets because it cannot import ``cw``; ``tests/test_gh_check_vocabulary.py``
pins that copy to these sets.

This is a leaf module: it imports nothing from ``cw``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class CheckRunStatus(StrEnum):
    """``CheckRun.status`` values GitHub reports."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    REQUESTED = "REQUESTED"
    WAITING = "WAITING"


class CheckRunConclusion(StrEnum):
    """``CheckRun.conclusion`` values GitHub reports."""

    ACTION_REQUIRED = "ACTION_REQUIRED"
    CANCELLED = "CANCELLED"
    FAILURE = "FAILURE"
    NEUTRAL = "NEUTRAL"
    SKIPPED = "SKIPPED"
    STALE = "STALE"
    STARTUP_FAILURE = "STARTUP_FAILURE"
    SUCCESS = "SUCCESS"
    TIMED_OUT = "TIMED_OUT"


class StatusContextState(StrEnum):
    """``StatusContext.state`` values GitHub reports."""

    ERROR = "ERROR"
    EXPECTED = "EXPECTED"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"


# Full allow-lists: adoption's fail-closed membership checks.
CHECK_RUN_STATUSES: Final[frozenset[str]] = frozenset(CheckRunStatus)
CHECK_RUN_CONCLUSIONS: Final[frozenset[str]] = frozenset(CheckRunConclusion)
STATUS_CONTEXT_STATES: Final[frozenset[str]] = frozenset(StatusContextState)

# Hydration classification subsets: explicit member lists, not "all minus X",
# so a newly added GitHub value never silently becomes pending or failed.
# Ported verbatim from _summarize_status_checks in
# .claude/scripts/review_monitor_lib/attention.py.
FAILED_CHECKRUN_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)
# GitHub #1684 — cw-local addition on top of the ported review-monitor set:
# CANCELLED and STARTUP_FAILURE are GitHub's two conclusions for a
# provider-side/infrastructure event (a run cancelled by a platform incident,
# or a runner that never started), not a real test/build failure. A checkrun
# landing in FAILED_CHECKRUN_CONCLUSIONS with ONLY one of these two
# conclusions present anywhere in the run carries no signal that the PR's
# own code is broken -- see tests/test_pr_hydrate.py::TestSummarizeStatusChecks::
# test_pure_infra_cancellation_is_ok. Deliberately does NOT suppress ci_failing
# when a genuine FAILURE/TIMED_OUT/ACTION_REQUIRED/STALE conclusion is ALSO
# present in the same run (e.g. GitHub Actions' own fail-fast cancelling
# sibling jobs after a real failure) -- see
# test_genuine_failure_with_cascaded_cancellations_is_not_ok. This also
# changes what _diff_transitions' PR_CI_FAILED emission, board.py's
# _render_pr_cell / _row_badge, and the address_review / request_reviewer
# review-recipe dispatch gates do downstream for this shape -- all are the
# INTENDED consequence of this fix, not untraced side effects. See
# ## Decisions in the #1684 plan.
INFRA_ONLY_CHECKRUN_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"CANCELLED", "STARTUP_FAILURE"}
)
PENDING_CHECKRUN_STATUSES: Final[frozenset[str]] = frozenset(
    {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}
)
FAILED_STATUS_CONTEXT_STATES: Final[frozenset[str]] = frozenset({"FAILURE", "ERROR"})
