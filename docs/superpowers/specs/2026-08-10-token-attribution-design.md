# Token Attribution Assessment — Design (#1810)

Date: 2026-08-10
Issue: #1810 — assess(tokens): systematic assessment of bookkeeping/auditing
token burn — MCP schema tax, stage-doc fixed cost, persisted stage artifacts

## Goal

Produce a measured, written token-attribution breakdown for cw's pipeline
session classes: how much of each session's input token spend is fixed
bookkeeping overhead (MCP tool schemas, CLAUDE.md + stage docs, materialized
stage artifacts) versus actual work, plus a ranked list of reduction levers.

This session is **analysis-first**: one-off measurement scripts over existing
data, no changes to `src/cw`. Extending cw's usage accounting to the Claude
worker path (issue method step 1) becomes a follow-up ticket only if the data
shows persistent measurement is worth it.

## Scope decisions (approved)

- **Scope:** analysis-first; no cw code changes in this session.
- **Report home:** full breakdown at `docs/token-attribution-2026-08.md` via
  PR; summary comment with link posted on #1810.
- **Follow-up tickets:** drafted in the report for operator review; filed only
  after approval (one per lever worth >10% of a session class's input).
- **Methodology:** hybrid — transcript forensics over real sessions, plus
  targeted probe sessions to isolate per-server MCP schema cost.

## Data sources

- Session transcripts: `~/.claude/projects/<slug>/*.jsonl`. Assistant turns
  carry per-turn `usage`: `input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens`. Verified present on recent cw
  worker transcripts (e.g. first turn of a `dev-1805` session: 55,643
  cache-creation tokens = the session's fixed prompt cost).
- Two client lanes available on disk: `-cw-wt-7dc983e2-*` (claude-workspace
  tickets) and `-cw-wt-bdf6a9bf-*` (second client) — satisfies the ≥2 lanes
  sampling requirement.
- Codex reviewer usage: already recorded per #1710
  (`src/cw/review_findings.py`, `src/cw/codex_review/_audit_events.py`);
  cited, not re-measured.
- Fixed-input constituents recoverable from disk/git: CLAUDE.md files, stage
  skill/command docs (`auto-dev*.md`), `.cw/context.json`, `.cw/plan.md`,
  handoff documents.

## Sampling plan

- **Worker class:** 12 completed sessions — 8 from the claude-workspace lane,
  4 from the `bdf6a9bf` lane. Every pipeline stage (intake, plan, impl,
  review, finalize) represented across the sample; a session's stage mix is
  identified by which stage docs appear in its transcript.
- **Orchestrator class:** 2–3 interactive cockpit sessions (project dirs under
  `~/.claude/projects/-home-matthew-workspace-*` showing dev-queue /
  orchestrate / event-tail activity).
- **Reviewer class:** #1710's recorded codex usage, cited for comparison.
- Sessions with unparseable usage are dropped and replaced from the same lane.

## Attribution mechanics (per session)

Categories, per the issue:

1. **MCP tool schemas** (per configured server) — from probe deltas (below).
2. **CLAUDE.md + stage skill/command docs** — tokenize the exact file
   contents the session loaded.
3. **Materialized bookkeeping read in** — `.cw/context.json`, `.cw/plan.md`,
   handoffs, prior-attempt summaries: locate the tool_result payloads in the
   transcript and tokenize them.
4. **Bookkeeping written out** — plan comments, sentinel blocks, PR status
   comments: locate the tool_use bodies and tokenize. Reported twice: once as
   output cost, once as recurring input cost on subsequent turns.
5. **Actual work** — residual: total input minus categories 1–4 and system
   baseline.

Mechanics:

- **Fixed prompt cost** = first assistant turn's `cache_creation_input_tokens`
  + `input_tokens`. Exact, from the transcript.
- **Cache economics** reported per session: cache_read vs cache_creation
  ratio, so fixed cost is stated both as raw tokens and as effective
  (cache-discounted) cost.
- **Token counting** of constituent files/payloads: Anthropic `count_tokens`
  endpoint when available; otherwise chars÷4, calibrated against known
  first-turn totals, with the calibration factor and fallback usage disclosed
  in the report.

## MCP schema probes

One-shot headless runs from a scratch directory, each:
`claude -p "reply with ok" --strict-mcp-config --mcp-config <variant>`.

Variants: (a) no MCP servers (baseline, run twice to confirm stability);
(b) Linear only; (c) GitHub only; (d) each remaining server in the operator's
real worker profile, one at a time; (e) the full real config.

Per-server schema tax = first-turn cache-creation delta versus baseline.
The baseline probe also pins the non-MCP system-prompt cost, sharpening the
residual in attribution category 5. Total probe cost: ~6 trivial sessions.

## Deliverables

- `docs/token-attribution-2026-08.md` — per-class fixed/variable breakdown,
  per-server MCP schema tax, cache economics, ranked levers with estimated %
  savings per session class, drafted follow-up ticket bodies, and the method
  described precisely enough to re-run.
- Summary comment on #1810 linking the doc.
- Analysis scripts stay in the session scratchpad (throwaway); the report's
  method section is the durable artifact. If the "persist usage accounting in
  cw" lever survives, its follow-up ticket carries the parsing logic forward.

## Error handling

- Malformed/truncated transcript lines: skipped and counted; skip count
  reported per session.
- Sessions failing usage parsing: dropped and replaced; noted in the report.
- chars÷4 fallback: flagged wherever used.

## Validation

- Reconciliation invariant: per session, the sum of attributed categories must
  match transcript-reported input totals within ±5%. Sessions outside
  tolerance are flagged in the report, never silently included.
- Probe stability: baseline run twice; if the two baselines diverge
  materially, probes are re-run before deltas are trusted.

## Acceptance mapping (from #1810)

- Written attribution breakdown per category per session class → the docs/
  report.
- MCP schema overhead per server and session class → probe matrix.
- Ranked lever list + follow-up ticket per >10% lever → report section,
  tickets drafted for operator gating.
- "Fixed overhead is acceptable, no action" is an allowed, measured outcome.
