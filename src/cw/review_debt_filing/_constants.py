"""Named constants for review-debt filing (#3041, RFC 0017 D-7)."""

from __future__ import annotations

from typing import Literal

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

#: Body markers. ``<!-- cw-review-debt: <key> -->`` is the dedup recovery path;
#: ``<!-- cw-review-debt-pr: <url> -->`` links the PR (III2 #3044 parses it).
DEBT_MARKER_PREFIX = "<!-- cw-review-debt: "
PR_MARKER_PREFIX = "<!-- cw-review-debt-pr: "
MARKER_SUFFIX = " -->"

#: The non-GitHub tracker system debt filing distinguishes.
TRACKER_LINEAR: Literal["linear"] = "linear"
