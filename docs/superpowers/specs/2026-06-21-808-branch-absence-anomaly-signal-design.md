# #808 — Branch-absence anomaly signal (observability-only)

**Status:** Design approved 2026-06-21. Parent epic #813. Sibling of #793 (the `completion_source` split). Supersedes the out-of-scope, security-flagged Signal #2 work on `dev/315 @ f93ea1f`.

## Problem

`cw`'s reaper declares a stalled headless session `timed_out` on wall-clock + absence of a parseable `AUTO_DEV_RESULT` sentinel. #315 (PR776) added **Signal #1** — consult PR-merged world-state before declaring `timed_out`, so a session that shipped but failed to emit a sentinel is marked COMPLETED instead of falsely TIMED_OUT.

#808 originally tracked **Signal #2** — a branch-deleted fallback that, when no merged PR is found, treats an absent dev branch as evidence the work shipped. A security review flagged this as a correctness bug, and the present design pass confirmed it: **branch-absence does not imply "merged."** A branch can be absent because it was (1) merged + auto-deleted, (2) never pushed (worker died before push), or (3) force-deleted without merge.

### Why a naive Signal #2 is both unsafe and redundant (verified)

`pr_is_merged_for_ticket` (`src/cw/gh.py:122`) finds a merged PR via two paths, **neither of which depends on the branch still existing**:
- primary: `_fetch_issue_pr_refs(ticket_id)` — PRs linked to the GitHub *issue*; branch-independent.
- fallback: `_fetch_branch_merged_pr(branch)` → `gh pr list --head <branch> --state merged`, which matches on the PR's recorded `headRefName`. GitHub retains this on the merged PR record **after** the branch is deleted.

Therefore, in the only case Signal #2 wanted to catch (merged + auto-deleted), Signal #1 already returns `(True, True)`. When Signal #1 returns `(False, True)` (no merged PR), an absent branch is necessarily case (2) or (3) — exactly the cases that must **not** be treated as completed. Routing them to COMPLETED would invert #315's false-negative into a worse false-*positive*: silently dropping genuinely-failed work with no retry.

A scan for a real incident where Signal #1 missed a true completion that a branch check would have caught found **none**. So branch-absence carries no safe *completion* value Signal #1 doesn't already provide.

## What this design does instead

Branch-absence is not a completion signal — but it *is* a useful **diagnostic** signal. A session that times out with no sentinel, no merged PR, **and** an absent branch is an anomaly (worker died pre-push, or branch force-deleted), categorically different from an ordinary slow/stuck timeout. This design surfaces that distinction **without changing any behavior**.

### Non-negotiable invariant

Branch-absence **never** routes a session to COMPLETED. The branch check is used **only** to annotate the `SESSION_TIMED_OUT` event. Retries, attempt-counting, and cost accounting are unchanged.

### Control flow

In the existing reaper pre-pass (`revert_stalled_headless_sessions` in `src/cw/reconcile/stalled.py`), after Signal #1 runs per candidate:

- `pr_is_merged_for_ticket` → `(True, True)` → COMPLETED — *unchanged*.
- `(None, _)` / gh-absent `(_, False)` → existing fail-open handling — *unchanged*.
- `(False, True)` (no merged PR) → **NEW** — call `branch_exists_on_origin`:
  - `(False, True)` branch **absent** → still `timed_out` → task reverts to PENDING (retry); the `SESSION_TIMED_OUT` event is tagged `branch_state: "absent_no_merged_pr"`.
  - `(True, True)` branch **present** OR `(None, _)` error / unavailable → `timed_out`, **no tag** (fail-open — never block on the gh call).

The branch check is purely diagnostic; the disposition (TIMED_OUT → PENDING) is identical in every branch.

## Surface — minimal footprint

Add a single nullable `branch_state` field to the **`SESSION_TIMED_OUT` event payload** only.

- **No** `src/cw/models.py` change.
- **No** new `CompletionReason` / `ReapReason` enum value.
- **No** new event type.

This mirrors the scope-down discipline of #315 / #793 (no new reason, no synthesized sentinel, no new event type). The per-candidate annotation is threaded via a `branch_absent_ticket_ids: frozenset[str]` forwarded through `_act_on_stalled_candidates`, exactly mirroring the existing `merged_ticket_ids` / `gh_blocked_ticket_ids` forwarding pattern (`stalled.py:255`, `:594`). The `SESSION_TIMED_OUT` emit site adds `branch_state` to its payload only when the ticket is in `branch_absent_ticket_ids`; in every other case (branch found, check error, check not run), the key is **omitted** (a consumer's `payload.get("branch_state")` returns `None`).

`branch_state` value vocabulary (two outcomes):
- `"absent_no_merged_pr"` — anomaly: no merged PR and branch gone (never-pushed / force-deleted).
- *omitted* — every other case: branch present, check error, check not run (fail-open; no claim made).

## Reuse, don't reinvent

Salvage `branch_exists_on_origin()` from `dev/315 @ f93ea1f`:
- `src/cw/gh.py`: `branch_exists_on_origin(ticket_id, *, branch, timeout=10) -> tuple[bool | None, bool]` via `gh api repos/{owner}/{repo}/git/refs/heads/{branch}` (HTTP 404 = absent → `(False, True)`, 200 = present → `(True, True)`, transient → `(None, True)`, gh absent → `(None, False)`). Follows the `_fetch_branch_merged_pr` (`gh.py:75`) implementation + return-tuple shape.
- `src/cw/reconcile/_deps.py`: re-export it (import + `__all__` entry).

**Reroute** its result to the event annotation — do **not** wire it into the merged path the way `dev/315` did.

## Files

| File | Change |
|------|--------|
| `src/cw/gh.py` | `+ branch_exists_on_origin` + `_fetch_branch_exists_on_origin` (salvaged) |
| `src/cw/reconcile/_deps.py` | `+1` import, `+1` `__all__` entry |
| `src/cw/reconcile/stalled.py` | pre-pass branch check on `(False, True)` candidates; forward `branch_absent_ticket_ids`; tag `SESSION_TIMED_OUT` payload |
| `tests/test_reconcile.py` | new + updated tests (below) |

Do **not** touch `src/cw/models.py`, `src/cw/reconcile/core.py`, `src/cw/reconcile/_shared.py`.

## Tests (`tests/test_reconcile.py`)

Mock seams: `cw.reconcile._deps.pr_is_merged_for_ticket` and `cw.reconcile._deps.branch_exists_on_origin`. Pass an explicit `now` past budget. Reuse `_mk_headless_daemon_session`, `_auto_config()`, `FakeNativeDaemonClient`, `read_events`.

- **(a)** no merged PR `(False, True)` + branch absent `(False, True)` → session `TIMED_OUT`, task `PENDING` (retry — **NOT** COMPLETED; this is the security assertion), `SESSION_TIMED_OUT` emitted with `branch_state == "absent_no_merged_pr"`, `SESSION_COMPLETED` **not** emitted.
- **(b)** no merged PR + branch present `(True, True)` → `TIMED_OUT`, `branch_state` key **absent** (key omitted, not `"present"`).
- **(c)** branch-check transient error `(None, True)` → `TIMED_OUT`, `branch_state` key omitted from payload (fail-open).
- **(d)** merged PR `(True, True)` → COMPLETED; `branch_exists_on_origin` **not called** (assert call_count == 0).
- **(e)** transient PR error `(None, True)` → existing `_merged is None` guard fires first; `branch_exists_on_origin` **not called**; `TIMED_OUT`.
- Update any existing `revert_stalled_headless_sessions` tests whose candidates now reach the new branch-check call to stub `branch_exists_on_origin` (default `(True, True)` = present, no anomaly tag).

Patch coverage ≥90% on changed lines, including the fail-open and not-called paths.

## Documentation updates (in-scope for this ticket)

The annotation is only useful if the disposition tooling and operators know to read it.

- **`docs/session-disposition.md`** — add a subsection (under §5 "The orphan condition", cross-linked from §3) documenting:
  - the `branch_state` field on `SESSION_TIMED_OUT` and its two-outcome contract;
  - that `absent_no_merged_pr` is an **anomaly** (never-pushed / force-deleted), distinct from an ordinary timeout, but still retried — **never** inferred-complete;
  - the rationale (branch-absence ≠ merged) and cross-references to #315 (Signal #1) and #808.
- **`docs/dispatch-runbook.md`** — in the disposition/monitor section (around the `cw-session-watch` breadcrumb, ~line 59–70), add a one-line breadcrumb: a `SESSION_TIMED_OUT` carrying `branch_state: absent_no_merged_pr` means the worker died before push or the branch was force-deleted — investigate the worker, don't just let it churn through retries.

## Skill updates (in-scope for this ticket)

- **`.claude/skills/cw-session-watch/SKILL.md`** — document that a `session.timed_out` outcome may carry `branch_state` on the event: `"absent_no_merged_pr"` means anomaly (died-pre-push / force-deleted); absent key means ordinary slow timeout. This skill reads the event bus, so it is the primary consumer.
- **`.claude/skills/cw-fanout/SKILL.md`** — light cross-reference: when a watched ticket fires `session.timed_out`, note the `branch_state` field as the discriminator for "stuck/slow" vs "died-pre-push/force-deleted".
- **`.claude/skills/cw-queue-peek/SKILL.md`** — peek is RUNNING-only and `timed_out` is terminal, so this is at most a one-line cross-reference to `session-disposition.md`'s new subsection; no behavioral change to the peek ladder.
- `.claude/skills/cw-followup/scripts/parse_sentinel.py` — **no change**: `branch_state` lives on the reaper-emitted `SESSION_TIMED_OUT` event, not on the `AUTO_DEV_RESULT` sentinel (a timed_out session has no parseable sentinel — that is why it timed out).

## Non-goals

- Inferring completion from branch-absence (the rejected, unsafe Signal #2).
- Any new model field, enum value, or event type.
- Changing retry / attempt-counting / cost-accounting behavior.
- Backfilling historical `timed_out` sessions.
- A `completion_source` marker or `session.completed_inferred` event (that is #793).
