---
description: "auto-dev Stage 3: Review — spawn review agents, adjudicate findings, fix loop"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 3: Review

**Orientation:** Read `.cw/context.json` for ticket context and `.cw/plan.md` for the approved plan. The feature branch must already be pushed to origin (Stage 2 complete).

**Comments are live, not cached (#1730).** The cached `comments` array in `.cw/context.json` is a Stage-0 (or earlier-stage) snapshot only. Dispatch spawns `/auto-dev-{stage.value} <ticket> --headless` **directly per stage** (`src/cw/executor.py`, RFC 0005 A2) — Stage 0 does NOT re-run between pipeline stages, so on a REVIEW-stage re-entry the cached array can be arbitrarily stale and an operator send-back comment would never reach this stage. Regardless of whether `.cw/context.json` already exists, this stage MUST live-fetch the ticket comments on every invocation via the active tracker's fetch op (`list_comments(<id>)` for `linear`; `gh issue view <n> --json comments` for `github-issues`), and overwrite `.cw/context.json`'s `comments` array with the fresh result. Mirrors `auto-dev-impl.md`'s "Comments are live, not cached (#1794)" paragraph. **WARN on comments-fetch failure** (same handling): emit an attention signal, log `"review_comments_fetch_failed"` in `friction_highlights`, and leave the existing cached array untouched rather than overwriting it with an empty/failed result.

**Elevated priority on a post-regress re-entry.** If `.claude/cw-context.json`'s `queue_metadata.pending_operator_comment` is `true`, this REVIEW re-entry followed a regress/requeue — treat the live-fetched comments as elevated-priority and check them before adjudicating any MUST_FIX/SHOULD_FIX finding. The marker is stamped at the regress (`_stage_regress`, `src/cw/dev_queue/lifecycle.py`) and cleared once a REVIEW-stage spawn consumes it (`src/cw/dispatch/claim.py`), so its presence means the operator's send-back has not yet been read. (`.claude/cw-context.json`, not `.cw/context.json` — the former is dispatch/session state written at spawn, `HOOK_CONTEXT_RELATIVE_PATH`; the latter is Stage-0 ticket context and never carries `queue_metadata`, #1730 round 5.)

This stage runs the full review pass AND the fix loop (Step 3a + Step 3b) when MUST_FIX findings exist. It does NOT create a PR — PR creation is Stage 4 (`auto-dev-finalize.md`).

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** All reviewer and fix-agent spawns in this file use explicit `model: "sonnet"` pins. Do not change any pin to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Stage 3: Review (Agents)

**Headless only — before spawning reviewers, emit `stage.entered` (`s3_review_started`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_started\",\"prev_stage\":\"s2_impl_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

### Step 3a: Spawn Review Agents

**Small scope:** Spawn these reviewers (all `model: "sonnet"` — heuristic checks; no novel reasoning):
- Code Quality Reviewer (`subagent_type: "Code Quality Reviewer", model: "sonnet"`)
- SysAdmin Reviewer (`subagent_type: "SysAdmin Reviewer", model: "sonnet"`)
- Data Safety Reviewer (`subagent_type: "Data Safety Reviewer", model: "sonnet"`) — only when the diff mutates persisted state (any DB write, external-system write, or `SENSITIVE_HITS` non-empty); skip on doc/config/style-only diffs
- Product Manager Reviewer (`subagent_type: "Product Manager Reviewer", model: "sonnet"`, Mode 2 — spec compliance)

**Large scope:** Spawn full reviewer set based on file categories (per `/review` command patterns) (all `model: "sonnet"` — heuristic checks; no novel reasoning):
- Code Quality (always)
- Architecture (any code changed)
- Test Quality (test files changed or testable code without test changes)
- Performance (Python DB/API/service layer)
- API Contract (both backend + frontend changed)
- Deployment (infra files changed)
- SysAdmin / Scope (always)
- Data Safety (always when persisted-state mutation is present)
- Product Manager (always — Mode 2 spec compliance)

**Track `SPAWNED_ROLES`:** record the roster of roles actually dispatched (conditional members — Data Safety, any Large-scope conditional entry — count only when actually spawned). After each reviewer returns, extract its `<<<REVIEW_FINDINGS ... REVIEW_FINDINGS>>>` block (see the Output contract below) and **Write it verbatim** to `.cw/review-findings/<role-slug>.json` per Checkpoint 3a step 1 (#1924); a response with no well-formed block writes no file and records a `ReviewerRunFailure {"role": "<role>", "reason": "unparseable_response"}` instead. These feed the `--documents-from` directory and the `failed_reviewers` array `cw review consolidate` validates at Checkpoint 3a — `review.agents_run` in the Stage 3 Completion sentinel is sourced from that call's `.review.agents_run`, not a manually tracked int (R3). Carry `SPAWNED_ROLES` and each role's captured document/failure unchanged across any Step 3b re-review cycles: Step 3a's own consolidate call is what gets frozen for the sentinel.

Dispatch shape depends on mode (see issues #175 / #176 in claude-workspace for the orphan hazard this avoids):

- **Interactive mode:** spawn all reviewers in parallel, in a single message (a human is watching and USER-origin Stop hooks do not auto-transition session state).
- **`--headless` mode:** reviewers run **serially** — spawn one, **end the parent turn**, and let that reviewer's completion notification resume you before dispatching the next. Serial ordering is retained so each reviewer's findings are in hand before the next is framed; it is no longer required for orphan-safety (see the async dispatch note below). The reviewer set typically completes in under 4 minutes serially for a Small-scope diff.

**Async dispatch note (verified 2026-08-19).** The Agent tool is asynchronous unconditionally — `run_in_background` is no longer one of its parameters and there is no way to block on a spawn. **Ending the parent turn is the supported wait**, and it is safe in headless: the Stop hook payload lists every in-flight subagent in `background_tasks` (`{"type": "subagent", "status": "running", ...}`), and `cw signal-stop` defers session completion for as long as that list is non-empty (`src/cw/cli/stop_hook.py:364`). The parent is not marked COMPLETED and the run is not orphaned — the hazard behind issues #175 / #176 is covered by that guard plus the #176 Layer-1 sentinel backstop.

**Never busy-wait.** Do not hold the turn open with no-op `Bash` calls (`true`, `sleep`, repeated status polls) to avoid ending it. Each poll costs a full model round-trip and buys nothing — one observed review pass spent 173 of its 234 Bash calls on `true`. Worse, busy-waiting camouflages a stuck worker: ADR-0014 removed every kill timer, so the only automated stuck-worker signal left is the liveness distress sweep (`src/cw/reconcile/liveness.py`), which keys on transcript staleness — no-op polls keep the transcript fresh, pin the session at LIVE, and `SESSION_NEEDS_ATTENTION` never fires.

**Parent turns and subagent turns are not symmetric.** A parent's turn-end is a *pause*; a **subagent's** turn-end is a *return* — so a subagent must finish what it started inside its own turn, since a backgrounding subagent reports "done" while the work is still running. The fuller pause-vs-return rationale lives in `.claude/commands/auto-dev-review-appendix.md`, section "Parent turns and subagent turns are not symmetric". Read it now if you want that fuller framing while drafting a reviewer or fix-agent prompt.

**Sandbox warning**: reviewer subagents spawned without `isolation: "worktree"` have inconsistent file access depending on sandbox state. **Inline the full diff directly in each reviewer's prompt** (captured from the main session) so reviewers evaluate purely from prompt content. Do not assume read access "just works."

**Before spawning reviewers, capture clean-tree evidence (#2087).** Run `git status --porcelain` and `git diff HEAD --stat` in the session worktree and record both outputs as `PRE_DISPATCH_TREE`. Both MUST be empty — a dirty tree here is a pipeline defect to resolve before any reviewer sees it, not something to review around. Re-capture the same two commands as `POST_CONSOLIDATE_TREE` immediately after Checkpoint 3a step 3's `cw review consolidate` call. Together they are the contemporaneous evidence the #1714 carve-out at Checkpoint 3a step 4 requires; without both, that carve-out is unavailable.

**Before spawning reviewers, load project-specific extensions** (both optional, both forwarded into every reviewer prompt):
- `.claude/review-extras.md` — free-form prose rubrics every reviewer applies on top of the global agent specs. Read verbatim; if absent, set `PROJECT_RUBRICS = null`.
- `.claude/sensitive-files.yml` — manifest of high-blast-radius paths. If present, diff the changed-files list against its globs and capture `(file_path, reason, category)` per match into `SENSITIVE_HITS`; if absent or no matches, set `SENSITIVE_HITS = []`.

**Every reviewer prompt must include:**
- **The full diff inlined as text in the prompt.** Before computing, fetch the branch so origin refs are current (the impl agent pushes from an isolation worktree; local refs may not reflect the push):
  ```bash
  git fetch origin <branch-name>
  git diff <FORK_POINT>...origin/<branch-name>
  ```
  Use `origin/<branch-name>`, not the bare local ref — the branch was pushed from an isolation worktree and may not be visible locally until fetched. Use the Checkpoint-2 fork point SHA for a deterministic diff; do NOT use `origin/main`, which may have advanced. Small scope: include the whole diff. Large scope: you may summarize non-critical files, but always inline the primary ones.
- Changed file list
- **`PROJECT_RUBRICS` block** (inline verbatim if non-null, omit the section entirely if null):
  ```
  ## Project-Specific Rubrics

  <verbatim contents of .claude/review-extras.md>
  ```
- **`SENSITIVE_HITS` block** (inline if non-empty, omit if empty):
  ```
  ## Sensitive Files Touched

  This diff modifies files the project flagged as high blast-radius. Apply maximum scrutiny when reviewing these paths — unintended scope changes, missing auth checks, new external write paths, error handling gaps, cross-org/tenant data leakage, destructive defaults.

  - <file_path> — <category>: <reason>
  ```
- **Business Context** (inlined verbatim — required for every reviewer, not just the Product Manager Reviewer): ticket ID and title; full ticket description; all ticket comments in chronological order (via the tracker's comment-fetch op — `list_comments` for `linear`, `gh issue view <n> --json comments` for `github-issues`), since decisions live in comments and supersede the description — a comment reflecting the operator's decision on a specific prior finding is a **binding adjudication input**, not background color; Step 1c ambiguity resolutions if any were collected; and for free-text tickets the user-supplied description marked `[free-text, no Linear ticket]`.
- Review focus areas:
  1. Does the change address the actual ticket? (PM Reviewer owns this lens; other reviewers flag only if blatantly obvious from their own domain.)
  2. Did implementation stay within plan scope? Flag creep.
  3. Do tests validate meaningful behavior?
  4. Could this break anything downstream?
  5. Debug artifacts left in? (`print()`, `breakpoint()`, `pdb`, `ic()`)
- **The shared-worktree rule, verbatim (#2087):**
  ```
  ## Shared Worktree — read-only

  The checkout you can see is the orchestrating session's own worktree, shared live with every sibling reviewer in this round. Treat it as READ-ONLY: no `git checkout`/`stash`/`revert`/`reset`/`apply`, no edits, no generated or scratch files, no dependency installs. Running the existing test suite read-only is fine.

  Empirical revert-and-rerun verification ("revert the fix, confirm the regression test goes red, restore") is a legitimate technique but NOT yours to run here: even self-restored, the transient mutation races your siblings, who will observe it as a real defect. Request it instead — in the finding's `suggested_fix`, name the exact line to revert (or argument to empty, or logger to no-op) and the test that must go red. The orchestrating session decides whether to run that kill-check itself before bucketing.

  Uncommitted working-tree state you observe mid-review — a non-empty `git status`, a file whose content differs from the diff, a stray untracked file — is review-process transient state, NOT a property of the diff. Never file it as a finding at any severity, and never set `no_diff_anchor` to smuggle it through. If it prevented a check you are required to perform, report that in `detail` with `status: "degraded"`.
  ```
- **Product Manager Reviewer only:** prepend `Mode: spec compliance` to the prompt (Mode 2 per the agent spec). Other reviewers do not need a mode declaration.
- **Output contract** (see below) — no praise, `NO_ISSUES` if clean.
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

**Output contract.** Every reviewer's response MUST end with a `<<<REVIEW_FINDINGS ... REVIEW_FINDINGS>>>` block — the same `<<<TOKEN ... TOKEN>>>` convention this file's own `<<<AUTO_DEV_RESULT>>>` sentinel (below) and `prep-pr.md`'s `<<<PREP_PR_BLOCK>>>` already use for mid-pipeline agent→orchestrator signaling — conforming to the #1237 `ReviewerFindingsDocument` schema:

```
<<<REVIEW_FINDINGS
{"reviewer_role": "<role>", "status": "ok|degraded|failed", "detail": "...",
 "findings": [{"severity": "MUST_FIX|SHOULD_FIX|NIT|PRINCIPLE", "file": "...",
   "line_start": <int|null>, "line_end": <int|null>, "summary": "...",
   "consequence": "...", "suggested_fix": "...", "evidence": "<verbatim diff substring>",
   "confidence": "HIGH|MEDIUM|LOW", "no_diff_anchor": <true|false>,
   "escalation": {"target_reviewer": "...", "evidence_quote": "..."} | null}]}
REVIEW_FINDINGS>>>
```

**`no_diff_anchor` — the finding with no diff artifact at all (#1817).** Default `false`. Set it `true` **only** when the finding's remedy lies outside this diff entirely: an acceptance criterion demanding a follow-up ticket never filed, a required artifact that exists nowhere in the repo. When you set it, emit `"file": "N/A"` — that exact literal string, never an invented or plausible-looking fake path, so the field stays queryable — and leave both `line_start` and `line_end` `null`; the schema rejects any other combination. `evidence` is still REQUIRED and verbatim, but quotes the *source of the obligation* (the acceptance-criterion sentence, the plan clause) rather than diff text — the one documented exemption from the verbatim-diff-substring rule below. Do NOT use it to smuggle through a finding whose evidence you could not locate in the diff: that is `status: "degraded"` with a stated reason.

`NO_ISSUES` (a clean review) maps to `status="ok", findings=[]`. `detail` is REQUIRED and MUST be non-empty on every status — a clean `"ok"` review states what was checked ("reviewed diff for X, Y, Z; no issues found"), and a `"degraded"`/`"failed"` verdict names what was skipped or broke; a blank `detail` is a schema-rejected contract violation `cw review consolidate` will not accept. Use `status="degraded"` rather than `"ok"` when a rubric-mandated check could not be performed. `evidence` MUST be a verbatim substring of the diff text at the claimed lines — `cw review consolidate` (Checkpoint 3a) rejects any finding whose evidence doesn't literally appear there. This block, plus the Friction Protocol and Health Check blocks, is the reviewer's entire structured output, and **supersedes** the reviewer agent spec's own prose "## Output Format" section. The Codex adapter (#1236) applies the same override — see `cw.codex_review._context`'s `_OUTPUT_INSTRUCTIONS`.

### Checkpoint 3a: Adjudicate every finding

**Extract, assemble, and consolidate mechanically** — this is a `cw review consolidate` call, not a prose "deduplicate/sort/group" step:

1. **Write each reviewer's findings block to disk verbatim — never retype it into JSON (#1924).** First clear last round's files so a role that failed this round cannot contribute a stale document:
   ```bash
   rm -f .cw/review-findings/*.json
   mkdir -p .cw/review-findings
   ```
   Then, for each role that produced a well-formed `<<<REVIEW_FINDINGS ... REVIEW_FINDINGS>>>` block, **Write** (the Write tool — not a heredoc) the block's JSON body, byte-for-byte as the reviewer emitted it, to `.cw/review-findings/<role-slug>.json`, where `<role-slug>` is the role name lowercased with spaces replaced by hyphens (`Code Quality Reviewer` → `code-quality-reviewer.json`). Copying the text is the whole point: `evidence` must be a verbatim substring of the diff, and re-typing it into an inline `documents` array is how a paraphrase gets in. A role whose response has no well-formed `REVIEW_FINDINGS` block writes no file and instead contributes a `ReviewerRunFailure {"role": "<role>", "reason": "unparseable_response"}` to `failed_reviewers` (see `SPAWNED_ROLES` tracking in Step 3a above).
2. Assemble the consolidate-input envelope. It carries **no `documents` key** — the documents come from the directory written in step 1. Capture `CHECKPOINT_3A_SHA="<HEAD sha>"` alongside it — the same sha as `reviewed_sha` below — so Step 3c has a concrete value to carry forward instead of re-deriving it:
   ```json
   {"diff": "<the same diff text sent to reviewers>", "reviewed_sha": "<HEAD sha>", "failed_reviewers": [...]}
   ```
3. Run, always passing `--base "$FORK_POINT"` (the same fork point every reviewer's diff was captured from at Step 3a — never `--no-base-check`, which is for tests and human recovery debugging only):
   ```bash
   printf '%s' "$CONSOLIDATE_INPUT" | cw review consolidate --documents-from .cw/review-findings/ --base "$FORK_POINT" -
   ```
   A non-zero exit here is a hard pipeline error (the mechanical validation itself failed — e.g. malformed JSON, a *structural* schema violation such as a missing `reviewer_role`/`status` or a document whose surviving findings still cannot satisfy its own invariants — note that since #2029 a single schema-invalid finding is NOT this case: it is rescued out and reported in `.rejected` with `reason: "schema_invalid"`, and its siblings consolidate normally — an unreadable `--documents-from` file, one of the #1924 diff-integrity guards rejecting the payload — a placeholder `diff` that never carried a real diff, or the same hunk repeated for the same file because the diff was reconstructed by hand — or a `DiffBaseMismatchError` from the `--base` check: the payload's diff text did not match the real `git diff $FORK_POINT...$CHECKPOINT_3A_SHA` output) — not a normal adjudication path. Do not attempt to recover by re-running adjudication on the raw prose; treat it the same as any other unrecoverable Stage 3 tool failure.
3.5. **Consult the voided-findings record — `cw review check-voided` (#1814).** An operator who rejected a finding on a prior pass settled it permanently; a re-review re-deriving it must not re-park the ticket. Assemble `{"verdict": <the verdict step 3 just printed>, "ticket_id": "<ticket id>", "comment_bodies": [<each live-fetched ticket comment body, per the Orientation section's mandatory live fetch>], "new_voided_entries": []}` and run:
   ```bash
   printf '%s' "$CHECK_VOIDED_INPUT" | cw review check-voided -
   ```
   Then:
   - **Use the printed `verdict` as the working set for the rest of Checkpoint 3a**, not step 3's raw consolidate output. Suppressed findings are stamped `disposition: "rejected"` and have already left `must_fix`/`blocking`.
   - **Append the printed `adjudications` verbatim to the `ADJUDICATIONS` array.** They are ordinary `outcome: "reject"` entries; do not re-word their rationale, which names the settling operator comment.
   - A non-zero exit is a hard pipeline error, same as step 3.

4. On success, parse the working `ReviewVerdict` (step 3.5's `verdict`, which is step 3's own output verbatim when no void matched) and:
   - Use `.accepted` (validated MUST_FIX/SHOULD_FIX findings, deduped across reviewers) as the finding set entering the FIX NOW / REJECT / DEFER bucket logic below — each finding carries structured `file`/`line_start`/`summary`/`suggested_fix` instead of freeform prose.
   - **An `.accepted` finding with `anchor_degraded: true` is a normal bucket candidate whose line anchor validation dropped (#2081).** The reviewer cited a line that exists in the file but resolves against nothing in the diff — the stale-base shape, where a line number drifts while the finding's text stays right — so `cw review consolidate` degraded it to file-level (`line_start`/`line_end` both `null`) and passed it through instead of rejecting it as `invalid_line_reference`. Adjudicate it on its `summary`/`evidence`/`consequence`, locating the code yourself; never REJECT it *for* lacking a line anchor, and never treat it as a finding the reviewer filed at file level. Its `ADJUDICATIONS` entry copies the null endpoints as printed.
   - Render `.accepted` entries with `severity` in `{NIT, PRINCIPLE}` informationally only — never eligible for bucket assignment (R5).
   - Render `.accepted` entries whose `disposition` is not `"fixed"` — a step-3.5 voided suppression, or a disposition an earlier step stamped — informationally only, on the same footing as NIT/PRINCIPLE (#1814). Their outcome is already in `ADJUDICATIONS`; bucketing one would append a second, conflicting entry for the same identity.
   - Discard `.rejected` findings (evidence didn't match the diff) whose `severity` is **not** `MUST_FIX` entirely — log them, do not present them to the bucket sort.
   - **A `.rejected` finding with `severity: MUST_FIX` may NOT be silently discarded** (#1714). Log it, and if any is present this run EXITS `blocked` with `blocker.reason: "review_blocked"` (same exit shape as the 5-cycle MUST_FIX case below). A finding rejected for a *mechanical* reason — bad line anchor, evidence quote absent from the diff — was never evaluated on its merits, so "no MUST_FIX survived validation" is not "the code is clean". Do not fix it either: its anchor is unreliable by definition, making it an operator-review signal, not a fix-loop input. `.rejected[].raw` carries the original `file`/`line_start`/`summary` — surface them in the exit's `blocker.details` so the operator can adjudicate manually. Also post them as a tracker comment per the blocking-findings comment rule below.
   - **The one carve-out — session-transient tree state (#2087).** A rejected MUST_FIX whose *subject* is the review process's own transient working-tree state — uncommitted changes, a non-empty `git status`, a file differing from the diff mid-review, typically because a sibling reviewer mutated the shared worktree against the rule above — is not a finding about the diff, and blocking the round on it buys an operator round-trip that can only end in "dismiss". It does NOT trigger the `review_blocked` exit **if and only if** both `PRE_DISPATCH_TREE` and `POST_CONSOLIDATE_TREE` (Step 3a) were captured and both were empty. Then: (a) do not exit; (b) append `transient_state_finding_dismissed: <reviewer_role>: <summary>` to `friction_highlights`; (c) in the round's tracker comment, list it under a `Dismissed — session-transient state (#2087)` sub-heading quoting both clean-tree captures, not as a blocking finding; (d) if the mutation's author is identifiable from a reviewer's own response, also append `reviewer_mutated_shared_worktree: <reviewer_role>` to `friction_highlights`. Judge the *subject* strictly: a MUST_FIX that names diff content, even with a bad anchor, stays on the exit above. Missing either capture, or either capture non-empty, means the evidence is not contemporaneous and the finding stays on the exit above too — the carve-out never widens #1714, it only declines to page an operator about state the session has already proven clean.
   - Log `.stripped_escalations` (escalations whose evidence quote wasn't in the diff — the finding itself survives and is adjudicated normally).
   - **Freeze** `.review.must_fix_initial`, `.review.should_fix`, `.review.agents_run`, `.review.rejected_count`, and `.review.rejected_count_by_severity` from this *first* `cw review consolidate` call for the final sentinel. A Step 3b re-review re-invokes `cw review consolidate` on the updated diff purely to re-check `blocking`, never to overwrite these frozen numbers. `rejected_count`/`rejected_count_by_severity` are copied verbatim from the first call's `.review` block — **never hand-computed and never omitted**: an omitted key is reported as not-reported (`null`), and a hand-written `0` is a false clean signal indistinguishable from a producer that actually confirmed zero rejections.

**Blocking-findings comment rule (#1815, third trigger #1817).** A round that exits `blocked` with `blocker.reason: "review_blocked"` (the mechanically-rejected MUST_FIX path above, or the cycle-5 hard exit in Step 3b.5) or with `blocker.reason: "plan_deviation"` (the 4a Exit rule below) MUST post the offending finding(s) as a tracker comment before exiting. **Reaching any of those three exits is rare** — the fixed comment header, the per-finding sub-section shape, the structured-finding source fields, the `friction_highlights` sentinel, and the severity-scope delta between the `review_blocked` and `plan_deviation` triggers live in `.claude/commands/auto-dev-review-appendix.md`, section
"Blocking-findings comment rule: header, body shape, and the three triggers". Read it now if this round is heading for any of those exits; do not improvise the header or the body shape from this summary alone.

**Non-deferrable pre-pass (run BEFORE bucket assignment):**

- **(4a) Non-deferrable pre-pass.** Before bucket assignment, scan every finding: any finding describing an implementation **deviation from an EXPLICIT plan requirement or prohibition** — a `do NOT X` the plan states, a mandated mechanism the plan named, a required test the plan named — is `NON_DEFERRABLE`. A NON_DEFERRABLE finding is eligible for **bucket 1 (FIX NOW) only**; it may never land in REJECT (bucket 2) or DEFER (bucket 3).
- **(4b) Spec-citation cross-check.** If a proposed bucket-2/3 rationale contains any of the literal trigger phrases — `"required by spec"`, `"plan mandated"`, `"plan requires"`, `"the RFC says"`, `"operator required"`, `"operator decided"`, `"ticket spec"` — the coordinating session MUST verify the claim against `.cw/plan.md` **verbatim** before accepting the rejection/deferral. If `.cw/plan.md` contradicts the justification, the finding is `NON_DEFERRABLE`.
- **(4c) Operator send-back cross-check (#1730).** The live-fetched ticket comments (see Orientation) are a **binding adjudication input**, not background color: a comment in which the operator adjudicated a specific prior finding — accepting, rejecting, or scoping it out — settles that finding here, and the coordinating session may not re-litigate it into a different bucket. When `.claude/cw-context.json`'s `queue_metadata.pending_operator_comment` is `true`, check the comments **before** assigning any MUST_FIX/SHOULD_FIX finding to a bucket, and record which comment settled it in the adjudication rationale. **An operator-settled finding is adjudicated exactly like any other (#1805):** it gets its own entry in the `ADJUDICATIONS` array below, with the `outcome` the comment dictated and — for `reject`/`defer`, where `rationale` is REQUIRED — a `rationale` naming the operator comment (author and date) as the source. Recording the decision only in prose would leave it the one adjudication missing from the array `cw review adjudicate` treats as the single source of truth.
  **Also make the decision durable (#1814).** An `ADJUDICATIONS` entry settles the finding for *this* pass only; the array is session-local and dies with the session. A regress/redispatch, or the codex backend (which has no coordinating session at all and re-derives its findings mechanically every pass), would raise the same finding again with nothing to stop it. So when an operator comment settles a REJECT, **also** append one entry to a session-local `NEW_VOIDED_FINDINGS` list:
  ```json
  {"severity": "<finding.severity>", "file": "<finding.file>", "summary": "<finding.summary>", "evidence": "<finding.evidence>",
   "operator_comment_id": "<author>@<created_at>", "operator_comment_excerpt": "<the sentence that settled it>",
   "original_rationale": "<the same one-line rationale you put in the ADJUDICATIONS entry>"}
  ```
  `severity`/`file`/`summary`/`evidence` are copied verbatim off the finding — the content anchor a later pass matches on, so a paraphrase breaks the match. `operator_comment_id` is the `"<author>@<created_at>"` composite (the materialized comments array carries no numeric id). Do **not** record a line number: position is deliberately not part of the identity, so a voided finding whose code later moves still matches and an unrelated new finding at the old line never does. Carry `NEW_VOIDED_FINDINGS` cumulatively across any Step 3b re-review cycles.
- **Exit rule.** If a NON_DEFERRABLE finding cannot be fixed within the fix loop, or is judged **"beyond fix-loop scope,"** the stage does **not** PROCEED. `"beyond fix-loop scope"` is an **escalation trigger, not an auto-approve**: Stage 3 EXITS `blocked` with `blocker.reason: "plan_deviation"` (open enum), routing to BLOCKED_ON_USER via existing dispatch rules. **Also post the finding(s) that caused this exit as a tracker comment per the blocking-findings comment rule above (Checkpoint 3a), regardless of their severity** — sentinel: append `blocking findings posted: plan_deviation` to `friction_highlights`, parallel to the existing `blocking findings posted: review_blocked` idiom. The "plan in question" note: *the pipeline never adjudicates plan-vs-impl blame — it always exits `blocked`/`plan_deviation`; whether to `cw dev-queue requeue --regress` back to impl or revisit the plan is orchestrator/operator policy.*
- **Relationship to the Step 2.5 scope-conformance gate (#1779).** Gross *file-set* drift no longer reaches this stage: `.claude/scripts/check_plan_scope_conformance.py` measures the delivered diff against the plan's `## Files Modified` list at Step 2.5 and exits `blocked`/`plan_scope_drift` **before any reviewer is spawned**. `plan_deviation` is the backstop for what a file-count comparison cannot see: **semantic** drift inside an otherwise-conforming file set — the planned files carrying the wrong content, mechanism, or behavior. Reaching for `plan_deviation` to describe "this diff is simply too big" is the mechanical gate's job; it either passed or failed open (`impl_scope_conformance_unparsed` in `friction_highlights`) — say so in the finding rather than re-deriving it here.

Adjudication assigns each finding a disposition. The coordinating session — never a subagent or executor — sorts **every** finding (MUST_FIX *and* SHOULD_FIX) into exactly one of four buckets. Exactly two carve-outs apply first: `severity` NIT/PRINCIPLE (never eligible, R5), and a `disposition` already not `"fixed"` (#1814 — its outcome is recorded, and severity alone does not exclude it since a voided finding keeps its severity). Everything else gets exactly one bucket:

1. **FIX NOW → the action list.** All surviving MUST_FIX plus any SHOULD_FIX the session accepts into scope. *If it stays in the review, it's worth fixing* — filtering happens here, at scoping, not by silently ignoring the returned list.
2. **REJECT (review-the-review).** The session disagrees: the finding is wrong, or the code is a deliberate choice / documented tradeoff. **Record the rationale** (see "Recording adjudication" below). No fix, no ticket. **Excludes NON_DEFERRABLE findings** — a plan-deviation finding may never be rejected.
3. **DEFER.** Valid but out of scope for this ticket ("handle when scale demands"). **Record now, file as a ticket on merge** (PR Hygiene Sweep Step H3). Skip the ticket only when the item is already a bucket-2 documented tradeoff. **Excludes NON_DEFERRABLE findings** — a plan-deviation finding may never be deferred.
4. **OPERATOR ACTIONABLE (#1817).** The session accepts the finding as valid and in-scope, but its remedy lies outside this diff entirely — it carries `no_diff_anchor: true`. **Never eligible for FIX NOW**: the fix loop cannot edit a diff that does not exist. **Scoped to `severity: MUST_FIX` only** — an accepted SHOULD_FIX `no_diff_anchor` finding routes through ordinary DEFER (bucket 3); `cw review adjudicate` enforces this at the model and rejects a SHOULD_FIX adjudicated `operator_action` outright. **Excludes NON_DEFERRABLE findings** — one that is both NON_DEFERRABLE and `no_diff_anchor` is by definition already "beyond fix-loop scope" under the 4a Exit rule and exits `blocked`/`plan_deviation` there instead. A `no_diff_anchor` finding the session *disagrees* with goes to ordinary REJECT (bucket 2). Record it with `outcome: "operator_action"` and post it per the operator-actionable findings comment rule below.

**Invariant:** every finding ends *fixed*, *rejected-with-reason*, *ticketed*, or *handed to the operator as an explicit action*. A reviewer finding that simply vanishes is a process failure.

**Operator-actionable findings comment rule (#1817).** Whenever `ADJUDICATIONS` contains any `outcome: "operator_action"` entry, those findings MUST be posted as a tracker comment. **Bucket 4 landing non-empty is rare** — the fixed comment header, the markdown-checklist line format, the REQUIRED-rationale requirement, the `friction_highlights` sentinel, and why the trigger is `ADJUDICATIONS` rather than `blocker.reason` live in `.claude/commands/auto-dev-review-appendix.md`, section
"Operator-actionable findings comment rule: header, checklist format, and trigger". Read it now if any finding was bucketed operator-actionable; do not improvise the header or the checklist line from this summary alone.

The **action list** (bucket 1) — not "MUST_FIX only" — drives Step 3b. An accepted SHOULD_FIX is fixed; a rejected or deferred one leaves the action list, recorded. If every finding lands in REJECT/DEFER the action list is empty and the pipeline continues.

**Adjudication is judgment** → it stays on the coordinating session. A stateless executor must never decide whether a finding is correct; it may only *mechanically apply* an action-list fix the session already decided ("change X to Y in file Z").

**Recording adjudication:** every bucket assignment — FIX included — becomes one entry in the `ADJUDICATIONS` array described below, which is the single source both the verdict and `.cw/deferred-findings.md` are generated from. Do not record a decision in only one of the two places.
- **Rejections (bucket 2):** one entry with `outcome: "reject"` + a one-line `rationale`; these reach the PR body via Stage 4 (Step 4d). Append the same to `friction_highlights`. For a rejection rooted in non-obvious design intent, add an inline `# Why:` comment at the code site (per the global review-culture rule).
- **Deferrals (bucket 3):** one entry with `outcome: "defer"` + `rationale`; Stage 4 (Step 4d) writes them into the machine-readable `DEFERRED-REVIEW-FINDINGS` block in the PR body for Step H3 to harvest. Append a one-line note to `friction_highlights`.
- **Operator actions (bucket 4):** one entry with `outcome: "operator_action"` + a REQUIRED `rationale` naming the concrete action the operator must take. These reach the tracker immediately via the operator-actionable findings comment rule above — deliberately NOT the PR body, so they are never rendered into `.cw/deferred-findings.md`. Append a one-line note to `friction_highlights`.
- **Fixes (bucket 1):** one entry with `outcome: "fix"` and no rationale. Omitting it does not fail the run, but it leaves the finding recorded as `"dropped"` (nobody decided) — and for a MUST_FIX that keeps the verdict blocking.

**Stamp the adjudication mechanically — `cw review adjudicate` (#1805).** The judgment above is unchanged; only its *serialization* is mechanical. Previously the outcome was typed twice — as prose into `.cw/deferred-findings.md`, and implicitly as the `disposition="fixed"` every accepted finding carried from `cw review consolidate` — and only the first was accurate.

1. As each finding is bucket-sorted, append one `Adjudication` object to an `ADJUDICATIONS` array, copying the identity fields straight off the finding:
   ```json
   {"severity": "<finding.severity>", "file": "<finding.file>", "line_start": <finding.line_start>, "line_end": <finding.line_end>, "evidence": "<finding.evidence>", "summary": "<finding.summary>", "outcome": "<fix|reject|defer|operator_action>", "rationale": "<one-line rationale>"}
   ```
   `rationale` is REQUIRED (non-blank) for `reject`/`defer`/`operator_action` and unused for `fix`. `evidence` is a same-location tiebreak, not part of the identity match — copy it, but a stale one does not break the match. An `operator_action` entry copies `"file": "N/A"` and null line endpoints straight off its `no_diff_anchor` finding, and its `severity` must be `MUST_FIX` — the command rejects any other severity for that outcome.
2. After the full sort, assemble `{"verdict": <the frozen Checkpoint-3a verdict>, "adjudications": ADJUDICATIONS}` and run:
   ```bash
   printf '%s' "$ADJUDICATE_INPUT" | cw review adjudicate --deferred-findings-out .cw/deferred-findings.md -
   ```
   This writes `.cw/deferred-findings.md` in the shape below (creating `.cw/` if absent, writing nothing when there is nothing to record) — do NOT also hand-author it.

   **The write MERGES, it does not overwrite (#1840).** Calling `adjudicate --deferred-findings-out` a second time in one Stage 3 pass (a re-adjudication after a fix cycle, say) keeps every entry the earlier call recorded. Entries dedupe on content, so an identical re-adjudication collapses to one entry, while a genuine outcome flip — REJECT on an earlier round, DEFER on a later one — accumulates as two entries that read as history. Each entry the call newly applies is stamped with a round number and a `recorded_at` timestamp; entries already in the file are carried through untouched, never re-stamped.
3. **Use the printed `ReviewVerdict` as this round's saved record from here on**, not the raw `cw review consolidate` output. It carries the real `disposition` per finding plus recomputed `blocking`/`must_fix`/`review.deferred`.
4. **If the printed verdict's `unmatched_adjudication_count` is > 0, append `"adjudication_unmatched_count: <N>"` to `friction_highlights`.** A non-zero count means an adjudication entry matched no accepted finding (stale anchor, ambiguous same-location collision, duplicate entry) — the command does not fail on it, so this is the only thing that makes it visible at the approval gate.
5. **Persist any newly-voided findings to the ticket (#1814) — UNCONDITIONAL.** If `NEW_VOIDED_FINDINGS` (step 4c) is non-empty, re-run `cw review check-voided` with it folded in and render the record out:
   ```bash
   printf '%s' "$CHECK_VOIDED_INPUT_WITH_NEW_ENTRIES" | cw review check-voided --voided-findings-out .cw/voided-findings-comment.md -
   ```
   Then post (or update) that file's contents as a ticket comment. It already carries its own `## Voided Review Findings` header and the machine-readable sentinel — post it verbatim; do NOT re-wrap it. This runs **regardless of whether Stage 3 continues to Stage 4 or exits `blocked`**: an exit is exactly the path where the next pass re-derives the finding. Append `"voided findings recorded: <N>"` to `friction_highlights`.

Findings the pass never covers are stamped `disposition: "dropped"`. A dropped **MUST_FIX** still counts toward `blocking`/`must_fix`: nobody decided its fate, and erring toward a gate failure beats silent pass-through. If the printed verdict blocks and you believe every MUST_FIX *was* adjudicated, the missing entry — not the finding — is the bug.

The generated file's shape (reproduced byte-for-byte by `cw review adjudicate`; Stage 4's Step 4d copies it into the PR body and the Step H3 sweep greps the sentinels literally):

```
# Deferred Review Findings
<!-- written by Stage 3 (auto-dev-review.md), consumed by Stage 4 Step 4d (auto-dev-finalize.md) -->

## Review adjudication

Rejected (intentional / documented tradeoff):
- <file> — "<summary>" — <one-line rationale>

<!-- DEFERRED-REVIEW-FINDINGS
- severity: <MUST_FIX|SHOULD_FIX>
  summary: "<finding summary>"
  file: <file>
  rationale: "<out-of-scope rationale>"
DEFERRED-REVIEW-FINDINGS -->
```

**Round stamping, a pre-#1840 legacy file, or a hard-error refusal to overwrite
`.cw/deferred-findings.md`** is rare — the stamped-entry shape, the
omit-when-empty rules, and the refusal semantics live in
`.claude/commands/auto-dev-review-appendix.md`, section "`.cw/deferred-findings.md`:
round stamping, legacy files, and hard-error refusal". Read it now if the command
errors on this file or you must reason about an unstamped entry; do not
hand-repair the file from memory.

**Headless:** adjudication is autonomous — **no AskUserQuestion.** The session adjudicates deterministically, records rationale for every REJECT/DEFER in `.cw/deferred-findings.md`, and proceeds. Interactive mode MAY surface the adjudication for confirmation but defaults to the same dispositions.

**Small scope + NO_ISSUES → AUTO-ACCEPT.** Log "Review clean" and proceed to S4.

**Small scope + SHOULD_FIX only (no MUST_FIX) → adjudicate per Checkpoint 3a, then:**
- Action list non-empty (accepted SHOULD_FIX) → run Step 3b on it.
- All SHOULD_FIX land in REJECT/DEFER → action list empty → AUTO-CONTINUE to S4 (rejections recorded, deferrals queued in `.cw/deferred-findings.md`).
- Log the disposition: "N SHOULD_FIX adjudicated — <a> fixed, <b> rejected, <c> deferred".

**An interactive run reaching the Small+MUST_FIX or Large-scope approval gate**
is rare (headless never does) — both prompts live in
`.claude/commands/auto-dev-review-appendix.md`, section "Interactive-only
adjudication gates". Read it now if this is an interactive run and either
applies.

**Headless:** Always run reviewers, then adjudicate every finding per Checkpoint 3a (autonomous — no AskUserQuestion; record rationale for every REJECT/DEFER in `.cw/deferred-findings.md`). Non-empty action list → run fix loop (expected 2 cycles, hard-cap at 5; cycles 3+ or scope growth append to `friction_highlights` and set `health.fix_loop_escalated: true`). Empty action list (every finding fixed / rejected / deferred) + small → emit `stage.entered` (`s3_review_complete`) then AUTO-CONTINUE to S4:
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_complete\",\"prev_stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```
Clean/SHOULD_FIX + large → EXIT `review_pending_approval`. MUST_FIX persists after 5 cycles → EXIT `blocked` with `blocker.reason: "review_blocked"`.

### Step 3b: Fix Loop (when MUST_FIX needs fixing)

**Important**: this session does **not** dispatch the fix agent. It records the action list on the dev-queue row and exits; the orchestrator's next reconcile tick issues the spawn. **Questioning why** is rare; the structural constraint lives in `.claude/commands/auto-dev-review-appendix.md`, section "Why the fix loop hands off asynchronously".

Prerequisite: the implementation branch must already be on origin (Step 2's agent pushes it) — if not, escalate BLOCK before starting the fix loop.

1. **Leave the worktree alone.** Do NOT remove it, and do NOT delete the local branch ref. `cw.reconcile.fix_dispatch` reuses this exact `(client, branch)` worktree, so it must still be there — `create_worktree` then takes its idempotent-reuse path and mutates nothing.

2. **Record the fix-agent handoff, then end the turn (#2017).** Write the fix-agent prompt — MUST_FIX findings + the Completion Artifacts block + Friction Protocol + Health Check, exactly the content described below — to `.cw/fix-agent-prompt.md` **via the Write tool**; never heredoc-inline it. That file is a transport only, read once by the snippet below; the durable copy is the dev-queue field it writes. Then:
   ```bash
   CLIENT="$(jq -r '.client' .claude/cw-context.json)"
   FIX_CYCLE=<N>   # current cycle, 1-5

   uv run python -c "
   from datetime import UTC, datetime
   from pathlib import Path
   from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
   from cw.models import PendingFixDispatch

   with dev_queue_lock():
       store = load_dev_queue()
       task = next(t for t in store.tasks
                   if t.ticket_id == '$TICKET' and t.client == '$CLIENT')
       task.pending_fix_dispatch = PendingFixDispatch(
           prompt=Path('.cw/fix-agent-prompt.md').read_text(encoding='utf-8'),
           label='fix-$TICKET',
           cycle=$FIX_CYCLE,
           requested_by_session_id='$CW_SESSION',
           requested_at=datetime.now(UTC),
       )
       save_dev_queue(store)
   "
   ```
   Then **end the turn with no further Bash calls this cycle**, emitting a `blocked` AUTO_DEV_RESULT sentinel with `blocker.reason: "fix_loop_pending_dispatch"` and `blocker.stage: "stage3_review"`.

   This is not a park. `cw.reconcile.fix_dispatch` picks the record up on the next reconcile tick and dispatches the fix agent as a first-class cw DAEMON session (`SessionPurpose.FIX`) — provisioning the branch-keyed worktree, verifying HEAD landed on the expected `origin/<branch-name>` commit, and merging `origin/<default-branch>` so the fix lands on top of any sibling PR that merged mid-pipeline. On a merge conflict it aborts and reports the conflicting files; it never forces and never auto-resolves. The row stays RUNNING throughout, so nothing re-dispatches it mid-fix, and it is unparked to PENDING automatically once the fix session goes terminal.

   **Why the handoff is asynchronous rather than a call from here:** `cw` provisions one worktree per `(client, branch)`, so the fix session's worktree IS this session's worktree, and `spawn_create_impl` refuses to overwrite a hook context that names a still-live session. Dispatching from inside this session can therefore never succeed. Do not "fix" this by dispatching directly.

   **Dispatch failures surface as events, not as a sentinel** (`stage.errored` with `error_kind: "fix_dispatch_failed"`, plus `session.needs_attention`) — no session is running when an asynchronous dispatch fails, so there is nothing to carry a `blocker.reason`. See `docs/events.md`.

3. The fix agent fixes MUST_FIX issues, re-runs quality gates, creates a NEW commit on top (do NOT amend) **with the trailer `Auto-Dev-Fix-Cycle: <N>`** (`<N>` = current cycle, 1-5; pass via `git commit --trailer "Auto-Dev-Fix-Cycle: <N>"`), and pushes to origin using the explicit-refspec form (`git push origin HEAD:refs/heads/<branch-name>`) — robust against a local branch rename. After pushing, verify with `git rev-parse origin/<branch-name>` matching `git rev-parse HEAD`.

The `Auto-Dev-Fix-Cycle` trailer is the durable cross-session signal for fix-loop progress: the resume detector reads the max `<N>` across fix-cycle trailers on commits newer than `Auto-Dev-Stage: impl-complete`, and on resume into `s3_fix_loop, substage="cycle_N"` the pipeline resumes at cycle `N+1`, preserving the cycle budget across session deaths.

**Steps 3b.5 and 4 below run in a DIFFERENT session from the one that recorded the handoff.** Once the fix session goes terminal, `cw.reconcile.fix_dispatch` unparks the row to PENDING and the pipeline dispatches a fresh REVIEW session, which resumes at `s3_fix_loop, cycle_{N+1}` via that same trailer mechanism. If you are reading this after such a resume, the fix agent has already run and pushed — verify its work, do not re-record a handoff for the cycle that just completed.

The fix-loop agent's prompt must end with both the Friction Protocol block and the following Health Check block verbatim:
   ```
   ## Health Check
   - **Context usage**: <rough % or HIGH/MEDIUM/LOW>
   - **On-spec confidence**: HIGH | MEDIUM | LOW
   - **Shortcuts taken under pressure**: [list or NONE]
   - **Could work be incomplete?**: NO | MAYBE | YES (explain)
   - **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
   ```

   The prompt must ALSO include the same **Completion Artifacts** block as Stage 2 (test command, test output tail, `git diff --stat`, `git log --oneline`, and the per-gate quality gate results table — one row per configured `quality_gate_commands` entry, `pass` | `<errors>` | `not_run`, per `auto-dev-impl.md`) — the orchestrator gates fix completion on facts, as it does impl completion. Incremental commits same as Stage 2: one commit per MUST_FIX item resolved.

   The fix-loop agent's prompt must ALSO instruct: "If your fix touches any file outside the original Stage 1 approved plan's file list, OR if your changes push the diff into Large tier (>10 files OR >500 lines OR a forbidden area), report this in the friction report under a new bullet `**Scope growth**: [list affected files / explain tier change]`. The main session uses this to decide escalation."

3b. **Orchestrator fix gate** (Subagent Reliability Mitigation 1, fix-loop variant). At the top of the resumed REVIEW session, before re-running review (or before the sparse-feedback gate in step 4):
   - Re-run the test command in the impl worktree. Non-zero exit → fix is false; treat as cycle failure (counts against the 5-cycle hard cap).
   - Re-run mypy/ruff. Non-zero on touched files → fix is false.
   - Compare pasted `git diff --stat` against live `git diff --stat $FORK_POINT`. Substantial mismatch → fix is false.
   - Verify the fix produced at least one new commit since the prior cycle (`git log $PRIOR_HEAD..HEAD --oneline` must be non-empty). Zero new commits → fix-loop agent did not actually fix anything; treat as cycle failure.

   On gate failure: log the failed check, increment the cycle counter, and record a fresh handoff per step 2 above (within the 5-cycle hard cap). Headless: append `"fix_loop_gate_failed_cycle_<N>"` to `friction_highlights`, and emit `stage.errored`:
   ```bash
   cw event record stage.errored \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"fix_cycle_failed\"}" || true
   ```
   The cycle still consumed budget — a false-completion fix counts against the cap.

   On all gates pass: proceed to step 4 (sparse gate / re-review).

4. **Sparse-feedback gate, then re-run review.** The fix-then-rereview cycle is NOT mandatory when initial feedback was sparse and the fix is small relative to the original change.

   **Skip re-review when ALL of the following hold (Small scope only):**
   - Scope tier was **Small** at Stage 1c, AND no scope growth was flagged in the fix-loop friction report
   - Initial review produced ≤2 MUST_FIX items
   - Fix-loop diff is small relative to the original implementation diff — judgment call, no hard line ceiling; a 2-line touch on a 50-line PR is sparse, a rewrite of half the implementation is not
   - Fix did not touch files outside the original Stage 1 plan's file list
   - No SHOULD_FIX items adjacent to the MUST_FIX areas were left unaddressed in a way warranting a second look

   When skipping: log `Skipping re-review — Small scope, sparse fix (<N MUST_FIX> resolved, fix diff small relative to original). Proceeding to Stage 4.`, document it in the friction report under `**Re-review skipped**`, and jump to Stage 4. When in doubt, run re-review — the skip is for unambiguously small fixes only.

   **Headless:** apply the same criteria deterministically. If all conditions hold, skip re-review and proceed to S4 AUTO-CREATE; append `"rereview_skipped_sparse"` to `friction_highlights`. If any condition is uncertain, run re-review.

   **Otherwise, re-run review agents (same set).** Before computing the updated diff, fetch the branch to pick up the fix-loop agent's push:
   ```bash
   git fetch origin <branch-name>
   ```
   Then pass the updated full diff (`git diff <FORK_POINT>...origin/<branch-name>`) and the fix-commit diff inlined in each reviewer's prompt (see Step 3a sandbox warning). Do NOT rely on reviewers reading files from disk. Each re-spawned reviewer prompt MUST include both the Friction Protocol block and the Health Check block, identical to the initial Step 3a spawn.

5. **Cycle budget:** 2 cycles is the expected baseline. If MUST_FIX persists past cycle 2, the loop may continue with escalation visibility, hard-capped at 5 total cycles. **Entering cycle 3+, growing scope mid-fix, or exhausting the cap** is rare — the escalation triggers, the per-mode escalation behaviour, the cycle-5 hard exit, and the cap-value maintenance note live in `.claude/commands/auto-dev-review-appendix.md`, section
   "Fix loop cycle budget: escalation triggers and the cycle-5 hard exit". Read it now if this round entered cycle 3 or the fix-loop friction report flagged scope growth; do not improvise the escalation record or the hard-exit sentinel from this summary alone.

**A recorded `pending_fix_dispatch` failing to dispatch across two observed
cycles** — visible as `stage.errored` (`error_kind: "fix_dispatch_failed"`) plus
`session.needs_attention` events for this ticket, NOT as a session sentinel,
since no session is running when an asynchronous dispatch fails — is rare; the
direct-execution fallback the main session then runs lives in
`.claude/commands/auto-dev-review-appendix.md`, section "Fallback — direct
execution from the main session's worktree". Read it now if that condition holds;
do not improvise the git sequence from memory.

**Pre-exit invariant (required, no exceptions):** Before ending the review session at any point — normal completion, error, context boundary, or after the fallback path above — run:
```bash
git status --porcelain
```
Empty output is the common path — proceed to the normal exit. **Non-empty output (staged or unstaged changes exist) is rare** and you MUST NOT exit on it without acting: the two permitted dispositions (commit-and-push, or a `dirty_tree_no_sentinel` blocked sentinel with the exact fields the schema validator requires) live in `.claude/commands/auto-dev-review-appendix.md`, section
"Pre-exit invariant: what to do when the tree is dirty". Read it now if `git status --porcelain` printed anything; do not improvise the sentinel fields from this summary alone.

Never exit with a dirty tree and no sentinel: to the dispatcher a sentinel-less exit is identical to "never ran", so it resets the task to the plan stage and discards origin commits — an infinite plan→impl→review→silent-exit loop.

### Step 3c: Verify the `fixed` claims against the diff (#1805)

Run this once the round's final verdict is settled — after the fix loop converged, or after the sparse-feedback skip decided there would be no fix cycle — and **before** emitting the Stage 3 Completion sentinel. It applies whether or not a fix loop ran: a finding adjudicated FIX with nothing ever committed for it is exactly the case this catches.

```bash
git fetch origin <branch-name>
FIX_TIP_SHA="$(git rev-parse origin/<branch-name>)"
FIX_DIFF="$(git diff "$CHECKPOINT_3A_SHA"...origin/<branch-name>)"
# envelope: {"verdict": <the adjudicated verdict from Checkpoint 3a / Step 3b>, "diff": "$FIX_DIFF", "reviewed_sha": "$FIX_TIP_SHA"}
printf '%s' "$VERIFY_INPUT" | cw review verify-fixes --base "$CHECKPOINT_3A_SHA" -
```

The diff boundary is cumulative — every commit since the verdict was frozen, not just the last fix cycle — so a fix landed in cycle 1 still counts in cycle 3. Always pass `--base "$CHECKPOINT_3A_SHA"` (the sha `CHECKPOINT_3A_SHA` captured at Checkpoint 3a step 2) — never `--no-base-check`, which is for tests and human recovery debugging only. A non-zero exit here — including a `DiffBaseMismatchError` from the `--base` check, or a `UsageError` if the flag itself was somehow dropped — is a hard pipeline error exactly like Checkpoint 3a step 3's: it must not be treated as "no downgrades this round," and must not be logged-and-continued.

Any `disposition: "fixed"` whose cited file/line that diff never touched is downgraded to `"dropped"`, with the reason in `disposition_detail`. **Use this command's output as the round's authoritative record**, and for each downgrade append a `friction_highlights` entry, e.g. `"fixed_disposition_downgraded_to_dropped: <file>:<line>"`.

This is **record-only**: a downgrade never triggers a new fix cycle and never re-opens the gate. It corrects what the record *claims* was done, which is the whole point — "we fixed it" with no diff behind it is the failure this ticket exists to make visible.

**Operator-action override (#1817) — the last check before the Stage 3 Completion sentinel.** After the verify-fixes pass, if `ADJUDICATIONS` contains any `outcome: "operator_action"` entry, Stage 3 EXITS `blocked` with `blocker.reason: "review_operator_actionable"` and `blocker.stage: "stage3_review"` (open enum; routes to BLOCKED_ON_USER via the generic dispatch rules, not to finalize — mirrors `plan_deviation`'s row shape), **instead of** whatever AUTO-CONTINUE / `review_pending_approval` outcome this round would otherwise produce. Surface the entries in `blocker.details`. The checklist comment already posted per the operator-actionable findings comment rule at Checkpoint 3a — do not post it again. Because that bucket is MUST_FIX-scoped at the model, this is a bare existence test with no severity branch.

**Which of Step 3b's exits this override governs.** Step 3c funnels two of Step 3b's three exits — fix-loop convergence and the sparse-feedback skip. The cycle-5 hard exit is neither (it exits `blocked`/`review_blocked` directly at Step 3b.5), and needs no override: it already posts the still-unresolved MUST_FIX findings, an operator-actionable finding's `ADJUDICATIONS` entry is recorded at Checkpoint 3a before Step 3b runs, and only the recorded `blocker.reason` differs.

---

## Stage 3 Completion (headless only)

After all Stage 3 steps complete successfully in headless mode (review clean or fix loop resolved, branch pushed with fix commits), emit the `AUTO_DEV_RESULT` sentinel:

**Before emitting the sentinel, resolve `scope.tier` explicitly.** The dispatcher's `apply_staged_decision` Rule 1 gates on this field — a null tier routes to `BLOCKED_ON_USER` when `queue_metadata.scope_hint` is also unset, a false-positive block. To resolve the tier:
1. Read `.cw/plan.md` — look for an explicit `Scope tier:`, `**Scope:** Small`, `tier: small`, or similar Stage-1c marker.
1.5. **One-time downgrade check (#1104 — defense-in-depth against a stale Stage-1 `forbidden_touched` misclassification).** If step 1's Stage-1c marker shows `forbidden_touched: true` AND every Stage-3 review agent that assessed the touched forbidden-area file(s) (Plan Soundness Reviewer's Tier-1/2 assessment plus any reviewer's own read of the diff) independently disagrees with the Stage-1 call AND the ticket is otherwise ≤10 files / ≤500 lines, THEN:
   - (a) Resolve `scope.tier = "small"` for **this stage's own sentinel and routing decision only** — do not re-run Stage 1.
   - (b) Append a `friction_highlights` entry, e.g. `"stage3_tier_downgrade: forbidden_touched corrected true->false for <file>, tier large->small"`.
   - (c) Rewrite the `**Scope tier:** ...` marker line in `.cw/plan.md` **in place** (single canonical line — replace, never append a second occurrence) to reflect the corrected tier and `forbidden_touched=false`, with an inline note `*(downgraded at Stage 3 — see friction_highlights)*`, so Stage 4/5's identical read-first-source precedence picks up the correction.
   - **One-time**: once rewritten, the marker no longer shows `forbidden_touched: true`, so the trigger cannot re-fire on a fix-loop re-entry.
   - No new Pydantic field — `health.downgrade_applied` is a different, confidence-driven concept and must NOT be reused here.
   - If the condition does not hold, skip this step and fall through to step 2 unchanged.
2. Fallback: read `.claude/cw-context.json` → `queue_metadata.scope_hint`.
3. Fallback: re-derive from the diff itself using the canonical Stage-1c thresholds — run `git diff --stat $FORK_POINT...origin/<branch-name>` and count changed files and lines. **Small** = ≤10 files AND ≤500 lines AND no forbidden-area touches; **Large** otherwise. (Account for any Step 3b scope growth.)
4. If no source yields `"small"` or `"large"`, **do NOT emit a `stage_complete` or `review_pending_approval` sentinel** — emit `blocked` instead with `blocker.reason: "scope_tier_unresolvable"`, `scope.tier: "small"` (required by the schema validator even on blocked — `auto_dev_result/schema.py`'s §3.3 validator rejects null at stage3_review), and `blocker.details: "scope.tier unresolvable — .cw/plan.md has no tier marker, .claude/cw-context.json queue_metadata.scope_hint is null, and diff stat was unavailable. Sentinel emitted with tier=null would fail schema validation and cause validation_failed retries rather than BLOCKED_ON_USER."`.

> **Maintenance note:** the `**Scope tier:** ...` marker format and its single-canonical-location convention are shared across 4 files: `auto-dev-plan.md` Step 1g (writer), `auto-dev-impl.md:53` (reader), this file's step 1.5 above (reader + conditional in-place rewriter), and `auto-dev-finalize.md:31` (reader). If the marker format is tuned, update all four locations atomically.

`scope.tier` must always be a concrete `"small"` or `"large"` in the emitted sentinel — the schema validator requires it beyond pre-impl. Fall back to `"small"` when emitting the `scope_tier_unresolvable` blocked sentinel above.

**`review.agents_run` must be set to the `agents_run` int from the frozen (Step 3a) `cw review consolidate` `.review` block** (see Checkpoint 3a) — not the template's placeholder `0`, not a manually tracked dispatch count (R3). Likewise `review.must_fix_initial` and `review.should_fix` come from that same frozen block, never recomputed at emission time.

**A Stage 3 pass whose diff against the base measures empty must exit `empty_diff_blocked` (#1870).** Measure it — `git diff --numstat "$(git merge-base origin/<default-branch> HEAD)"..HEAD` — and if the result is 0 files / 0 lines, emit `status: "empty_diff_blocked"` with `blocker.reason: "empty_diff_no_commits"`, NOT `review_pending_approval` and NOT `stage_complete`. This holds independently of whether reviewers ran and of what they concluded: a clean verdict over an empty branch reviewed nothing. It mirrors the mechanical check `codex_review/_verdict.py` performs for the codex-review executor, giving the Claude-native headless path parity. A measurement that *cannot be taken* (no `origin/<default-branch>` ref, git unavailable) is NOT this case — fall through to the ordinary exit rather than reporting an empty diff you did not observe. Dispatch re-verifies the same condition with its own git measurement at the REVIEW→FINALIZE checkpoint, so an omission here is caught rather than shipped, but it is caught with less context than you have now. **Caveat:** dispatch's backstop (`commits_ahead_of_default`, a commit-count measurement) is not identical to this diff-stat measurement — a content-empty commit (e.g. `git commit --allow-empty`) measures 0 files/0 lines here but a positive commit count there, so it is caught by this check but not by dispatch's. Don't rely on the dispatch backstop to cover a producer-side omission of *this specific* check.

**`review.deferred` is NOT one of the frozen three (#1805).** `must_fix_initial`/`should_fix`/`agents_run` are a cycle-0 baseline that must not move; `deferred` is by definition zero until adjudication and non-zero after. Source it from the `cw review adjudicate` output (Checkpoint 3a), which recomputes it while preserving the frozen three verbatim.

**Only emit this sentinel when invoked as a standalone `/auto-dev-review <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

**Validating is not emitting (#1890).** `cw result validate -` confirms the JSON is well-formed — it does not emit the sentinel. Never narrate emission as a separate act from performing it: the literal `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` frame, wrapping the validated JSON, MUST be the final characters of this same message.

**No interactive escalation, ever (#1890).** In headless mode there is no listener. Never escalate a MUST_FIX / `review_blocked` / `plan_deviation` / `review_operator_actionable` / `empty_diff_blocked` finding by asking a question and ending your turn — headless adjudication is already autonomous (Checkpoint 3a: "no AskUserQuestion"). Escalate exclusively via this sentinel's `blocker` field with `status: "blocked"`.

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 5,
  "ticket_id": "<ticket-id>",
  "status": "<review_pending_approval | blocked | empty_diff_blocked>",
  "stage_reached": "stage3_review",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": null,
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0, "deferred": 0, "agents_run": 0, "rejected_count": null, "rejected_count_by_severity": null},
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

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output review`.
