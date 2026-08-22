---
description: "auto-dev Stage 4+5: Finalize — PR creation via /prep-pr, merge gate, auto-merge, CI wait"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 4+5: Finalize

**Orientation:** Read `.cw/context.json` for ticket context. The feature branch must be pushed to origin with review complete (Stage 3 complete). This stage creates the PR and waits on CI.

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here — `auto-dev.md` owns the single final sentinel AND the `done` stage event.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** All agent spawns in this file use explicit `model:` pins (Sonnet or Haiku). Do not change any pin to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Resolve carried-through context before emitting the sentinel

Every `AUTO_DEV_RESULT` sentinel emitted from this file requires concrete, non-null `plan_source` and `scope.tier` values (see "every field above is required" below). On a normal run these were classified in Stage 0/1 and Stage 2; on a **concierge-rescued respawn** the fresh worker starts from a re-materialized `.claude/cw-context.json` with no memory of that classification. Resolve both explicitly before filling in any sentinel template — never copy the raw placeholder text verbatim.

**Resolve `plan_source`:**
1. `.claude/cw-context.json` → `queue_metadata.plan_source` — populated by dispatch's `_persist_carried_context` write-back (`_route_staged_decision`, `src/cw/dispatch/routing.py`) from the prior stage's own sentinel, so a rescue respawn's fresh claim→spawn re-materializes it here.
2. Fallback: `.cw/context.json` — infer from the tracker (`github_issue_existing` when the ticket is a GitHub issue, the dispatch default). **If the file is absent, prose-delegate to `auto-dev-intake.md` first to materialize it** (mirroring `auto-dev-plan.md`/`auto-dev-impl.md`'s Orientation fallback) — a concierge-rescued respawn has its stale `.cw/context.json` deleted before spawn (`dispatch/gating.py:_invalidate_stale_context_json`, #1046), so this step would otherwise fall through to step 3 on exactly the rescue path it targets.
3. Fallback: `"none"`.

Use the resolved value in every sentinel `plan_source` field below.

**Resolve `scope.tier`:** mirrors `auto-dev-review.md`'s "Before emitting the sentinel, resolve `scope.tier` explicitly" — same precedence chain, same rationale (a null tier routes `apply_staged_decision` Rule 1 to `BLOCKED_ON_USER`):
1. Read `.cw/plan.md` — look for an explicit `Scope tier:`, `**Scope:** Small`, `tier: small`, or similar Stage-1c marker.
2. Fallback: read `.claude/cw-context.json` → `queue_metadata.scope_hint` (operator hint).
3. Fallback: re-derive from the diff itself using the canonical Stage-1c thresholds — run `git diff --stat $FORK_POINT...origin/<branch-name>` and count changed files and lines. **Small** = ≤10 files AND ≤500 lines AND no forbidden-area touches; **Large** otherwise.
4. If no source yields `"small"` or `"large"`, do **not** emit a `shipped` or `merge_gate_blocked` sentinel — emit `blocked` instead with `blocker.reason: "scope_tier_unresolvable"` and `scope.tier: "small"` (required by the schema validator even on blocked).

`scope.tier` must always be a concrete value (`"small"` or `"large"`) in the emitted sentinel.

**R3 edge case:** `.cw/plan.md` and `.cw/deferred-findings.md` persist across a normal requeue (`allow_dirty_reuse=True`); the only loss path is a `StaleWorktreeError` rebuild, where the plan-marker step falls through to re-derivation and the deferred-findings section is legitimately omitted.

## Stage 4: PR Creation (Merge-Gated)

### Pre-Stage Detector Guard

Before invoking Step 4a, check for an existing PR on **this ticket's branch** (distinct from Step 4a, which scans all open pipeline PRs):

```bash
gh pr view <branch-name> --json number,state,url 2>/dev/null
```

- **PR exists and is OPEN** → reuse it. Skip the `/prep-pr` create-step (Step 4c); proceed directly to Step 4d (auto-merge enablement + Linear comment if not already posted) and then Stage 5.
- **PR exists and is MERGED** → detector should have returned `merged`; surface as already-done and exit successfully with `status: shipped`.
- **PR exists and is CLOSED (not merged)** → EXIT `blocked` with `blocker.reason: "pr_already_terminal"`. Do not create a duplicate — the closed PR is a human decision the pipeline must not work around.
- **No PR exists** → proceed with normal Step 4a (Merge Gate Check) → Step 4b → Step 4c flow.

This guard makes Stage 4 idempotent across session restarts.

### Step 4a: Merge Gate Check

Before creating any PR, check for open PRs from this pipeline:

```bash
gh pr list --author @me --state open --json number,title,headRefName,mergeable,mergeStateStatus
```

Filter results for branches matching the pipeline's naming pattern (`<branch-prefix>/*`).

**If open PR found from this pipeline:**

Gather its status for the user:
```bash
gh pr checks <number> --json name,state,conclusion
gh pr view <number> --json state,mergeable,mergeStateStatus,reviewDecision
```

**An interactive run finding an open pipeline PR** is rare (headless never
reaches this branch) — the AskUserQuestion prompt and the Wait/Force/Abort
option semantics live in `.claude/commands/auto-dev-finalize-appendix.md`,
section "Step 4a — interactive open-PR prompt". Read it now if that applies; do
not improvise the prompt from memory.

- **Fix** → Enter fix mode for the prior PR: if CI is failing, spawn agent (`model: "sonnet"`) in that branch's worktree to fix and push; if merge conflicts, fetch main, merge, resolve, push; if changes requested, enter Step 5b feedback handling for that PR. Then re-check status and re-present options.

**If no open PR from this pipeline:** Proceed immediately.

**Headless:** (Small only — large already exited at S3.)

1. Collect the changed-file list for the current candidate branch:
   ```bash
   git diff --name-only <fork_point_sha>...HEAD
   ```
2. List all other open pipeline PRs:
   ```bash
   gh pr list --author @me --state open --json number,headRefName
   ```
   Filter for branches matching `<branch-prefix>/*`, excluding the current branch.
3. For each such PR, collect its changed-file list via `gh pr diff <number> --name-only` (fallback: `git diff --name-only origin/main...origin/<headRefName>`).
4. Compute the intersection of the candidate file list with each open PR's file list.
   - **Non-empty intersection** for any open PR → EXIT `merge_gate_blocked` with populated `blocker`:
     ```json
     "blocker": {
       "stage": "stage4a_merge_gate",
       "reason": "prior_pipeline_pr_open",
       "details": "PR #<number> (<headRefName>) is open and shares files with this branch: <comma-separated overlap list>",
       "recovery_hint": "Wait for PR #<number> to merge, then re-dispatch this ticket.",
       "retry_eligible": true,
       "retry_delay_seconds": null
     }
     ```
     When multiple open PRs overlap, list all overlapping PRs in `details`.
   - **Empty intersection** for ALL open PRs (or no other open pipeline PRs) → proceed to Step 4b. Log: `"All open pipeline PRs are file-disjoint — proceeding to PR creation."`

### Step 4b: Pipeline-Level PR Approval

**Headless:** AUTO-CREATE PR with auto-merge enabled (skip this gate entirely; go directly to Step 4c).

**An interactive run reaching this gate** is rare — the ship-summary prompt and
its Yes/Abort semantics live in
`.claude/commands/auto-dev-finalize-appendix.md`, section "Step 4b — interactive
ship-summary prompt". Read it now if this is an interactive run.

### Step 4c: Delegate to /prep-pr

**Do NOT spawn anything in this step. The spawn happens in Step 4c.2, and ONLY after
the Step 4c.1 gate has run.** This step delegates PR creation to a `/prep-pr` agent, but
the spawn is **gated**: your **first action is the mandatory isolation check in Step 4c.1**,
whose result sets the spawn's `isolation` flag. Spawning here — before running 4c.1 — is the
exact mistake that causes the guaranteed hang documented in 4c.1 (#1097, #1123). The
`/prep-pr` agent spawned in 4c.2 handles sync-with-main (+ conflict handling), quality-gate
detection + re-run, and ship-it delegation.

**Proceed to Step 4c.1 now — do not skip ahead to 4c.2.**

**Questioning why this stage delegates rather than creating the PR inline** is rare — the rationale lives in `.claude/commands/auto-dev-finalize-appendix.md`, section "Why Stage 4 delegates to /prep-pr". Read it now if you are tempted to inline `gh pr create`.

#### Step 4c.1 — MANDATORY isolation gate (run FIRST; NEVER skip) — #766/#1047/#1097

> **STOP.** Do **not** spawn the agent with `isolation: "worktree"` by default. You **MUST**
> run the check below first and let its result choose the `isolation` flag. Skipping this
> gate and defaulting to `isolation: "worktree"` causes a **guaranteed, non-recoverable
> hang**: every cw-spawned finalize — dev-queue dispatch **and** standalone `cw spawn
> --worktree` alike — already runs inside a pre-provisioned worktree with the feature branch
> checked out, so a nested isolation worktree's `git checkout -B <branch>` collides
> (`fatal: '<branch>' is already used by worktree ...`), the agent improvises an
> `EnterWorktree` into the real worktree, and **that call hangs with no result, killing both
> the child and this finalize session** until `needs_salvage` fires ~40 min later
> (#1097; earlier #1031/#1047).

Run this exact check before spawning:

```bash
if [ -f ".claude/cw-context.json" ]; then
  IN_DISPATCH_WORKTREE=true   # cw provisioned this worktree (dispatch OR standalone `cw spawn`)
else
  IN_DISPATCH_WORKTREE=false  # interactive hand-run from a normal checkout — the ONLY isolation case
fi
```

`cw-context.json` is written into the worktree by **both** dispatch and standalone
`cw spawn` (`src/cw/spawn.py`), so for every headless/cw-driven finalize the answer is
`true`. `isolation: "worktree"` is reserved for a human hand-running `/auto-dev-finalize`
from a normal checkout.

#### Step 4c.2 — spawn the agent, `isolation` flag SET BY the gate

Only now, with `IN_DISPATCH_WORKTREE` decided by Step 4c.1, spawn the agent.
Spawn a **general-purpose** agent (`model: "sonnet"`) scoped to run `/prep-pr`, and set its
`isolation` flag from the gate result per the two cases below:

- **`IN_DISPATCH_WORKTREE=true` (default for every headless/cw-spawned run): OMIT
  `isolation: "worktree"` entirely.** Spawn scoped to the session cwd (`worktree_path` in
  `.claude/cw-context.json` is the authoritative anchor) — no nested worktree, so no branch
  collision and no primary-checkout path to `cd` into. **Keep the git-refresh sequence below
  unconditionally** (fetch + `checkout -B` + `merge origin/main`); it runs against the
  session cwd instead of a spawned worktree.
- **`IN_DISPATCH_WORKTREE=false` (interactive hand-run only): spawn WITH `isolation:
  "worktree"`** and have the agent re-checkout the feature branch from origin (the same
  push-then-recheckout pattern as the Stage 3b fix loop) before invoking `/prep-pr`.

**A `/prep-pr` invocation blocked by the harness's own permission classifier** is rare — the known limitation (#636, deferred) and why neither the `Bash(gh pr:*)` allowlist nor a per-spawn `bypassPermissions` suppresses it live in `.claude/commands/auto-dev-finalize-appendix.md`, section "Step 4c.2: the `auto` permission-mode limitation, and the no-`/ship-it` block". Read it now if `/prep-pr` blocked on a permission prompt; the block surfaces as a BLOCK for manual ship either way.

**Agent prompt must include:**
- Branch name, fork point SHA (from Checkpoint 2), and ticket ID
- Instruction to re-checkout the branch from origin AND refresh with main:
  ```bash
  git fetch origin
  # -B (not -b): idempotent reset-to-origin. cw provisions the per-ticket
  # worktree on this same feature branch (#712), so -b would fail "already
  # exists". The reset still pulls any fix-loop pushes to origin/<branch>
  # not yet reflected locally (#1047).
  git checkout -B <branch-name> origin/<branch-name>

  # Refresh with latest main — catches upstream commits landed between the
  # last fix push and now. Required even though /prep-pr also merges main,
  # because /prep-pr runs once.
  git merge origin/main --no-edit
  ```
  If conflicts → BLOCK with file list; do NOT force.
- Once merged cleanly, push immediately — before any quality-gate work begins (GEN-5343: the merge commit must not exist only in this local worktree while `/prep-pr`'s quality gates run for up to 5400s):
  ```bash
  git push origin HEAD:refs/heads/<branch-name>
  git fetch origin <branch-name>
  test "$(git rev-parse origin/<branch-name>)" = "$(git rev-parse HEAD)"
  ```
  If the push fails, or the verify comparison mismatches → do NOT invoke `/prep-pr`. STOP and return a BLOCK whose text includes the verbatim push failure output (or the mismatch detail) — the Unavailability classifier below inspects that text and, on a signature match, emits the `push_auth_failed` sentinel via the existing template (`stage_reached: "stage4b_pr_create"`); otherwise it falls through to a generic `agent_block` per the "Any other agent BLOCK" gate-collapse row.
  This `git push` has no `timeout` wrapper — no push site in this file family does (#1414 R9: accepted residual risk, consistent with `ship-it.md`'s push and `prep-pr.md`'s own Step 1 push).
- Instruction to invoke `/prep-pr --skip-review --base main` via the Skill tool. **If the user chose Force at Step 4a (stacking onto an open pipeline PR), append `--draft`** so `/prep-pr` passes it through to the project's `/ship-it` (its Step 8 supports `--draft` pass-through). The PR must stay a draft until the parent merges. `--skip-review` is required: Stage 3 already ran the full scope-aware review set, and `/prep-pr`'s thinner pass would double up.

  **Headless:** when finalize itself runs headless (`--headless` in "$ARGUMENTS"), the `/prep-pr` invocation MUST include `--headless` so the flag propagates down the `prep-pr` → `ship-it` chain and every interactive gate collapses per the gate-collapse table:
  - Plain case: `/prep-pr --skip-review --base main --headless`
  - Force + `--draft` case (stacking onto an open pipeline PR): `/prep-pr --skip-review --base main --headless --draft`

  Instruct the subagent explicitly: any interactive prompt in the delegated chain that cannot be auto-resolved (`/prep-pr`'s Step-7 "Ship anyway" gate, a project `ship-it.md`'s tag confirmation) MUST surface as `agent_block` per the gate-collapse table (`docs/headless-contract.md` §2, "Any other agent BLOCK") — NEVER silently skipped.
- **Required deliverable:** the JSON output of `~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --json`, run from the worktree after `/prep-pr` returns. The friction report MUST paste this JSON verbatim — never summarized.
- Instruction: if `/prep-pr` aborts (no project `/ship-it`, merge conflicts, gate failures), escalate as BLOCK with the specific cause — do NOT fall back to inline `gh pr create`
- The friction protocol block
- The following health check block verbatim:
  ```
  ## Health Check
  - **Context usage**: <rough % or HIGH/MEDIUM/LOW>
  - **On-spec confidence**: HIGH | MEDIUM | LOW
  - **Shortcuts taken under pressure**: [list or NONE]
  - **Could work be incomplete?**: NO | MAYBE | YES (explain)
  - **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
  ```

**Unavailability classifier (#1049, generalized #1156 — RFC 0011 A2):** Immediately after the subagent returns — before the verify-script gate below — inspect its returned text (friction report / BLOCK message) for an unavailability failure (a locked SSH key, an unreachable host, a GitHub 5xx or secondary rate limit). A healthy run matches nothing and falls straight through to the verify-script gate below; **an actual match is rare** — the verbatim signature table, the `push_auth_failed` sentinel template to emit and stop on, and the three push sites the match can point at live in `.claude/commands/auto-dev-finalize-appendix.md`, section "Unavailability classifier: signature table, sentinel, and push sites". Read it now if the subagent returned any failure text; do not add, remove, or improvise a signature or a sentinel field from memory, and do NOT proceed to the verify-script gate or Step 4c.5 once a signature matches.

**Main-session re-verification (do not skip):** After the subagent returns, re-run finalize from the impl worktree (`cd <worktree>` or `git -C <worktree>`). This is the load-bearing check for #1140: `gh pr merge --auto` inside the subagent can report success while the `autoMergeRequest` read-back stays null.

```bash
~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --require-monitor --json
```

Required: parse the JSON. `status` must be `"ok"` and `pr_number` must be non-null before proceeding to Step 4c.5. If either fails:

**Interactive:** report the failed checks to the user via AskUserQuestion: "Subagent claimed ship complete but finalize failed (<failed-checks>). Re-run /prep-pr in the worktree, skip ticket, or abort pipeline?"

**Headless:** inspect the parsed JSON's `checks` array. If `automerge-enabled` is among the failed checks, emit the `automerge_not_armed` sentinel and stop — do NOT proceed to Step 4c.5. A failing verify is rare — the exact sentinel template, and why the reason must not join `FINALIZE_REGRESS_BLOCKER_REASONS`, live in `.claude/commands/auto-dev-finalize-appendix.md`, section "Step 4c re-verification failure: the `automerge_not_armed` sentinel". Read it now if the verify reported any failed check; do not improvise the sentinel from this summary alone. Any *other* failed check (PR existence, SHA match, monitor registration) collapses under the "Any other agent BLOCK" gate-collapse row — emit `blocked` with `blocker.reason: "agent_block"`; do not invent a new reason.

**If the agent returns BLOCK due to "no project `/ship-it`":** the recovery prompt is in `.claude/commands/auto-dev-finalize-appendix.md`, section "Step 4c.2: the `auto` permission-mode limitation, and the no-`/ship-it` block".

### Step 4c.5: Post-Push Mergeability Verification

**Why this exists:** `/prep-pr` syncs with main *before* creating the PR, so sibling pushes to `origin/main` can flip a freshly-opened PR to CONFLICTING before the worker exits, leaving it orphaned until something kicks `/review-monitor` (claude-workspace #305/#309). This step closes that window.

**Sequence (headless and interactive alike):**

After Step 4c's main-session re-verification confirms `pr_number` is non-null:

```bash
gh pr view <pr_number> --repo <owner>/<repo> --json mergeable,mergeStateStatus
```

Parse the response. Then:

| `mergeable` | `mergeStateStatus` | Action |
|---|---|---|
| `MERGEABLE` | `CLEAN` / `UNSTABLE` / `HAS_HOOKS` / `UNKNOWN` | **Proceed to Step 4d.** Mergeable wins regardless of secondary status. |
| `CONFLICTING` | `DIRTY` | **One auto-rebase attempt.** See below. |
| `MERGEABLE` | `BEHIND` | **One auto-rebase attempt.** See below. |
| anything else | `BLOCKED` | **Proceed to Step 4d.** BLOCKED means required reviews / branch protection — not a code-fixable conflict; the `/review-monitor` cycle handles it from here. |

**Single auto-rebase attempt (no loops):**

```bash
# In the impl worktree
git fetch origin main
git rebase origin/main
# If rebase fails with conflicts here → abort and attempt semantic auto-resolve (see below);
# if that refuses or fails, emit blocker exactly as before
git push --force-with-lease origin HEAD:<branch-name>
```

**Push-auth-failure check (#1049):** Before re-verifying mergeability, check this push's output against the **full signature table under the Step 4c classifier above** — all three families, not just the original 4 auth signatures. On a match, emit the `push_auth_failed` sentinel (same shape as the Step 4c template) with `stage_reached` and `blocker.stage` set to `"stage5_post_create"` and `blocker.details` naming this site and the matched signature, e.g. `"Step 4c.5 rebase-retry push: Permission denied (publickey)"`. Stop — do not proceed to the mergeability re-check.

After the push, re-verify mergeability once:

```bash
gh pr view <pr_number> --repo <owner>/<repo> --json mergeable,mergeStateStatus
```

- **Now MERGEABLE** → log to the cycle summary: `auto-rebase succeeded, PR <N> now mergeable`. Proceed to Step 4d.
- **Still CONFLICTING / DIRTY / BEHIND, OR rebase aborted on conflict** → run the **semantic auto-resolve attempt** below. If it refuses, or resolves but fails a gate, emit the structured `blocked` sentinel (template below). Do **not** force, do **not** attempt a second rebase, do **not** fall through silently.

**Semantic auto-resolve attempt (operator direction, #1850):**

**Why this exists:** a large share of the parks above are conflicts no human would think twice about — two branches appending disjoint CHANGELOG sections, two branches adding different imports to the same block, one branch inserting where the other changed nothing. `prep-pr.md` Step 1's *pre-push* refusal ("a mis-resolved merge is worse than a surfaced block") stands unchanged and is not touched by this step; what follows is the narrow, enumerated, fail-closed version of autonomous resolution that its reasoning does not rule out. The decision is made by a deterministic script, never by agent judgement — same orchestrator-run-fact-gate discipline as the UI Evidence Gate in Step 4d.

1. **Restore a clean state and record the revert anchor.**

   ```bash
   git rebase --abort
   PRE_MERGE_SHA=$(git rev-parse HEAD)
   ```

2. **Plain merge** (a genuine merge commit, not a history rewrite — matching `prep-pr.md` Step 1's own recipe):

   ```bash
   git fetch origin main
   git merge origin/main
   ```

   - **No conflicts** → identical to the auto-rebase-succeeded branch above: push (step 6), re-verify mergeability (step 7), proceed to Step 4d.
   - **Conflicts** → continue to step 3.

3. **Capture the conflicted set:**

   ```bash
   git diff --name-only --diff-filter=U > /tmp/conflicted-files-$CW_SESSION
   ```

4. **Classify and resolve — one attempt, no loops:**

   ```bash
   RESOLVE_OUTPUT=$(uv run python .claude/scripts/classify_merge_conflict.py resolve \
     --conflicted-files /tmp/conflicted-files-$CW_SESSION --json)
   RESOLVE_EXIT=$?
   ```

   (Repo-relative, `uv run python`-invoked — this script ships only to *this* repo's `.claude/scripts/`, never to the global `~/.claude/scripts/`, unlike `prep_pr_finalize.py`/`prep_pr_state.py` below. Same convention as `check_impl_guard_staleness.py`/`check_plan_scope_conformance.py` elsewhere in this pipeline.)

   The script resolves only three enumerated safe shapes (`one_sided_insert`, `import_union`, and a path-gated `doc_append`), atomically across every conflicted file, and writes nothing at all if any block is unsafe.

   - **Exit 1 or 2 (refused)** → `git merge --abort`, then fall through to the **existing, unchanged** `merge_conflict_post_push` sentinel below, appending to `blocker.details`: `"; semantic auto-resolve attempted — refused: $RESOLVE_OUTPUT"`.
   - **Exit 0 (resolved)** → stage the reported `resolved_files`, confirm nothing is still unmerged, and commit with a message that records what was auto-synthesized (never `--no-edit` — the default merge message is the only artifact of this event that outlives the pipeline run, since `friction_highlights` does not persist):

     ```bash
     RESOLVED_FILES=$(echo "$RESOLVE_OUTPUT" | jq -r '.resolved_files[]')
     git add $RESOLVED_FILES
     test -z "$(git diff --name-only --diff-filter=U)"   # defense in depth
     CATEGORIES=$(echo "$RESOLVE_OUTPUT" | jq -r '.categories | to_entries | map("\(.key)=\(.value)") | join(", ")')
     git commit -m "Merge origin/main (semantic auto-resolve: $CATEGORIES)" \
       --trailer "Auto-Resolved-By: classify_merge_conflict.py (#1850)"
     ```

5. **Run the project's quality gates once — this is a hard escalation valve, not a fix loop.**

   ```bash
   ~/.claude/scripts/prep_pr_state.py detect-gates
   ```

   Run each detected gate's `command` directly via Bash: foreground, **no autofix, no fix loop, no backgrounding**, fail fast on the first failure. Do NOT reuse `/prep-pr` Step 7's fix-loop machinery.

   - **Any gate fails** → revert the whole attempt and park:

     ```bash
     git reset --hard $PRE_MERGE_SHA
     ```

     Fall through to the unchanged sentinel below, appending to `blocker.details`: `"; semantic auto-resolve resolved conflicts (<categories>) but gate <name> failed: <output tail>; reverted"`. **This park is terminal for this run** — do NOT re-run the resolver, do NOT try a different resolution, do NOT re-run the gate.
   - **All gates pass** → continue to step 6.

6. **Push — plain, non-force** (a merge commit, so nothing is being rewritten):

   ```bash
   git push origin HEAD:refs/heads/<branch-name>
   git fetch origin <branch-name>
   test "$(git rev-parse origin/<branch-name>)" = "$(git rev-parse HEAD)"
   ```

   Check this push's output against the **full signature table under the Step 4c classifier above**, exactly as the rebase-retry push does. On a match, emit the `push_auth_failed` sentinel with `blocker.details` naming this site, e.g. `"Step 4c.5 semantic-resolve push: Permission denied (publickey)"`, and stop.

7. **Re-verify mergeability once:**

   ```bash
   gh pr view <pr_number> --repo <owner>/<repo> --json mergeable,mergeStateStatus
   ```

   - **MERGEABLE** → log to the cycle summary: `semantic auto-resolve succeeded, PR <N> now mergeable (categories: <list>)`, append `"semantic_merge_conflict_auto_resolved"` to `friction_highlights`, and proceed to Step 4d.
   - **Anything else** (defensive) → fall through to the unchanged sentinel below.

**Contract notes:** this step introduces no new `blocker.reason`, no new `status`, and no schema change — a failed or refused attempt parks under the pre-existing `merge_conflict_post_push` reason with only an appended `blocker.details` clause, and `friction_highlights` is already a free-form `list[str]`. Do **not** add `merge_conflict_post_push` to `FINALIZE_REGRESS_BLOCKER_REASONS` (`src/cw/auto_dev_result/schema.py`): a conflict the resolver refused is not fixed by regressing to IMPL.

**Sentinel template — `merge_conflict_post_push` blocker:** reaching this template is rare (the semantic auto-resolve above must have failed or been refused) — the full JSON, the every-field-is-required rule, the producer note, and the `/review-monitor` defense-in-depth handoff live in `.claude/commands/auto-dev-finalize-appendix.md`, section
"Post-push merge conflict: the `merge_conflict_post_push` sentinel". Read it now if the resolve attempt did not land; do not improvise the sentinel from this summary alone.

### Step 4d: Post-Ship Pipeline Bookkeeping

After `/prep-pr` returns with a PR number:

1. **UI Evidence Gate (orchestrator-run fact gate):** project `/ship-it` skills are expected to attach screenshots / video for UI changes, but agent self-report silently falls through to "skip with a note." Same orchestrator-run-gate discipline as `auto-dev-impl.md`'s Completion Artifacts: verify deterministically against the PR body, never the agent's prose.

   ```bash
   # Detect non-test frontend files in the shipped diff
   ui_files=$(git diff --name-only <FORK_POINT>...origin/<branch> \
     | grep -E '^frontends/.*\.(tsx|ts)$' \
     | grep -v -E '(__tests__|\.test\.|\.spec\.)' || true)

   # If any, check the PR body for embedded-media markers
   if [ -n "$ui_files" ]; then
     body=$(gh pr view <pr-number> --json body --jq .body)
     if ! echo "$body" | grep -qE '<img|!\[.*\]\(.*\)|user-attachments|\.webm|\.mp4'; then
       echo "UI_EVIDENCE_MISSING"
     fi
   fi
   ```

   Repos can override the frontend-file regex via a single-line `ui_paths_regex:` entry in `.claude/project-config.yaml`; absent it, the gate defaults to `^frontends/.*\.(tsx|ts)$` minus test files. Repos with no UI surface should set `ui_paths_regex: ^$` to make the gate a no-op.

   **If the gate fires (UI files present, no media markers in body):**

   *Interactive:* AskUserQuestion:
   ```
   PR #<N> changed UI files but the body contains no screenshots / video:
     <list of ui_files>

   Options:
   1. Capture now — spawn an agent in the worktree to run playwright-cli and embed in the body
   2. Ship anyway — proceed to auto-merge (you'll attach evidence post-merge)
   3. Hold — leave PR open, do NOT enable auto-merge, exit ticket
   ```
   - **Capture now** → apply the same Dispatch Detection test as Step 4c (`.claude/cw-context.json` present). **In a dispatch worktree:** spawn a `general-purpose` agent (`model: "haiku"`, no `isolation` key) scoped to the session cwd — same #766/#1047 rationale as Step 4c. **Otherwise:** spawn a `general-purpose` agent (`isolation: "worktree"`, `model: "haiku"`). Agent spawns are async unconditionally (`run_in_background` is not one of their parameters) — end the turn and resume on the completion notification rather than polling. **Non-Claude executor (opencode FINALIZE, #1670):** this file is also consumed by `opencode run`, which has no Agent tool, no Stop hook, and no completion notifications — there, do NOT attempt a spawn or a turn-end wait; run the capture inline in the current session and keep going. Either way, pass the playwright-cli capture + `gh pr edit --body` instructions from the project's `/ship-it` Step 6b. Re-run this gate after the agent returns; max 2 capture attempts before falling through to "Hold".
   - **Ship anyway** → continue to step 2 (auto-merge enable). Append `"ui_evidence_missing_user_override"` to `friction_highlights`.
   - **Hold** → skip step 2 entirely (do NOT enable auto-merge). The PR waits on the human to attach evidence and run `gh pr merge --auto --squash`. Set `pr.auto_merge: false` and `next_actions: ["attach_ui_evidence"]`.

   *Headless:* never block — append `"ui_evidence_missing"` to `friction_highlights`, set `pr.auto_merge: false` and `next_actions: ["attach_ui_evidence_and_enable_automerge"]`, skip step 2, continue to step 3. Status stays `shipped` because the PR exists; the human decides from the structured output.

   **If the gate is clean** (no UI files in diff, OR UI files plus media markers in body): proceed to step 2.

2. **Append review adjudication to the PR body (record-now for DEFER + REJECT):** Stage 3 (Checkpoint 3a) owns adjudication and stashes outcomes in `.cw/deferred-findings.md`. The pipeline session is gone by merge time (especially under `auto_merge: false`), so filing deferrals must be merge-triggered — Step H3 of the next sweep harvests the `DEFERRED-REVIEW-FINDINGS` block. Read the current PR body (`gh pr view <pr-number> --json body --jq .body`), then author the concatenated PR-body content (existing body + `.cw/deferred-findings.md` artifacts) via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule — to a scratch path (e.g. `.cw/pr-body.md`). Re-write via `gh pr edit <pr-number> --body-file .cw/pr-body.md`:

   The format written to the PR body (verbatim from `.cw/deferred-findings.md`):

   ```
   ## Review adjudication

   Rejected (intentional / documented tradeoff):
   - eval/runner.py — "drop retry wrapper" — deliberate: caller already retries at the queue layer

   <!-- DEFERRED-REVIEW-FINDINGS
   - severity: SHOULD_FIX
     summary: "extract shared retry helper"
     file: eval/runner.py
     rationale: "out of scope; revisit when a 3rd caller appears"
   DEFERRED-REVIEW-FINDINGS -->
   ```

   The example above shows the bare (unstamped) shape — still valid, and exactly what a `.cw/deferred-findings.md` written before #1840 looks like. A round-stamped entry additionally carries `[round <N>, <recorded_at>] ` in front of its `Rejected` bullet and trailing `round:` / `recorded_at:` lines inside its sentinel-block entry, recording which adjudication round settled it. Copy whatever the file holds verbatim either way — the stamps are inside the block, so the sentinels Step H3 greps are unaffected. Because Stage 3 now merges rather than overwrites that file, one PR body can legitimately carry both a REJECT and a later DEFER of the same finding; the round stamps are what make that read as history rather than as a contradiction.

   One block per PR; list every deferred finding inside the single `DEFERRED-REVIEW-FINDINGS` comment (open/close sentinels exact — Step H3 greps them verbatim). Omit the section when `.cw/deferred-findings.md` is absent or empty. For pipeline exits that never create a PR (large-scope `review_pending_approval`, or a BLOCK) there is no body to write — rejections/deferrals stay in `friction_highlights` and surface in the structured output instead.

3. **Enable auto-merge:** `gh pr merge <pr-number> --auto --squash`. Auto-merge may be enabled on a draft PR — it won't trigger until the PR is marked ready (`/review-monitor` does this when the stack parent merges) AND CI passes. Enable unconditionally, EXCEPT when the UI Evidence Gate above resolved to "Hold" (interactive) or fired in headless: then skip this step and set `pr.auto_merge` to `false`.

   **Verify after arming (#1140 — do not skip):** Skip this sub-step if the arm itself was skipped (Hold / headless UI-evidence-missing branch). Otherwise, immediately after the `gh pr merge --auto` call, read back whether it took:

   ```bash
   ~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --json
   ```

   This is the **sole** auto-merge verification on the Pre-Stage Detector Guard reuse path (reusing an existing open PR skips Step 4c and its re-verification entirely). Parse the JSON; if the `automerge-enabled` check fails:

   **Interactive:** AskUserQuestion:
   ```
   Auto-merge did not take on PR #<N> — gh pr merge --auto reported success
   but autoMergeRequest read back null.

   Options:
   1. Retry — run gh pr merge <pr-number> --auto --squash again and re-verify
   2. Leave open — do not enable auto-merge; human merges manually
   3. Abort — stop pipeline
   ```
   - **Retry** → re-run the arm command once, then re-run this verify. If it still fails, fall through to **Leave open**.
   - **Leave open** → set `pr.auto_merge: false`; continue to step 4.
   - **Abort** → stop the pipeline.

   **Headless:** emit the `automerge_not_armed` sentinel — same shape as the Step 4c template above — with `blocker.stage`/`stage_reached` set to `"stage5_post_create"` and `blocker.details` naming this site, e.g. `"Step 4d auto-merge enable (reuse path): automerge-enabled check failed for PR #<N>"`. Stop — do not proceed to step 4.
4. **Post to Linear:** Comment on the issue with PR link (skip for free-text tickets). For drafts, note in the comment: "Created as draft — stacked behind PR #<parent>; will auto-promote to ready when parent merges."
5. **Store pipeline state:** Record PR number, branch, and ticket ID for the next ticket's Step 4a merge-gate check
6. **Headless only — emit `stage.entered` (`s4_pr_created`) then proceed to Stage 5:**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s4_pr_created\",\"prev_stage\":\"s3_review_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```
7. **Proceed to Stage 5** (CI Wait)

Note: monitor registration happens inside `/prep-pr` Step 9 — do NOT re-register here.

---

## Stage 5: CI Wait

**Headless only — on entering Stage 5, emit `stage.entered` (`s5_ci_waiting`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s5_ci_waiting\",\"prev_stage\":\"s4_pr_created\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

After every push (PR creation or fix push), wait up to 10 minutes for CI to complete.

**Headless:** AUTO-SKIP entire Stage 5 — return immediately after auto-merge is enabled in Step 4d. Do not poll CI; do not run Step 5b feedback handling.

**Reaching Stage 5 at all** is rare (only an interactive run gets here) — the
full procedure for Step 5a (CI polling and the CI-failure prompt), Step 5b
(review-feedback check and its prompt), and Step 5c (continue to next ticket)
lives in `.claude/commands/auto-dev-finalize-appendix.md`, section "Stage 5: CI
Wait and review-feedback handling (interactive only)". Read it now if this is an
interactive run; do not improvise the polling loop or either prompt from memory.
The two agent spawns it refers back to are pinned here:

### Step 5a: Wait for CI (10 minutes max)

- **Fix** → Spawn agent (`model: "sonnet"`) in the worktree to investigate CI failure, apply fix, push to branch. Loop back to Step 5a. Max 2 fix attempts, then escalate.

### Step 5b: Initial Review Feedback Check

- **Address** → Spawn agent (`model: "sonnet"`) in the worktree. Agent reads all review comments, applies fixes, pushes to branch, and replies to each addressed comment summarizing the fix. Loop back to Step 5a for CI wait on the new push.

---

## Stage 4+5 Completion (headless only)

After PR creation, auto-merge enablement, and CI monitoring complete, emit the `done` event and the `AUTO_DEV_RESULT` sentinel.

**Only in standalone `/auto-dev-finalize <ticket-id> --headless` invocation. In the interactive monolith chain, `auto-dev.md` owns the `done` event and the final sentinel.**

**Validating is not emitting (#1890).** `cw result validate -` confirms the JSON is well-formed — it does not emit the sentinel. Never narrate emission as a separate act from performing it: the literal `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` frame, wrapping the validated JSON, MUST be the final characters of this same message.

**No interactive escalation, ever (#1890).** In headless mode there is no listener. Never escalate a `merge_gate_blocked` finding, a CI failure, or review feedback by asking a question and ending your turn — Stage 5 headless already auto-skips CI polling. Escalate exclusively via this sentinel's `blocker` field with `status: "blocked"`.

```bash
# Emit done event (pipeline path only)
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"done\",\"prev_stage\":\"s5_ci_waiting\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true

# Validate and emit sentinel
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<shipped | merge_gate_blocked | blocked>",
  "stage_reached": "stage5_post_create",
  "scope": {"tier": "<resolved scope.tier — see 'Resolve carried-through context' above>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<resolved plan_source (github_issue_existing | generated | free_text | none) — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": {
    "number": 0,
    "url": "<pr url>",
    "auto_merge": true,
    "base": "main"
  },
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
  "health": {
    "lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
```

Note: when `status` is `merge_gate_blocked` due to file overlap with an open pipeline PR, `blocker` may be non-null:
```json
"blocker": {
  "stage": "stage4a_merge_gate",
  "reason": "prior_pipeline_pr_open",
  "details": "PR #<N> (<branch>) is open and shares files: <list>",
  "recovery_hint": "Wait for PR #<N> to merge, then re-dispatch this ticket.",
  "retry_eligible": true,
  "retry_delay_seconds": null
}
```

The sentinel must be the LAST thing emitted. No trailing prose, no further tool calls. See `auto-dev.md` Appendix for full field reference. Contract: `cw schema stage-output finalize`.
