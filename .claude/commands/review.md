---
description: Code review using parallel specialized agents. Only actionable findings are surfaced.
argument-hint: "[PR #number | last N commits | branch-name | (default: diff vs main)]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Code Review

Run a comprehensive code review using a team of specialized agents in parallel. Each agent focuses on a different concern. Only agents with actionable findings get surfaced.

**Arguments:** "$ARGUMENTS"

---

## Step 1: Parse Review Target

Determine what to review from `$ARGUMENTS`:

| Input | Example | Diff Command |
|-------|---------|-------------|
| PR number | `#123`, `123` | `gh pr diff 123` (see PR base note below) |
| Commit count | `last 3`, `3 commits` | `git diff HEAD~3` |
| Commit SHA (fork point) | `a1b2c3d` | `git diff a1b2c3d...HEAD` |
| Branch name | `feature/foo` | `git fetch origin feature/foo --quiet && git diff origin/feature/foo...HEAD` |
| Nothing (default) | _(empty)_ | `git fetch origin main --quiet && git diff origin/main...HEAD` |

**Why `origin/main`, not `main`:** local `main` may be stale. The three-dot form (`A...B`) diffs against `git merge-base A B`, so even if origin/main has been merged into the branch, only the branch's own changes appear.

**Detection rules:**
- Bare number or `#N` → PR number
- `last N` or `N commits` → commit count
- 7+ hex chars matching a valid commit (`git rev-parse --verify`) → fork-point diff
- String that matches a branch → branch diff
- Empty or unrecognized → diff against `origin/main`

**For PR reviews**, fetch PR metadata, **checkout the PR branch**, and use the PR's actual base ref (not hardcoded `main`) — handles stacked PRs and non-main bases:
```bash
gh pr view <number> --json title,body,baseRefName,headRefName
git fetch origin pull/<number>/head:pr-review-<number> 2>/dev/null || git fetch origin <headRefName>
git fetch origin <baseRefName> --quiet
git checkout pr-review-<number> 2>/dev/null || git checkout <headRefName>
```
Diff command for PR review: `git diff origin/<baseRefName>...HEAD` (or `gh pr diff <number>` — equivalent).
**Verify:** `git branch --show-current` — must NOT be `main` or `master`. Agents that read source files from the working tree will see stale code if this step is skipped.

**After review completes:** restore the previous branch with `git checkout - 2>/dev/null || git checkout main`.

## Step 1.5: Detect and Fetch Ticket Context

Every review needs to know what business problem the change is trying to solve. Without this, the review is purely code-quality — useful, but missing the most important question: did this deliver what the business asked for?

**Resolve the tracker:** read `.claude/project-config.yaml` →
`tracking.primary.system`. Recognized values: `github-issues` or `linear`. If
the file is absent or the key is missing, default to `linear` (legacy
behavior). In `github-issues` mode, **never call the Linear MCP** — a bare
integer (e.g. `403`) is a GitHub issue number, and the Linear OAuth prompt
cannot be answered in a headless run (it stalls until the session is reaped).

**Detect the ticket ID** from these sources, in priority order:

1. **Explicit `$ARGUMENTS`** — a ticket reference passed in args. For `linear`, a `PROJ-1234`-style `[A-Z]+-\d+` ID; for `github-issues`, a bare integer issue number like `403`.
2. **PR body** — for PR reviews, `gh pr view <number> --json body,title,headRefName`. For `linear`, grep the body for a Linear issue link (`linear.app/.*/[A-Z]+-\d+`) or bare ticket ID (`\b[A-Z]+-\d+\b`); for `github-issues`, read the `Closes #<n>` / `Fixes #<n>` trailer.
3. **Branch name** — extract a ticket ID from the branch (e.g., `dev/proj-1234-fix-login` → `PROJ-1234`). Common patterns: `<prefix>/<lowercased-id>-<slug>` or `<id>-<slug>`.
4. **PR title** — fall back to grepping the PR title (`\b[A-Z]+-\d+\b` for `linear`; a `#<n>` reference for `github-issues`).

**If a ticket ID is found:** fetch via the active tracker and store the result as `BUSINESS_CONTEXT` for use in Step 4:
- `linear` — Linear MCP `get_issue` (issue body) + `list_comments` (full comment thread).
- `github-issues` — `gh issue view <n> --json title,body,state,url,comments`.

**If no ticket ID can be detected:** log "No ticket detected — review will proceed without business context. Product Manager Reviewer will be skipped." Continue to Step 2. Set `BUSINESS_CONTEXT = null`.

**Caveats:**
- If multiple ticket IDs are found in the PR body (e.g., a PR closing two tickets), use the first one mentioned and note the others in the report footer.
- If the ticket fetch fails (auth, network, ticket not found), log the failure and proceed without ticket context — do not block the review.

## Step 1.6: Sensitive Files Check

Cross-reference the changed files against a sensitive-files registry, if one
exists in the current repo. The registry lists high-blast-radius files
(auth boundaries, billing paths, LLM/agent core, workflow engine, etc.) that
should trigger elevated scrutiny.

**Registry lookup** (first hit wins):
1. `.claude/sensitive-files.yml` at the repo root
2. `.github/sensitive-files.yml` at the repo root

Both have the same schema:

```yaml
sensitive_files:
  - path: "src/services/auth_service.py"
    category: auth
    reason: "JWT blocklist, session revocation, refresh token rotation"
  - path: "src/api/ws_helpers.py"
    category: auth
    reason: "Auth boundary for chat websockets"
```

Match changed files against each entry's `path` using substring/endswith
(repo-relative paths). Build a `SENSITIVE_HITS` block listing each
touched file with its category and reason. **Persist this block for use in
Steps 4 and 6.** If no registry is found, or no matches, skip the block
entirely — do not inject empty sections.

If matches are found, the block injected into every reviewer prompt must
include this elevated-scrutiny header verbatim:

```text
SENSITIVE FILES TOUCHED — APPLY ELEVATED SCRUTINY

These files are high blast-radius. Apply maximum scrutiny for: unintended
scope changes, missing auth checks, new external write paths, error handling
gaps, cross-org data leakage, and regression risk.

Touched sensitive files:
- <path> (<category>) — <reason>
- ...
```

## Step 1.7: Pre-Agent Deterministic Gate

Before spawning agents, run cheap deterministic checks for showstoppers and
obvious findings. These don't need an LLM and catch things agents sometimes
miss in large diffs.

**Conflict markers — short-circuit MUST_FIX:**

```bash
# Use the same diff command from Step 1; example assumes PR diff is in $DIFF
echo "$DIFF" | grep -n '^[+].*<<<<<<<\|^[+].*=======\|^[+].*>>>>>>>'
```

If any matches: report as a single MUST_FIX (`Conflict markers in diff`),
skip the agent spawn entirely, and present the report with the artifact.
There is no value in running reviewers on a diff with unresolved conflicts.

**Debug artifacts — note for report (do not short-circuit):**

```bash
echo "$DIFF" | grep -nE '^[+].*\bprint\(|^[+].*\bbreakpoint\(\)|^[+].*\bpdb\.|^[+].*\bic\(|^[+].*console\.log'
```

Record any matches as deterministic findings (severity: MUST_FIX, source:
"Pre-Agent Gate"). Merge them with the agent findings in Step 5.9. This
ownership belongs to the gate, not the SysAdmin Reviewer — SysAdmin's prompt
should reference this gate rather than re-running the grep.

## Step 2: Gather Diff and Changed Files

1. Run the appropriate diff command from Step 1 to get the full diff.
2. Run `git diff --name-only` (same args) to get the list of changed files.
3. **Load project review extras** (`.claude/review-extras.md` at the project root): free-form prose rubrics the project owner wants every reviewer to apply on top of the global agent specs. Read verbatim, do not summarize. If absent, set `PROJECT_RUBRICS = null`. (Sensitive-files registry handled in Step 1.6 above; do not re-load here.)
4. **Load the per-reviewer policy file** (`.claude/review-policy.md` at the project root, optional). Unlike `review-extras.md` — which is *broadcast*, its whole body forwarded to every reviewer — this file is *targeted*: each section is forwarded only to the reviewer it names. A repo may ship either file, both, or neither.
   - The file is markdown with H2 (`##`) sections whose headings are reviewer agent names from the Step 3 table (e.g. `## Architecture Reviewer`, `## Code Quality Reviewer`, `## SysAdmin Reviewer`).
   - Parse each H2 section's body into a `REPO_POLICY` map keyed by the exact reviewer name. Read each section verbatim — do not summarize. If the file is absent, set `REPO_POLICY = {}`.
   - **Validation (warn, never fail):** if an H2 heading does not match any reviewer name in the Step 3 table, log `review-policy.md: unmatched section "<heading>" — skipped (typo?)` and drop that section. Do not abort the review.
5. Categorize changed files:

| Category | File Patterns |
|----------|--------------|
| python | `*.py` |
| frontend | `*.ts`, `*.tsx`, `*.js`, `*.jsx`, `*.css` |
| tests | `test_*`, `*_test.*`, `tests/`, `__tests__/` |
| infra | `Dockerfile*`, `*.yaml`, `*.yml`, `devspace.yaml`, `.github/`, `k8s/` |
| config | `*.toml`, `*.cfg`, `*.ini`, `*.json` (non-package.json) |

## Step 3: Select Reviewers

Only spawn reviewers relevant to what changed. Each reviewer is a Task agent.

| Reviewer | Agent Type | When to Spawn | Model |
|----------|-----------|---------------|-------|
| **Code Quality** | `Code Quality Reviewer` | Any code changed (python OR frontend) | sonnet |
| **Architecture** | `Architecture Reviewer` | Any code changed (python OR frontend) | sonnet |
| **Test Quality** | `Test Reviewer` | Test files changed OR testable code changed without test changes | sonnet |
| **Performance** | `Performance Reviewer` | Python files changed (especially DB/API/service layer) | sonnet |
| **API Contract** | `API Contract Validator` | Both python AND frontend files changed | sonnet |
| **Deployment** | `Deployment Reviewer` | Infra files changed | haiku |
| **Scope & Quality Gate** | `SysAdmin Reviewer` | Always | sonnet |
| **Data Safety** | `Data Safety Reviewer` | Always when the diff mutates persisted state (any DB write, external-system write, or SENSITIVE_HITS non-empty) — skip only on doc/config/style-only diffs | sonnet |
| **Product Manager** | `Product Manager Reviewer` | Always when `BUSINESS_CONTEXT` is non-null (skip if no ticket detected) — Mode 2 spec compliance | sonnet |

**Minimum:** Always spawn Code Quality + Scope & Quality Gate (+ Data Safety when persisted-state mutation is present, + Product Manager when a ticket was detected).
**Maximum:** All 9 if the diff touches everything and a ticket is attached.

## Step 4: Spawn Review Agents in Parallel

Launch all selected reviewers simultaneously using the Task tool with `run_in_background: true`.

**Every agent prompt MUST include:**

1. The full diff (or relevant portion for large diffs — filter to files matching their concern)
2. The list of changed files
3. The repo's `CLAUDE.md` and `ARCHITECTURE.md` content (if they exist in the project root)
4. **`PROJECT_RUBRICS` block** (from Step 2 — inline verbatim if non-null, omit the section entirely if null). This is project-owner-authored guidance the global agents should layer on top of their own specs:
   ```
   ## Project-Specific Rubrics

   <verbatim contents of .claude/review-extras.md>
   ```
5. **`REPO_POLICY` block** (from Step 2 — inline this reviewer's section if `REPO_POLICY` has an entry keyed by its name, omit the section entirely otherwise). Unlike `PROJECT_RUBRICS`, which every reviewer receives, this block is **targeted**: a reviewer gets only the `.claude/review-policy.md` section that names it. The injected text is repo-owner-authored review rules — treat it as carrying the same severity weight as the global agent spec, using the same MUST_FIX / SHOULD_FIX vocabulary:
   ```
   ## Repo Policy

   <verbatim body of the H2 section for this reviewer from .claude/review-policy.md>
   ```
6. **`SENSITIVE_HITS` block** (from Step 1.6 — inline if non-empty, omit if empty). The orchestrator already matched changed files against the project's `.claude/sensitive-files.yml`; reviewers should treat any matched file with elevated scrutiny. Inject the `SENSITIVE FILES TOUCHED — APPLY ELEVATED SCRUTINY` block verbatim (defined in Step 1.6).
7. **`BUSINESS_CONTEXT` block** (inline, verbatim — required for every reviewer when ticket was detected in Step 1.5):
   ```
   ## Business Context

   Ticket: <ID> — <title>
   Source: <tracker> (<ticket URL> — e.g. linear.app/.../<ID> or the GitHub issue URL)

   ### Description
   <full ticket description>

   ### Comments (chronological)
   <comment 1 author, timestamp, body>
   <comment 2 ...>
   ```
   Other reviewers use this passively (to understand intent and judge scope creep). The Product Manager Reviewer uses it as the spec to evaluate against. If `BUSINESS_CONTEXT` is null (no ticket detected), omit this section and skip the Product Manager Reviewer entirely.
8. **Product Manager Reviewer only:** prepend `Mode: spec compliance` to the prompt (Mode 2 per the agent spec).
9. These strict output rules:

```
OUTPUT RULES — follow these exactly:

1. ONLY report problems that require action. No praise, no summaries, no "looks good" filler.
2. If you find ZERO actionable issues, respond with exactly: NO_ISSUES
3. Tag every finding with a severity:
   - MUST_FIX: Correctness bugs, security issues, breaking changes, architectural violations
   - SHOULD_FIX: Pattern deviations, maintainability concerns, missing tests for risky code
4. EVIDENCE DISCIPLINE (non-negotiable):
   - Findings MUST be grounded in the diff provided. Do not flag code that is unchanged in this diff, even if you think it's wrong elsewhere.
   - Each finding MUST include a verbatim quote from the diff (the exact added/changed lines, no paraphrasing) under an `evidence:` field.
   - The orchestrator validates every quote against the diff after you return — findings whose quote does not appear verbatim in the diff are dropped silently. Hedged or hallucinated findings cost you nothing to omit; they cost the user trust to include.
5. For each finding, include:
   - File path and line number(s) from the diff hunk
   - What the problem is (1-2 sentences)
   - Why it matters (consequence, not "best practice")
   - Suggested fix (specific enough to act on)
   - `evidence:` — verbatim quote of the offending added/changed lines from the diff
6. Group findings by severity, then by file.
7. Be direct. No hedging ("might want to consider..."). State the problem and the fix.
8. ESCALATIONS (optional): If you spot something outside your own remit that another reviewer should look at, append an `ESCALATIONS:` block at the end of your output:
   ```
   ESCALATIONS:
   - to: <Reviewer Name from the table in Step 3>
     reason: <1-2 sentences — what you saw and why their lens is needed>
     evidence: <verbatim quote from the diff, same rules as findings>
   ```
   Only escalate when there's concrete diff evidence that crosses into another reviewer's domain (e.g., a YAML rename that will break Pydantic models, a schema change that breaks frontend types). Do NOT escalate speculatively or to seem thorough — unevidenced escalations are dropped.
```

**Agent prompt template:**

```
You are reviewing a code diff as the [REVIEWER_NAME].
[For Product Manager Reviewer only — prepend: "Mode: spec compliance"]

## Changed Files
[LIST]

## Project Context
[CLAUDE.md content if exists]
[ARCHITECTURE.md content if exists]

## Project-Specific Rubrics
[PROJECT_RUBRICS block from Step 2 — omit this section entirely if .claude/review-extras.md was absent]

## Repo Policy
[REPO_POLICY block for this reviewer from Step 2 — omit this section entirely if .claude/review-policy.md has no section naming this reviewer]

## Sensitive Files Touched
[SENSITIVE_HITS block from Step 1.6 — omit this section entirely if no changed files matched .claude/sensitive-files.yml]

## Business Context
[BUSINESS_CONTEXT block from Step 1.5 — omit this section entirely if no ticket was detected]

## Diff
[DIFF CONTENT — filtered to relevant files for this reviewer]

[OUTPUT RULES from above]

Review the diff now. Focus exclusively on your area of expertise.
```

## Step 5: Collect First-Wave Results

Wait for all first-wave agents to complete. Then:

1. **Filter out NO_ISSUES responses** — don't mention reviewers that found nothing.
2. **Validate evidence quotes against the diff.** For each finding AND each escalation, take the `evidence:` quote and confirm the exact string appears in the diff (`grep -F` against the diff content saved to a temp file). Drop any finding or escalation whose quote does not match — these are hallucinations from a summarized read. If a reviewer returned findings without quotes, drop them all and note the reviewer as "unevidenced" in the report footer.

## Step 5.5: Process Escalations (Second Wave)

Collect every validated `ESCALATIONS:` entry from first-wave output. Then:

1. **Filter to reviewers that did NOT run in the first wave.** If an escalation targets a reviewer already in wave one, drop it — that reviewer already had the diff and chose not to flag it. (Cross-checks like "Architecture should also see this" between two agents that both already ran add nothing.)
2. **Deduplicate by target reviewer.** If three agents all escalate to Performance Reviewer, spawn it once with all three reasons concatenated.
3. **Spawn the second wave in parallel**, same Task tool + `run_in_background: true` pattern as Step 4. Each second-wave prompt gets the standard agent prompt template PLUS an "Escalation context" section listing what triggered the escalation:
   ```
   ## Escalation Context

   You were not initially selected for this review. The following reviewer(s) flagged
   diff content as needing your lens. Focus your review on these areas first, but you
   may surface other findings in your domain as normal.

   - From [Escalating Reviewer]: <reason>
     evidence: <verbatim diff quote>
   ```
4. **No third wave.** Second-wave agents may NOT escalate further. Strip any `ESCALATIONS:` blocks they return. Combined with the wave-1-filter rule above, this caps total agents at the full reviewer set (each reviewer runs at most once across both waves) and prevents cascades.
5. **Validate second-wave findings the same way** (evidence quotes against the diff).

## Step 5.9: Final Consolidation

Combine first-wave and second-wave findings:

1. **Deduplicate** — if multiple reviewers flag the same file:line, keep the most specific finding.
2. **Sort by severity** — MUST_FIX first, then SHOULD_FIX.
3. **Group by file** within each severity level.
4. **Tag escalated findings** — findings from second-wave reviewers should be labeled `[Reviewer Name, escalated by Other Reviewer]` so the user sees the chain.

## Step 6: Present Consolidated Report

Format the final output:

```markdown
## Code Review: [target description]

⚠️ **Sensitive Files Touched:** (omit this block entirely when no sensitive files were touched in Step 1.6)
- `path/to/file.py` (category) — reason
- ...

**Reviewed by:** [list of agents that ran — mark second-wave entries as "(escalated)"]
**Agents with findings:** [list of agents that found issues]
**Repo policy applied:** [comma-separated reviewer names whose `.claude/review-policy.md` section was injected — omit this line entirely if `REPO_POLICY` was empty]

---

### Must Fix

**file/path.py**
- **[Code Quality]** L42: Description of problem. Why it matters. → Suggested fix.
- **[Architecture, escalated by SysAdmin]** L78-85: Description. Consequence. → Fix.

**another/file.ts**
- **[API Contract]** L15: Description. → Fix.

---

### Should Fix

**file/path.py**
- **[Test Quality]** Missing test coverage for new branch at L42. Risk of regression. → Add test for error case.
- **[Performance, escalated by Architecture]** L90: N+1 query in loop. Scales linearly with user count. → Use selectinload.

---

_N reviewers ran in wave 1. K reviewers added in wave 2 via escalation. M found actionable issues._
```

**If NO reviewers found issues:**

```markdown
## Code Review: [target description]

**Reviewed by:** [list of agents that ran]
**Repo policy applied:** [comma-separated reviewer names whose `.claude/review-policy.md` section was injected — omit this line entirely if `REPO_POLICY` was empty]

No actionable issues found.
```

## Important

- Do NOT post this review anywhere (GitHub, etc.) unless explicitly asked.
- Present the draft to the user. They decide what to act on.
- If the user wants to fix issues, switch to implementation mode — don't re-review.
- For large diffs (>2000 lines), consider splitting the diff across agents by file relevance rather than sending the entire diff to every agent.
