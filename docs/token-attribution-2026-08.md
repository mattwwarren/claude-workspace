# Token Attribution Assessment — 2026-08 (#1810)

Systematic measurement of fixed and variable input-token cost across cw's
Claude session classes (worker, orchestrator; reviewer cited from prior
work, not re-measured), answering #1810's motivating question: is the
"Linear MCP = 23% of context" observation a general problem, and where does
fixed per-session overhead actually go.

**Headline: it is not a general problem for local cw workers.** The local
Claude Code CLI defers MCP tool schemas to short stubs (~11.4 tokens/tool);
the measured full-default-config MCP cost for a real worker is 2,044 tokens
— 2.3% of a worker's fixed cost. The issue's 23% figure is a Work Cloud
(claude.ai) phenomenon where this deferred-loading mechanism does not apply,
and it was never measured here (out of scope, no probe access). The
dominant, actionable fixed cost is the **instruction layer** (CLAUDE.md +
stage docs), averaging 30.9% of a worker's fixed cost and running as high as
54,597 tokens (62% of that session's entire measured fixed cost) for a
session that loaded all four `auto-dev-*.md` stage docs.

## Method

**Measurement mechanism.** Two complementary techniques, run across a fixed
16-session sample (Tasks 1-2) plus a standalone live probe matrix (Task 4):

- **Forensic transcript analysis** — parsing real `~/.claude/projects/*.jsonl`
  session transcripts for (a) per-turn `usage` blocks (`input_tokens` +
  `cache_creation_input_tokens` + `cache_read_input_tokens` + `output_tokens`,
  Task 2) and (b) structural markers identifying bookkeeping artifact
  reads/writes (Task 3).
- **Live probe matrix** — repeated `claude -p --output-format json` /
  `--strict-mcp-config --mcp-config <file>` invocations against synthetic
  MCP server configs, to isolate MCP-schema cost from everything else
  (Task 4).

**Two token units are in play, and every number below is tagged with one:**

- **measured** — read directly from a transcript's `usage` block (a real
  cost the model was actually billed for). `fixed_measured` /
  `first_turn_context` = a session's first assistant turn's
  `input + cache_creation + cache_read`. `first_turn_context` (not the
  narrower `cache_creation + input`, which is what the original brief
  specified) was used because two sampled sessions (1730, 1805) had first
  turns served substantially from a warm prefix cache — `first_turn_fixed`
  (creation-only) under-measured their true fixed cost by ~30K tokens each.
- **~estimated, chars/4** — `est_tokens = len(text) / 4` for file content
  (stage docs, CLAUDE.md, bookkeeping-artifact payloads) that never appears
  in a transcript's `usage` block in isolation. This is the brief's original
  estimator, uncalibrated by design (Task 3); Task 4 attempted to calibrate
  an equivalent constant for MCP tool schemas and found the naive chars/4
  model does not hold there at all (see next point) — no evidence surfaced
  that it's wrong for plain prose/markdown docs, but it was never
  independently calibrated against those either. Treat every chars/4 number
  as a plausible, unverified estimate, not a measurement.

**Method corrections made mid-plan (all controller-authorized, all recorded
in `progress.md`):**

1. **Count-based stage detection** (Task 1) — the brief's substring-presence
   check (`if stage_name in transcript_text`) flagged every one of the five
   `auto-dev-*` stages in every session, because a skills-catalog
   system-reminder block lists all stage names once per transcript
   regardless of what the session actually did. Replaced with an
   occurrence-count threshold (`>=5` mentions = "substantive"), which itself
   has a known edge case (see Limitations).
2. **Clause-scoped bookkeeping extraction** (Task 3) — the brief's marker
   code attributed an entire combined-command `tool_result` payload (e.g.
   `cat .cw/plan.md; cat .cw/deferred-findings.md; gh pr view --json...`) to
   whichever label matched first, or to none. Rewritten to split commands
   into clauses, bound each label's payload to its own clause/JSON-blob
   span, and evaluate every matcher (not first-match-wins). Effect size
   varied hugely by label: `pr_status` shrank 82.5% (18,257 → 3,197 raw
   tokens across the sample) once isolated from sibling `cat` clauses in the
   same Bash call; `handoff` shrank 0% (every hit in-sample was already a
   clean `Read` call or command-bounded `cat`).
3. **chars-vs-bytes unification** (Task 5) — `constituents.json`'s `"bytes"`
   key actually stores `len(text)` (a char count post-UTF-8-decode), not a
   true byte count. The first pass of lane-B (`agentic-dev-squad`) stage-doc
   measurement used real `wc -c` byte counts instead, producing a spurious
   11,940-vs-11,859-token "drift" for `auto-dev-finalize.md` — a file
   confirmed byte-identical (sha256) between the two lanes. Traced to the
   units mismatch and unified onto chars/4 for both lanes; the file's true
   token estimate is 11,859 tokens in both.
4. **MCP probe redesign** (Task 4) — the brief's calibration formula
   (`chars_per_token = len(schema_json) / delta_vs_baseline`) implicitly
   assumed the full JSON tool schema is what gets sent at session start.
   Live `-d mcp` debug logs showed Claude Code has dynamic/deferred MCP tool
   loading active by default: most tools are advertised as a short
   name+one-line-description **stub**, with the full schema loading lazily
   via `ToolSearch` only if the model looks the tool up. A 24-tool server
   (playwright) costs 242 tokens on first turn, not the ~4,800 tokens
   `19,467 chars / 4` would predict. The portable calibration unit became
   `tokens_per_deferred_tool` (11.43, averaged across 4 clean
   single/dual-server measurements: 10.08, 12.28, 12.08, 11.28), applied to
   tool *counts*, not schema *character counts*.

**Reconciliation invariant.** For every session,
`known_fixed = system_baseline + mcp_schemas + instruction_layer` is checked
against `fixed_measured`; a session is flagged if `known_fixed` exceeds
`fixed_measured` by more than 5%. 0/16 sampled sessions flagged.

## Sample

13 workers + 3 orchestrators, selected 2026-08-10/11 from live
`~/.claude/projects/*.jsonl` transcripts (Task 1). Selection was recency-first
with a stage-coverage-widening pass; **not reproducible** on a re-run against
current data (see Limitations).

**Workers — lane `7dc983e2` (repo `claude-workspace`, 9 distinct tickets):**

- ticket 1763 — stages: review, finalize — 149 turns
- ticket 1730 — stages: plan, impl, review, finalize (only session in-sample
  covering all four) — 188 turns
- ticket 1805 — stages: impl, review, finalize — 95 turns
- ticket 1624 — stages: impl, review, finalize — 60 turns
- ticket 1764 — stages: finalize — 39 turns
- ticket 1717 — stages: finalize — 34 turns
- ticket 1784 — stages: impl, review, finalize — 58 turns
- ticket 1788 — stages: impl, review, finalize — 50 turns
- ticket 1776 — stages: intake, plan — 60 turns (added specifically to
  restore intake/plan stage coverage lost when two live/growing sessions had
  to be dropped — see Limitations)

**Workers — lane `bdf6a9bf` (repo `agentic-dev-squad`, 4 distinct tickets,
all finalize-only in-sample):**

- ticket 21 — 47 turns
- ticket 109 — 49 turns
- ticket 28 — 57 turns
- ticket 107 — 36 turns

**Orchestrators (3, verified via genuine `Bash`-tool `cw ...` tool_use
blocks, not text mentions):**

- `agentic-dev-squad` main clone — 476 turns, 75 verified cw invocations
- `claude-workspace` main clone — 587 turns, 64 verified cw invocations
- `claude-workspace` worktree `polymorphic-stargazing-puffin` — 399 turns,
  28 verified cw invocations

## Fixed cost per session class

### Worker (n=13)

`fixed_measured`: mean 88,260.6 / median 89,895 / range [82,882, 95,762]
tokens.

Decomposition (class-average % of `fixed_measured`, these four categories
sum to exactly 100% by construction):

- **system_baseline** — 32,156 tokens, constant across every session
  (measured via the `strict-empty` probe: system prompt + skills catalog +
  hooks + global `~/.claude/CLAUDE.md`). **36.5%** of worker fixed cost.
- **mcp_schemas** — 2,044 tokens, constant (measured via the `default-real`
  probe delta — the config every real dispatched worker actually runs
  under, since `native_daemon.py:237` spawns `claude --bg` with no
  `--strict-mcp-config`/`--mcp-config`/`--safe-mode` flags). **2.3%**.
- **instruction_layer** — repo CLAUDE.md + whichever `auto-dev-*.md` stage
  docs the session's stage list substantively covered (~estimated,
  chars/4). Mean ~27,392, range [14,263 (finalize-only), 54,597 (ticket
  1730, all four stages)]. **30.9%**.
- **residual_work** — `fixed_measured - known_fixed`; everything not
  otherwise categorized (system reminders beyond the strict-empty probe,
  ticket-specific context injected before turn 1, model variance).
  **30.3%**.

Ticket 1730 is the one session where `instruction_layer`'s additive stage-doc
estimate (54,597) comes out **above** `known_fixed`'s comparison point —
`residual_work` is -1,409 (-1.6% relative), i.e. the instruction-layer
estimate alone accounts for essentially the entire measured fixed cost of
that session. Inside the 5% reconciliation tolerance; see Limitations.

### Orchestrator (n=3)

`fixed_measured`: mean 58,424 / median 57,615 / range [53,671, 63,986].

- **system_baseline** — 32,156 constant. **55.3%**.
- **mcp_schemas** — 2,044 constant. **3.5%**.
- **instruction_layer** — repo CLAUDE.md only (orchestrators never load a
  stage doc): 2,404 (`agentic-dev-squad`) or 6,689 (`claude-workspace`) ×2
  sessions. **8.8%**.
- **residual_work** — **32.3%**.

Orchestrators pay a much larger *relative* system_baseline share than
workers (55.3% vs 36.5%) simply because they carry no stage-doc instruction
weight to dilute it against.

### Reviewer (codex; not in this sample — cited per task instruction, not
re-measured)

**Instrumentation status (corrected post-review — see Limitations).** At
#1710's filing, cw recorded zero token/usage fields for codex reviewers
("cw measures nothing about reviewer cost today" — #1710's own framing at
the time). That is no longer current: #1710 merged 2026-08-07 (PR #1721,
commit `9c4ebdb2`), and `ReviewerRunRecord` /
`src/cw/codex_review/_audit_events.py` now carry `input_tokens`,
`cached_input_tokens`, and `reasoning_tokens` per reviewer run (verified
2026-08-11 via `grep -rn "input_tokens\|cached_input_tokens\|reasoning_tokens"
src/cw/review_findings.py src/cw/codex_review/_audit_events.py`). This
assessment's sample (selected 2026-08-10/11) contains **no reviewer-class
sessions at all** — codex reviewer figures below are cited from prior
tickets, not measured from real post-#1710 production data, so the ~3 days
of production reviewer telemetry that now exists since #1710 landed is an
unexamined gap (see Limitations). Two real figures exist from prior work,
both cited here rather than re-derived:

- **#1710**'s plan-stage grounding captured two real JSONL usage snapshots
  from the live `codex exec --json` event stream: a clean run with no tool
  calls — 13,239 input tokens, 9,984 cached; a run with one command
  execution — 26,617 input tokens, 19,968 cached.
- **#1549**'s call-count arithmetic: one codex invocation per selected
  reviewer role per review pass (small tier selects 4 roles, large tier 9).
  With the fix loop enabled, worst case is `6 × review_pass_size + 5` codex
  calls per ticket — **29 for small tier, 59 for large tier** — before the
  loop gives up. Any per-call fixed-cost savings compounds by this
  multiplier.
- **#1711**'s single synthetic probe (self-described as "directional
  evidence, not a benchmark," cache warmth uncontrolled): an ordinary
  isolated codex reviewer invocation cost 47,542 total input tokens (13,824
  cached) vs. 40,605 for a lean-profile invocation (19,200 cached);
  comparing uncached input specifically, 33,718 vs. 21,405 — a 12,313-token
  difference, 36.5% lower.

## Variable bookkeeping cost

Read-in and written-out artifact totals (~estimated, chars/4, clause-scoped
extraction — Task 3) across the full 16-session sample.

**Read-in, by label (sample-wide totals, post clause-scoped extraction vs.
raw un-isolated payload):**

- `context.json` — 24,160 tokens extracted (vs. 38,158 raw; 36.7%
  reduction from clause-scoping)
- `plan.md` — 47,358 tokens extracted (vs. 50,881 raw; 6.9% reduction)
- `handoff` — 10,778 tokens extracted (0% reduction — every in-sample hit
  was already a clean `Read` call or command-bounded `cat`; found only in
  the 3 orchestrator sessions, never in a worker session — headless
  `/auto-dev` workers don't consume handoff docs)
- `pr_status` — 3,197 tokens extracted (vs. 18,257 raw; **82.5%**
  reduction — the largest clause-scoping effect, from isolating
  `gh pr view/checks/list --json` calls out of combined Bash commands that
  also `cat` unrelated files)

**Per-class read totals:** workers sum 73,154 / mean 5,627 (**6.4%** of
worker `fixed_measured` mean); orchestrators sum 12,339 / mean 4,113
(**7.0%** of orchestrator `fixed_measured` mean). Both are variable costs
paid once per session, not part of the fixed-cost partition above.

**Per-class written-out totals:** workers sum 6,744 / mean 519 (**0.6%**
of worker fixed cost); orchestrators sum 34,632 / mean 11,544 (**19.8%**
of orchestrator fixed cost) — dominated by `gh_comment_bodyfile` when a
session runs a harden-ticket-style pre-flight-resolution batch (comment
bodies authored inline via heredoc in the same Bash call rather than
referenced by path — up to ~14.8k tokens in one session) and by
`handoff_write` (orchestrators write handoff docs roughly as often as they
read them).

**`bookkeeping_written_recurring`** — `written_out × max(0, n_turns - 1)`,
a lifetime metric that scales with turn count, not a fixed-cost component
(Task 5's own partition explicitly excludes it, and this report keeps it
excluded per the same reasoning): worker ticket 1763 alone accrues 103,304
tokens of this metric over 149 turns; orchestrator `agentic-dev-squad`
accrues 9,798,300 over 476 turns. Expressing either as "% of fixed cost"
would be nonsensical (multi-thousand-percent), which is exactly why it's
reported only as a `class_summary` sum, never a percentage, and never used
as a lever-ranking denominator below.

## Cache economics

Per-session `cache.read / cache.creation` ratios (attribution.json) range
15.29–69.93 for workers and 11.76–28.96 for orchestrators. Summed across
all 13 worker sessions: ~140.1M cache-read tokens against ~3.76M
cache-creation tokens — an aggregate ratio of **~37.3:1**. In steady state,
the overwhelming majority of a session's fixed-cost tokens are served from
cache, not recreated.

This matters for how the ranked levers below should actually be read. This
report ranks levers against `fixed_measured` (the raw first-turn cost, as
the brief specifies), but a lever's *real-world* value scales with how
often that fixed content must be **recreated** rather than **read from
cache** — a session start, a resumed/retried session (cache-cold), or a
cache-TTL expiry. A static per-session saving of N tokens is worth N tokens
on every cache-miss and close to nothing on every cache-hit turn
thereafter. This is visible directly in the ratio spread: ticket 1730 (188
turns, ratio 60.24) amortizes its fixed cost over far more turns than
ticket 107 (36 turns, ratio 15.29) — a fixed-cost reduction is worth
relatively more, per session, to short/mechanical sessions than to
long-running ones, and worth the most of all to workflows with high
retry/resume frequency (#1471's wasted-stage evidence, cited in #1810's own
motivation, is exactly this failure mode).

## Ranked levers

Ranked against the brief's four candidate levers, plus one additional
finding surfaced by the data but outside that candidate list (flagged as
such, per the Campground Rule).

**1. Stage-doc diet — worker class — CLEARS 10%, drafted ticket below.**
`instruction_layer` averages 30.9% of worker `fixed_measured` (mean
~27,392 of 88,260.6 tokens), the second-largest category after
`system_baseline` and the largest one actually under this codebase's
control. `auto-dev-plan.md` alone is 17,091 est. tokens — 19.4% of average
worker fixed cost by itself, the single most expensive doc in the pipeline.
`auto-dev-finalize.md` (11,859 tokens) is loaded in 12/13 sampled worker
sessions — a near-universal tax, since finalize is the terminal stage of
essentially every ticket. Confidence: **HIGH** (measured category,
consistent pattern across 13/13 sessions, corroborated by direct
`repo_config` file-size measurement in `attribution.json`). Evidence:
`attribution.json` `sessions.*.categories.instruction_layer`,
`repo_config.claude-workspace.stage_docs`.

**2. Compact reprs for persisted artifacts (#839-style) — orchestrator
class — CLEARS 10%, drafted ticket below; worker class — sub-threshold, no
action.** Orchestrator `bookkeeping_written_out` mean 11,544 = **19.8%** of
orchestrator `fixed_measured` mean, dominated by heredoc-inlined
`gh_comment_bodyfile` posts and `handoff_write`. Confidence: **MEDIUM** (n=3
orchestrators only, high per-session variance: sums 20,628 / 9,745 / 4,259).
For workers, the same artifact-cost pattern (`bookkeeping_read` mean 6.4%,
`bookkeeping_written_out` mean 0.6% of fixed cost) sits well under the 10%
bar — **no action** needed there; #839 already addressed the analogous
worker-side cost (planner-prompt ticket serialization) in 2026-06.
Evidence: `attribution.json` `class_summary.orchestrator.bookkeeping_*`,
Task 3's per-label breakdown.

**3. Per-session-class MCP allowlists (Work-Cloud-scoped) — local cw
workers/orchestrators — DOES NOT CLEAR 10%, no action.** `mcp_schemas` is a
constant 2,044 tokens = **2.3%** of worker fixed cost / **3.5%** of
orchestrator fixed cost, measured via the `default-real` probe delta (the
literal config every real dispatched session runs under —
`native_daemon.py` spawns flagless). Root cause of the smallness: Claude
Code CLI defers MCP tool schemas to ~11.4-token stubs (measured: playwright
242 tokens/24 tools, chrome-devtools 356 tokens/29 tools;
`tokens_per_deferred_tool` calibrated at 11.43 across 4 clean
single/dual-server probes). **This directly contradicts #1810's motivating
observation** (Linear MCP = 23% of context) for the session class measured
here — that 23% figure is a **Work Cloud (claude.ai)** phenomenon, a
different code path than the local CLI probed in this assessment, and was
never measured here (no probe access). Confidence: **HIGH** for local cw
(measured, calibrated, cross-checked via an independent clean-room
fake-MCP-server probe that reproduced the same per-tool cost under
non-colliding names). Verdict: fixed overhead acceptable for local cw
workers/orchestrators, no action. If an MCP-allowlist lever is pursued, it
must be scoped explicitly to Work Cloud sessions and re-measured there —
not filed here, since no Work Cloud numbers exist in this assessment to
substantiate a savings percentage. Evidence: `attribution.json.mcp_detail`,
`probes.json`.

**Per-configured-server breakdown (~estimated: tool count ×
`tokens_per_deferred_tool`, 11.43 — `probes.json` variants with
`kind: "estimated"`; none of these six servers were live-probed directly,
unlike playwright/chrome-devtools above, which are measured).** This is
the direct, named answer to #1810's "Linear MCP = 23% of context" headline
observation, for the local-cw code path this assessment actually measured:

- **Linear** — 37 tools → ~423 tokens ≈ **0.48%** of worker fixed-cost mean
  (88,260.6). The specific server the issue names, at 1/48th the weight the
  Work Cloud observation reported.
- **Notion** — 16 tools → ~183 tokens ≈ **0.21%**
- **Slack** — 13 tools → ~149 tokens ≈ **0.17%**
- **Google Calendar** (connector) — 9 tools → ~103 tokens ≈ **0.12%**
- **Gmail** (connector) — 7 tools → ~80 tokens ≈ **0.09%**
- **sonarqube** — 17 tools → ~194 tokens ≈ **0.22%** — flagged uncertain in
  `probes.json` (tool count sourced from a historical allow-list entry, not
  corroborated by a live `claude mcp list` at probe time)

Sum of all six ≈ 1,132 tokens, well under the 2,044-token `default-real`
delta already reported above — the two live-connected, measured servers
(playwright 242, chrome-devtools 356) plus overlap/rounding account for the
rest. Even Linear alone, the server the issue names by name, does not
approach a double-digit percentage of local worker fixed cost — reinforcing
the Work-Cloud-scoping verdict above rather than changing it.

**4. Lean reviewer profiles (#1711 generalization) — reviewer (codex)
class — CLEARS 10% by #1711's own numbers; already tracked, no new ticket
drafted.** #1711's probe: 47,542 vs. 40,605 total input tokens (14.6%
reduction), 33,718 vs. 21,405 uncached (36.5% reduction) — both above the
10% bar. Confidence: **LOW-MEDIUM** — #1711 self-describes its own number
as "directional evidence, not a benchmark" (single run, cache warmth
uncontrolled). #1549's call-count multiplier (up to 59 codex calls/ticket
on large tier with the fix loop enabled) means any confirmed per-call
saving compounds substantially. **No new ticket is drafted for this lever**
— #1711 is already open and already scopes exactly this work, including
citing #1710 as a prerequisite dependency in its own "Related" section.
#1710 has since merged (PR #1721, 2026-08-07 — see the Reviewer-class
section above), so that prerequisite is now satisfied and a real
before/after benchmark using #1710's landed instrumentation is unblocked.
Filing a duplicate would violate scope discipline; the actionable
recommendation is to continue #1711, citing #1549's multiplier as added
urgency for prioritization.

**5. [Outside the brief's 4 candidates — bonus finding] Skills-catalog
over-budget delivery — worker + orchestrator class — CLEARS 10%, drafted
ticket below.** Task 4's `-d mcp` debug log surfaced a live warning: `Skill
listing over budget: 122 skills, 32039 chars > 8000 budget` — about 4x over
its own configured budget. The gap between the `--safe-mode` true-zero
probe (21,939 tokens) and the `strict-empty` baseline (32,156 tokens) is
10,217 tokens, attributed by Task 4 as "almost entirely" skills-catalog +
hooks cost (not MCP — confirmed separately: `--strict-mcp-config
--mcp-config empty.json` shows zero MCP server connection lines in that
gap). That's **11.6%** of worker `fixed_measured` mean and **17.5%** of
orchestrator `fixed_measured` mean. Confidence: **MEDIUM** — Task 4 did not
decompose skills-vs-hooks further within that 10,217-token gap; it's a
residual/gap measurement, not a direct skills-catalog-only measurement.
Flagged here under the Campground Rule (a configured budget being visibly
violated by 4x reads as close to a bug, not just an optimization
opportunity) rather than one of #1810's four named candidates. Evidence:
Task 4 report ("Step 1: Enumeration" correction), `probes.json`
`safe-empty`/`strict-empty` entries.

## Drafted follow-up tickets

**DRAFTS ONLY — awaiting operator approval. None of the three below are to
be filed as-is.**

---

### Draft A — perf(auto-dev): diet the auto-dev-\*.md stage docs

**Why:** `instruction_layer` averages 30.9% of a worker session's measured
fixed cost (mean ~27,392 of 88,260.6 tokens, range 14,263–54,597 across the
13-session sample — `attribution.json` `class_summary.worker
.category_pct_of_fixed_measured_avg.instruction_layer`). `auto-dev-plan.md`
is 17,091 ~est. tokens by itself (`constituents.json` files map), the
single largest stage doc and 19.4% of average worker fixed cost alone.
`auto-dev-finalize.md` (11,859 ~est. tokens) loads in 12 of 13 sampled
worker sessions — a near-universal tax on every ticket regardless of scope.
Overlaps #1490 work item 4 (prompt scaffolding audit for Opus 5
staleness) — this ticket extends that item's already-scoped concern with a
concrete cost baseline.

**Method:** Audit `auto-dev-plan.md`, `auto-dev-impl.md`,
`auto-dev-review.md`, `auto-dev-finalize.md`, `auto-dev-intake.md` for
scaffolding vs. load-bearing content (per #1490's own framing). For
`auto-dev-plan.md` specifically, evaluate splitting into a core body +
an on-demand appendix section a worker only needs for edge cases. Dedupe
any content that repeats across CLAUDE.md and the stage docs. Re-run this
assessment's Task 3/4/5 pipeline against a post-diet sample to measure the
actual `instruction_layer` reduction.

**Acceptance:** Measured token reduction per doc (before/after chars/4 or,
preferably, a real transcript re-measurement). No regression in plan-review
pass rate or MUST_FIX rate attributable to lost instructional content —
gate the diet behind a review of what's actually being cut, not a blind
truncation.

---

### Draft B — perf(orchestrator): compact reprs for handoff docs and gh_comment_bodyfile posts

**Why:** Orchestrator `bookkeeping_written_out` averages 19.8% of an
orchestrator session's fixed cost (mean 11,544 of 58,424 —
`attribution.json` `class_summary.orchestrator.bookkeeping_written_out_sum`
÷ n), dominated by `gh_comment_bodyfile` (heredoc-inlined comment bodies —
harden-ticket pre-flight-resolution batches posted up to ~14.8k tokens in
one sampled session) and `handoff_write`. This is the orchestrator-side
analog of #839's already-shipped worker-side compact-repr fix (planner
ticket serialization, ~300–500 → ~50–80 tokens/ticket).

**Method:** For `gh_comment_bodyfile`: prefer writing the comment body to
disk first (as the cheap variant already does for `.cw/plan.md` — a 13-token
file reference, since `gh` reads the body from disk) over heredoc-inlining
the full text into the same Bash call that also has to hold it in context.
For `handoff_write`: apply a compact-repr pass to handoff doc templates,
distinguishing sections a resuming session actually needs from narrative
padding, in the spirit of #839.

**Acceptance:** Measured reduction in `bookkeeping_written_out` sum across
a resampled set of orchestrator sessions running the harden-ticket /
handoff flows. No loss of information a resuming session or a tracker
reader actually depends on.

---

### Draft C — investigate skills-catalog delivery exceeding its own configured budget

**Why:** A live `-d mcp` debug run recorded `WARN Skill listing over
budget: 122 skills, 32039 chars > 8000 budget` — ~4x over its own
configured limit (Task 4 report, "Step 1: Enumeration" correction). The
gap this contributes to (`--safe-mode` true-zero 21,939 tokens vs.
`strict-empty` baseline 32,156 tokens = 10,217 tokens, confirmed not
MCP-related) is 11.6% of worker fixed cost and 17.5% of orchestrator fixed
cost.

**Method:** Decompose the 10,217-token gap into its skills-catalog vs.
hooks components (not done in this assessment — flagged as MEDIUM
confidence for exactly this reason). If the skills catalog is genuinely
exceeding its own budget, either fix the budget-enforcement path or
explicitly revise the budget with a stated rationale, and re-measure.

**Acceptance:** Skills-catalog delivery lands within its own configured
8,000-char budget, or the budget is explicitly revised with rationale.
Measured `system_baseline` reduction re-verified via the `strict-empty`
probe.

---

## Reconciliation & limitations

- **Reviewer-class instrumentation gap (post-review addition).** #1710
  (per-role codex reviewer token/usage metrics) merged 2026-08-07, three-ish
  days before this assessment's 2026-08-10/11 sample selection. This
  assessment's sample contains **no reviewer-class sessions** — the
  Reviewer-class figures cited above (#1710, #1549, #1711) are pulled from
  those tickets' own text, not measured from real production data. That
  means the ~3 days of real post-#1710 reviewer telemetry that now exists
  in production (`ReviewerRunRecord.input_tokens` /
  `cached_input_tokens` / `reasoning_tokens`, populated by
  `src/cw/codex_review/_audit_events.py`) was never pulled into this
  assessment — an unexamined-data gap, not a measurement. A follow-up pass
  extending Tasks 1-5's pipeline to the reviewer class using this now-real
  instrumentation would directly ground #1711's "directional evidence, not
  a benchmark" caveat in real numbers instead of a single synthetic probe.
- **chars/4 estimator.** Uncalibrated by design for non-MCP content (Task
  3). The one calibration attempt made in this assessment (Task 4, for MCP
  tool schemas) found the naive chars/4 model breaks entirely against the
  deferred-stub reality — a 24-tool server's schema is 19,467 chars but
  costs only 242 tokens on first turn, not ~4,867. `tokens_per_deferred_tool`
  (11.43) was used for MCP instead; plain chars/4 (`CHARS_PER_TOKEN=4.0`)
  remains the estimator for stage docs, CLAUDE.md, and bookkeeping-artifact
  payloads, with no independent confirmation it holds accurately there
  either — treat every ~estimated figure in this report as directional.
- **`"bytes"` field misnaming.** `constituents.json`'s files-map `"bytes"`
  key actually stores `len(text)` (chars, post-UTF-8-decode), not a true
  byte count. This caused a spurious lane-A/lane-B "drift" finding during
  Task 5 (11,940 vs. 11,859 tokens for `auto-dev-finalize.md`, a file
  confirmed byte-identical by sha256 between lanes) before being traced to
  the units mismatch and fixed. Any future consumer of `constituents.json`
  should treat `"bytes"` as chars, or the field should be renamed.
- **Stage-detection edge case.** The `>=5`-occurrence "substantive stage"
  threshold (Task 1's fix for the original substring-matches-everything
  bug) can still cross on topical mentions rather than genuine stage
  execution — e.g. ticket 1805's session `d336cea1` recorded an
  `auto-dev-review` count of 5 partly from prose discussing the review doc,
  not exclusively from invocation. Downstream consumers of any session's
  `"stages"` list should treat it as "substantively present," not
  "cleanly, exclusively executed."
- **Sample not reproducible.** Selected 2026-08-10/11 from live, growing
  `~/.claude/projects/*.jsonl` transcripts by a recency-first,
  stage-coverage-widening algorithm. Re-running the selection script today
  would very likely produce a different sample — sessions in this sample
  have since completed further, and new sessions have accrued. The sample
  is 13 workers (9 distinct `claude-workspace` tickets + 4 distinct
  `agentic-dev-squad` tickets) + 3 orchestrators, verified via genuine
  `Bash`-tool `cw ...` tool_use blocks (not text-mention substring matching,
  which Task 1 round 4 found had credited orchestrator status to a session
  with zero real cw invocations — its only "match" was prose mentioning `cw
  dev-queue`).
- **Reconciliation: 0/16 sessions flagged**, `known_fixed` within 5% of
  `fixed_measured` for every session. Ticket 1730 is the one session where
  `residual_work` goes slightly negative (-1,409, -1.6% relative) —
  `known_fixed` (88,797, driven by its 54,597-token instruction-layer
  estimate from loading all four `auto-dev-*.md` stage docs) comes in
  marginally above `fixed_measured` (87,388). This is the one worker
  session where the additive stage-doc estimate approaches the measured
  fixed-cost ceiling; it sits comfortably inside the 5% tolerance gate and
  is not treated as a data bug.
- **Resumed-session caveat.** `fixed_measured` uses `first_turn_context`
  (input + cache_creation + cache_read), not the narrower `first_turn_fixed`
  (creation-only), specifically because two sampled sessions (1730, 1805)
  showed ~30K-token gaps between the two — their first turns were served
  substantially from a warm prefix cache (a resumed lane), and
  creation-only would have undercounted their true fixed cost.
- **13 workers, not the round-2/3 count of 12.** Widened from 12 to 13
  solely to restore `auto-dev-intake`/`auto-dev-plan` stage coverage after
  two live/growing sessions (tickets 1815, 1823) had to be dropped for
  being incomplete at selection time — see `progress.md` / Task 1 report
  Round 4 for the full trace. 3 orchestrators, unchanged.

## Method reproducibility

Nothing from this assessment is committed except this report. The pipeline,
if re-run, is five scripts executed in order, all in the scratchpad
directory (`/tmp/claude-1000/.../scratchpad/1810/`, not the repo):

1. `select_sample.py` → `sample.json` — enumerates
   `~/.claude/projects/*.jsonl`, filters to completed (>60min stale)
   sessions, counts stage-name occurrences (>=5 = substantive), and selects
   a ticket-diverse worker set + a genuine-cw-invocation-verified
   orchestrator set.
2. `extract_usage.py` → `usage.json` — parses every sampled transcript's
   per-turn `usage` blocks; computes `first_turn_fixed` and
   `first_turn_context`.
3. `constituents.py` → `constituents.json` — measures fixed-input file
   sizes (CLAUDE.md, stage docs) and applies the 8 structural bookkeeping
   markers (4 read, 4 write) to every sampled transcript, with clause-scoped
   payload extraction.
4. `probes/` + `run_probes.sh` + `probe_results.py` → `probes.json` — a
   standalone live probe matrix (`claude -p` invocations against synthetic
   `--strict-mcp-config` MCP configs) independent of the transcript sample,
   producing the MCP calibration constants.
5. `attribute.py` → `attribution.json` — joins all of the above into the
   per-session and per-class attribution this report cites.

Re-running steps 1-3 requires access to the same (or equivalently fresh)
live `~/.claude/projects/` transcripts and will select a different sample
by construction (see Limitations). Re-running step 4 requires a live
`claude` CLI session and will reflect whatever MCP/plugin configuration and
deferred-tool eligibility state exists for the account at re-run time —
Task 4 found the eager-vs-deferred tool split appears usage-frequency
personalized per account, though the per-tool stub cost (~11.4
tokens/deferred tool) is expected to be stable regardless.
