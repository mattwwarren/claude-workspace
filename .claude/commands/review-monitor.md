---
description: "Monitor PRs from first review through merge. Polls, checks threads, delta-reviews, approves, nudges."
argument-hint: "[status | drop <PR#> | (no args = poll cycle) | natural language query]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Review Monitor

Poll monitored PRs for thread activity, perform delta reviews on new pushes, approve when all threads are addressed, and nudge authors who haven't responded.

When running from the checked-out `global-claude` repo or a Codex wrapper, prefer repo-relative script paths like `scripts/review_monitor.py`. The `~/.claude/scripts/...` examples below remain valid installed-path fallbacks.

**Arguments:** "$ARGUMENTS"

---

## Argument Routing

Parse `$ARGUMENTS` before doing anything else:

| Input | Action |
|-------|--------|
| Empty | Run the full poll cycle (Steps 1–5 below) |
| `status` | Run `scripts/review_monitor.py status` (or the installed `~/.claude/...` path) and display output, then stop |
| `drop <N>` | Run `scripts/review_monitor.py drop <N>` (or the installed `~/.claude/...` path) and confirm removal, then stop |
| Anything else | Load state via `scripts/review_monitor.py status --json` (or the installed `~/.claude/...` path), then answer the query conversationally using that data |

**Natural language query examples:**
- "where are my PRs?" — list each PR with author role (reviewer/author), open thread count, and last activity
- "who hasn't made fixes?" — list PRs where you are the reviewer and threads remain unaddressed past 24h
- "what's blocking approval on 123?" — detail open threads on PR 123, their age, and nudge status

If the argument is unrecognized and doesn't look like a natural language question, print usage and stop.

---

## Configuration

Workplace-specific defaults (which repos to auto-discover, which team to tag as reviewer) live outside the skill so the same skill ships unchanged across machines and employers. Two-tier lookup:

1. **Repo-local:** `<repo_path>/.claude/review-monitor.yaml` — overrides global, per PR's `repo_path`
2. **Global:** `~/.claude/review-monitor/config.yaml` — fallback default
3. **Unset:** skip the action. Never hardcode a team or repo.

Both files are optional; if neither exists the cycle still runs — Step 0.5 (auto-discover) becomes a no-op and Step 4c'/4e (reviewer request) just won't add reviewers.

**Schema** (both files share the same shape; repo-local typically only sets `default_reviewer`):

```yaml
# ~/.claude/review-monitor/config.yaml — global defaults
discover:
  # Each entry runs both `discover` and `recover-reviews` against the repo.
  # All worktrees of a repo roll up to one entry (use the canonical clone path).
  - repo: owner/name
    path: /abs/path/to/canonical/clone
    days: 7

# Reviewer team/user to request when an orphaned author PR enters REVIEW_REQUIRED
# with zero reviewers (Step 4c'), or when promoting a draft to ready (Step 4e).
# Leave null/unset to skip the auto-request.
default_reviewer: null

# Slack channel (or any string the Desktop drain understands) where stale
# ready_to_approve PRs get batched-bumped (Step 5a). Leave null/unset to
# skip channel bumps entirely.
channel_bump_target: null

# Override the Desktop-drain queue directory. Precedence:
#   GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR env var > this config value > built-in default.
# Built-in default is ~/.claude/review-monitor/desktop-queue/ (generic, employer-agnostic).
desktop_queue_dir: null
```

```yaml
# <repo_path>/.claude/review-monitor.yaml — repo-local override
default_reviewer: owner/team
# `discover` lives in global only — it's about which repos to scan,
# not a per-repo setting.
```

**Reading the config in the cycle:**

- For `discover` (Step 0.5): read global only. If file missing or list empty, skip the step.
- For `default_reviewer` (Steps 4c', 4e): read `<pr.repo_path>/.claude/review-monitor.yaml` first; if missing or unset, fall back to global. If still unset, skip the reviewer-request action and log a Step 5 summary row like `Skipped reviewer request (no default_reviewer configured)`.
- For `channel_bump_target` (Step 5a): read global only (the bump is cross-PR by design). If unset, skip channel bumps and log `Skipped channel bump (no channel_bump_target configured)`.
- For `desktop_queue_dir`: prefer `GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR` env var, then config, then the built-in default `~/.claude/review-monitor/desktop-queue/`.

Use a small `python3 -c` one-liner with `yaml.safe_load` (or `tomllib` if you prefer TOML — adjust filenames) to read the values; the skill stays declarative.

---

## Poll Cycle

### Step 0: Consume Pending Registrations

Projects' `ship-it.md` can drop PR announcement metadata into `/tmp/review-monitor/pending/` as a file-drop handoff (no dependency on this repo). Consume them before loading state:

```bash
~/.claude/scripts/review_monitor.py consume-pending
```

Returns `{"consumed": [...], "skipped": [...], "purged": [...]}`. Consumed keys are now registered as author-role monitored PRs with `slack_channel` + `slack_ts` populated. Safe to run always — no-op when the inbox is empty.

### Step 0.5: Auto-Discover and Recover (config-driven)

Read `~/.claude/review-monitor/config.yaml`. If the file is missing or its `discover` list is empty or absent, **skip this step entirely**. Otherwise, for each entry `{repo, path, days}` run two passes — `discover` (pick up open PRs you authored that aren't already monitored) and `recover-reviews` (catch the "review left, register skipped" failure mode where a delta/ship-it/manual review posted threads but the PR never entered the monitor):

```bash
~/.claude/scripts/review_monitor.py discover \
  --repo <entry.repo> \
  --repo-path <entry.path> \
  --days <entry.days>

~/.claude/scripts/review_monitor.py recover-reviews \
  --repo <entry.repo> \
  --repo-path <entry.path> \
  --days <entry.days>
```

Both are idempotent. `discover` returns `{"registered": [...], "skipped": [...], "repo": "..."}`; `recover-reviews` returns `{"recovered": [...], "skipped_monitored": [...], "skipped_completed": [...], "skipped_already_approved": [...], "skipped_no_open_threads": [...], "repo": "..."}`. All worktrees of a repo roll up to its single canonical entry; the dispatched auto-fix agent in Step 4 resolves the right worktree from `<entry.path>`. Each recovered PR is registered as reviewer-role at the SHA of your most recent review, so the author's later commits surface as a delta in Step 3. If `recovered` is non-empty for any entry, mention it in the Step 5 summary.

### Step 0.6: Orphaned-Auto-Dev PR Fast-Path

**Why this exists:** when an `/auto-dev` worker hits the `merge_conflict_post_push` blocker (or, worse, times out without emitting any sentinel — see global-claude issue #13), the open PR is left in a `CONFLICTING` / `DIRTY` state with no further worker activity. Until this monitor's next cycle picks it up via the normal flow, the PR sits orphaned. This fast-path identifies and prioritizes those PRs so the cycle that *does* notice them addresses them first, shrinking the orphan-window even on slow cron cadences.

This step is the consumer side of auto-dev's Step 4c.5 (`merge_conflict_post_push` blocker emission). The contract: auto-dev attempts one auto-rebase before giving up; this monitor takes over from there.

**Sequence:**

For each entry `{repo, path}` from the `discover` config above (or all configured repos if you reach this step without a `discover` list):

```bash
# Identify candidate orphan PRs: authored by @me, open, conflicting/behind,
# with no commits in the last N minutes (default: 5min — adjust per cron cadence).
gh pr list --repo <entry.repo> --author @me --state open \
  --json number,headRefName,mergeable,mergeStateStatus,updatedAt,baseRefName \
  --jq '
    .[] |
    select(.mergeable == "CONFLICTING" or .mergeStateStatus == "DIRTY" or .mergeStateStatus == "BEHIND") |
    select(.baseRefName == "main") |
    select(.headRefName | test("^(auto-dev|dev)/")) |
    {number, headRefName, mergeable, mergeStateStatus, updatedAt}
  '
```

The branch-prefix filter (`auto-dev/*` or `dev/*`) targets pipeline-authored PRs specifically — manual feature-branch PRs are excluded so this fast-path doesn't trample on human-driven work.

For each match, register it (idempotent) so the rest of the cycle treats it as a monitored PR:

```bash
~/.claude/scripts/review_monitor.py register <pr_number> --repo <repo> --role author
```

Then add it to a `priority_set` — these PRs get processed FIRST in Step 4b's auto-fix dispatch (see priority hook in Step 4b). The rationale: an orphan-from-auto-dev PR is by definition *behind* on its merge window; processing it ahead of other PRs reduces the chance it grows stale enough to break a developer's branch or get manually intervened on.

**If the candidate list is empty:** no-op, proceed to Step 1.

**If the candidate list is non-empty:** Add a Step 5 summary row per PR: `#<N> | author | orphan-fast-path: registered as <attention_state>, prioritized for auto-fix`.

**Cross-skill handoff documented:**

| Producer (auto-dev) | Consumer (this monitor) |
|---|---|
| Step 4c.5 emits `blocker.reason: "merge_conflict_post_push"` with `retry_eligible: true` after one failed auto-rebase | Step 0.6 picks up the orphan CONFLICTING PR on the next cycle and registers it; Step 4b auto-fix dispatches a rebase agent |
| Worker exits to sentinel; PR sits CONFLICTING | Fast-path priority + standard Step 4b machinery rebase/push → PR returns to MERGEABLE → orchestrator can re-dispatch the ticket if needed |

### Step 1: Load Monitored PRs

```bash
~/.claude/scripts/review_monitor.py status --json
```

This returns a JSON array of monitored PRs. Each entry includes:

| Field | Description |
|-------|-------------|
| `pr_number` | GitHub PR number |
| `repo` | `owner/repo` slug |
| `role` | `reviewer` or `author` |
| `open_threads` | Count of unresolved review threads |
| `has_delta_diff` | `true` if new commits have landed since last review |
| `delta_diff` | The unified diff of new changes (populated when `has_delta_diff: true`) |
| `touched_threads` | List of tracked thread IDs whose file:line a new commit changed. **Candidate** signal only — `check` does NOT mark these addressed. The Step 3 confirmation pass verifies each and calls `confirm-thread` for the ones genuinely resolved. |
| `nudge_ok` | `true` if it's appropriate to nudge (last nudge was >24h ago or never sent) |
| `all_addressed` | `true` if all threads are marked resolved |
| `head_sha` | Current HEAD SHA |
| `failing_checks` | List of failed CI checks (each `{workflow, name, conclusion, url}`) — populated by `check`, not `status` |
| `pending_checks_count` | Count of in-progress / queued CI checks — populated by `check`, not `status` |
| `ci_ok` | `true` when no failing checks (pending checks do not flip this) — populated by `check`, not `status` |
| `merge_state_status` | GitHub mergeability state: `CLEAN`, `DIRTY` (conflicts), `BEHIND` (needs rebase), `BLOCKED`, `UNSTABLE`, `HAS_HOOKS`, `UNKNOWN` — populated by `check`, not `status` |
| `merge_blocked` | `true` when `merge_state_status` is `DIRTY`, `BEHIND`, or `BLOCKED` — populated by `check`, not `status` |
| `attention_state` | Author-role only: `merge_blocked` / `ci_failing` / `changes_requested` / `no_reviewer` / `ready_to_approve` / `null`. Drives notification routing. `changes_requested` fires on any of: unresolved inline threads, top-level `reviewDecision == "CHANGES_REQUESTED"`, or a comment-review fallback flagged by the Step 4b' classifier. `no_reviewer` fires when a non-draft PR is `REVIEW_REQUIRED` with zero reviewers requested — an orphaned PR; Step 4c' clears it. |
| `awaiting_rereview` | Author-role only: `true` when `attention_state == "ready_to_approve"` **and** a human reviewer has already left a `COMMENTED` / `CHANGES_REQUESTED` review. Distinguishes "waiting on a re-review" (reviewer engaged, their context is now stale, a delta-check is due) from "waiting on a first review" (`false` — nobody has looked yet). Does not change routing; only refines escalation wording. |
| `reviewer_count` | Number of reviewers/teams currently requested on the PR. `0` on a non-draft `REVIEW_REQUIRED` PR is what drives `attention_state == "no_reviewer"`. |
| `change_request_source` | When `attention_state == "changes_requested"`: `"inline"` (unresolved thread), `"formal"` (`reviewDecision == "CHANGES_REQUESTED"`), or `"comment"` (Step 4b' classifier flagged a `COMMENTED` review as requesting changes). `null` otherwise. Routes the auto-fix prompt. |
| `pending_comment_reviews` | List of `{review_id, author, submitted_at, body}` for non-bot `COMMENTED` reviews submitted after the latest push / formal review, not yet classified. **Only populated when no higher-priority signal is active** (the fallback gate). The Step 4b' classifier consumes this list. |
| `needs_local_ping` | `true` when the attention state has changed since the last local ping fired |
| `needs_escalation` | `true` when a `dm_escalation` action should be enqueued for the Desktop schedule this cycle (immediate for ci/merge, 15-min grace for ready_to_approve) |
| `slack_channel` / `slack_ts` / `slack_last_seen_ts` | Optional — present when the PR was announced to Slack via a ship-it file-drop |
| `is_draft` / `base_ref_name` | Populated by `check`. Used by `auto_fix_ok` gating — drafts (especially stacked ones) are skipped automatically. |
| `auto_fix_blocked_reason` | When `auto_fix_ok == false`, a short human-readable reason: `"daily cap reached"`, `"draft"`, or `"draft stacked on '<base>'"`. |

**If the array is empty:** Print "No PRs currently monitored." and stop.

### Step 2: Check Each PR

For each PR in the list, run:

```bash
~/.claude/scripts/review_monitor.py check <pr_number>
```

This refreshes thread state, resolves any threads that GitHub has auto-resolved, and returns updated fields for that PR. Use the refreshed data for all subsequent steps.

### Step 2b: Merged-PR Follow-up Tickets

When the `check` output for a PR has `completed: true`, `pr_state: "MERGED"`, AND `deferred_threads` is non-empty, the PR landed with one or more reviewer points that we replied-to-defer (matched `is_deferral` language — "follow up", "next PR", "later", etc.) but never resolved in-code. The author already deemed them out-of-scope for the merging PR; the merge would otherwise drop them on the floor.

For each `deferred_threads` entry, create one follow-up ticket **via the active tracker** — do NOT hardcode Linear. Resolve the tracker once from `.claude/project-config.yaml` → `tracking.primary.system` (recognized: `github-issues` | `linear`; absent or missing → `linear`, the legacy default). Then file with the matching tool:

- **`linear`:** the Linear MCP plugin (`mcp__plugin_linear_linear__save_issue`).
- **`github-issues`:** `gh issue create -R <repo> --title <title> --body <body>`. NEVER call the Linear MCP in a github-issues repo — it is the wrong tracker and a headless run will stall on the unanswerable OAuth prompt.

Ticket fields (identical for both trackers):

- **Title:** `Follow-up from PR #<n>: <file>:<line>` (truncate file to basename if path is long)
- **Description:**
  ```
  Deferred during review of [PR #<n>](<pr_url>).

  **File:** `<file>:<line>`
  **Reviewer:** @<reviewer>
  **Reviewer comment:**
  > <reviewer_comment quoted line-by-line>

  **Our deferral reply:**
  > <deferral_reply quoted line-by-line>

  **GitHub thread:** <url>
  ```

Tracker-specific routing and assignment:

- **`linear`** — **Project:** route by repo / branch-prefix using the existing mapping the rest of the skill uses for ticketing (Platform for non-client work, otherwise the client project). **Assignee:** the PR's author (`gh pr view <n> --json author --jq .author.login`), mapped to their Linear user.
- **`github-issues`** — file the issue in `<repo>` itself. **Assignee:** the PR's author's GitHub login (`gh issue edit <new_issue> --add-assignee <login>`, or `--assignee` on create). Apply the repo's follow-up/tech-debt label if one exists.

Skip the PR if `deferred_threads` is empty — no follow-ups needed.

After ticket creation, log a summary row: `#<n> | author | MERGED — N follow-up ticket(s) filed: <ref>, <ref>` where `<ref>` is the active tracker's id (e.g. `LIN-1234` for Linear, `#456` for github-issues).

The `check` call already ran `complete_pr` on the script side; nothing further to do for the monitoring state.

For PRs with `completed: true` but `pr_state: "CLOSED"` (un-merged), skip — the work was abandoned and its deferrals went with it.

### Step 3: Delta Review (Reviewer Role Only)

For each PR where `role == "reviewer"` and `has_delta_diff == true`, run two passes in order:
**3a confirms** that new commits addressed open threads (moves the PR *toward* approval),
**3b scans** the delta for regressions the push introduced (kept deliberately narrow).

The delta diff is already scoped — `delta_diff` is `git diff <delta_base_sha>..<new_sha>`, the
incremental change only. Never re-review code outside it.

`delta_base_sha` is the delta-review baseline. It advances **only** when you
positively ack the delta (Step 3c below) — never as a side effect of `check`
observing it. So an unconsumed delta is never lost: if Step 3 misses a cycle,
the same delta resurfaces next cycle. You MUST reach Step 3c, or the PR will
re-surface the delta indefinitely.

**Baseline integrity — verify before trusting a negative.** `has_delta_diff`,
`delta_diff`, and `touched_threads` are only as good as `delta_base_sha`. For a
reviewer-role entry, `delta_base_sha` MUST equal the `commit_id` of your most
recent review on the PR. If it was registered against live HEAD instead (a known
mis-registration mode), the delta is computed from the wrong base, and the
author's fix commits land *before* it — invisibly. Before reporting "nothing
addressed" for a reviewer PR that still has open threads, confirm the baseline:

```bash
REVIEW_SHA=$(gh api "repos/<repo>/pulls/<pr>/reviews/<our_review_id>" --jq .commit_id)
git -C <repo_path> log --oneline "$REVIEW_SHA..<pr_head_sha>"
```

- If `delta_base_sha != REVIEW_SHA`, the entry is mis-anchored. Re-register with
  `--sha "$REVIEW_SHA"` (see Step 0.6's recovery contract) — `register` resets
  `delta_base_sha` to that SHA — then re-run `check`.
- If `git log` shows commits between the review and HEAD — **especially any whose
  message names review feedback** ("address review", "review feedback", "fix
  comments") — those are the author's fixes. `git show` them before saying
  anything.

A monitor result that contradicts direct evidence — a fix-named commit, a moved
PR HEAD, or the author stating in Slack that they pushed — is a **mis-anchored
baseline until proven otherwise**. Re-derive from `git log` / `git show` against
`REVIEW_SHA`; never rationalize the tool's negative output into a story about the
author ("forgot to push", "wrong branch") without having run `git show` on the
actual commits.

#### Step 3a: Confirm Thread Resolution

The `check` output lists `touched_threads` — tracked thread IDs whose file:line a new commit
changed. A touched line is a **candidate** for resolution, not a confirmation: the commit may
have changed that line for an unrelated reason. `check` does NOT mark these addressed — this
pass does, after verifying.

**If `touched_threads` is empty:** skip to Step 3b.

**Otherwise:**

1. Fetch the originating review comment for each touched thread (the body of the first comment
   in the thread) via `gh api graphql` against the PR's `reviewThreads`.
2. Spawn ONE confirmation Task agent (sonnet model) covering all touched threads. Use this
   prompt verbatim:

```
You are verifying whether new commits on a pull request addressed specific review comments.
You are NOT reviewing code quality and NOT looking for new issues — only judging, per thread,
whether the change resolves the concern the reviewer raised.

DELTA DIFF (the only changes since the last review cycle):
<insert delta_diff here>

THREADS TO VERIFY (each is a review comment whose file:line the delta touched):
<for each touched thread, insert: thread_id, file, line, and the original comment body>

OUTPUT RULES — follow these exactly:
1. For each thread, return a JSON object on its own line:
   - "thread_id": the thread ID
   - "verdict": "ADDRESSED" | "NOT_ADDRESSED" | "UNCLEAR"
   - "reason": one sentence grounded in the delta diff
2. ADDRESSED — the delta makes a change that resolves the reviewer's specific concern.
3. NOT_ADDRESSED — the delta touched these lines but the concern still stands (or the change
   is unrelated to what the reviewer raised).
4. UNCLEAR — you cannot tell from the delta alone whether the concern is resolved.
5. Be strict: when in doubt, UNCLEAR or NOT_ADDRESSED. A false ADDRESSED approves a PR early.
6. Output ONLY these JSON lines, one per thread. No other text.
```

3. Act on each verdict:
   - **ADDRESSED** → mark the thread resolved:
     ```bash
     ~/.claude/scripts/review_monitor.py confirm-thread <pr_number> --repo <repo> --thread <thread_id>
     ```
     This sets `code_changed`, re-runs the status transition, and prints a JSON summary
     (`status`, `all_addressed`, `unaddressed`). Use the refreshed `status` in Step 4 instead of
     the now-stale value from `check`.
   - **NOT_ADDRESSED** → leave the thread open. Optionally post a one-line reply on the thread
     noting it still looks unresolved and why (the agent's `reason`). Do not open a new thread.
   - **UNCLEAR** → leave the thread open, no reply. It will be re-evaluated next cycle if
     another commit touches it, or the author can resolve/reply directly.

#### Step 3b: Regression Scan

Spawn a bug-hunter Task agent (sonnet model) with the delta diff. Use this prompt verbatim:

```
You are a focused bug-hunter checking whether an incremental push to a pull request BROKE or
SKIPPED something. You are NOT auditing the codebase and NOT re-reviewing previously reviewed
code. Scope: only the delta diff below. It is not your job to find every imperfection — only
regressions the push itself introduced.

DELTA DIFF:
<insert delta_diff here>

OUTPUT RULES — follow these exactly:
1. Report ONLY MUST_FIX problems: a correctness bug, a security issue, or a breaking change
   that this push introduced. Do NOT report style, maintainability, or "should fix" items —
   those are out of scope for a delta pass.
2. If you find ZERO MUST_FIX issues, respond with exactly: NO_ISSUES
3. EVIDENCE DISCIPLINE: every finding MUST quote, verbatim, an added (`+`) line from the delta
   diff under an "evidence" field. A finding without a verbatim `+`-line quote is dropped.
4. For each finding, return a JSON object on its own line:
   - "path": file path relative to repo root
   - "line": line number in the NEW version of the file (from the diff's +N line numbers)
   - "body": 1-2 sentence description — the bug, why it matters, and the specific fix
   - "evidence": verbatim quote of the offending added line from the delta diff
5. Output ONLY these JSON lines, one per finding. No other text.
6. Be direct. No hedging. State the problem and the fix.
```

**Validate evidence before posting.** For each finding, confirm the `evidence` quote appears
verbatim in `delta_diff`. Drop any finding whose quote does not match — it's a hallucination
from a loose read.

**If the agent returns `NO_ISSUES`** (or all findings were dropped): no new threads. Continue to Step 3c.

**If validated findings remain:** Post them as inline review comments, then submit:

```bash
# For each validated finding:
gh review comment <pr_number> --file <path> --line <line> --body "**[Delta Review]** <body>"

# Submit the review
gh review submit <pr_number> --comment --body "Delta review of new commits — see inline comments.

🤖 Generated with [Claude Code](https://claude.ai/code)"
```

After posting, register the new threads so nudge and approval logic tracks them:

```bash
~/.claude/scripts/review_monitor.py register <pr_number> \
  --role reviewer \
  --repo <repo> \
  --repo-path <repo_path> \
  --sha <new_sha> \
  --threads <thread_id1> <thread_id2> ... \
  --thread-details '[{"id":"<thread_id1>","file":"<path>","line":<line>}, ...]'
```

(Use the thread IDs and metadata returned by `gh review comment`; pass all newly posted thread IDs via `--threads` and their details via `--thread-details`.)

#### Step 3c: Acknowledge the Delta Baseline

**Mandatory — runs on every Step 3, regardless of what 3a and 3b found.** This
is the close of the delta review. Without it, the delta baseline never advances
and the PR re-surfaces the same delta every cron cycle.

```bash
~/.claude/scripts/review_monitor.py ack-delta <pr_number> --repo <repo> --sha <new_sha>
```

Pass the `new_sha` from **this cycle's `check` result** — not live HEAD. Any
commits the author pushed *during* Step 3 stay above the baseline and surface
as a fresh delta next cycle, instead of being silently skipped.

`ack-delta` is the positive confirmation that the delta was processed: it
advances `delta_base_sha`. `check` never advances it on its own — a reviewer
delta is a lossy read, so the baseline only moves when *you* say you consumed
it. (The Step 3b `register --sha <new_sha>` call already advances the baseline
as a side effect when findings were posted; `ack-delta` is still safe to call —
it is idempotent — and is the *only* baseline advance on the NO_ISSUES and
empty-`touched_threads` paths.)

### Step 4: Evaluate and Act

For each PR, apply the logic for its role:

#### Reviewer PRs

**All threads addressed (`all_addressed == true`):**

Approve the PR:

```bash
gh review submit <pr_number> --approve --body "All threads addressed. Thanks!

🤖 Generated with [Claude Code](https://claude.ai/code)"
```

Then mark it complete:

```bash
~/.claude/scripts/review_monitor.py complete <pr_number> --reason approved
```

**Threads remain unaddressed + `nudge_ok == true`:**

Do **not** post the nudge from here — the monitor sends nothing externally.
Enqueue **one** PR-level nudge action to the Desktop queue (not per-thread; per-thread
replies generate N notifications and feel like pressure). The Claude Desktop schedule
drains the queue, posts the `gh pr comment`, and calls `record-nudge` itself.

```bash
~/.claude/scripts/review_monitor.py enqueue-action \
  --type nudge --repo <repo> --pr <pr_number> \
  --payload '<JSON — see the payload schema in "Desktop action queue" below>'
```

The payload must carry enough for the consumer to decide without re-deriving via `gh`:
`body`, `reviewers`, `state_age_hours`, `prior_nudge_count` (from `nudge_count` in
`status --json`), and `punt_reason: "reviewer nudge — not a review-based action"`.

Do **not** call `record-nudge` here — it advances the 24h cooldown and now fires at
*drain* time (the consumer calls it). The `nudge_ok` gate is unchanged: it still reads
`last_nudge_at`, which only the consumer's `record-nudge` advances.

**Threads remain unaddressed + `nudge_ok == false`:** Skip. A nudge was sent recently; no action.

#### Author PRs

Author-role PRs route by `attention_state` from the `check` result. Four side-effect channels:

- **Auto-fix dispatch:** background `Task` agent (sonnet) fixes the underlying problem and pushes a new commit. Capped at 2 attempts/PR/day (`auto_fix_ok == false` when capped).
- **Reviewer request:** `no_reviewer` PRs get the default team requested (Step 4c') — a one-line fix, no agent.
- **Channel bump:** stale `ready_to_approve` PRs accrue business-hour minutes; once ≥ 240 (4 working hours, 8a–6p ET Mon–Fri) they get batched into a single `channel_bump` queue action for the Desktop schedule to post to the configured `channel_bump_target` (skipped if unset — see [Configuration](#configuration)).
- **DM escalation:** for human-in-the-loop signals only — `dm_escalation_reason` is `"loop"` (cap hit) or `"week_old"` (PR ≥ 7 days old). Enqueued as a `dm_escalation` action; the Desktop schedule routes the DM.

The last two are **not sent by the monitor** — it never talks to Slack or sends a DM. It enqueues them to the Desktop action queue (see [Desktop Action Queue](#desktop-action-queue)).

The local ping is a separate Tier 0 — fires once on every `needs_local_ping == true` to make state changes audible.

**Step 4a: Local ping when `needs_local_ping == true`:**

Point `REVIEW_MONITOR_NOTIFY` at any local notification script that accepts
`(message, title, urgency)` — e.g. a `notify-send` / `terminal-notifier`
wrapper. If unset, the local ping is skipped.

```bash
[ -n "${REVIEW_MONITOR_NOTIFY:-}" ] && "$REVIEW_MONITOR_NOTIFY" \
  "PR #<pr_number> (<title>) — <attention_state>" \
  "Review Monitor" \
  red
~/.claude/scripts/review_monitor.py mark-notified <pr_number> \
  --repo <repo> --state <attention_state>
```

**Step 4b': Classify pending comment reviews (fallback change-request signal).**

GitHub records "Comment"-radio reviews as `state: COMMENTED`, which does **not** move `reviewDecision` off `REVIEW_REQUIRED` — so without this step a PR with a reviewer asking for changes via a plain comment review sits idle waiting for a formal CR or approval that may never come.

The `check` output surfaces `pending_comment_reviews` only when no higher-priority signal is already active (the fallback gate is enforced at source — `merge_blocked` of the code-fixable kind, `ci_failing`, or inline / formal `changes_requested` will all suppress it). So if the list is non-empty, this is the last actionable signal before the PR drops to a passive channel-bump.

For each PR with non-empty `pending_comment_reviews`:

1. Spawn ONE classifier Task agent per PR (`subagent_type: "general-purpose"`, `model: "sonnet"`, foreground — the verdict gates the next step), batching all of that PR's pending reviews into a single prompt:

```
You are classifying pull-request review comments. For each review below, decide
whether the author is requesting changes to the code (REQUESTS_CHANGES) or just
making a non-actionable comment like "lgtm", "nice", "fyi", a question for their
own understanding, or an approval-equivalent (NEUTRAL).

REQUESTS_CHANGES signals: "should", "could you", "needs", "missing",
"before merging", "concerned", "nervous", "blocker", "let's", suggestions to
modify code, asks to add/remove code, requests for more tests or more coverage,
"I'd feel better if".

NEUTRAL signals: "lgtm", "nice", "fyi", "wow", praise, generic questions for
their own context, off-topic chat.

When in doubt, NEUTRAL — false positives trigger an unwanted auto-fix loop.

REVIEWS (one per object — review_id, author, body):
<insert reviews here>

OUTPUT: one JSON object per review on its own line, no other text:
  {"review_id": "...", "verdict": "REQUESTS_CHANGES" | "NEUTRAL", "reason": "one sentence"}
```

2. For each verdict, persist it (so we never re-classify the same review):

```bash
~/.claude/scripts/review_monitor.py mark-comment-review <pr_number> \
  --repo <repo> --review-id <review_id> \
  --classification {requests_changes|neutral}
```

3. If **any** review on the PR came back `REQUESTS_CHANGES`, re-fetch `check` for that PR — `attention_state` will now be `changes_requested` with `change_request_source == "comment"`, and the existing Step 4b auto-fix branch picks it up. If all came back `NEUTRAL`, the PR stays `ready_to_approve` and falls through to channel-bump as usual.

Cycle summary row format:
`#<N> | author | comment-review classified REQUESTS_CHANGES (<author>); auto-fix dispatched`
or `#<N> | author | comment-review classified NEUTRAL (<author>); no action`

**Step 4b: Auto-fix dispatch.**

When `auto_fix_ok == true` AND any of:
- `attention_state == "ci_failing"`
- `attention_state == "merge_blocked"` AND `merge_state_status in {"DIRTY", "BEHIND"}` (i.e., conflicts or branch-behind — code-fixable)
- `attention_state == "changes_requested"`

**Dispatch ordering:** Process PRs from Step 0.6's `priority_set` (orphaned auto-dev PRs left CONFLICTING after a `merge_conflict_post_push` blocker or worker timeout) BEFORE other auto-fix candidates this cycle. The rationale is in Step 0.6; the mechanic is simply: sort the auto-fix queue with `pr_number in priority_set` as the primary key, then existing ordering. Same dispatch limits apply — the priority set jumps the line, it doesn't expand the cap.

The script enforces draft-skipping at the source: `auto_fix_ok` is `false` whenever `is_draft == true` (with `auto_fix_blocked_reason` populated as `"draft"` or `"draft stacked on '<base>'"`). Drafts are WIP by definition; stacked drafts would also rebase onto the wrong base. Note the reason in the cycle summary and move on.

`merge_state_status == "BLOCKED"` is **NOT auto-fixable** — it means missing required reviews / branch protection / unmet status checks. Fall through to channel-bump (Step 5a) or DM (Step 4d) instead.

For `changes_requested`: first fetch each thread's originator to skip bot threads. The skill replies on human threads only (sourcery is excessive most of the time and gets ignored silently). Use `gh api` to inspect the first comment author of each unaddressed thread:

```bash
gh api "repos/<repo>/pulls/<pr_number>/reviews" \
  --jq '.[] | select(.state=="CHANGES_REQUESTED" or .state=="COMMENTED") | {author: .user.login, body: .body, id: .id}'
```

A login is a bot if it ends with `[bot]`, `-ai`, or `-bot`, or matches a known list (`sourcery-ai`, `coderabbitai`, `dependabot`, `renovate`, `github-actions`, `codecov-commenter`). **Exception:** `sonarqubecloud` / `sonarcloud` / `sonarqube` (with or without `[bot]`) BLOCK merge and must be treated as human-equivalent — fix their findings, do not skip.

Dispatch one agent per PR (parallel-safe — each operates in its own worktree). Use the Agent tool with `subagent_type: "general-purpose"`, `model: "sonnet"` (spawns are async unconditionally — `run_in_background` is no longer a parameter; see #1944).

**Prompt template** (substitute placeholders per PR):

```
Fix PR #<N> in <repo>. Branch: <branch>. URL: <url>

State: merge_state_status=<DIRTY|BEHIND|UNKNOWN>, ci_ok=<true|false>.
Failing checks (if any):
- <workflow>/<name>: <conclusion> — <details_url>

Open review threads to address (if changes_requested):
- file=<path> line=<line> author=<login>: <body>
(Skip threads from bots [`[bot]`, `-ai`, `-bot` suffix or in known bot list];
sonarqube/sonarcloud are NOT bots for this purpose — their findings block merge.)

If `changes_requested` fired with no inline threads, the signal source is set on
the check output as `change_request_source`:

- `change_request_source == "formal"` — a top-level "Request changes" review.
  Pull the body and address it:
    gh api "repos/<repo>/pulls/<N>/reviews" \
      --jq '.[] | select(.state=="CHANGES_REQUESTED") | {author: .user.login, body: .body, submittedAt: .submittedAt}'
  Treat the latest non-bot CHANGES_REQUESTED review as the request to address.

- `change_request_source == "comment"` — a top-level `COMMENTED` review that the
  Step 4b' classifier flagged as REQUESTS_CHANGES. Pull the body:
    gh api "repos/<repo>/pulls/<N>/reviews" \
      --jq '.[] | select(.state=="COMMENTED") | {author: .user.login, body: .body, id: .id, submittedAt: .submittedAt}'
  Treat the latest such review as the request. After pushing fixes, do NOT
  try to re-request review (no formal CR was filed); instead post a reply on
  the review summarizing what you addressed:
    gh api -X POST "repos/<repo>/pulls/<N>/reviews/<review_id>/comments" -f body='<summary>'
  Or, if `gh review reply <review_id>` is available, use that.

REPO ROOT: <repo_path>

WORKTREE SETUP (always — required for parallel safety):
1. Run `git worktree list` from <repo_path>
2. If a worktree already tracks <branch>, cd into it
3. Otherwise create one:
   git worktree add <repo_path>/.claude/worktrees/autofix-<N> <branch>
   cd into it
4. Symlink CLAUDE.local.md and .claude/settings.local.json from <repo_path> if present (skip if missing). Symlink .env if present.

WORK:
- Rebase against origin/main: `git fetch origin && git rebase origin/main`
- Resolve conflicts. If domain knowledge is required, STOP and report — do NOT guess.
- **If you discover the branch's commits are already in main (squash-merged):** STOP and report — do NOT skip commits or force a no-op rebase. The user will close the PR. Signs: every conflicted file's HEAD-side already contains your branch's intended change; commits in your branch reference work the user has already shipped.
- For ci_failing: read failure log via `gh run view <run_id> --log-failed`, fix root cause. Do NOT skip tests, do NOT add # noqa or # type: ignore without reason.
- For changes_requested human threads: apply suggested fixes when correct. If a suggestion is unclear or wrong, post a substantive technical reply explaining why via `gh review reply <thread_id>` (this is NOT a nudge — it's clarifying disagreement on a real finding).
- Push: `git push --force-with-lease origin <branch>` (rebase rewrites history; --force-with-lease is required and safe — refuses to overwrite if remote moved unexpectedly)
- **For changes_requested (after pushing fixes): re-request review from every human who hit "Request changes".** GitHub does not auto-clear `CHANGES_REQUESTED` when new commits land — the PR stays stuck until the reviewer is re-pinged. Identify the requesters and re-request:
  ```bash
  REQUESTERS=$(gh api "repos/<repo>/pulls/<N>/reviews" \
    --jq '[.[] | select(.state=="CHANGES_REQUESTED") | .user.login] | unique | .[]')
  for r in $REQUESTERS; do gh pr edit <N> --repo <repo> --add-reviewer "$r"; done
  ```
  Skip bot logins (`[bot]`, `-ai`, `-bot` suffix; sonarqube/sonarcloud are not bots for this purpose but they also don't need re-pinging — they'll re-scan automatically on push).
- **Re-arm auto-merge after the fix — but only if a human did not deliberately disable it.** When the merge queue ejects a branch (failed check, gate failure), GitHub turns auto-merge OFF — so a fixed-and-green PR can sit OPEN forever, never merging. But a human may also have turned auto-merge OFF on purpose (e.g. to run a migration before deploy). GitHub's `autoMergeRequest` field carries no reason, so check the actor of the most recent `auto_merge_disabled` timeline event before re-arming:
  ```bash
  AM=$(gh pr view <N> --repo <repo> --json autoMergeRequest --jq '.autoMergeRequest.mergeMethod // "OFF"')
  if [ "$AM" = "OFF" ]; then
    DIS_ACTOR=$(gh api repos/<repo>/issues/<N>/timeline --paginate \
      --jq '[.[] | select(.event=="auto_merge_disabled")] | last | .actor.login // ""')
    # Re-arm ONLY when the disable is positively a bot/system action or has no
    # actor (merge-queue ejection). A human login means a deliberate hold —
    # skip and report. Why: default to NOT merging when intent is unclear; a
    # stalled PR is recoverable, a PR merged against the user's wishes is not.
    case "$DIS_ACTOR" in
      ""|*"[bot]"|github-actions|*-bot) gh pr merge <N> --repo <repo> --auto --squash ;;
      *) echo "auto-merge disabled by '$DIS_ACTOR' (human) — deliberate hold, NOT re-arming" ;;
    esac
  fi
  ```
  `gh pr merge --auto` is a no-op safety: if the PR is already queued it reports "already queued to merge" and exits non-zero — harmless. When the disable was a deliberate human hold, do NOT re-arm — report it in the Step 5 summary so the user knows their fix landed but the PR is intentionally held. Also do NOT re-arm a PR that genuinely needs the user (unresolved human threads, a real CHANGES_REQUESTED you could not address) — for those, report instead.

CONSTRAINTS:
- Do NOT amend commits, do NOT --no-verify, do NOT modify CI configs or coverage thresholds
- 70/30 rule (certainty threshold): act when you're ≥70% sure of the right move; stop and report when you're below. Don't wait for 100% before acting on something you understand, and don't plow ahead on a guess. Sub-70% certainty on a conflict resolution, a missing test, or a "fix" is a STOP signal, not a "try anyway" signal.
- Verification is not optional. Pushing without running the relevant tests / confirming green / reading the diff back means you are NOT done. The push is the last step, not the only step.
- No hard time cap. If you're making real progress, keep going. If you're spinning — repeating the same approach with no new information after 3 iterations — STOP and report what's blocking. System resources will catch infinite loops; logic loops are on you to recognize.

DELIVERABLE: brief report — new HEAD SHA, what was fixed, verification output (test counts, lint clean), push confirmation. Or: stopped-because-X with the specific blocker.
```

After dispatching, record the attempt:

```bash
~/.claude/scripts/review_monitor.py record-auto-fix <pr_number> --repo <repo>
```

**Step 4c: Channel bump (deferred to batch — see Step 5b).**

When `needs_channel_bump == true`, do nothing here — the batch step at the end of the cycle enqueues a single `channel_bump` action covering all pending bumps.

**Step 4c': Request a reviewer when `attention_state == "no_reviewer"`.**

An orphaned PR — non-draft, `REVIEW_REQUIRED`, zero reviewers requested — will never merge because nobody was asked to look at it. Look up `default_reviewer` for the PR's repo using the two-tier config lookup (repo-local → global; see [Configuration](#configuration)). If `default_reviewer` is unset, **skip the action** and add a Step 5 summary row: `#<n> | Skipped reviewer request (no default_reviewer configured) — orphan PR`. Otherwise:

```bash
gh pr edit <pr_number> --repo <repo> --add-reviewer <default_reviewer>
```

This is self-healing: next cycle `reviewer_count > 0`, the state clears, no DM is ever fired. Add a Step 5 summary row: `Requested <default_reviewer> (was orphaned — no reviewer)`. No auto-fix, no escalation — this is a one-line fix, not a loop.

**Step 4d: Enqueue a DM escalation when `dm_escalation_reason` is set.**

The DM is the only signal the user gets when they need to jump in — it must carry enough context to act without first reverse-engineering the PR. Build a **structured** message, not a one-liner and not a log dump. Required fields:

- PR number, title, URL
- Why it's stuck: `dm_escalation_reason` (`loop` = auto-fix cap hit; `week_old` = open ≥ 7 days) and `attention_state`
- For `ci_failing`: each entry of `failing_checks` as `<workflow>/<name>` + its `url`
- For `changes_requested`: `change_request_source` and the open-thread count
- For `merge_blocked`: `merge_state_status`
- One line on what auto-fix already tried this cycle and why it's still stuck (from the dispatched agent's report, if any) — a one-sentence diagnosis, **not** the agent's raw stdout or CI log tail

Message shape (fill only the rows that apply):

```
🔴 PR #<n> needs you — <reason>
<title>
<pr_url>

State: <attention_state>  ·  auto-fix attempts today: <N>
Failing: <workflow>/<name> — <url>
Tried: <one-line diagnosis of why auto-fix could not resolve it>
```

Keep it to the lines that have content. Do not paste CI logs, tracebacks, or agent transcripts into the message — link to them instead.

The monitor does not send the DM itself. Enqueue it for the Desktop schedule:

```bash
~/.claude/scripts/review_monitor.py enqueue-action \
  --type dm_escalation --repo <repo> --pr <pr_number> \
  --payload '<JSON — see the payload schema in "Desktop action queue" below>'
```

Payload fields: `message` (the structured message above), `reason`
(`dm_escalation_reason`), `reviewers`, `state_age_hours`,
`prior_escalation_count` (from `escalation_count` in `status --json`), and
`punt_reason: "<dm_escalation_reason> escalation"`.

Do **not** call `mark-escalated` here — it advances the escalation cooldown and now
fires at *drain* time, called by the consumer once the DM actually goes out.

**Step 4e: Slack announcement thread.** Reading the Slack announcement thread for
new replies has moved to the Claude Desktop schedule — the cron runs without MCP
tools, so it cannot call Slack. The monitor no longer reads or advances
`slack_last_seen_ts`; Desktop owns that cursor.

**First-cycle burst prevention:** On a freshly-upgraded state file run `~/.claude/scripts/review_monitor.py catchup` before the first cycle to avoid N concurrent pings.

### Step 5a: Batch Stale-Review Channel Bumps

After all per-PR processing, query for any author PRs that have accrued ≥ 4 business hours in `ready_to_approve` without a recent bump:

```bash
~/.claude/scripts/review_monitor.py pending-channel-bumps
```

Returns a list of `{repo, pr_number, business_minutes_in_state, last_channel_bump_at}`. If non-empty, look up `channel_bump_target` from global config. If unset, **skip** and log `Skipped channel bump (no channel_bump_target configured)` in the Step 5 summary. Otherwise fetch each PR's title + URL and build **a single batched message**. The monitor does not post to Slack — enqueue one `channel_bump` action; the Desktop schedule posts it to the configured target.

Message format (one PR per bullet, no preamble of who/why). Use the
`awaiting_rereview` value from each PR's `check` result earlier this cycle to
word the bullet — a re-review PR tells the reviewer their context is stale and
a delta-check is due, vs. a fresh PR nobody has looked at yet:

```
PRs ready for review:
• <PR #N — title> <pr_url> — re-review (changes addressed since last look)
• <PR #M — title> <pr_url> — first review
```

Enqueue the batched message as one `channel_bump` action (a singleton — one queue
file, refreshed each cycle with the current stale list):

```bash
~/.claude/scripts/review_monitor.py enqueue-action \
  --type channel_bump \
  --payload '<JSON — see the payload schema in "Desktop action queue" below>'
```

Payload fields: `message` (the batched message above), `prs` (a list of
`{repo, pr_number, state_age_hours, prior_bump_count}` — `prior_bump_count` from
`channel_bump_count` in `status --json`), and `target: "<channel_bump_target>"` (from config).

Do **not** call `record-channel-bump` here — it advances the 24h bump cooldown and
now fires at *drain* time, called by the consumer per PR once the message is posted.

Cooldown: each PR is eligible for re-bumping after 24h (handled by `pending-channel-bumps`). If the user merges or someone reviews, the next `check` advances state out of `ready_to_approve` and the PR drops off this list naturally.

**Step 4e: Promote drafts whose dependency has landed.**

Author drafts in this repo are not WIP — they're either standalone (based on `main`) or stacked on another open branch. Each cycle, promote any author draft whose base is ready.

For each monitored PR where `role == "author"`:

1. Check whether the PR is still a draft and fetch its base:
   ```bash
   gh pr view <pr_number> --json isDraft,baseRefName,headRefName,title,reviewRequests \
     --jq '{isDraft, baseRefName, headRefName, title, reviewers: [.reviewRequests[] | (if .login then .login else .name end)]}'
   ```
   If `isDraft == false`, skip — already promoted.

2. **WIP check** — if `headRefName` matches `(?i)wip` or `title` matches `^\[?WIP\]?` (e.g. `WIP:`, `[WIP]`), leave as draft. The author has explicitly marked it as work-in-progress. Add a row to the Step 5 summary: `Held as draft (explicit WIP marker)`.

2a. **Author-review-required check** — if `headRefName` starts with `docs/`, leave as draft. These are auto-drafted by the `doc-debt-branches` weekend pass (`branch_prefix: "docs"`) and are proposals that need the author's eyes before reviewers see them. The author opens them deliberately when ready by running `gh pr ready` (or merging directly). Add a row to the Step 5 summary: `Held as draft (docs/ branch — author-review required)`.

3. **Dependency check** — determine if the PR is stacked:
   - If `baseRefName` is `main` or `master`: **not stacked**. Proceed to promotion.
   - Otherwise: find a PR whose head matches `baseRefName`:
     ```bash
     gh pr list --repo <repo> --state open --head <baseRefName> --json number,isDraft,state --jq '.[0]'
     ```
     - If that PR is still open: **dependency unmet**. Add a row to the Step 5 summary: `Held as draft (waiting on parent PR #<n>)`.
     - If no such PR is found (parent merged/closed and branch deleted): **dependency met**. Proceed to promotion. Note: the child's base may need to be retargeted to `main` if the parent's branch was deleted — GitHub usually does this automatically but check with `gh pr view`.

4. **Promote, request review, AND dispatch a same-cycle rebase:**
   ```bash
   gh pr ready <pr_number>
   ```
   Then, only if no reviewers are currently requested (`reviewers` array is empty from step 1), look up `default_reviewer` for the PR's repo using the two-tier config lookup (repo-local → global; see [Configuration](#configuration)). If `default_reviewer` is set:
   ```bash
   gh pr edit <pr_number> --add-reviewer <default_reviewer>
   ```
   Don't double-tag if reviewers were already set. If `default_reviewer` is unset, skip the request — the PR is now ready but un-reviewed, which next cycle's Step 4c' will surface as `no_reviewer` (and also skip there, consistently).

   Post a confirmation comment:
   ```bash
   gh pr comment <pr_number> --body "Promoting from draft to ready for review."
   ```

   **Then immediately dispatch a rebase agent for this PR** using the Step 4b auto-fix prompt template (Bash + Agent tool, `subagent_type: "general-purpose"`, `model: "sonnet"`; the spawn is async unconditionally, see #1944). Substitute the PR's branch and metadata. The agent's `git fetch && git rebase origin/main` is a no-op if the branch is already current, so this is safe even for never-stacked drafts. Increment the daily counter:
   ```bash
   ~/.claude/scripts/review_monitor.py record-auto-fix <pr_number> --repo <repo>
   ```

   Rationale: a promoted draft whose parent just merged is almost always DIRTY against `main`. Waiting for the next cron tick to discover DIRTY and dispatch a rebase adds an hour of lag for no benefit — we already know the rebase is needed at promotion time.

   Add a row to the Step 5 summary: `Promoted draft → ready, requested <default_reviewer or "(no reviewer — default_reviewer unset)">, dispatched rebase`.

This step is idempotent — running on an already-ready PR is a no-op (the `isDraft == false` check at step 1 returns early).

### Step 5: Summary

Print a summary table of all actions taken this cycle:

```
| PR    | Repo             | Role     | Action                        |
|-------|------------------|----------|-------------------------------|
| #123  | owner/repo       | reviewer | Approved (all threads resolved) |
| #456  | owner/repo       | reviewer | Nudged 2 threads              |
| #789  | owner/repo       | author   | 3 threads need your response  |
| #101  | owner/repo       | reviewer | Delta review posted (2 findings) |
```

If no actions were taken for a PR (e.g., waiting, `nudge_ok == false`), include a "No action" row so all monitored PRs are visible.

---

## Desktop Action Queue

The monitor performs **review-based actions itself** — `gh review submit --approve`,
delta-review comments, adopting a manual review. It sends **no other external
message**. Every nudge, channel bump, and DM escalation — plus the cron's own
failure alert — is written to a local queue that a separate **Claude Desktop
schedule drains and sends**. This split exists because the cron runs `claude -p`
with no MCP tools (it cannot reach Slack at all) and because outbound messaging
wants a human-paced cadence, not an hourly one.

**Location:** resolved by [Configuration](#configuration) precedence —
`GLOBAL_CLAUDE_DESKTOP_QUEUE_DIR` env var > `desktop_queue_dir` in global
config > built-in default `~/.claude/review-monitor/desktop-queue/`. One JSON
file per pending action. Drained files move to `<queue_dir>/drained/`. If the
Desktop drain runs sandboxed (e.g. macOS Claude Desktop) and cannot read
`~/.claude/`, point the queue at a folder both processes can see — typically a
connected project folder — via env var or config.

**File schema:**

```json
{
  "action": "nudge | channel_bump | dm_escalation | cron_failure",
  "repo": "owner/name",             // null for singletons
  "pr_number": 3734,                 // null for singletons
  "queued_at": "ISO-8601",           // first enqueue; preserved across refreshes
  "refreshed_at": "ISO-8601",        // last time the producer rewrote the payload
  "sent_at": null,                   // consumer sets this at drain
  "payload": { ... }                 // action-specific, see below
}
```

**Filenames are deterministic** — `nudge-<repo_slug>-<pr>.json`,
`dm_escalation-<repo_slug>-<pr>.json`, and the singletons `channel_bump.json` /
`cron_failure.json`. Re-enqueuing the same action each cycle **refreshes** that
one file (payload updates, `queued_at` preserved) — it never piles up duplicates.

**Producer (this skill / the cron) responsibilities:**
- Enqueue via `enqueue-action`. Never post to Slack/GitHub-comments directly.
- Never call `record-nudge` / `record-channel-bump` / `mark-escalated` — those
  advance the real cooldowns and belong to the consumer at drain time.

**Consumer (Claude Desktop schedule) responsibilities:**
- The consumer is draft-first, so minutes-to-hours can pass between reading an
  action and actually sending it. **Claim the file by moving it to `drained/`
  before sending**, then re-validate the PR's current state — a concurrent cron
  cycle may have refreshed the file (new payload, `sent_at` reset) or the PR may
  have merged / been reviewed in the interim. Operate on the claimed copy.
- Perform the send, set `sent_at` on the claimed copy, then call the matching
  `record-*` so the 24h cooldowns advance from the *actual* send time. Never set
  `sent_at` on the live queue path.
- Decline freely — a declined action just stays queued (or is removed); because
  cooldowns only advance on a real send, nothing lies.

**Payload schema by action:**

- `nudge` — `{body, reviewers, state_age_hours, prior_nudge_count, punt_reason}`
- `channel_bump` — `{message, prs: [{repo, pr_number, state_age_hours, prior_bump_count}], target}`
- `dm_escalation` — `{message, reason, reviewers, state_age_hours, prior_escalation_count, punt_reason}`
- `cron_failure` — `{exit_code, log_path, log_tail}`

`prior_*_count` come from `nudge_count` / `channel_bump_count` / `escalation_count`
in `status --all --json`; they increment only when the consumer calls `record-*`.

---

## Philosophy

**Never do co-workers' work.** The goal of monitoring is not to chase or pressure — it's to make it easy for authors to finish. If a suggestion is confusing, clarify it. If an alternative approach is better, say so. If the author disagrees, discuss it.

**Encourage, don't gatekeep.** Approval is the default outcome; review threads are questions, not roadblocks. Threads get resolved when the author either makes the change or explains why they won't.

**Nudge tone.** The default nudge message is deliberately light: "Hey — just checking in on this one. Happy to clarify if the suggestion is unclear or if you'd prefer to handle it differently." This is not a demand. It's an offer to help. Never use language that implies the author is blocked, wrong, or slow.

**Delta reviews are scoped, and bias toward closing.** When new commits land, the delta agents only ever see the incremental diff — never re-litigate already-reviewed code. The first pass (3a) *confirms* whether the push resolved open threads, which moves the PR toward approval. The second pass (3b) only catches MUST_FIX regressions the push introduced. It is not the monitor's job to find every flaw in a large PR — opening a steady drip of new threads while old ones sit unaddressed makes the bot adversarial. Confirm first, scan narrowly, and let SHOULD_FIX go.

---

## Scheduling

Run on a schedule via `scripts/review_monitor_cron.sh`, invoked hourly. The
cron script gates itself to weekday working hours and skips the LLM session when
nothing is actionable.

**Linux (cron):**

```bash
# crontab -e — run hourly
0 * * * * ~/.claude/scripts/review_monitor_cron.sh
```

**macOS (launchd):** drop a LaunchAgent plist that calls the same script on a
`StartInterval` of **3600 seconds (hourly)**, then `launchctl load` it.

Hourly polling matches the cadence of normal review feedback without generating noise. A sub-hour interval is rarely worthwhile because (a) feedback seldom arrives at sub-hour resolution, (b) tier-1 local pings already fire on state changes between cycles, and (c) draft-stack promotion (Step 4e) is not time-critical.

Logs land in the path set by `LOG` in the cron script. The schedule itself has no day-of-week or hour-of-day gating — the cron script handles working-hours suppression internally (the local ping respects quiet hours; Slack escalation respects the 15-minute grace timer for `ready_to_approve`).

---

## Notes

- The state file is managed by `review_monitor.py`. Do not edit it directly.
- `register` (called with `--threads` and `--thread-details` after delta review) merges newly posted comments into the tracked state so nudge and approval logic works correctly. It is idempotent: calling it again with overlapping thread IDs is safe. Passing `--sha` also re-anchors `delta_base_sha` (the delta-review baseline).
- `ack-delta <N> --repo <repo> --sha <new_sha>` closes a Step 3 delta review — it advances `delta_base_sha` to confirm the delta was processed. `check` never advances that baseline itself (an unconsumed reviewer delta would be lost), so Step 3 MUST end with `ack-delta`. Idempotent.
- `complete --reason approved` removes the PR from monitoring. If merged without approval (e.g., author self-merged), the script will detect the closed state on the next `check` call and remove it automatically.
- `drop <N>` removes a PR from monitoring without taking any GitHub action — useful when you've handed off a review or no longer want to track a PR.
- The `gh review` CLI extension is required. Verify it's installed with `gh extension list | grep review`.
