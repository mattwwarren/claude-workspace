---
description: "auto-dev Stage 1: Plan — ambiguity scan, scope classification, plan-quality review, approval gate, post plan to tracker"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 1: Plan

**Orientation:** Before running this stage, read `.cw/context.json` for ticket context (ticket body, title, prior decisions). If the file is absent, prose-delegate to `auto-dev-intake.md` first to materialize it.

**Comments and body are live, not cached.** Regardless of whether `.cw/context.json` already exists, Stage 1 MUST **live-fetch the ticket comments AND the ticket body on every invocation** — including a standalone `/auto-dev-plan <ticket-id> --headless` plan-round re-dispatch — via the active tracker's fetch op (`get_issue` + `list_comments(<id>)` for `linear`; `gh issue view <n> --json body,comments` for `github-issues`). The cached `comments` array in `.cw/context.json` and the cached `body` field are both a **Stage-0 provenance snapshot only** and MUST NOT be treated as current: on a plan-round re-dispatch the intake stage does not re-run, so either can be stale — e.g. missing the operator "Pre-flight Resolutions" comment posted after Stage 0 materialized the file (#952), or missing a later edit that folds those same resolutions into the issue body instead (the body-fold variant of the same staleness bug, #980). Use the live fetch — of both comments and body — as the source of truth for every comment-derived or body-derived decision below.

This stage runs as a standalone headless entrypoint (`/auto-dev-plan <ticket-id> --headless`) or as part of the interactive monolith chain. In the chained path, the monolith controls sentinel emission — do NOT emit the `AUTO_DEV_RESULT` sentinel in the chained/interactive context; emit it only under `--headless` standalone invocation.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** All subagent spawns in this file use explicit `model:` pins. Do not change any pin to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Stage 1: Plan

For each ticket in the queue:

### Step 1a: Check for Existing Plan

0. **Resume check (runs before the tracker scan):** if `.cw/plan-draft.md` exists in the worktree AND `.cw/plan.md` does NOT exist, treat it exactly like "Plan found (sufficient)" below — extract its content, skip Step 1b's generation entirely, log "Found persisted plan draft from prior blocked attempt — resuming, not regenerating," append a `friction_highlights` entry noting the resume (e.g. `resumed plan from .cw/plan-draft.md — prior blocked attempt`, since `plan_source` stays the existing `"generated"` literal and carries no signal of its own that this was a resume rather than a fresh generation), and proceed to Step 1c (the ambiguity scan still runs unconditionally). **Supersession/ordering guard:** if `.cw/plan.md` already exists, ignore `.cw/plan-draft.md` regardless of either file's timestamp — an approved `.cw/plan.md` always wins over a stale draft.

1. **Tracked tickets:** Read the issue description AND comments via the active
   tracker's fetch ops (`get_issue` + `list_comments` for `linear`; a single
   `gh issue view <n> --json title,body,comments` for `github-issues`). Look for content that constitutes an implementation plan — specifically:
   - File paths with described changes
   - Phased approach (tests + implementation)
   - Estimated scope (files/lines)
   - Sections titled "Plan", "Implementation Plan", "Approach", or similar
   - A clear, actionable description that specifies what to change and where (even without formal plan structure)

2. **Free text input:** No existing plan — proceed to Step 1b.

3. **Decision:**
   - **Plan found (sufficient) + a later non-pipeline comment present**
     (a live-fetched comment newer than the plan content, excluding this
     file's own `## Multi-Marker Gate Blocked` and `## Pending Verification
     Scan` fixed-header comments): Extract the existing plan — do **not**
     proceed to Step 1b. AUTO-SKIP plan approval entirely, same as the "Plan
     found (sufficient)" branch below. Keep `plan_source:
     github_issue_existing` when the Stage 1 Completion sentinel is emitted
     — this is still an extraction, never a regeneration, regardless of what
     the later comment says. Proceed to Step 1c: the live-fetched comments
     already include the later comment, so the ambiguity/premise scan
     naturally re-evaluates against it — no separate "apply the delta" step
     is needed here. If the later comment supplies an answer to a
     previously-posted ambiguity/premise, fold it into the plan's `##
     Decisions` section at Step 1g exactly as the existing interactive-path
     convention already does — this extends that convention to the headless
     re-entry case rather than inventing a new one. Log: "Found existing
     plan + later comment on ticket — merging, not regenerating; plan
     pre-approved."
   - **Plan found (sufficient) (no later comment):** Extract it. AUTO-SKIP plan approval entirely. Log: "Found existing plan on ticket — plan pre-approved." Proceed to scope classification.
   - **Partial plan found** (e.g., high-level approach but no file paths or phases): Note what exists, proceed to Step 1b with context.
   - **No plan found:** Proceed to Step 1b.

### Step 1b: Generate Plan (Agent)

**Step 1b setup — Pre-flight Resolution pre-extraction (orchestrator, before spawning the Plan agent).** Grep the **Stage-1 live-fetched ticket comments** (the Orientation live fetch above, run via the active tracker's fetch op on this very invocation) for the marker `<!-- auto-dev-preflight-resolutions -->` (the `/harden-ticket` resolution comment), AND ALSO grep the live-fetched issue BODY's resolutions section for the same marker — the operator may fold the same resolutions into the body instead of (or in addition to) a comment, and both channels use the identical marker convention (see `harden-ticket/SKILL.md`). Grep that live fetch — **NEVER the `.cw/context.json` `comments` array or cached `body` field**, both of which are a Stage-0 provenance snapshot and can be stale on a plan-round re-dispatch (exactly the staleness that hid operator resolutions in #952, and the body-fold variant of the same bug class in #980). When tallying, exclude any comment bearing the pipeline's own blocker header `## Multi-Marker Gate Blocked` (see the >1 branch below) from the marker-bearing count — those are self-authored gate diagnostics, never operator resolutions, so a stray marker inside one must never inflate the count and re-trip the gate (defense in depth, #967).

**Body/comment precedence (the #980 body-fold rule).** The marker-bearing comment tally used for the >1-marker gate stays scoped to comments only — body markers are EXCLUDED from that tally, checked separately as at most a single authoritative source. The body is the authoritative/preferred channel: if the body's resolutions section carries the marker, the body's copy is authoritative — use the body's list and do not separately inject the comment's copy, even when a marker-bearing comment carrying the same or older resolutions is also present. A marker-bearing comment plus the same resolutions folded into the body (the observed #929 pattern) is the expected steady state and must NOT trip the gate; the gate exists to catch genuinely conflicting/duplicate operator submissions, not this sanctioned dual-channel echo. Treat the ticket's `updatedAt` (or equivalent tracker metadata) as at most a coarse re-read trigger: any non-body metadata edit (label, assignee) may cause an unnecessary re-read of the body, and that is an accepted tradeoff — the failure asymmetry favors over-reading (a redundant fetch) over missing a response (a burned round).

Branch on the resulting count of operator marker comments (body markers excluded per above):
- **>1 marker comment** → EXIT `ambiguities_pending_resolution` with the message `multiple resolution comments detected — re-run /harden-ticket to consolidate.` Post that message as a blocker comment titled `## Multi-Marker Gate Blocked` (headless: also include the message in the result payload; no branch is created — same EXIT idiom as the Step 1c `ambiguities_pending_resolution` clause). The blocker comment MUST NOT contain the literal pre-flight resolutions marker string anywhere in its body — describe it in prose (e.g. "the pre-flight resolutions marker"), never paste the HTML comment marker, because the gate counts marker-bearing comments and a verbatim marker in this very diagnostic would become an additional marker and re-trip the gate on the next run even after the operator consolidates (#967). The `## Multi-Marker Gate Blocked` header is what the tally above excludes as defense in depth. Do NOT build supersession logic here; consolidating multiple resolution comments is `/harden-ticket`'s job.
- **0 marker comments AND no marker-bearing body resolutions section** → proceed normally; no resolutions section is injected into the plan-agent prompt.
- **exactly 1 marker comment, OR 0/1 marker comments with a marker-bearing body resolutions section** → parse the authoritative source's numbered resolution items — the body's copy when the body carries the marker (per the precedence rule above), the comment's copy otherwise — and inject them into the plan-agent prompt as a dedicated `## Binding Pre-flight Resolutions` numbered list (one line per resolution, preserving the `R<n>` numbering). Instruct the plan agent to treat every item as a **binding constraint** on the plan — not advisory context — and to prove conformance via the `## Pre-flight Resolution Conformance` producer rule below.

Spawn a **Plan** agent (`subagent_type: "Plan", model: "sonnet"`) synchronously — the orchestrator must consume the plan result (and the `## Ambiguities` section) in Step 1c before continuing, so `run_in_background: true` is intentionally NOT used here. Background dispatch ends the parent's turn and trips Stop-hook session-completion (see issue #151 in claude-workspace), orphaning the plan agent. **Why Sonnet (not Opus):** the plan is deliberately generated by the weaker model so the Sonnet plan-review stations (Step 1f.2) are calibrated against Sonnet-level plan output — an Opus-authored plan that Sonnet reviewers then rubber-stamp defeats the point of the review. The plan-revision agent (Step 1f.4) is Sonnet for the same reason.

**Prompt must include:**
- Ticket description / user description **and ALL ticket comments in chronological order** (via the active tracker's fetch ops — same as Step 1c: `get_issue` + `list_comments` for `linear`, `gh issue view <n> --json title,body,comments` for `github-issues`)
- Any partial plan context from Step 1a (if applicable)
- Instruction to read CLAUDE.md and ARCHITECTURE.md if present
- Instruction to read actual model/schema definitions — never guess field names
- **Pattern discovery (required before proposing any new abstraction).** Before the plan adds a new endpoint, route, cache table, model, repository method, UI component, settings surface, strategy hook, or service class, the agent MUST grep the repo for sibling patterns that already solve a similar shape. The plan must include a `## Patterns Found` section with one entry per new abstraction proposed, in this format:
  ```
  ## Patterns Found

  - Proposed: <new thing being added, e.g. "POST /v1/widgets/{id}/preview endpoint">
    Searched for: <grep queries actually run, e.g. "grep -rn 'POST.*preview' --include='*.py'", "grep -rn 'cst_<billing>_' --include='*.py'">
    Found: <list of sibling patterns located, with file:line refs — or "no sibling pattern in tree">
    Decision: USE_EXISTING <name+path> | EXTEND_EXISTING <name+path> | NEW_PATTERN
    Justification (if NEW_PATTERN): <why no existing pattern fits — be specific>
  ```
  If the plan adds *no* new abstraction (pure bug fix, parameter tweak, copy edit), write `## Patterns Found\n\nN/A — no new abstraction proposed.` instead. The orchestrator validates this section is present and well-formed; a plan with new abstractions but no `Patterns Found` section is a friction-block (BLOCK level), not a warning.
  Common patterns worth grepping for, regardless of project: cache/reference tables, sibling endpoints with the same verb/resource shape, base-class hooks, shared UI components, settings/feature-flag surfaces, audit-log helpers.
- **Touch-point contract (required before asserting how existing code behaves).** `Patterns Found` covers code the plan *creates*; this covers code the plan *attaches to*. Before the plan calls a function, reads/writes a dataclass field, renders a template, accesses an attribute, or attaches instrumentation to a call site that is NOT code the plan itself creates, the agent MUST read the touch-point and quote the actual contract verbatim. The plan must include a `## Touch-point Contract` section with one entry per touch-point, in this format:
  ```
  ## Touch-point Contract

  - Touch-point: <what is being attached to, e.g. "process_record return value", "RequestContext fields", "render_prompt template body", "record.parsed attribute">
    File:line: <where it lives, e.g. "src/services/record_service.py:142">
    Read: <verbatim quote of signature / dataclass field list / template body excerpt / attribute definition — long enough to prove the claim, not paraphrased>
    Plan asserts: <what the plan claims about it, e.g. "returns was_created: bool", "has field context_path_taken", "uses Python str.format {content} substitution", "exposes .full_text">
    Match: CONFIRMED | NEEDS_CHANGE <how the plan adapts> | NOT_FOUND <plan asserts something absent — STOP and revise the plan>
  ```
  If the plan touches no existing code (pure greenfield — no new function calls, no new field reads, no template rendering, no instrumentation at an existing call site), write `## Touch-point Contract\n\nN/A — no existing touch-points.` instead. The orchestrator validates this section is present and well-formed; a plan that attaches to existing code but omits `Touch-point Contract` is a friction-block (BLOCK level), not a warning.
  Common touch-points worth quoting verbatim, regardless of project: function signatures of anything called, response/dataclass model field lists when claiming a field exists, prompt template bodies when claiming substitution behavior, attribute access chains on ORM models, call graphs into modules being instrumented.
- Instruction to produce a test-first plan:
  - Phase 1: Tests to write/update BEFORE implementation (for larger scope, integration tests run in isolation)
  - Phase 2: Implementation — specific file changes with paths
- List all files that will be modified with estimated line counts
- **Pre-flight Resolution Conformance:** When the prompt contains a `## Binding Pre-flight Resolutions` section, the plan MUST include a `## Pre-flight Resolution Conformance` section placed immediately before `## Ambiguities`, one line per item in the format `- R<n>: <short restatement> — <how the plan honors it> [SATISFIED | NOT APPLICABLE]`. An item with no conformance line is MISSING (a plan-review MUST_FIX). **If no `## Binding Pre-flight Resolutions` was injected into this prompt, omit `## Pre-flight Resolution Conformance` entirely — do not emit an empty or `N/A` section.**
- **Ambiguity pre-flight:** after the plan, append a section titled `## Ambiguities` listing anything in the ticket that you had to interpret without an explicit answer (file naming, behavior on edge cases, scope boundaries, role/auth assumptions, error handling defaults). Format each item exactly as the Product Manager Reviewer agent's Mode 1 output: question, plan's assumption, alternatives, why-it-matters, ticket quote, and a `Recommendation:` sub-bullet. **Recommendation is mandatory on every item — never omit it.** Use the typed form `Recommendation: ADOPT — <why safe to auto-adopt> | PARK — <why a human must decide>`, matching `product-manager-reviewer.md`'s Mode 1 contract. A missing or malformed `Recommendation` line (wrong token, absent sub-bullet, anything other than a leading `ADOPT`/`PARK` token) is treated as PARK downstream — a deliberate fail-closed default, not a bug, and never a shortcut for writing ADOPT. If you made no interpretive choices, write exactly `NO_AMBIGUITIES`. The main session will route these into Step 1c without re-spawning a separate agent.
- **Pre-flight verification:** instruction to read the targeted files / artifacts and check whether the requested change is already in the desired state. If ALL targeted changes are already applied (no plan-agent disagreement, no ambiguity), the agent MUST report this clearly under `**Discoveries**` in the friction report with the exact phrase `pre-flight: already satisfied` (verbatim, lowercase, including the colon) plus a per-artifact rundown showing the desired state was found. **The phrase is matched literally by the orchestrator** — paraphrases like "already done", "already in desired state", or "work complete" will NOT trigger the `no_op` exit and the pipeline will instead implement the (possibly empty) plan redundantly. The agent must reproduce the phrase exactly. If the situation is ambiguous or only partially satisfied, the agent must produce a normal plan covering the gap and NOT use the `pre-flight: already satisfied` phrase.
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

   **Headless only — after plan agent returns, emit `stage.entered` (`s1_plan_generated`):**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_generated\",\"prev_stage\":\"s0_intake\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```

### Step 1c: Ambiguity Verification

After a plan is in hand (whether extracted from Linear or generated by the Plan agent), run an ambiguity scan against the ticket BEFORE proceeding to scope classification. This step runs in every case — including the auto-skip path where the plan was authored in Linear. A pre-approved plan is not the same as a plan free of ambiguity; the human authored it but may not have caught every interpretive gap.

**This step is non-negotiable.** Do NOT do an "inline scan" instead. Specifically, none of the following are valid reasons to skip the agent spawn:

| Rationalization | Why it's wrong |
|---|---|
| "Ticket is highly prescriptive — file paths, exact code, test cases" | Prescriptive tickets are the most dangerous: detail creates false confidence, and the implicit assumptions (edge cases, error handling defaults, what NOT to touch, scope boundaries) usually go unstated precisely because the author thought everything was covered. Ambiguity scan exists to surface those. |
| "User said move without pausing / don't ask questions" | That instruction governs clarifying questions to the user. The PM Reviewer runs **in background** and asks nothing of anyone. If it returns `NO_AMBIGUITIES`, no one is interrupted. The instruction does not authorize skipping background review steps. |
| "I can scan it faster myself" | The cost of the agent is small; the cost of a missed ambiguity is rework or a wrong implementation. Speed-over-quality during this step is exactly the trade-off the orchestrator is structured to prevent. |
| "Ticket is short / scope is small" | Small scope ≠ unambiguous scope. A one-line change can have multiple plausible interpretations. |

If you catch yourself drafting prose that explains *why* the agent isn't needed this time, that IS the signal — spawn it.

1. **Source the ambiguity list.**
   - If the Plan agent ran in Step 1b and emitted a `## Ambiguities` section, use that list directly — do NOT re-spawn an agent. Skip to step 2 below.
   - Otherwise (plan was extracted from Linear in Step 1a, not generated): you **MUST** spawn a **Product Manager Reviewer** agent in **ambiguity scan** mode (`model: "sonnet"`). No inline shortcut is permitted regardless of how prescriptive or small the ticket appears.
     - **Interactive mode:** `subagent_type: "Product Manager Reviewer"`, `run_in_background: true` (parallel — the parent waits for the next user gate anyway).
     - **`--headless` mode:** `subagent_type: "Product Manager Reviewer"`, **synchronous** (omit `run_in_background`). Same orphan-hazard rationale as Step 1b's Plan agent (`750ea77`) and Step 1f.2 — see issues #175 / #176 in claude-workspace.

   **Prompt must include:**
   - Mode declaration: `Mode: ambiguity scan`
   - The ticket: description + ALL comments in chronological order (via the active tracker's fetch ops — `get_issue` + `list_comments` for `linear`, `gh issue view <n> --json title,body,comments` for `github-issues`), or the free-text description if no ticket
   - The plan: full text, file list, phases
   - Instruction to output either `AMBIGUITIES — N items` (with the question format defined in the agent spec) or exactly `NO_AMBIGUITIES`
   - The friction protocol block
   - The Health Check block (verbatim from Step 1b)

2. Parse the agent's output.

   The agent emits up to two blocks — an ambiguities verdict (`NO_AMBIGUITIES` or `AMBIGUITIES — N items`) and, optionally, a `PREMISES TO VERIFY — N items` block. Handle both whenever present.

   **Pinned exit-comment header (both outcomes).** Whichever headless exit fires below — `ambiguities_pending_resolution` or `premises_pending_verification` — post the exit comment under the fixed header `## Pending Verification Scan`, mirroring the `## Multi-Marker Gate Blocked` fixed-header idiom already used by the Step 1b multi-marker gate: one shared, greppable baseline header for this step's two headless exits, with the JSON `status` field (not the header text) distinguishing which of the two fired. Interactive-mode AskUserQuestion prompts are unaffected — the header applies only to the tracker comment posted on a headless EXIT.

   - **`NO_AMBIGUITIES`** and no premises block → proceed to Step 1d (scope classification). Log: "Ambiguity scan: clean."
   - **`AMBIGUITIES — N items`** → present each ambiguity to the user via AskUserQuestion (one question per ambiguity, with the plan's current interpretation as the first / recommended option and the alternatives listed). Collect answers, append them as decision context to the plan.
   - **`PREMISES TO VERIFY — N items`** → for each premise, AskUserQuestion with options: *verify before continuing* / *confirmed true — proceed* / *skip ticket*. A premise is a factual claim, not a preference — on "verify", the human checks it (or you spawn an exploration agent against Datadog / captured payloads / the API stub) and the verified outcome is appended to the plan as decision context before continuing; on "skip", EXIT. Do NOT route premises to the plan-revision loop — revising a plan does not make a false premise true.
   - Proceed to Step 1d only once both blocks are resolved.

3. **Free-text tickets:** if no Linear ticket and no description was supplied, skip the ambiguity scan entirely (nothing to compare the plan against). Log: "Ambiguity scan: skipped (no ticket context)."

4. **Headless mode:**

   **Step 4a — Partition (adopt-assumption fast path, headless only).** Before any exit/continue decision, if an `AMBIGUITIES — N items` block is present, split its items by each item's `Recommendation:` sub-bullet into `adopted` (the Recommendation field's leading token is exactly `ADOPT`, case-insensitive) and `parked` (everything else — explicit `PARK`, a missing `Recommendation:` sub-bullet, or any unparseable/malformed value all default-safe to parked, no exceptions). If no `AMBIGUITIES` block is present, both `adopted` and `parked` are empty — the unchanged `NO_AMBIGUITIES` path. Scoped to this headless branch only: the interactive branch above (Step 1c.2, the `AskUserQuestion` bullet) is untouched and carries no Recommendation/adopted/parked language.

   **Same partition, mirrored for premises.** Before any exit/continue decision, if a `PREMISES TO VERIFY — N items` block is present, split its items by each item's `Verified:` sub-bullet into `self_verified` (the Verified field's leading token is exactly `YES`, case-insensitive, backed by a quoted citation) and `unverified` (everything else — explicit `NO`, a missing `Verified:` sub-bullet, or any unparseable/malformed value all default-safe to unverified, no exceptions). If no `PREMISES TO VERIFY` block is present, both `self_verified` and `unverified` are empty. Scoped to this headless branch only: the interactive branch above (Step 1c.2, the premises `AskUserQuestion` bullet) is untouched — it already has its own self-verification-adjacent proceed path (the existing "confirmed true — proceed" option).

   **Malformed-recommendation tally (additive, no new bucket).** Alongside the `adopted`/`parked` partition above, also tally `malformed_recommendation_count` — the count of items in `parked` whose `Recommendation:` sub-bullet was missing entirely or did not parse to a leading `ADOPT`/`PARK` token (i.e., excluding items that parked via a well-formed, explicit `Recommendation: PARK — ...` line). This is telemetry only: it does not change `adopted`/`parked` membership, does not introduce a third partition bucket, and every item counted here remains in `parked` exactly as it already was. Used in Step 4c's `stage.entered` payload and in the `## Pending Verification Scan` comment note below.

   **Step 4b — Plan-body disposition.**
   - If `adopted` is non-empty, insert a `## Adopted Assumptions` section into the plan body — immediately AFTER `## Pre-flight Resolution Conformance` if that section is present, else immediately before `## Ambiguities` — one entry per adopted item: the question, the plan's chosen interpretation, and the one-line ADOPT rationale.
   - If the plan body contains a producer-emitted `## Ambiguities` section (the Plan-agent self-emission case from Step 1b), rewrite that section's contents in place: literal `NO_AMBIGUITIES` when `parked` is empty, or the renumbered `parked` items only (adopted items fully removed, never left showing) when `parked` is non-empty. The original section must never be left displaying the full unpartitioned list alongside `## Adopted Assumptions`.
   - If the ambiguities list instead came from a standalone Product Manager Reviewer agent spawn (no pre-existing `## Ambiguities` section in the plan body), there is no section to rewrite — `## Adopted Assumptions` is still inserted fresh, and `parked` is carried forward for the comment/sentinel in Step 4c only. Anchor for this fresh insertion follows the same rule as the first bullet above (immediately after `## Pre-flight Resolution Conformance` if present, else immediately before `## Ambiguities` if present); if the plan body has **neither** section (e.g. an extracted-from-Linear plan that never ran the Step 1b Plan-agent producer), insert `## Adopted Assumptions` as the first section immediately after the plan's title/summary — never silently drop it.
   - If `self_verified` is non-empty, insert a `## Self-Verified Premises` section into the plan body — immediately after `## Adopted Assumptions` if that section is present (fresh or rewritten in this same pass), else immediately after `## Pre-flight Resolution Conformance` if present, else immediately before `## Ambiguities` if present, else as the first section immediately after the plan's title/summary — one entry per self-verified premise: the claim, the quoted evidence/citation from its `Verified: YES` sub-bullet, and what the plan depends on it for.
   - For each `self_verified` premise, also append one line to `friction_highlights` in the form `self-verified premise: <claim> — <evidence citation>`, so the operator can audit every claim the worker settled after the fact rather than blocking on it (the mechanism the ticket's proposed fix names — no schema change, `friction_highlights` is an existing `list[str]` field).

   **Step 4c — Exit/continue decision.** Keys on `parked` and `unverified`, not the raw `AMBIGUITIES`/`PREMISES TO VERIFY` block presence — an all-adopt ambiguity scan (`parked` empty) or an all-self-verified premises scan (`unverified` empty) is functionally `NO_AMBIGUITIES`/`no premises pending` even though the raw scans returned items:
   - `parked` empty AND `unverified` empty → AUTO-CONTINUE to Step 1d. (An all-self-verified premises scan, `self_verified` non-empty but `unverified` empty, is functionally `NO_AMBIGUITIES`/`no premises pending` for gating purposes — the plan already carries `## Self-Verified Premises` and the `friction_highlights` citations from Step 4b.)

   **Headless only — on AUTO-CONTINUE path, emit `stage.entered` (`s1_ambiguity_scan_complete`):**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_complete\",\"prev_stage\":\"s1_plan_generated\",\"adopted_count\":<N>,\"malformed_recommendation_count\":<M>,\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```
   `<N>` is a literal placeholder — substitute the computed `len(adopted)` integer directly into the payload string before running the command (0 on the unchanged `NO_AMBIGUITIES` path, non-zero only when the partition above actually adopted items). It is not a shell variable; unlike `$TICKET`/`$CW_SESSION` (pre-established session context), nothing exports an `ADOPTED_COUNT` env var, so leaving a literal `$ADOPTED_COUNT` token in the command would expand to empty and produce invalid JSON. `<M>` is likewise a literal placeholder — substitute the computed `malformed_recommendation_count` integer from Step 4a directly into the payload string before running the command. On this specific AUTO-CONTINUE emission `<M>` will always be `0`, since this event only fires when `parked` is empty and malformed items are always a subset of `parked`; the field exists for payload-shape consistency and forward compatibility, and the non-zero case surfaces instead via the `## Pending Verification Scan` comment note below.
   - `parked` non-empty AND `unverified` empty → EXIT `ambiguities_pending_resolution`. Post only the `parked` items (renumbered) to the Linear ticket as a comment under the `## Pending Verification Scan` header (one numbered question per item, with the plan's current interpretation and the alternatives) and include only the `parked` items in the result payload under `ambiguities` — never the raw N-item list. The branch is NOT created. The cw orchestrator surfaces the questions to the human; once answered (either by updating the ticket description / comments or by re-invoking with explicit overrides), the pipeline can be re-entered.
   - `unverified` non-empty AND `parked` empty → EXIT `premises_pending_verification`. Post ONLY the `unverified` premises (renumbered) to the Linear ticket as a comment under the `## Pending Verification Scan` header (one numbered item per premise, with what the plan depends on it for and how to verify) and include only the `unverified` items in the result payload under `premises` — never `self_verified` items, and never the raw N-item list. The branch is NOT created. Re-enter the pipeline once each premise is verified.
   - `unverified` non-empty AND `parked` non-empty → EXIT `premises_pending_verification` (still the stronger signal — a false premise invalidates the ambiguity resolutions too), posting BOTH lists under the same `## Pending Verification Scan` header, where the premises half of the pair is the `unverified`-only subset and the ambiguities half is the `parked`-only subset — never either raw N-item list.

   **Malformed-recommendation note (#1274).** Whenever `parked` items are posted to the `## Pending Verification Scan` comment (both the ambiguities-only EXIT and the combined premises+ambiguities EXIT above), if `malformed_recommendation_count` computed in Step 4a is non-zero, prepend a one-line note above the numbered list: `Note: <malformed_recommendation_count> of these <parked_count> item(s) parked because the Recommendation field was missing or malformed, not because of a genuine PARK decision.` `<malformed_recommendation_count>` and `<parked_count>` are literal placeholders — substitute the computed `malformed_recommendation_count` and `len(parked)` integers directly into the note before posting; do not leave the literal `<...>` tokens in the operator-facing comment. This is a count-only note — no per-item classification, no new partition bucket; `adopted`/`parked` membership and the numbered list itself are unchanged.

### Step 1d: Scope Classification

From the plan (existing or generated), classify scope:

1. Count planned files and estimated line changes
2. Check if any planned files touch forbidden areas (migrations, auth/security core, CI/CD, shared base classes with 3+ consumers):
   - **Step 1d.2a — path pre-filter (unchanged in spirit):** flag any planned file under `migrations/**`, auth/security-core paths, `.github/workflows/**`, or shared base classes with 3+ consumers as a **candidate** forbidden-area hit.
   - **Step 1d.2b — content gate (new, CI/CD only):** for every candidate hit specifically under `.github/workflows/**`, inspect the actual diff hunk — not just the file path — and only set `forbidden_touched=true` for that file if the hunk touches a YAML **key** that defines pipeline behavior: job `steps:`, `run:`, `uses:`, `permissions:`, `on:`/trigger blocks, or matrix definitions, or `env:` when it's consumed by a step. A hunk confined to a string/comment payload inside a step (e.g. a `body:`/`echo`/markdown text value, a `# comment`, install-command example text) does NOT set `forbidden_touched=true` for that file, even though the path pre-filter matched. Migrations, auth/security-core, and shared-base-class candidates are unaffected by this content gate — it narrows CI/CD only; see the Worked Examples below.
3. Classify:
   - **Small:** ≤10 files AND ≤500 lines AND no forbidden-area touches
   - **Large:** >10 files OR >500 lines OR touches forbidden areas

4. **Constraint enforcement** (if `--scope-limit small` is active):
   - If classified Large: **AskUserQuestion:** "Ticket <id> exceeds scope limit (estimated N files, ~M lines). Skip this ticket, or abort pipeline?"

5. **Constraint enforcement** (if `--forbidden <areas>` is active):
   - If plan touches any forbidden area: **AskUserQuestion:** "Ticket <id> touches forbidden area (<area>). Skip this ticket, or abort pipeline?"

#### Worked Examples

These calibrate Step 1d.2b's judgment call with concrete before/after cases, mirroring `plan-soundness-reviewer.md`'s Tier 1/2 shape catalog for a different lens.

**Example 1 — inert payload, `forbidden_touched: false`.** The real #1091 diff hunk in `.github/workflows/release.yml`, inside a `steps[].with.body:` block (a multi-line markdown string, not `run:`/`uses:`/`permissions:`/triggers):

```diff
-            uv tool install git+https://github.com/${{ github.repository }}.git@${{ github.ref_name }}
+            uv tool install "claude-workspace[mcp] @ git+https://github.com/${{ github.repository }}.git@${{ github.ref_name }}"
```

The path pre-filter (1d.2a) matches (`.github/workflows/release.yml`), but the hunk only edits text inside a `body:` string value — no job step, `run:` command, `uses:` action, trigger, or permission changed. Content gate (1d.2b) does NOT set `forbidden_touched=true`.

**Example 2 — pipeline logic, `forbidden_touched: true`.** A contrasting edit in the same file's `run:` step:

```diff
       - name: Publish
         run:
-          uv publish
+          uv publish --token ${{ secrets.PYPI_TOKEN }}
```

The path pre-filter matches, and the hunk touches a `run:` step's shell command — a pipeline-behavior YAML key. Content gate sets `forbidden_touched=true`.

### Checkpoint 1 (Plan Approval)

**If plan was auto-skipped** (existing plan found): Skip this checkpoint entirely.

**If plan was generated or built on partial:**

Present to user:
- Ticket summary
- Plan source (generated fresh / built on partial)
- File list + estimated scope (files/lines)
- Scope classification (Small / Large)
- Phase 1 test approach
- Phase 2 implementation approach
- Friction report highlights (skip if NONE)

**AskUserQuestion:** "Approve plan, adjust, or skip ticket?"

- **Approve** → proceed to Stage 2
- **Adjust** → re-plan with user's adjustments, re-present
- **Skip** → move to next ticket in queue

**Headless** (clauses are evaluated in order; first match wins): If plan agent reported `pre-flight: already satisfied` → EXIT `no_op` (see Step 1e) — this preempts every other clause below, including scope and forbidden-area rejections, since there is nothing to implement. Otherwise: if plan in Linear or resumed from `.cw/plan-draft.md` → AUTO-SKIP plan-approval question (ambiguity scan in Step 1c still runs and may exit `ambiguities_pending_resolution`). If plan generated + small → AUTO-APPROVE and proceed (Step 1c still gates on ambiguities). If plan generated + large → EXIT `plan_pending_approval` (post plan to Linear, no branch — ambiguity scan is bypassed since the human will see the plan and ambiguities together when reviewing the Linear post). If `--scope-limit small` rejects → EXIT `scope_exceeded`. If `--forbidden` rejects → EXIT `forbidden_area`.

### Step 1e: Pre-flight Already-Satisfied Check

Before running Plan Quality Review (Step 1f) or posting the plan to Linear (Step 1g), check the plan agent's friction report for the exact phrase `pre-flight: already satisfied` under `**Discoveries**`. **Match literally** — case-insensitive substring match against the Discoveries body is acceptable, but do not infer the signal from paraphrases ("work already complete", "nothing to change", etc.). If the agent paraphrased and you suspect the intent, the safe action is to re-spawn the plan agent with a corrected prompt rather than guess. This signal means all targeted changes are already in the desired state and there is nothing to implement.

**If the signal is present:**
- Do NOT create or push a branch.
- Do NOT post a plan to Linear (the ticket is being closed, not implemented).
- In headless mode: EXIT immediately with `status: "no_op"`, `blocker: null`, `next_actions: ["close_issue_as_completed"]`, and `health.recommendation: "EXIT_FOR_HUMAN_REVIEW"` (a human still needs to close the ticket — the skill does not auto-close).
- In interactive mode: surface the agent's per-artifact rundown to the user and **AskUserQuestion:** "Plan agent reports the requested work is already in the desired state. Close ticket as completed, re-plan (in case the agent missed something), or skip?"
- Best-effort clear the draft: if `.cw/plan-draft.md` is present, delete it on the way out — this exit neither writes `.cw/plan.md` nor intends a resume, so a stale draft left behind is worse than none (same rationale as the Step 1a supersession guard). A pre-existing draft from an earlier blocked attempt must not survive a `no_op` exit. Deletion is best-effort and must not fail this exit.

The `no_op` status is distinct from `blocked` + `agent_block`: "already satisfied" is a healthy outcome, not a failure that needs human attention. Routing it through `blocked` produces alerting noise once orchestrators (e.g. cw) act on the structured contract.

**If the signal is absent:** proceed to Step 1f (Plan Quality Review).

### Step 1f: Plan Quality Review

The Plan Quality Review fires after Checkpoint 1 (approval), after the Step 1e pre-flight `no_op` short-circuit, and after any ambiguity resolutions from Step 1c have been merged into the plan body. The goal is to catch a flawed plan before Stage 2 — every MUST_FIX caught here saves a full review cycle downstream.

It is two stations with two distinct lenses, run in parallel:

- **Plan Reviewer** — *is the plan specified well enough to implement?* Catches under-specification: gaps the implementer would have to guess at.
- **Plan Soundness Reviewer** — *is the plan's chosen direction sound?* Catches a well-specified plan that builds the wrong thing — a direction that contradicts a codified `ARCHITECTURE.md` §7/§8 rule, or matches a known high-blast-radius shape.

Together with Step 1c (Product Manager Reviewer Mode 1 — "did the ticket leave gaps?"), these are the plan-time pre-review: requirements, specification, and direction, each its own lens. All three run.

**Step 1f.1 — Signoff marker check (cheap, runs first):**

Each station has its own marker, versioned independently — `ARCHITECTURE.md` §7/§8 churns faster than the Plan Reviewer's 4 checks, so soundness goes stale on its own cadence without forcing a spec re-review:

```
<!-- plan-spec-reviewed: YYYY-MM-DD vN -->
<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->
```

For each marker independently:
- **Present AND version current** → trust, AUTO-SKIP that station.
- **Absent OR version stale** → that station runs in Step 1f.2.

If both markers are present and current → log `Plan signoff valid (spec vN, soundness vN).` and proceed to Step 1g. Current versions: `plan-spec` `v2`, `plan-soundness` `v1`. Bumping a version constant invalidates that station's existing markers and forces re-review under the new criteria — independently per station. (`plan-spec` `v2` adds the `## Touch-point Contract` check to Plan Reviewer's Contract Specificity verification — see `agents/plan-reviewer.md` Check 1.)

**Step 1f.2 — Spawn the stale/missing stations:**

Dispatch shape depends on mode (see issue #175 / #176 in claude-workspace for the orphan hazard this avoids):

- **Interactive mode:** spawn both stations in parallel, both `run_in_background: true`, in a single message. A human is watching and the Stop-hook session-completion path does not auto-transition USER-origin sessions.
- **`--headless` mode:** spawn the stations **serially** (no `run_in_background: true`). Block on each result before dispatching the next, and do NOT end the parent turn between them. Background dispatch in headless trips the cw-side Stop-hook session-completion (the parent's post-wait turn ends with `background_tasks: []` while pipeline work remains), orphaning the run with no sentinel — same failure mode the Step 1b Plan agent was hardened against in `750ea77`. Losing parallelism here is the price of correctness; the two stations combined typically take under 90s.

Stations (whichever Step 1f.1 did not skip):
- **Plan Reviewer** (`subagent_type: "Plan Reviewer", model: "sonnet"`) — 4 checks in `agents/plan-reviewer.md`: Contract Specificity, File Enumeration, Test Helper Inventory, Observability Call Inventory. Verdicts: **NO_ISSUES**, **SHOULD_FIX**, **PRINCIPLE**, **MUST_FIX**.
- **Plan Soundness Reviewer** (`subagent_type: "Plan Soundness Reviewer", model: "sonnet"`) — two tiers in `agents/plan-soundness-reviewer.md`: Tier 1 codified violations of `ARCHITECTURE.md` §7/§8, Tier 2 Risk Radar shapes. Verdicts: **NO_ISSUES**, **RISK**, **MUST_FIX**.

**Each prompt must include:** full plan text (with Step 1c ambiguity resolutions merged in), Linear ticket ID + description, target repo path, that station's current marker version, the friction protocol block, the standard health check block.

**Step 1f.3 — Gating:**

Gate each station independently; the plan proceeds to Step 1g only when **both** markers are present (skipped-as-current or freshly appended). A station's marker is appended only when that station clears per the rules below.

*Plan Reviewer verdicts:*
- **NO_ISSUES / SHOULD_FIX only / PRINCIPLE only** (any scope, any mode) → log findings (do NOT re-argue PRINCIPLE), append the `plan-spec-reviewed` marker.
- **MUST_FIX where every persisting finding's category is exactly `Format-Only`, format-only cycle not yet used (any scope, any mode)** → spawn plan-revision agent (Step 1f.4) in **format-only mode**, scoped to just the format-only findings; does not require or consume the standard revision-cycle budget. Re-review only Plan Reviewer. Skips the Large-scope AskUserQuestion gate deliberately: this branch only fires for a finding whose regression made it unverifiable (a verified format regression never reaches MUST_FIX at all, per the severity floor), the fix is a deterministic schema re-emit capped at 1 attempt, and a failure falls through to the same Large-scope AskUserQuestion gate below rather than skipping it permanently.
- **MUST_FIX, 1st cycle, Small or interactive** → spawn plan-revision agent (Step 1f.4), re-review once.
- **MUST_FIX, 1st cycle, Large + interactive** → AskUserQuestion: revise / surface to human / skip ticket. On "revise" → Step 1f.4.
- **MUST_FIX persists after 1 revision cycle, interactive** → AskUserQuestion: post stale plan to Linear anyway / abandon ticket.
- **MUST_FIX persists after 1 revision cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unreviewable"`. Do NOT post the stale plan to Linear. Before exiting, write the plan's current text — as it stands at the moment of exit, including any already-appended signoff marker — to `.cw/plan-draft.md`, so a subsequent retry can resume from it instead of regenerating from scratch.
- **MUST_FIX persists after the format-only cycle (still all `Format-Only`)** → falls through to the standard "persists after 1 revision cycle" branches above (same AskUserQuestion / `blocked` `plan_unreviewable` EXIT, including the existing `stage.errored` emission) — the format-only path is a one-shot, not a second standard cycle.

*Plan Soundness Reviewer verdicts:*
- **NO_ISSUES** (any scope, any mode) → append the `plan-soundness-reviewed` marker.
- **RISK, interactive** → AskUserQuestion per finding: acknowledge & proceed / treat as MUST_FIX & revise / codify as a §7 principle. On "revise" → Step 1f.4; on acknowledge or codify → append the marker (record acknowledged RISKs and any `codify:` proposals in `friction_highlights`).
- **RISK, headless** → append each finding's `codify:` line to `friction_highlights`, append the marker, continue. RISK is advisory and never blocks a headless run on its own.
- **MUST_FIX, 1st cycle, interactive** → AskUserQuestion: revise approach / accept with explicit override / skip ticket. On "revise" → Step 1f.4; on "override" → append the marker and record the override verbatim in `friction_highlights`; on "skip" → abandon the ticket per the existing skip handling (same as the Plan Reviewer "skip ticket" branch above).
- **MUST_FIX, 1st cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unsound"`. Do NOT post the plan to Linear. Before exiting, write the plan's current text — as it stands at the moment of exit, including any already-appended signoff marker — to `.cw/plan-draft.md`, so a subsequent retry can resume from it instead of regenerating from scratch.
- **MUST_FIX persists after 1 revision cycle, interactive** → AskUserQuestion: accept with explicit override / abandon ticket.
- **MUST_FIX persists after 1 revision cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unsound"`. Before exiting, write the plan's current text — as it stands at the moment of exit, including any already-appended signoff marker — to `.cw/plan-draft.md`, so a subsequent retry can resume from it instead of regenerating from scratch.

**Codify lessons → wiki inbox:** every RISK finding's `codify:` proposal is written to `~/.claude/wiki/local/inbox/` as a lesson file — filename `lesson-soundness-codify-<shape>-<YYYY-MM-DD>.md`, with the standard `source` / `date` / `topic` frontmatter. This is the *durable* sink; `friction_highlights` is per-run and dies with the result payload. The wiki inbox accumulates across runs, `/wiki-lint` dedupes repeat shapes, and a shape that keeps recurring is the signal to promote it into the target repo's `ARCHITECTURE.md` §7. Route to `wiki/local/inbox/` (not the tracked `wiki/inbox/`) — a codify proposal names a specific repo's architecture and is project-scoped. This write happens for every RISK finding, in both interactive and headless runs, independently of how the human dispositioned the RISK.

If revision was performed, a marker reflects the revised plan, not the original.

**Step 1f.4 — Plan revision (when MUST_FIX from either station):**

Re-spawn the **Plan** agent (`model: "sonnet"`) — same agent type as Step 1b, but Sonnet suffices here since this is structured feedback application rather than original design — with the current plan, the verbatim findings from *every* station that returned MUST_FIX (and any RISK the user chose to "treat as MUST_FIX"), and an instruction to revise addressing each one. The revision agent returns a new plan text; route back to Step 1f.2, re-running **only the stations that triggered the revision** (a clean station's marker stays valid). Maximum **1 revision cycle** — if a revised plan still has MUST_FIX, exit per the gating rules above. Don't loop; a second failure needs human judgment.

**Format-only revision (defense-in-depth).** Fires only when Step 1f.3 routes here because every persisting Plan Reviewer MUST_FIX carries the `Format-Only` category (should rarely fire, since a compliant reviewer raises such findings as SHOULD_FIX per plan-reviewer.md's severity floor). Spawns the same Plan agent, scoped to just the format-only findings, with instruction to re-emit the affected section(s) in the required schema and make no other content changes. Tracked on an **independent axis** from, and does **not** decrement, the standard 1-cycle budget above — it may run even when that budget is already spent. This format-only cycle is itself capped at **1 attempt**; if it is exhausted and MUST_FIX (still all `Format-Only`) persists, fall through to the standard "persists after 1 revision cycle" exit branches (Step 1f.3, unchanged).

**Headless only — if the 1 revision cycle is exhausted and MUST_FIX persists, emit `stage.errored` before exiting:**
```bash
cw event record stage.errored \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_reviewed\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"plan_revision_failed\"}" || true
```

**Friction & health check note:**

If either reviewer's friction is **BLOCK** (e.g., couldn't access the repo, plan malformed), treat as `agent_block` per the existing escalation path — do NOT treat agent failure as a clean review.

### Step 1g: Persist Plan + Post to Linear

After plan is approved (or auto-skipped with existing plan) AND Plan Quality Review has passed (Step 1f):

**FIRST, persist the plan file (#943 — this is the stage's primary artifact, not optional):** Write the full reviewed plan text verbatim — including both signoff markers below — to `.cw/plan.md` in the worktree, BEFORE posting to the tracker and BEFORE emitting the sentinel. Stage 2 (`auto-dev-impl.md`) hard-requires this file; a plan that exists only as a tracker comment leaves the ticket unimplementable. Verify the write (`test -s .cw/plan.md`) as part of this step.

**Then, best-effort clear the draft:** if `.cw/plan.md` was written successfully, delete `.cw/plan-draft.md` if present — a stale draft would otherwise resurface on the next Step 1a resume check even though the plan it drafted has since been approved and superseded. Deletion is best-effort: if it fails (permissions, already absent), log and continue — a failed deletion must NOT fail Step 1g.

**THEN** post the same plan as a comment on the Linear issue (skip for free-text tickets — but never skip the `.cw/plan.md` write). This documents the implementation approach on the ticket; the tracker comment is the audit copy, the file is the pipeline artifact.

**Marker requirement:** The plan body posted to Linear MUST include both signoff markers, each on its own line near the top, exactly:

```
<!-- plan-spec-reviewed: YYYY-MM-DD v2 -->
<!-- plan-soundness-reviewed: YYYY-MM-DD v1 -->
```

Use today's date and each station's current marker version. By the time Step 1g runs, both markers are settled — a station that did not clear (and was not overridden) exits the pipeline before this step, so reaching Step 1g means both stations cleared or were overridden per Step 1f.3. These markers are the contract that lets future `/auto-dev` runs against the same ticket skip Step 1f.2 (the expensive reviewer spawns) — only the cheap marker grep needs to fire.

**Scope tier marker requirement:** the persisted `.cw/plan.md` MUST also include a single canonical scope-tier line, near the scope classification, in the exact form:

```
**Scope tier:** <small|large> (<N> files, ~<M> lines, forbidden_touched=<bool>)
```

This is the one reliable, machine-greppable location the three downstream readers (`auto-dev-impl.md:61`, `auto-dev-review.md`'s tier-resolution step, `auto-dev-finalize.md:31` — all already loosely matching this shape via "or similar Stage-1c marker") depend on, rather than relying on free-text presentation happening to be greppable. Exactly one occurrence of this line must exist in the persisted plan — a later stage that rewrites it (see `auto-dev-review.md`'s one-time tier downgrade) replaces it in place rather than appending a second occurrence.

If the plan was loaded from Linear in Step 1a and already contained a current marker, the marker can be preserved as-is (no need to re-stamp). If the plan was revised in Step 1f.4, stamp with today's date.

If Step 1c surfaced ambiguities AND the user resolved them (interactive path), OR a later ticket comment resolved a previously-posted ambiguity/premise per Step 1a's "later non-pipeline comment" merge branch (headless re-entry), include the resolved answers in the Linear comment under a `## Decisions` section. This preserves the trail of what was clarified and when, so the same questions don't get re-asked in a future re-run.

**Headless only — after plan is posted / confirmed, emit `stage.entered` (`s1_plan_reviewed`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_reviewed\",\"prev_stage\":\"s1_ambiguity_scan_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

---

## Stage 1 Completion (headless only)

After all Stage 1 steps complete successfully in headless mode (plan posted to tracker, both plan review markers appended), emit the `AUTO_DEV_RESULT` sentinel:

**Only emit this sentinel when invoked as a standalone `/auto-dev-plan <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<plan_pending_approval | ambiguities_pending_resolution | premises_pending_verification | no_op | blocked>",
  "stage_reached": "stage1_plan",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": null,
  "worktree_path": "<session worktree path>",
  "fork_point_sha": null,
  "commits": [],
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

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output plan`.
