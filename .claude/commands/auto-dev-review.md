---
description: "auto-dev Stage 3: Review — spawn review agents, adjudicate findings, fix loop"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 3: Review

**Orientation:** Read `.cw/context.json` for ticket context and `.cw/plan.md` for the approved plan. The feature branch must already be pushed to origin (Stage 2 complete).

**Comments are live, not cached (#1730).** The cached `comments` array in `.cw/context.json` is a Stage-0 (or earlier-stage) snapshot only. Dispatch spawns `/auto-dev-{stage.value} <ticket> --headless` **directly per stage** (`src/cw/executor.py`, RFC 0005 A2) — Stage 0 does NOT re-run between pipeline stages, so on a REVIEW-stage re-entry (a queue re-dispatch, a `cw dev-queue requeue`, or a `--regress --stage review`) the cached array can be arbitrarily stale, and an operator send-back comment posted after it was last written would otherwise never reach this stage. Regardless of whether `.cw/context.json` already exists, this stage MUST live-fetch the ticket comments on every invocation via the active tracker's fetch op (`list_comments(<id>)` for `linear`; `gh issue view <n> --json comments` for `github-issues`), and overwrite `.cw/context.json`'s `comments` array with the fresh result. Mirrors `auto-dev-impl.md`'s own "Comments are live, not cached (#1794)" paragraph. **WARN on comments-fetch failure** (same handling as that paragraph): emit an attention signal, log `"review_comments_fetch_failed"` in `friction_highlights`, and leave the existing cached array untouched rather than overwriting it with an empty/failed result — a stale-but-real array is strictly better evidence than discarding what's there.

**Elevated priority on a post-regress re-entry.** If `.claude/cw-context.json`'s `queue_metadata.pending_operator_comment` is `true`, this REVIEW re-entry followed a regress/requeue — treat the live-fetched comments as elevated-priority and check them before adjudicating any MUST_FIX/SHOULD_FIX finding. The marker is stamped at the regress (`_stage_regress`, `src/cw/dev_queue/lifecycle.py`) and cleared once a REVIEW-stage spawn has consumed it (`src/cw/dispatch/claim.py`), so its presence means the operator's send-back has not yet been read by any reviewer. (`.claude/cw-context.json`, not `.cw/context.json` — the former is dispatch/session state written at spawn, `HOOK_CONTEXT_RELATIVE_PATH`; the latter is Stage-0 ticket context and never carries `queue_metadata`, #1730 round 5.)

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

**Track `SPAWNED_ROLES`:** as reviewers are spawned in this pass, record the roster of roles actually dispatched (conditional roster members — Data Safety, and any Large-scope conditional entry — count only when actually spawned, not when skipped). After each reviewer returns, extract its `<<<REVIEW_FINDINGS ... REVIEW_FINDINGS>>>` block (see the Output contract below) into that role's `ReviewerFindingsDocument`; when a response has no well-formed block, record a `ReviewerRunFailure {"role": "<role>", "reason": "unparseable_response"}` instead. Together these feed the `documents`/`failed_reviewers` arrays `cw review consolidate` validates at Checkpoint 3a — `review.agents_run` in the Stage 3 Completion sentinel below is sourced from that call's `.review.agents_run`, not a manually tracked int (R3). Carry `SPAWNED_ROLES` (and each role's captured document/failure) unchanged across any Step 3b re-review/fix-loop cycles — Step 3a's own consolidate call is what gets frozen for the sentinel, not fix-cycle churn.

Dispatch shape depends on mode (see issues #175 / #176 in claude-workspace for the orphan hazard this avoids):

- **Interactive mode:** all reviewers run with `run_in_background: true` (parallel — a human is watching and USER-origin Stop hooks do not auto-transition session state).
- **`--headless` mode:** reviewers run **serially** (no `run_in_background: true`). Block on each before dispatching the next, and do NOT end the parent turn between them. Background dispatch in headless trips the cw-side Stop-hook session-completion the same way Step 1b's Plan-agent dispatch did before `750ea77` — the parent's post-wait turn ends with `background_tasks: []` while pipeline work remains, orphaning the run with no sentinel. Losing parallelism is the price of correctness; the reviewer set typically completes in under 4 minutes serially for a Small-scope diff.

**Sandbox warning**: reviewer subagents spawned without `isolation: "worktree"` may have inconsistent file access depending on sandbox state — sometimes reads work, sometimes they're denied. The safest pattern is to **inline the full diff directly in each reviewer's prompt** (captured from the main session). This lets reviewers evaluate purely from the prompt content without needing filesystem access. Do not assume read access "just works."

**Before spawning reviewers, load project-specific extensions** (both optional, both forwarded into every reviewer prompt):
- `.claude/review-extras.md` at the project root — free-form prose rubrics the project owner wants every reviewer to apply on top of the global agent specs. Read verbatim. If absent, set `PROJECT_RUBRICS = null`.
- `.claude/sensitive-files.yml` at the project root — manifest of high-blast-radius paths. If present, diff the changed-files list against the manifest's globs. For every match, capture `(file_path, reason, category)` into `SENSITIVE_HITS`. If absent or no matches, set `SENSITIVE_HITS = []`.

**Every reviewer prompt must include:**
- **The full diff inlined as text in the prompt.** Before computing, fetch the branch so origin refs are current (the impl agent pushes from an isolation worktree; local refs may not reflect the push):
  ```bash
  git fetch origin <branch-name>
  git diff <FORK_POINT>...origin/<branch-name>
  ```
  Use `origin/<branch-name>`, not the bare local ref — the feature branch was pushed to origin from an isolation worktree and may not be visible in local refs until fetched. Use the fork point SHA from Checkpoint 2 for a deterministic diff. Do NOT use `origin/main` — it may have advanced since the worktree was created.
  For small scope, include the whole diff. For large scope, you may summarize the non-critical files but always inline the primary ones.
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
- **Business Context** (inlined verbatim — required for every reviewer, not just the Product Manager Reviewer):
  - Ticket ID and title
  - Ticket description (full text)
  - All ticket comments in chronological order (via the tracker's comment-fetch op — `list_comments` for `linear`, `gh issue view <n> --json comments` for `github-issues`) — decisions often live in comments and supersede the description — a comment reflecting the operator's decision on a specific prior finding is a **binding adjudication input**, not background color
  - Step 1c ambiguity resolutions (if any were collected) — the answers the human gave to ambiguous questions
  - For free-text tickets: the user-supplied description, marked as `[free-text, no Linear ticket]`
- Review focus areas:
  1. Does the change address the actual ticket? (PM Reviewer owns this lens; other reviewers flag only if blatantly obvious from their own domain.)
  2. Did implementation stay within plan scope? Flag creep.
  3. Do tests validate meaningful behavior?
  4. Could this break anything downstream?
  5. Debug artifacts left in? (`print()`, `breakpoint()`, `pdb`, `ic()`)
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
   "confidence": "HIGH|MEDIUM|LOW",
   "escalation": {"target_reviewer": "...", "evidence_quote": "..."} | null}]}
REVIEW_FINDINGS>>>
```

`NO_ISSUES` (a clean review) maps to `status="ok", findings=[]` — `detail`
MUST briefly state what was checked (e.g. "reviewed diff for X, Y, Z; no
issues found") since a blank `detail` on a clean `status="ok"` review is
rejected by the schema. If a rubric-mandated check could not be performed
in this pass, use `status="degraded"` (with `detail` naming what was
skipped) rather than reporting `"ok"`. `detail` is REQUIRED and MUST be non-empty whenever `status` is `"degraded"` or `"failed"` — a degraded or failed verdict with a blank `detail` is rejected by the schema as a contract violation, exactly like a blank `detail` on a clean `status="ok"` review with no findings; `cw review consolidate` (Checkpoint 3a) will not accept it. `evidence` MUST be a verbatim substring of the diff text at the claimed lines — `cw review consolidate` (Checkpoint 3a) rejects any finding whose evidence doesn't literally appear there. This block, plus the Friction Protocol and Health Check blocks, is the reviewer's entire structured output — this instruction **supersedes** the reviewer agent spec's own prose "## Output Format" section (MUST_FIX/SHOULD_FIX headings). The Codex adapter (#1236) applies the same override — see `cw.codex_review._context`'s `_OUTPUT_INSTRUCTIONS`, which inlines each agent spec verbatim then appends its own final JSON-schema instruction last so it takes precedence.

### Checkpoint 3a: Adjudicate every finding

**Extract, assemble, and consolidate mechanically** — this is a `cw review consolidate` call, not a prose "deduplicate/sort/group" step:

1. Extract each reviewer's `<<<REVIEW_FINDINGS ... REVIEW_FINDINGS>>>` block from its raw subagent response into `documents` (a JSON array of `ReviewerFindingsDocument` objects — one entry per role that produced a well-formed block). A role whose response has no well-formed `REVIEW_FINDINGS` block instead contributes a `ReviewerRunFailure {"role": "<role>", "reason": "unparseable_response"}` to `failed_reviewers` (see `SPAWNED_ROLES` tracking in Step 3a above).
2. Assemble the consolidate-input envelope:
   ```json
   {"documents": [...], "diff": "<the same diff text sent to reviewers>", "reviewed_sha": "<HEAD sha>", "failed_reviewers": [...]}
   ```
3. Run:
   ```bash
   printf '%s' "$CONSOLIDATE_INPUT" | cw review consolidate -
   ```
   A non-zero exit here is a hard pipeline error (the mechanical validation itself failed — e.g. malformed JSON or a schema violation) — not a normal adjudication path. Do not attempt to recover by re-running adjudication on the raw prose; treat it the same as any other unrecoverable Stage 3 tool failure.
3.5. **Consult the voided-findings record — `cw review check-voided` (#1814).** An operator who rejected a finding on a prior pass settled it permanently; a re-review that re-derives the same finding must not re-park the ticket on it. Assemble `{"verdict": <the verdict step 3 just printed>, "ticket_id": "<ticket id>", "comment_bodies": [<each live-fetched ticket comment body, per the Orientation section's mandatory live fetch>], "new_voided_entries": []}` and run:
   ```bash
   printf '%s' "$CHECK_VOIDED_INPUT" | cw review check-voided -
   ```
   Then:
   - **Use the printed `verdict` as the working set for the rest of Checkpoint 3a**, not step 3's raw consolidate output. Suppressed findings are stamped `disposition: "rejected"` and have already left `must_fix`/`blocking`.
   - **Append the printed `adjudications` verbatim to the `ADJUDICATIONS` array**, exactly as printed. They are ordinary `outcome: "reject"` entries; do not re-word their rationale, which names the settling operator comment.
   - A non-zero exit is a hard pipeline error, same as step 3.

4. On success, parse the working `ReviewVerdict` (step 3.5's `verdict`, which is step 3's own output verbatim when no void matched) and:
   - Use `.accepted` (validated MUST_FIX/SHOULD_FIX findings, deduped across reviewers) as the finding set entering the FIX NOW / REJECT / DEFER bucket logic below (unchanged prose, §4a/4b non-deferrable pre-pass, three buckets) — each finding now carries structured `file`/`line_start`/`summary`/`suggested_fix` instead of freeform prose.
   - Render `.accepted` entries with `severity` in `{NIT, PRINCIPLE}` informationally only — they are never eligible for bucket assignment (R5).
   - Render `.accepted` entries whose `disposition` is not `"fixed"` — a step-3.5 voided suppression, or a disposition some earlier step already stamped — informationally only, on the same footing as NIT/PRINCIPLE above (#1814). Their outcome is already recorded in `ADJUDICATIONS`; sorting one into a bucket would append a second, conflicting entry for the same identity.
   - Discard `.rejected` findings (whose evidence didn't match the diff) whose `severity` is **not** `MUST_FIX` from adjudication entirely — log them, do not present them to the bucket sort.
   - **A `.rejected` finding with `severity: MUST_FIX` may NOT be silently discarded** (#1714). Log it as above, but if any such finding is present, this pipeline run EXITS `blocked` with `blocker.reason: "review_blocked"` (same exit shape as the 5-cycle MUST_FIX case below). A finding rejected for a *mechanical* reason — bad line anchor, evidence quote absent from the diff — was never evaluated on its merits, so "no MUST_FIX survived validation" is not the same as "the code is clean". Do not attempt to fix it either: its anchor is by definition unreliable, so it is an operator-review signal, not a fix-loop input. `.rejected[].raw` carries the original `file`/`line_start`/`summary` — surface them in the exit's `blocker.details` so the operator can adjudicate manually. Also post them as a tracker comment per the blocking-findings comment rule below.
   - Log `.stripped_escalations` (escalations whose evidence quote wasn't in the diff — the underlying finding still survives and is adjudicated normally).
   - **Freeze** `.review.must_fix_initial`, `.review.should_fix`, and `.review.agents_run` from this *first* `cw review consolidate` call for the final sentinel (the "_initial" naming is the field's own contract). A Step 3b re-review re-invokes `cw review consolidate` on the updated diff purely to re-check `blocking`, not to overwrite these frozen numbers.

**Blocking-findings comment rule (#1815).** When Stage 3 exits `blocked` with `blocker.reason: "review_blocked"` — either the mechanically-rejected MUST_FIX path (#1714) above or the cycle-5 hard exit in Step 3b.5 — post the still-unresolved MUST_FIX finding(s) as a tracker comment under the fixed header `## Blocking Review Findings`, the same surface `auto-dev-plan.md` uses for its own `plan_unreviewable`/`plan_unsound` blocked exits. Source the body directly from the already-structured finding data (`file`/`line_start`/`line_end`/`summary`/`suggested_fix`, and each finding's `reviewer_role`) — one `### <reviewer_role> — MUST_FIX` sub-section per finding. No PR exists yet at this exit, so the comment posts to the ticket, not a PR. Sentinel: append `blocking findings posted: review_blocked` to `friction_highlights`, mirroring the same idiom `auto-dev-plan.md` uses.

**Non-deferrable pre-pass (run BEFORE bucket assignment):**

- **(4a) Non-deferrable pre-pass.** Before bucket assignment, scan every finding: any finding describing an implementation **deviation from an EXPLICIT plan requirement or prohibition** — a `do NOT X` the plan states, a mandated mechanism the plan named, a required test the plan named — is `NON_DEFERRABLE`. A NON_DEFERRABLE finding is eligible for **bucket 1 (FIX NOW) only**; it may never land in REJECT (bucket 2) or DEFER (bucket 3).
- **(4b) Spec-citation cross-check.** If a proposed bucket-2/3 rationale contains any of the literal trigger phrases — `"required by spec"`, `"plan mandated"`, `"plan requires"`, `"the RFC says"`, `"operator required"`, `"operator decided"`, `"ticket spec"` — the coordinating session MUST verify the claim against `.cw/plan.md` **verbatim** before accepting the rejection/deferral. If `.cw/plan.md` contradicts the justification, the finding is `NON_DEFERRABLE`.
- **(4c) Operator send-back cross-check (#1730).** The live-fetched ticket comments (see Orientation) are a **binding adjudication input**, not background color: a comment in which the operator adjudicated a specific prior finding — accepting it, rejecting it, or scoping it out — settles that finding here, and the coordinating session may not re-litigate it into a different bucket. When `.claude/cw-context.json`'s `queue_metadata.pending_operator_comment` is `true`, this REVIEW re-entry followed a regress/requeue and the send-back has not yet been read by any reviewer: check the comments **before** assigning any MUST_FIX/SHOULD_FIX finding to a bucket, and record which comment settled it in the adjudication rationale. **An operator-settled finding is adjudicated exactly like any other (#1805):** it gets its own entry in the `ADJUDICATIONS` array below, with the `outcome` the comment dictated and — for `reject`/`defer`, where `rationale` is REQUIRED — a `rationale` that names the operator comment (author and date) as the source of the outcome. Recording an operator's decision only in prose would make it the one adjudication missing from the array that `cw review adjudicate` treats as the single source of truth, reintroducing from a new direction the exact "recorded in only one of the two places" defect that step exists to close.

  **Also make the decision durable (#1814).** An `ADJUDICATIONS` entry settles the finding for *this* pass only; the array is session-local and dies with the session. A regress/redispatch, or the codex backend (which has no coordinating session at all and re-derives its findings mechanically every pass), would raise the same finding again with nothing to stop it. So when an operator comment settles a REJECT, **also** append one entry to a session-local `NEW_VOIDED_FINDINGS` list:
  ```json
  {"severity": "<finding.severity>", "file": "<finding.file>", "summary": "<finding.summary>", "evidence": "<finding.evidence>",
   "operator_comment_id": "<author>@<created_at>", "operator_comment_excerpt": "<the sentence that settled it>",
   "original_rationale": "<the same one-line rationale you put in the ADJUDICATIONS entry>"}
  ```
  `severity`/`file`/`summary`/`evidence` are copied verbatim off the finding — they are the content anchor a later pass matches on, so a paraphrase breaks the match. `operator_comment_id` is the `"<author>@<created_at>"` composite (the same author+date citation this step's rationale already uses; the materialized comments array carries no numeric comment id). Do **not** record a line number: position is deliberately not part of the identity, so a voided finding whose code later moves still matches, and an unrelated new finding at the old line never does. Carry `NEW_VOIDED_FINDINGS` cumulatively across any Step 3b re-review cycles.
- **Exit rule.** If a NON_DEFERRABLE finding cannot be fixed within the fix loop, or is judged **"beyond fix-loop scope,"** the stage does **not** PROCEED. `"beyond fix-loop scope"` is an **escalation trigger, not an auto-approve**: Stage 3 EXITS `blocked` with `blocker.reason: "plan_deviation"` (open enum), routing to BLOCKED_ON_USER via existing dispatch rules. The "plan in question" note: *the pipeline never adjudicates plan-vs-impl blame — it always exits `blocked`/`plan_deviation`; whether to `cw dev-queue requeue --regress` back to impl or revisit the plan is orchestrator/operator policy.*
- **Relationship to the Step 2.5 scope-conformance gate (#1779).** Gross *file-set* drift no longer reaches this stage: `.claude/scripts/check_plan_scope_conformance.py` measures the delivered diff against the plan's `## Files Modified` list at Step 2.5 and exits `blocked`/`plan_scope_drift` **before any reviewer is spawned**, precisely because a fix loop cannot converge on a diff that outgrew its approved file set. `plan_deviation` remains the backstop for what a file-count comparison structurally cannot see: **semantic** drift inside an otherwise-conforming file set — the planned files, carrying the wrong content, mechanism, or behavior. If you find yourself reaching for `plan_deviation` to describe "this diff is simply too big," that is the mechanical gate's job and it either already passed or failed open (`impl_scope_conformance_unparsed` in `friction_highlights`) — say so in the finding rather than re-deriving it here.

Adjudication assigns each finding a disposition. The coordinating session — never a subagent or executor — sorts **every** finding (MUST_FIX *and* SHOULD_FIX) into exactly one of three buckets. Two carve-outs, and only two: a finding whose `severity` is NIT/PRINCIPLE (never eligible, R5), and a finding whose `disposition` is already not `"fixed"` (#1814 — its outcome is recorded, and severity alone does not exclude it because a voided finding keeps its MUST_FIX/SHOULD_FIX severity). Everything else gets exactly one bucket:

1. **FIX NOW → the action list.** All surviving MUST_FIX plus any SHOULD_FIX the session accepts into scope. Principle: *if it stays in the review, it's worth fixing.* The filtering happens here, at scoping — not by silently ignoring the returned list.
2. **REJECT (review-the-review).** The session disagrees: the finding is wrong, or the code is a deliberate choice / documented tradeoff. **Record the rationale** (see "Recording adjudication" below). No fix, no ticket. **Excludes NON_DEFERRABLE findings** — a plan-deviation finding may never be rejected.
3. **DEFER.** Valid but out of scope for this ticket ("handle when scale demands"). **Record now, file as a ticket on merge** (PR Hygiene Sweep Step H3). Skip the ticket only when the item is already a bucket-2 documented tradeoff. **Excludes NON_DEFERRABLE findings** — a plan-deviation finding may never be deferred.

**Invariant:** every finding ends *fixed*, *rejected-with-reason*, or *ticketed*. A reviewer finding that simply vanishes is a process failure.

The **action list** (bucket 1) — not "MUST_FIX only" — is what drives Step 3b. An accepted SHOULD_FIX is fixed; a rejected or deferred SHOULD_FIX leaves the action list, recorded. If every finding lands in REJECT/DEFER the action list is empty and the pipeline continues (rejections recorded, deferrals queued for merge).

**Adjudication is judgment** → it stays on the coordinating session. A stateless executor must never decide whether a finding is correct — that is exactly how the "we did X for a reason" pushback gets lost. An executor may only *mechanically apply* an action-list fix the session has already decided ("change X to Y in file Z").

**Recording adjudication:** every bucket assignment — FIX included — becomes one entry in the `ADJUDICATIONS` array described below, which is the single source both the verdict and `.cw/deferred-findings.md` are generated from. Do not record a decision in only one of the two places.
- **Rejections (bucket 2):** one entry with `outcome: "reject"` + a one-line `rationale`; these reach the PR body via Stage 4 (Step 4d). Append the same to `friction_highlights`. For a rejection rooted in non-obvious design intent, add an inline `# Why:` comment at the code site (per the global review-culture rule).
- **Deferrals (bucket 3):** one entry with `outcome: "defer"` + `rationale`; Stage 4 (Step 4d) writes them into the machine-readable `DEFERRED-REVIEW-FINDINGS` block in the PR body for Step H3 to harvest. Append a one-line note to `friction_highlights`.
- **Fixes (bucket 1):** one entry with `outcome: "fix"` and no rationale. Omitting it does not fail the run, but it leaves the finding recorded as `"dropped"` (nobody decided) — and for a MUST_FIX that keeps the verdict blocking.

**Stamp the adjudication mechanically — `cw review adjudicate` (#1805).** The judgment above is unchanged; only its *serialization* is now mechanical. Before this step existed, the outcome was typed twice — once as prose into `.cw/deferred-findings.md`, once implicitly as the `disposition="fixed"` every accepted finding still carried from `cw review consolidate` — and only the first was accurate.

1. As each finding is bucket-sorted, append one `Adjudication` object to an `ADJUDICATIONS` array, copying the identity fields straight off the finding:
   ```json
   {"severity": "<finding.severity>", "file": "<finding.file>", "line_start": <finding.line_start>, "line_end": <finding.line_end>, "evidence": "<finding.evidence>", "summary": "<finding.summary>", "outcome": "<fix|reject|defer>", "rationale": "<one-line rationale>"}
   ```
   `rationale` is REQUIRED (non-blank) for `reject`/`defer` and unused for `fix`. `evidence` is a same-location tiebreak, not part of the identity match — copy it, but a stale one does not break the match.
2. After the full sort, assemble `{"verdict": <the frozen Checkpoint-3a verdict>, "adjudications": ADJUDICATIONS}` and run:
   ```bash
   printf '%s' "$ADJUDICATE_INPUT" | cw review adjudicate --deferred-findings-out .cw/deferred-findings.md -
   ```
   This writes `.cw/deferred-findings.md` for you in the documented shape below (creating `.cw/` if absent, and writing nothing at all when every finding was fixed) — do NOT also hand-author it.
3. **Use the printed `ReviewVerdict` as this round's saved record from here on**, not the raw `cw review consolidate` output. It carries the real `disposition` per finding plus recomputed `blocking`/`must_fix`/`review.deferred`.
4. **If the printed verdict's `unmatched_adjudication_count` is > 0, append `"adjudication_unmatched_count: <N>"` to `friction_highlights`.** A non-zero count means an adjudication entry matched no accepted finding (stale anchor, ambiguous same-location collision, duplicate entry) — the command does not fail on it, so this is the only thing that makes it visible at the approval gate.
5. **Persist any newly-voided findings to the ticket (#1814) — UNCONDITIONAL.** If `NEW_VOIDED_FINDINGS` (step 4c) is non-empty, re-run `cw review check-voided` with it folded in and render the record out:
   ```bash
   printf '%s' "$CHECK_VOIDED_INPUT_WITH_NEW_ENTRIES" | cw review check-voided --voided-findings-out .cw/voided-findings-comment.md -
   ```
   Then post (or update) that file's contents as a ticket comment. The file already carries its own `## Voided Review Findings` header and the machine-readable sentinel — post it verbatim; do NOT hand-author or re-wrap it. This runs **regardless of whether Stage 3 continues to Stage 4 or exits `blocked`**, mirroring the blocking-findings comment rule's own unconditional placement: an exit is exactly the path where the next pass re-derives the finding, so skipping the post on a blocked exit would defeat the record's entire purpose. Append `"voided findings recorded: <N>"` to `friction_highlights`.

Findings the pass never covers are stamped `disposition: "dropped"`. A dropped **MUST_FIX** deliberately still counts toward `blocking`/`must_fix`: nobody decided its fate, and erring toward a gate failure beats silent pass-through. If the printed verdict blocks and you believe every MUST_FIX *was* adjudicated, the missing entry — not the finding — is the bug.

The generated file's shape (unchanged, reproduced byte-for-byte by `cw review adjudicate`; Stage 4's Step 4d copies it into the PR body and the Step H3 sweep greps the sentinels literally):

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

The `Rejected` section is omitted when there are no rejections, the `DEFERRED-REVIEW-FINDINGS` block when there are no deferrals, and the file is not written at all when every finding was fixed — all three handled by the command, not by you.

**Headless:** adjudication is autonomous — **no AskUserQuestion.** The session adjudicates deterministically, records rationale for every REJECT/DEFER in `.cw/deferred-findings.md`, and proceeds. Interactive mode MAY surface the adjudication for confirmation but defaults to the same dispositions.

**Small scope + NO_ISSUES → AUTO-ACCEPT.** Log "Review clean" and proceed to S4.

**Small scope + SHOULD_FIX only (no MUST_FIX) → adjudicate per Checkpoint 3a, then:**
- Action list non-empty (accepted SHOULD_FIX) → run Step 3b on it.
- All SHOULD_FIX land in REJECT/DEFER → action list empty → AUTO-CONTINUE to S4 (rejections recorded, deferrals queued in `.cw/deferred-findings.md`).
- Log the disposition: "N SHOULD_FIX adjudicated — <a> fixed, <b> rejected, <c> deferred".

**Small scope + MUST_FIX → AskUserQuestion (interactive only):**
- Present MUST_FIX findings (with file, line, description, suggested fix)
- Present SHOULD_FIX findings if any
- "MUST_FIX findings block shipping. Fix and re-review, skip fixes and ship anyway, skip ticket, or abort?"

**Large scope (any result) → AskUserQuestion (interactive only):**
- Present full consolidated review report
- If MUST_FIX: "Fix these issues and re-review, or abort?"
- If clean or SHOULD_FIX only: "Review complete. Proceed to PR creation?"

**Headless:** Always run reviewers, then adjudicate every finding per Checkpoint 3a (autonomous — no AskUserQuestion; record rationale for every REJECT/DEFER in `.cw/deferred-findings.md`). Non-empty action list → run fix loop (expected 2 cycles, hard-cap at 5; cycles 3+ or scope growth append to `friction_highlights` and set `health.fix_loop_escalated: true`). Empty action list (every finding fixed / rejected / deferred) + small → emit `stage.entered` (`s3_review_complete`) then AUTO-CONTINUE to S4:
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_complete\",\"prev_stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```
Clean/SHOULD_FIX + large → EXIT `review_pending_approval`. MUST_FIX persists after 5 cycles → EXIT `blocked` with `blocker.reason: "review_blocked"`.

### Step 3b: Fix Loop (when MUST_FIX needs fixing)

**Important**: you cannot attach a new subagent to the original implementation worktree. Subagents spawned without `isolation: "worktree"` inherit the main session's sandbox, which typically does not include other worktrees. `isolation: "worktree"` always creates a *new* worktree, not an attachment to an existing one. The correct pattern is **push-then-recheckout**.

Prerequisite: the implementation branch must already be on origin. Step 2's Implementation agent should have pushed per its instructions — if not, escalate BLOCK before starting the fix loop.

1. Remove the stale implementation worktree and the local branch ref from the main session (the branch still exists on origin):
   ```bash
   git worktree remove --force <impl-worktree-path>
   git branch -D <branch-name>  # local only; origin still has it
   ```

2. Spawn the fix agent with `isolation: "worktree"`, `model: "sonnet"`, and `run_in_background: true`. The agent's **first actions** must be:
   ```bash
   git fetch origin
   git checkout -B <branch-name> origin/<branch-name>   # -B: idempotent (cw may pre-provision this branch, #712)
   git log --oneline -1  # verify at expected impl commit

   # Refresh with latest main so the fix lands on top of upstream moves.
   # Failure mode avoided: a sibling PR (e.g. another ticket in this same
   # pipeline run) may have merged to main AFTER Step 2 pushed. Without
   # this merge, subsequent pushes ship a branch that's silently missing
   # commits from main — CI passes because it runs branch-HEAD, not the
   # branch-merged-with-main state.
   git merge origin/main --no-edit
   ```
   If merge conflicts occur → BLOCK with file list. Do not force.

3. Agent fixes MUST_FIX issues, re-runs quality gates, creates a NEW commit on top (do NOT amend) **with the trailer `Auto-Dev-Fix-Cycle: <N>`** (where `<N>` is the current cycle number, 1-5; pass via `git commit --trailer "Auto-Dev-Fix-Cycle: <N>"`), and pushes to origin using the explicit-refspec form (`git push origin HEAD:refs/heads/<branch-name>`) — defensive form, robust against any local branch rename even after the `git checkout -b <branch-name> origin/<branch-name>` in step 2 above. After pushing, verify with `git rev-parse origin/<branch-name>` matching `git rev-parse HEAD`.

The `Auto-Dev-Fix-Cycle` trailer is the durable cross-session signal for fix-loop progress. The resume detector reads the max `<N>` across fix-cycle trailers on commits newer than `Auto-Dev-Stage: impl-complete` to determine current cycle. On resume into `s3_fix_loop, substage="cycle_N"`, the pipeline resumes at cycle `N+1` (next iteration), not from cycle 1 — preserving the cycle budget across session deaths.

The fix-loop agent's prompt must end with both the Friction Protocol block and the following Health Check block verbatim:
   ```
   ## Health Check
   - **Context usage**: <rough % or HIGH/MEDIUM/LOW>
   - **On-spec confidence**: HIGH | MEDIUM | LOW
   - **Shortcuts taken under pressure**: [list or NONE]
   - **Could work be incomplete?**: NO | MAYBE | YES (explain)
   - **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
   ```

   The fix-loop agent's prompt must ALSO include the same **Completion Artifacts** block as Stage 2 (Test command, test output tail, `git diff --stat`, `git log --oneline`, and the per-gate quality gate results table — one row per configured `quality_gate_commands` entry, `pass` | `<errors>` | `not_run`, per the Stage 2 contract in `auto-dev-impl.md`) — the orchestrator gates fix completion on facts the same way it gates impl completion (Subagent Reliability Mitigation 2). Incremental commits same as Stage 2 (Mitigation 3): one commit per MUST_FIX item resolved, not a single end-of-loop commit.

   The fix-loop agent's prompt must ALSO instruct: "If your fix touches any file outside the original Stage 1 approved plan's file list, OR if your changes push the diff into Large tier (>10 files OR >500 lines OR a forbidden area), report this in the friction report under a new bullet `**Scope growth**: [list affected files / explain tier change]`. The main session uses this to decide escalation."

3b. **Orchestrator fix gate** (Subagent Reliability Mitigation 1, fix-loop variant). After the fix-loop agent returns (or times out), before re-running review (or before the sparse-feedback gate in step 4):
   - Re-run the test command in the impl worktree. Non-zero exit → fix is false; treat as cycle failure (counts against the 5-cycle hard cap).
   - Re-run mypy/ruff. Non-zero on touched files → fix is false.
   - Compare pasted `git diff --stat` against live `git diff --stat $FORK_POINT`. Substantial mismatch → fix is false.
   - Verify the fix produced at least one new commit since the prior cycle (`git log $PRIOR_HEAD..HEAD --oneline` must be non-empty). Zero new commits → fix-loop agent did not actually fix anything; treat as cycle failure.

   On gate failure: log the failed check, increment the cycle counter, and re-spawn the fix agent (within the 5-cycle hard cap). Headless: append `"fix_loop_gate_failed_cycle_<N>"` to `friction_highlights`, and emit `stage.errored`:
   ```bash
   cw event record stage.errored \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"fix_cycle_failed\"}" || true
   ```
   The cycle still consumed budget — a false-completion fix counts against the cap.

   On all gates pass: proceed to step 4 (sparse gate / re-review).

4. **Sparse-feedback gate, then re-run review.** Before re-running review, check whether the cycle qualifies for the sparse-feedback skip. The fix-then-rereview cycle is NOT mandatory when initial feedback was sparse and the fix is small relative to the original change.

   **Skip re-review when ALL of the following hold (Small scope only):**
   - Scope tier was **Small** at Stage 1c, AND no scope growth was flagged in the fix-loop friction report (still Small)
   - Initial review produced ≤2 MUST_FIX items
   - Fix-loop diff is small relative to the original implementation diff — judgment call, no hard line ceiling. A 2-line touch on a 50-line PR is sparse; a rewrite of half the implementation is not. Proportionality is what matters.
   - Fix did not touch files outside the original Stage 1 plan's file list
   - No SHOULD_FIX items adjacent to the MUST_FIX areas were left unaddressed in a way that warrants a second look

   When skipping: log `Skipping re-review — Small scope, sparse fix (<N MUST_FIX> resolved, fix diff small relative to original). Proceeding to Stage 4.`, document the decision in the friction report under `**Re-review skipped**`, and jump directly to Stage 4.

   When in doubt, run re-review. The skip is for unambiguously small fixes only.

   **Headless:** apply the same criteria deterministically. If all conditions hold, skip re-review and proceed to S4 AUTO-CREATE; append `"rereview_skipped_sparse"` to `friction_highlights`. If any condition is uncertain, run re-review.

   **Otherwise, re-run review agents (same set).** Before computing the updated diff, fetch the branch to pick up the fix-loop agent's push:
   ```bash
   git fetch origin <branch-name>
   ```
   Then pass the updated full diff (`git diff <FORK_POINT>...origin/<branch-name>`) and the fix-commit diff inlined in each reviewer's prompt (see Step 3a sandbox warning). Do NOT rely on reviewers reading files from disk. Each re-spawned reviewer prompt MUST include both the Friction Protocol block and the Health Check block, identical to the initial Step 3a spawn.

5. **Cycle budget:** 2 cycles is the expected baseline. If MUST_FIX persists past cycle 2, the loop may continue with escalation visibility, hard-capped at 5 total cycles. Escalation behavior differs between modes — see below.

   **Escalation triggers** (any of these counts as "an escalation event"):
   - Cycle 3, 4, or 5 entered (i.e., MUST_FIX persisted past the expected 2)
   - Fix-loop diff touches files outside the original Stage 1 approved plan's file list
   - Fix-loop diff promotes scope tier from Small → Large (file count > 10 OR line count > 500 OR forbidden area touched)

   The fix-loop agent's friction report MUST flag scope growth explicitly so the main session can decide whether the cycle counts as an escalation event. Do not let the agent silently grow scope.

   **Interactive — on each escalation event:** log a one-line notice to the user describing the trigger (e.g., `⚠ Fix loop entered cycle 3 (expected baseline is 2)` or `⚠ Fix loop cycle 2 grew scope outside plan: <files>`). Do NOT block on AskUserQuestion for these — the user can stop the pipeline between agent dispatches after seeing the notice, and the cycle-5 hard gate provides the final decision point. This is a deliberate trade-off: the prior spec gated at cycle 2; the current spec exchanges that early hard gate for reduced prompt fatigue, accepting that interactive cycles 3-4 will run with notice-only visibility rather than gating.

   **Headless — on each escalation event:** append a string to `friction_highlights` (e.g., `"fix_loop_cycle_3_entered"`, `"fix_loop_scope_growth: <files>"`) AND set `health.fix_loop_escalated: true` in the structured output payload. Continue the loop without any AskUserQuestion. (`health.fix_loop_escalated` is distinct from `health.downgrade_applied`, which is set only by the Headless Mode health aggregation rule for confidence-driven status downgrades.)

   **Hard exit (cycle 5 failed to clear MUST_FIX) — applies in both modes:**
   - **Interactive:** AskUserQuestion: "MUST_FIX issues persist after 5 fix cycles: [details]. Continue manually from worktree, skip ticket, or abort pipeline?"
   - **Headless:** EXIT `blocked` with `blocker.reason: "review_blocked"`. The `friction_highlights` field will contain the per-cycle escalation notes from cycles 3–5; the human reviewer sees them in the structured output. Also post the still-unresolved MUST_FIX findings — verbatim — as a tracker comment per the blocking-findings comment rule above (Checkpoint 3a).

   > **Maintenance note:** the cap values (`expected 2`, `hard-cap at 5`) appear in 6 locations: this Step 3b.5 (multiple), the Checkpoint 3a Headless callout, the gate-collapse table rows for `S3 action list non-empty`, `S3 action list non-empty after 5 fix cycles`, and `S3 fix-loop cycle 3+`, and the `blocker.reason` table description for `review_blocked`. If you tune either value, update all locations atomically.

**Fallback — direct execution**: If the isolation fix agent also hits sandbox failures (Read/Write/Bash denied inside its own new worktree — this has been observed), the main session can apply the fix directly from its own worktree:

```bash
# From the main session's worktree
git fetch origin <branch-name>
git checkout -B <branch-name> origin/<branch-name>   # -B: idempotent — cw provisions this worktree on <branch-name> (#712), so plain -b would fail "already exists"
git merge origin/main --no-edit                      # refresh with main (see Step 3b.2 rationale)
# apply edits via Read/Edit/Write tools
# run quality gates
git add -- <changed files>
git commit -m "..."
git push origin HEAD:refs/heads/<branch-name>        # explicit refspec — robust if local branch was renamed
test "$(git rev-parse origin/<branch-name>)" = "$(git rev-parse HEAD)"  # verify the push landed
git checkout <original-branch>   # restore main session state
```

Direct execution is slower than delegation but guaranteed to work. Use it as a last resort when two subagent attempts have failed due to sandbox issues.

**Pre-exit invariant (required, no exceptions):** Before ending the review session at any point — on normal completion, on error, at a context boundary, or after the fallback path above — run:
```bash
git status --porcelain
```
If the output is non-empty (staged or unstaged changes exist), you MUST either:
- **Commit and push** the staged changes (if they represent completed work), then emit the sentinel as normal, OR
- **Emit a `blocked` sentinel** using the full sentinel template from Stage 3 Completion (scroll down to it), with `blocker.reason: "dirty_tree_no_sentinel"`, `scope.tier: "small"` (required by the schema validator even on blocked — `auto_dev_result/schema.py`'s §3.3 validator rejects null at stage3_review), `blocker.details: "staged or unstaged changes exist but could not be committed and pushed before session end — emitting blocked rather than exiting silently with a dirty index and no sentinel"`, and `health.lowest_agent_confidence` set to a non-null value (the same §3.3 validator in `auto_dev_result/schema.py` requires it for stage3_review; omitting it causes schema rejection → `validation_failed` retries rather than `BLOCKED_ON_USER`).

Never exit with a dirty tree and no sentinel. A session exit with no sentinel looks identical to "never ran" to the dispatcher — it resets the task to the plan stage and discards origin commits, causing an infinite plan→impl→review→silent-exit loop.

### Step 3c: Verify the `fixed` claims against the diff (#1805)

Run this once the round's final verdict is settled — after the fix loop converged, or after the sparse-feedback skip decided there would be no fix cycle — and **before** emitting the Stage 3 Completion sentinel. It applies whether or not a fix loop ran: a finding adjudicated FIX that nothing was ever committed for is exactly the case this catches.

```bash
git fetch origin <branch-name>
FIX_DIFF="$(git diff <the sha the Checkpoint-3a verdict was frozen at>...origin/<branch-name>)"
# envelope: {"verdict": <the adjudicated verdict from Checkpoint 3a / Step 3b>, "diff": "$FIX_DIFF"}
printf '%s' "$VERIFY_INPUT" | cw review verify-fixes -
```

The diff boundary is cumulative — every commit since the verdict was frozen, not just the last fix cycle — so a fix landed in cycle 1 still counts in cycle 3.

Any `disposition: "fixed"` whose cited file/line that diff never touched is downgraded to `"dropped"`, with the reason in `disposition_detail`. **Use this command's output as the round's authoritative record**, and for each downgrade append a `friction_highlights` entry, e.g. `"fixed_disposition_downgraded_to_dropped: <file>:<line>"`.

This is **record-only**: a downgrade never triggers a new fix cycle and never re-opens the gate. It corrects what the record *claims* was done, which is the whole point — "we fixed it" with no diff behind it is the failure this ticket exists to make visible.

---

## Stage 3 Completion (headless only)

After all Stage 3 steps complete successfully in headless mode (review clean or fix loop resolved, branch pushed with fix commits), emit the `AUTO_DEV_RESULT` sentinel:

**Before emitting the sentinel, resolve `scope.tier` explicitly.** `apply_staged_decision` Rule 1 in the dispatcher gates on this field — a null tier causes Rule 1 to route to `BLOCKED_ON_USER` when `queue_metadata.scope_hint` is also unset, creating false-positive blocks. The model does not always carry the Stage-1c tier classification forward automatically.

To resolve the tier:
1. Read `.cw/plan.md` — look for an explicit `Scope tier:`, `**Scope:** Small`, `tier: small`, or similar Stage-1c marker.
1.5. **One-time downgrade check (#1104 — defense-in-depth against a stale Stage-1 `forbidden_touched` misclassification).** If the Stage-1c marker read in step 1 shows `forbidden_touched: true` AND every Stage-3 review agent that assessed the touched forbidden-area file(s) (Plan Soundness Reviewer's Tier-1/2 assessment plus any reviewer's own read of the diff) independently agrees the touched content is non-pipeline (a disagreement with the Stage-1 call) AND the ticket is otherwise ≤10 files / ≤500 lines, THEN:
   - (a) Resolve `scope.tier = "small"` for **this stage's own sentinel and routing decision only** — do not re-run Stage 1.
   - (b) Append a `friction_highlights` entry, e.g. `"stage3_tier_downgrade: forbidden_touched corrected true->false for <file>, tier large->small"`.
   - (c) Rewrite the `**Scope tier:** ...` marker line in `.cw/plan.md` **in place** (single canonical line — replace, don't append a second occurrence) to reflect the corrected tier and `forbidden_touched=false`, with an inline note `*(downgraded at Stage 3 — see friction_highlights)*`, so Stage 4/5's identical read-first-source precedence naturally picks up the correction.
   - This is a **one-time** operation: once the marker is rewritten, this step does not downgrade a second time even on a fix-loop re-entry — the marker no longer shows `forbidden_touched: true`, so the trigger condition above doesn't re-fire.
   - No new Pydantic field is used for this — `health.downgrade_applied` is a different, confidence-driven concept (status downgrades only, hard-constrained by its schema validator) and must NOT be reused here.
   - If the condition does not hold (marker shows `forbidden_touched: false` already, or a reviewer disagrees with the downgrade, or the ticket exceeds 10 files / 500 lines), skip this step and fall through to step 2 unchanged.
2. Fallback: read `.claude/cw-context.json` → `queue_metadata.scope_hint`.
3. Fallback: re-derive from the diff itself using the canonical Stage-1c thresholds — run `git diff --stat $FORK_POINT...origin/<branch-name>` and count changed files and lines. **Small** = ≤10 files AND ≤500 lines AND no forbidden-area touches; **Large** otherwise. (Account for any Step 3b scope growth.)
4. If no source yields `"small"` or `"large"`, **do NOT emit a `stage_complete` or `review_pending_approval` sentinel** — emit `blocked` instead with `blocker.reason: "scope_tier_unresolvable"`, `scope.tier: "small"` (required by the schema validator even on blocked — `auto_dev_result/schema.py`'s §3.3 validator rejects null at stage3_review), and `blocker.details: "scope.tier unresolvable — .cw/plan.md has no tier marker, .claude/cw-context.json queue_metadata.scope_hint is null, and diff stat was unavailable. Sentinel emitted with tier=null would fail schema validation and cause validation_failed retries rather than BLOCKED_ON_USER."`.

> **Maintenance note:** the `**Scope tier:** ...` marker format and its single-canonical-location convention are shared across 4 files: `auto-dev-plan.md` Step 1g (writer), `auto-dev-impl.md:61` (reader), this file's step 1.5 above (reader + conditional in-place rewriter), and `auto-dev-finalize.md:31` (reader). If the marker format is tuned, update all four locations atomically.

`scope.tier` must always be a concrete value (`"small"` or `"large"`) in the emitted sentinel — the schema validator requires it for any stage beyond pre-impl. Use the resolved tier when available; fall back to `"small"` when emitting the `scope_tier_unresolvable` blocked sentinel above.

**`review.agents_run` must be set to the `agents_run` int from the frozen (Step 3a) `cw review consolidate` `.review` block** (see Checkpoint 3a), not left at the template's placeholder `0` and not a manually tracked dispatch count (R3). Likewise, `review.must_fix_initial` and `review.should_fix` must be sourced from that same frozen block, not recomputed at sentinel-emission time.

**`review.deferred` is NOT one of the frozen three (#1805).** It is a different kind of field: `must_fix_initial`/`should_fix`/`agents_run` are a cycle-0 baseline that must not move, whereas `deferred` is *by definition* zero until adjudication happens and non-zero afterwards. Source it from the `cw review adjudicate` output (Checkpoint 3a), which recomputes it while preserving the frozen three verbatim — freezing it too would pin it at the meaningless pre-adjudication `0`.

**Only emit this sentinel when invoked as a standalone `/auto-dev-review <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 5,
  "ticket_id": "<ticket-id>",
  "status": "<review_pending_approval | blocked>",
  "stage_reached": "stage3_review",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": null,
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0, "deferred": 0, "agents_run": 0},
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
