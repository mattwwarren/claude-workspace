---
description: "auto-dev Stage 2: Implement — spawn impl agent in worktree, commit, push branch"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "Skill"]
---

# auto-dev Stage 2: Implement

**Orientation:** Read `.cw/plan.md` for the approved plan from Stage 1. Read `.cw/context.json` for ticket context (or prose-delegate to `auto-dev-intake.md` first if absent). **`.cw/plan.md` being absent** is rare — the tracker-aware plan-recovery fallback (#943/#1906), the per-tracker authorship checks it must satisfy before honoring a `<!-- plan-spec-reviewed` marker, and the `plan_missing` exit that fires only once every tracker branch is exhausted live in `.claude/commands/auto-dev-impl-appendix.md`, section "Orientation: tracker-aware plan recovery when `.cw/plan.md` is absent". Read it now if the plan file is missing; do not improvise a recovery path, and do not exit `plan_missing` from this summary alone.

**Comments are live, not cached (#1794).** The cached `comments` array in `.cw/context.json` is a Stage-0 (or earlier Stage-2 re-materialization) snapshot only. Dispatch spawns `/auto-dev-{stage.value} <ticket> --headless` **directly per stage** (`src/cw/executor.py`, RFC 0005 A2) — Stage 0 does NOT re-run between pipeline stages, so on an IMPL-stage re-entry (queue re-dispatch, `--regress --stage impl`, or a resumed `s2_implementing` session) the cached array can be arbitrarily stale and an operator send-back comment would never reach this stage. Regardless of whether `.cw/context.json` already exists, this stage MUST live-fetch the ticket comments on every invocation via the active tracker's fetch op (`list_comments(<id>)` for `linear`; `gh issue view <n> --json comments` for `github-issues`), overwrite `.cw/context.json`'s `comments` array with the fresh result (each entry mapped to `{"author": "<login>", "created_at": "<createdAt>", "body": "<text>"}`), and bump `materialized_by_session` to the current session id. Mirrors `auto-dev-plan.md`'s "Comments and body are live, not cached" paragraph. Also write the fresh array verbatim to `/tmp/impl-comments-$CW_SESSION.json` — the Pre-Stage Detector Guard below consumes it. **WARN on comments-fetch failure** (mirrors `auto-dev-intake.md`'s Step 0d): if the live fetch exits non-zero or returns malformed JSON, emit an attention signal, log `"impl_comments_fetch_failed"` in `friction_highlights`, and leave the existing `comments` array untouched rather than overwriting it with an empty/failed result — a stale-but-real array is better evidence than none. Still write whatever was fetched (possibly `[]`) to `/tmp/impl-comments-$CW_SESSION.json`; the staleness script degrades gracefully on a bad or empty comments file without masking an independently-sourced `regressed_into_stage` signal.

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here; the monolith owns it.

**Arguments:** "$ARGUMENTS"

---

> **Model selection (scope-based):** resolve `$IMPL_MODEL` from the ticket's scope tier via
> "Resolve the impl model from scope tier" below **before** spawning — never `model: inherit`
> (it propagates the operator's Opus default into every fan-out).

## Stage 2: Implement (Agent in Worktree)

### Dispatch Detection — #766 (skip redundant EnterWorktree when already in a cw worktree)

**Before spawning the Stage 2 agent, check whether this session already runs
inside a cw dispatch worktree:**

```bash
if [ -f ".claude/cw-context.json" ]; then
  IN_DISPATCH_WORKTREE=true
else
  IN_DISPATCH_WORKTREE=false
fi
```

Fail open to interactive when the file is missing, unreadable, or the field is
absent/false — the authoritative check `_is_headless()` implements.

**Needing the value-based `"headless"` check, or the reason a nested worktree is
harmful,** is rare — both live in `.claude/commands/auto-dev-impl-appendix.md`,
section "Dispatch detection: the alternative check and the #766 nested-worktree
leak". Read it now if the presence check above is ambiguous; do not improvise
the detection rule from memory.

### Resolve the impl model from scope tier

**Before spawning the impl agent, resolve `$IMPL_MODEL` from the ticket's scope tier** — the
same ladder Stage 3 (`auto-dev-review.md`) and finalize use, minus the post-impl diff-stat
source (no diff exists yet):

1. Read `.cw/plan.md` — look for an explicit `Scope tier:`, `**Scope:** Small`, `tier: small`,
   or similar Stage-1c marker.
2. Fallback: `.claude/cw-context.json` → `queue_metadata.scope_hint` (the operator's
   `cw dev-queue add --scope` hint).
3. Fallback (unresolvable): default to `small`, per the pipeline's existing
   unresolvable→`small` convention (`auto-dev-review.md`, `auto-dev-finalize.md`).

Map the resolved tier to `$IMPL_MODEL`: **`large` → `"opus"`**, **`small` → `"sonnet"`**.
Impl is a fanned-out subagent, and Sonnet is the CLAUDE.md implementation default; the
unresolvable case defaults to `small`/Sonnet, and the operator can re-dispatch with
`--scope large` to force Opus.

**Spawn shape depends on mode AND dispatch context** (all variants pass `model: $IMPL_MODEL`
resolved above — Sonnet for Small scope, Opus for Large):

- **Not in a dispatch worktree (either mode):** `isolation: "worktree"`, `model: $IMPL_MODEL`.
- **In a dispatch worktree (either mode):** **omit `isolation: "worktree"` entirely** — the
  dispatch worktree IS the sandbox. Spawn with no `isolation` key and
  with `model: $IMPL_MODEL` — the agent works directly in the current cwd, and
  `worktree_path` in `.claude/cw-context.json` is the authoritative anchor for all git
  operations.

**Async dispatch note (verified 2026-08-19).** The Agent tool is asynchronous
unconditionally: waiting for the impl agent means **ending the parent turn** and resuming
on its completion notification, which is safe in headless. **Never** hold the turn open
with no-op `Bash` calls (`true`, `sleep`, repeated polls).

**Being tempted to busy-wait, or writing the impl agent's own turn-ending
instructions,** is rare — the full reasoning (why headless turn-ending is safe,
why the poll defeats the liveness sweep, and why a subagent's turn-end is a
*return* rather than a pause) lives in
`.claude/commands/auto-dev-impl-appendix.md`, section "Async dispatch: why never
to busy-wait on the impl agent". Read it now if either applies.

### Worktree Isolation Guard (headless) — #402

**A headless worker MUST NEVER run a git mutation against the operator's main
checkout.** The authoritative working directory for this stage is the worktree
recorded as `worktree_path` in `.claude/cw-context.json` (injected by `cw` at
spawn — see `spawn.py:_write_hook_context`); the impl agent additionally runs
inside its own `isolation: "worktree"` sandbox. Every `git`, `git -C <path>`,
or `cd`-then-git invocation in this stage MUST target that worktree (or the
trap-cleaned temp worktree created below), **never** the client's
`workspace_path` (the operator's live checkout).

**Being tempted by any direct-git fallback onto another checkout** is rare — the
#402 breach this codifies is described in
`.claude/commands/auto-dev-impl-appendix.md`, section "Worktree isolation: the
#402 breach this codifies". Read it now if that temptation arises; the rules
below are binding regardless:

- If the isolation worktree or `worktree_path` is unreachable, or any step is
  tempted to fall back to direct git on another checkout, **do NOT fall back**
  — EXIT `blocked` with `blocker.reason: "impl_failed"` and let the
  orchestrator re-dispatch on a fresh worktree. Mutating a shared checkout to
  make progress is never an acceptable workaround.
- Before any commit/push, confirm the cwd resolves under `worktree_path` (or
  `$TMPWT`); if it resolves to `workspace_path`, abort and exit `blocked`.

### Pre-Stage Detector Guard

Before starting S2 work, run `detect_current_stage()` (see [Resume Detection](#resume-detection)).

- **`pre_flight`, any `s1_*`, or no detector signal** — the common path: proceed
  with fresh implementation as specified below.
- **Any other verdict** (`s2_implementing`, or a stage past S2: `s3_*`, `s4_*`,
  `s5_*`, `merged`) means this ticket already carries branch work, which is rare
  — the resume dispositions, and the staleness/regress check (#1794) that MUST
  run before them whenever the detector reports a stage past S2, live in
  `.claude/commands/auto-dev-impl-appendix.md`, section
  "Pre-Stage Detector Guard: resume dispositions and the staleness check (#1794)".
  Read it now; do not decide from this summary whether to advance, resume, or
  re-implement, and never re-implement over existing branch work by default.

**Headless only — before spawning Stage 2 agent, emit `stage.entered` (`s2_impl_started`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s2_impl_started\",\"prev_stage\":\"s1_plan_reviewed\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

Stage 2 agent spawn:

**Prompt must include:**
- The approved plan (full text)
- Branch naming: `<branch-prefix>/<ticket-id-or-short-slug>` (default prefix: `dev`)
- **Branch freshness:** Before starting work, sync with latest main:
  ```bash
  git fetch origin main
  git merge origin/main
  ```
  A merge conflict here is a BLOCK in friction — the worktree starts from a conflicted state and cannot proceed.
- **Record the fork point** immediately after merging main — the exact commit the feature branch diverged from, used for all deterministic diffs downstream:
  ```bash
  FORK_POINT=$(git merge-base origin/main HEAD)
  ```
  Include `FORK_POINT` in the friction report. The value is immutable and must be used for every subsequent diff operation in the pipeline.
- Phase 1: write tests, run in isolation, confirm they FAIL
- Phase 2: implement fix, run tests again, confirm they PASS
- Post-implementation: `uv run ruff check --fix` on changed files
- **Type check gate (do NOT declare complete until clean):**
  - Run `uv run mypy <touched_files>` (pass the changed paths explicitly, not `.`).
  - Fix every mypy error in touched files, pre-existing ones included — global CLAUDE.md treats those as active bugs, not noise to inherit.
  - Adding `# type: ignore`, `# noqa`, or weakening a type to `Any` requires explicit user approval. If you reach for one, STOP and report it as a BLOCK in friction with the specific error and proposed ignore. Do NOT add it unilaterally.
  - If touched files transitively expose untouched-file mypy errors that did not exist before your edit, those are your errors too — fix them or report as BLOCK.
- Instruction to read model/schema definitions before writing code
- Instruction to use Read/Write tools for file operations, not Bash cp/mv/cat
- **Pre-mutation guard (hard, not prose) — #766:** Before any `git add`, `git commit`,
  or `git push`, run:
  ```bash
  python .claude/scripts/check_not_main_checkout.py
  ```
  The script reads `.claude/cw-context.json` (searching upward from cwd) and exits
  non-zero with a `BLOCKED (#766)` message if the current git repo root matches the
  operator's main checkout (`workspace_path`). On non-zero exit: DO NOT proceed with the
  git operation — EXIT `blocked` with `blocker.reason: "impl_failed"`,
  `blocker.details: "check_not_main_checkout exited <N>: <stderr>"`. It is a no-op
  (exit 0) when no dispatch context is found, so interactive runs are unaffected. If the
  script is absent (pre-#766 checkout), skip the check and log
  `"check_not_main_checkout: script absent, skipped"` in friction, but do NOT proceed
  past any git op resolving to a path other than `worktree_path`.
- Instruction: if anything fails or surprises you, report it in friction — do NOT silently skip or suppress
- **Incremental commits required** (Subagent Reliability Mitigation 3): commit after every logical step — do NOT defer all commits to the end, or an OOM/crash loses all the work. For each file or coherent feature: make changes → `git add <files>` → `git commit -m "..."`. The friction report's `git log --oneline` output (see Completion Artifacts below) MUST show more than one commit for any non-trivial change; a single end-of-run commit is a discipline failure.
- Instruction to stage and commit changes with a conventional commit message
- **Branch discipline (pre-commit and pre-push):** `isolation: "worktree"` provisions the worktree on an auto-generated session branch (e.g. `agent-<hash>`), NOT on `<branch-name>`; never assume the local branch matches the feature branch name. Before committing and again before pushing, run `git branch --show-current` and record the actual local branch in the friction report.
- **Instruction to push the branch to origin** after committing — use the explicit-refspec form so the local branch name (a session branch) need not match the remote feature branch:
  ```bash
  git push -u origin HEAD:refs/heads/<branch-name>
  ```
  Do NOT use the short form `git push -u origin <branch-name>`: when the local branch is `agent-<hash>` it either fails or pushes the wrong ref. After pushing, verify with `git rev-parse origin/<branch-name>` and confirm it matches `git rev-parse HEAD`. Subsequent stages (fix loop, PR creation) depend on the branch being on origin rather than locked inside an isolation worktree new subagents cannot reach.
- Instruction to include the final pushed commit SHA, the local branch name (from `git branch --show-current`), and the push confirmation (`origin/<branch-name>` SHA) in the friction report
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
- **The following Completion Artifacts block verbatim** (Subagent Reliability Mitigation 2 — orchestrator gates on these, NOT on the prose claim of "done"):
  ```
  ## Completion Artifacts (required for "done" claims)
  - **Test command used:** <exactly what was run>
  - **Test output (tail):** <paste last 50 lines verbatim>
  - **git diff --stat (vs fork point):** <paste verbatim — git diff --stat $FORK_POINT> (advisory — orchestrator re-runs live)
  - **git log --oneline (vs fork point):** <paste verbatim — git log --oneline $FORK_POINT..HEAD>
  - **git diff line count:** <total lines added + removed>
  - **Quality gate results — one row per gate command** (report every command named in this session's quality-gate sentence as its own row — e.g. `ruff check` and `ruff format --check` are separate gates and MUST be separately reportable, never collapsed into a single "ruff" line): for each configured gate, report `<command>` → `pass` | `<exit code + error output>` | `not_run`. A gate you did not run MUST be reported `not_run` — never omitted, never folded into a sibling gate's result, and never reported `pass` on the strength of a different command.
  ```
  Hallucinating or paraphrasing these is a discipline failure — the orchestrator parses them. Pasted output contradicting the "done" claim (FAILED in test tail, non-empty mypy errors, empty diff) results in `impl_failed`, and a report missing a row for any gate named in the session's quality-gate sentence draws the same `impl_failed` disposition.

### Step 2.5: Orchestrator Completion Gate

After the implementation agent returns (or times out — see Mitigation 4), before logging to Checkpoint 2, the main session runs deterministic gates against the impl by verifying the pushed branch on `origin`. Do NOT trust the prose "done" claim alone; do NOT read from the impl isolation worktree (the orchestrator cannot reach it).

**Pre-gate: verify branch was pushed**

```bash
git fetch origin main <branch-name>
git rev-parse --verify origin/<branch-name> || { echo "IMPL_NOT_PUSHED"; exit 1; }
```

Fetching `main` alongside `<branch-name>` ensures `origin/main` is current before the `merge-base` call below. If the impl branch ref is absent → EXIT `blocked` with `blocker.reason: "impl_not_pushed"`. The agent claimed done but nothing was pushed; the empty-diff vacuous pass would fire on every subsequent gate.

**Gate setup: create one trap-cleaned temp worktree at `origin/<branch-name>`**

```bash
FORK_POINT=$(git merge-base origin/main origin/<branch-name>)
: "${CW_SESSION:?CW_SESSION must be set}"
TMPWT="/tmp/gate-wt-$CW_SESSION"
# Deterministic path (keyed on $CW_SESSION, not $$) — reconstructable by an
# external reconciler even if this invocation is SIGKILLed before any trap runs.
# Self-heal: a prior invocation for this same session may have been killed
# after `git worktree add` registered the entry but before cleanup ran.
git worktree remove --force "$TMPWT" 2>/dev/null
rm -rf "$TMPWT" 2>/dev/null
git worktree prune
gate_wt_cleanup() { git worktree remove --force "$TMPWT" 2>/dev/null; rm -rf "$TMPWT" 2>/dev/null; }
trap gate_wt_cleanup EXIT
# INT/TERM must also actually stop the script — a trap alone only runs
# cleanup and then resumes execution; without the explicit exit here the
# gate would keep running gate checks after the harness believed it had
# killed this invocation.
trap 'gate_wt_cleanup; exit 143' INT TERM
git worktree add --detach "$TMPWT" origin/<branch-name> || { echo "IMPL_FAILED: git worktree add exited $?"; exit 1; }
```

All gates below run inside `$TMPWT`. Do NOT run gates from the cw session worktree with origin-qualified refs — that cwd confusion is the bug this step fixes. FORK_POINT is recomputed from `origin` refs; do not trust the impl agent's reported value.

**Gate checks (all run in `$TMPWT`):**

1. **Diff is non-empty:**
   ```bash
   git -C "$TMPWT" diff --stat "$FORK_POINT" | grep " changed" || { echo "IMPL_FAILED: empty diff"; exit 1; }
   ```
   Empty diff → `impl_failed`. The agent claimed work; no work exists.

   > **Not the only line of defence (#1870)** — why, and where the second check
   > lives, is in `.claude/commands/auto-dev-impl-appendix.md`, section
   > "Step 2.5 gate 1: why the empty-diff check is not the only line of defence (#1870)".
   > Nothing changes here; this gate stays the first and cheapest catch.

2. **File set is within the plan's enumeration** (mechanical, not prose — #1779):
   ```bash
   git -C "$TMPWT" diff --name-only "$FORK_POINT" | sort > /tmp/touched_files-$CW_SESSION
   SCOPE_CONFORMANCE_OUTPUT=$(uv run python .claude/scripts/check_plan_scope_conformance.py \
     --plan .cw/plan.md --touched-files /tmp/touched_files-$CW_SESSION)
   SCOPE_CONFORMANCE_EXIT=$?
   ```
   Note the script itself runs from the **cw session worktree**, not `$TMPWT`: `.cw/plan.md` is session state that was never committed to the branch, so it does not exist inside the detached gate worktree. Only the file-set extraction is `-C "$TMPWT"`.

   The script compares the delivered file set against the plan's `## Files Modified` enumeration and allows `max(SCOPE_DRIFT_ABS_FLOOR, round(plan_files * (SCOPE_DRIFT_RATIO - 1)))` unplanned files (v1: floor 5, ratio 1.5; per-repo override via `[tool.cw.scope_conformance]` in `pyproject.toml`). It prints a JSON verdict — `triggered`, `extra_files`, `allowed_extra`, `plan_file_count`, `delivered_file_count` — to stdout, captured above in `$SCOPE_CONFORMANCE_OUTPUT`.

   Disposition by exit code: **exit 0 with an empty `extra_files`** — the
   delivered file set matches the plan's enumeration — is the common path;
   continue to gate 3. **Every other outcome** (exit 1, exit 2, or exit 0 with a
   non-empty `extra_files`) is rare — the full disposition, including the
   JSON-verdict validation that must precede treating exit 1 as genuine drift,
   lives in `.claude/commands/auto-dev-impl-appendix.md`, section
   "Step 2.5 gate 2: scope-conformance disposition by exit code (#1779)". Read it
   now if the script exited non-zero or reported any `extra_files`; do not
   improvise a blocker reason or a friction key from this summary alone.

   Missing planned files (a planned path absent from the diff) is a separate signal the script deliberately does not fold in → flag as missing work, unchanged.

3. **Test command exit code is 0:** Re-run the agent's claimed test command in `$TMPWT`:
   ```bash
   cd "$TMPWT" && <test_command>
   ```
   Non-zero exit → `impl_failed`. The agent's pasted tail was either fabricated or stale.

4. **Mypy/ruff clean on touched files** (if Python): Re-run in `$TMPWT`; non-zero exit → `impl_failed`.

5. **Incremental commit discipline** (Mitigation 3): `git -C "$TMPWT" log --oneline "$FORK_POINT"..HEAD | wc -l` MUST be > 1 for any non-trivial change (>50 lines OR >3 files touched). A single commit on a large change → flag as discipline failure in friction (does NOT block, but compromises OOM recovery for any follow-up fix loop; record `"impl_no_incremental_commits"` in `friction_highlights`).

**On gate failure (gate setup itself — e.g. `git worktree add` erroring — or any of checks 1, 3, 4):**
- **Interactive:** AskUserQuestion with the failed-check output: "Implementation agent's completion claim failed gate <N>: <details>. Retry impl, abort ticket, or override?"
- **Headless:** EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Step 2.5 gate <N> failed: <verbatim check output>"`. Do NOT spawn reviewers — the impl is not done.

**On all gates pass:** Proceed to Checkpoint 2 (existing logic).

**Headless only — on all gates pass, emit `stage.entered` (`s2_impl_complete`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s2_impl_complete\",\"prev_stage\":\"s2_impl_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

This step replaces "trust the agent's `Could work be incomplete?: NO`" with "verify the facts the agent claims", entirely against the pushed branch.

### Checkpoint 2 (Implementation Approval)

**Small scope → AUTO-ACCEPT.** Log to user: worktree path and branch name; **fork point SHA** (the merge-base recorded after syncing with main — required for all subsequent diffs); **pushed commit SHA** (verify the agent actually pushed to origin — if not, escalate as BLOCK; the fix loop and PR creation both depend on it); files changed with line counts; test results; lint + type check results; friction report (highlight WARN/BLOCK — BLOCK still stops regardless of scope).

**Large scope → AskUserQuestion:** present the same information, then "Implementation complete for <ticket-id>. Approve for review, adjust, or abort?"

**Headless:** AUTO-CONTINUE always (never gate, regardless of scope). On BLOCK or 2x failure → EXIT `blocked` with `blocker.reason: "impl_failed"`.

### Implementation Failure Escalation

**An interactive run hitting a BLOCK, or a second failed tests/lint/mypy
attempt,** is rare — the escalation procedure lives in
`.claude/commands/auto-dev-impl-appendix.md`, section "Implementation failure
escalation (interactive only)". Read it now if this is an interactive run and
either condition applies. In headless mode neither branch applies: escalate
exclusively through the sentinel's `blocker` field.

### S2 Completion Marker

The final implementation commit on the branch MUST include the trailer:

    Auto-Dev-Stage: impl-complete

This is the durable signal the resume detector uses to advance past S2 — attach it via `git commit --trailer "Auto-Dev-Stage: impl-complete"`, to the last commit only when the implementation spans several.

On resume, a branch with this trailer + no PR → `s3_review_pending`; a branch without it → `s2_implementing` (resume in-flight). **Reasoning about the trailer's survival across a squash-merge** is rare — see `.claude/commands/auto-dev-impl-appendix.md`, section "S2 completion marker: why the trailer survives squash-merge".

The Stage 2 agent prompt MUST include this trailer requirement, added to the "Instruction to stage and commit changes with a conventional commit message" bullet:

> The final commit before push must include the trailer `Auto-Dev-Stage: impl-complete`. Use `git commit --trailer "Auto-Dev-Stage: impl-complete" -m "..."` (or append the trailer to the message body if your commit tool does not support `--trailer`). The pipeline's resume detector reads this trailer to determine S2 is complete.

---

## Stage 2 Completion (headless only)

After all Stage 2 steps complete in headless mode (branch pushed, gates passed, impl-complete trailer present), emit the `AUTO_DEV_RESULT` sentinel. IMPL advances the pipeline to review — it does NOT create a PR (FINALIZE owns that). Use `status: "stage_complete"` (not `"shipped"`); do NOT set `pr` or include `wait_for_ci` in `next_actions`.

**Only emit this sentinel when invoked as a standalone `/auto-dev-impl <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

**Validating is not emitting (#1890).** `cw result validate -` confirms the JSON is well-formed — it does not emit the sentinel. Never narrate emission as a separate act from performing it: the literal `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` frame, wrapping the validated JSON, MUST be the final characters of this same message.

**No interactive escalation, ever (#1890).** In headless mode there is no listener. Never escalate an implementation blocker (BLOCK, `impl_not_pushed`, `impl_failed`, `plan_scope_drift`, `agent_block`) by asking a question and ending your turn. Escalate exclusively via this sentinel's `blocker` field with `status: "blocked"`.

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | blocked>",
  "stage_reached": "stage2_impl",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": null,
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

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output impl`.
