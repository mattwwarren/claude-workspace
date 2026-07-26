# Branch-absence is a diagnostic signal, never a completion signal

**Status:** Accepted
**Driven by:** #808 (epic #813); sibling of #315 (Signal #1), #793 (`completion_source`)

## Decision

When the reaper times out a headless session with no parseable sentinel and no
merged PR, an **absent dev branch** is surfaced only as a diagnostic annotation
on the `SESSION_TIMED_OUT` event — it is **never** used to infer the work
completed. Branch-absence does not imply "merged."

## Invariant

1. Branch-absence **never** routes a session to `COMPLETED`. A timed-out
   session with no merged PR reverts to `PENDING` (retry) regardless of whether
   its branch exists.
2. The branch check annotates the `SESSION_TIMED_OUT` event payload only — via
   a nullable `branch_state` field. It changes no retry, attempt-counting, or
   cost-accounting behavior.
3. `branch_state` has exactly two outcomes: `"absent_no_merged_pr"` (anomaly —
   worker died pre-push or branch force-deleted) or **omitted** (every other
   case: branch present, gh error, check not run — fail-open, no claim made).

## What this means for producers

- `revert_stalled_headless_sessions` (`src/cw/reconcile/stalled/core.py`) calls
  `branch_exists_on_origin` only on `(False, True)` (no-merged-PR) candidates,
  forwards `branch_absent_ticket_ids` through `_act_on_stalled_candidates`
  (mirroring `merged_ticket_ids` / `gh_blocked_ticket_ids`), and tags the
  `SESSION_TIMED_OUT` payload only for those tickets.
- The branch check must fail open: a gh error or absent `gh` never blocks the
  disposition and never adds the tag.

## What this means for consumers

- A `SESSION_TIMED_OUT` carrying `branch_state: "absent_no_merged_pr"` means
  *investigate the worker* (died pre-push / force-deleted) — not "let it churn
  through retries." `cw-session-watch` is the primary consumer; see
  `docs/session-disposition.md`.
- `branch_state` lives on the reaper-emitted event, **not** on
  `AUTO_DEV_RESULT` (a timed-out session has no parseable sentinel).

## Consequences

- One extra `gh api .../git/refs/heads/<branch>` call per no-merged-PR timeout
  candidate. Bounded (only that subset) and fail-open.
- No new model field, enum value, or event type — the annotation is a single
  nullable payload key, mirroring the scope discipline of #315 / #793.

## Alternatives considered

- **Signal #2 (rejected):** treat branch-absence as evidence the work shipped →
  route to `COMPLETED`. A security review and this design pass confirmed it is
  unsafe: `pr_is_merged_for_ticket` already finds merged PRs branch-independently
  (issue linkage + retained `headRefName`), so the only case Signal #2 would
  "catch" is already handled by Signal #1; the remaining absent-branch cases are
  never-pushed / force-deleted — exactly the failures that must NOT be inferred
  complete. Routing them to `COMPLETED` would silently drop genuinely-failed
  work with no retry.

## Referenced by

- #808, #813, #315, #793
