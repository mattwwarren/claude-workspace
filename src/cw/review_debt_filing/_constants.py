"""Named constants for review-debt filing (#3041, RFC 0017 D-7)."""

from __future__ import annotations

from typing import Literal

from cw import tracker as _tracker

#: The label every review-debt ticket carries; the marker lookup lists it.
REVIEW_DEBT_LABEL = "review-debt"

#: The label Step H3 applies to a ticket it harvested from a merged PR body.
REVIEW_DEBT_HARVESTED_LABEL = "review-debt-harvested"

#: ``gh label create --force`` color and description for :data:`REVIEW_DEBT_LABEL`.
REVIEW_DEBT_LABEL_COLOR = "d4c5f9"
REVIEW_DEBT_LABEL_DESCRIPTION = "Review finding deferred during cw review (RFC 0017)"

#: Total wall-clock budget for one filing run. Entries still unfiled when it
#: runs out stay unticketed; the next run or the finalize gate picks them up.
DEBT_FILING_BUDGET_S = 90

#: Per-call ``gh`` timeouts. Each call gets ``min(constant, remaining budget)``.
ENSURE_LABEL_TIMEOUT_S = 20
LIST_ISSUES_TIMEOUT_S = 30
CREATE_ISSUE_TIMEOUT_S = 30
FETCH_BODY_TIMEOUT_S = 20
UPDATE_BODY_TIMEOUT_S = 30
POST_COMMENT_TIMEOUT_S = 30
VISIBILITY_TIMEOUT_S = 15
PR_VIEW_TIMEOUT_S = 20

#: The smallest per-call timeout worth starting a ``gh`` call with.
MIN_CALL_TIMEOUT_S = 1

#: Close sweep (#3044): the total wall-clock budget for one sweep across every
#: client, the per-call ``gh issue close`` and ``gh issue view --json state``
#: caps, the most issues one sweep closes, and how often the dispatch loop
#: runs a sweep. Work left when a cap or the budget is hit waits for the next.
DEBT_CLOSE_BUDGET_S = 60
CLOSE_ISSUE_TIMEOUT_S = 30
ISSUE_STATE_TIMEOUT_S = 15
MAX_DEBT_CLOSES_PER_SWEEP = 10
DEBT_CLOSE_SWEEP_INTERVAL_S = 600

#: ``gh issue close --reason`` for debt whose PR closed unmerged (#3044).
CLOSE_REASON_NOT_PLANNED = "not planned"

#: The ``gh issue view --json state`` value of a closed issue.
GH_ISSUE_STATE_CLOSED = "CLOSED"

#: Title summary cap, failure-detail cap and the stderr-line summary cap.
TITLE_SUMMARY_MAX_CHARS = 80
DETAIL_MAX_CHARS = 200
STDERR_SUMMARY_MAX_CHARS = 80

#: Characters of a key shown in a stderr line.
KEY_DISPLAY_CHARS = 12

#: Fenced-evidence placeholder for an entry read back from the artifact, which
#: does not record evidence (plan Adopted Assumption 13).
EVIDENCE_PLACEHOLDER = "(evidence not recorded)"

#: Shortest evidence fence; a longer backtick run inside evidence lengthens it.
MIN_FENCE_TICKS = 3

#: Roll-up sizing. GitHub rejects an issue body over 65536 characters, so a
#: roll-up body stays at or under :data:`ROLLUP_BODY_MAX_CHARS` (leaving room for
#: the header, the PR link markers and edits by hand) and the findings that do
#: not fit continue in a numbered continuation issue. One item is capped so a
#: single finding can never exceed the budget on its own: its evidence at
#: :data:`ROLLUP_EVIDENCE_MAX_CHARS`, its summary and rationale at
#: :data:`ROLLUP_FIELD_MAX_CHARS` each.
ROLLUP_BODY_MAX_CHARS = 60000
ROLLUP_EVIDENCE_MAX_CHARS = 6000
ROLLUP_FIELD_MAX_CHARS = 2000

#: The severity the roll-up title counts separately.
MUST_FIX_SEVERITY = "MUST_FIX"

#: Body markers. ``<!-- cw-review-debt: <key> -->`` is the dedup recovery path;
#: ``<!-- cw-review-debt-pr: <url> -->`` links the PR; III2 (#3044) parses it.
DEBT_MARKER_PREFIX = "<!-- cw-review-debt: "
PR_MARKER_PREFIX = "<!-- cw-review-debt-pr: "
#: ``<!-- cw-review-debt-rollup: part=<n> -->`` names a roll-up issue and its
#: part number (1 for the first issue, 2 and up for continuations).
ROLLUP_MARKER_PREFIX = "<!-- cw-review-debt-rollup: "
ROLLUP_PART_KEY = "part="
MARKER_SUFFIX = " -->"

#: The non-GitHub tracker system debt filing distinguishes. Defined once in
#: ``cw.tracker`` (#3055); bound here so existing importers keep working.
TRACKER_LINEAR: Literal["linear"] = _tracker.TRACKER_LINEAR
