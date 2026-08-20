---
description: "auto-dev Stage 1: Plan — ambiguity scan, scope classification, plan-quality review, approval gate, post plan to tracker"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 1: Plan

**Orientation:** Read `.cw/context.json` for ticket context (body, title, prior decisions). If absent, prose-delegate to `auto-dev-intake.md` first to materialize it.

**Comments and body are live, not cached.** Stage 1 MUST **live-fetch the ticket comments AND the ticket body on every invocation** — including a standalone `/auto-dev-plan <ticket-id> --headless` re-dispatch — via the active tracker's fetch op (`get_issue` + `list_comments(<id>)` for `linear`; `gh issue view <n> --json body,comments` for `github-issues`). The cached `comments` array and the cached `body` field in `.cw/context.json` are a **Stage-0 provenance snapshot only** and MUST NOT be treated as current: intake does not re-run between stages, so either can be stale — an operator "Pre-flight Resolutions" comment posted after Stage 0 (#952), or a later edit folding those resolutions into the body instead (#980). Use the live fetch of both as the source of truth for every comment-derived or body-derived decision below.

This stage runs as a standalone headless entrypoint (`/auto-dev-plan <ticket-id> --headless`) or as part of the interactive monolith chain. In the chained path the monolith owns sentinel emission — do NOT emit `AUTO_DEV_RESULT` there; emit it only under `--headless` standalone invocation.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** All subagent spawns in this file use explicit `model:` pins. Do not change any pin to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Stage 1: Plan

For each ticket in the queue:

### Step 1a: Check for Existing Plan

0. **Resume check (runs before the tracker scan):** if `.cw/plan-draft.md` exists in the worktree AND `.cw/plan.md` does NOT exist, treat it exactly like "Plan found (sufficient)" below — extract its content, skip Step 1b's generation entirely, log "Found persisted plan draft from prior blocked/parked attempt — resuming, not regenerating," append a `friction_highlights` entry noting the resume (e.g. `resumed plan from .cw/plan-draft.md — prior blocked/parked attempt`; `plan_source` stays `"generated"` and carries no resume signal of its own), and proceed to Step 1c (the ambiguity scan still runs unconditionally). **Supersession/ordering guard:** if `.cw/plan.md` already exists, ignore `.cw/plan-draft.md` regardless of either file's timestamp — an approved `.cw/plan.md` always wins over a stale draft. **Checkpoint-origin note (#1778):** a draft may also originate from the Step 1b post-generation checkpoint or the Step 1f.4 post-revision checkpoint, not only from a blocked/parked EXIT. Such a draft is never treated as final or pre-approved: the resume check is presence-based (keyed only on whether `.cw/plan.md` exists) and routes ANY resumed draft through Step 1c's ambiguity scan and Step 1f's review stations unconditionally — so no separate checkpoint-specific guard is needed. **Round counter (#1683):** `.cw/plan-draft.md`'s literal first line carries the scan round counter `<!-- plan-stage-scan-round: N -->`. The resume check reads `plan-stage-scan-round` from that first line (default 0 when the line is absent) and hands the value to Step 1c.0's round-cap read; it never increments the counter itself — only a Step 4c park EXIT does, via the pre-branch cap check.

1. **Tracked tickets:** Read the issue description AND comments via the active tracker's fetch ops (`get_issue` + `list_comments` for `linear`; `gh issue view <n> --json title,body,comments` for `github-issues`). **Pipeline-authored comment exclusion (#1650):** content in a comment bearing a pipeline fixed header (`## Pending Verification Scan`, `## Multi-Marker Gate Blocked`, `## Blocking Review Findings`, `## Operator-Actionable Review Findings`) MUST NOT be treated as an existing plan — a consolidated park's `### Draft plan (unreviewed — context only)` sub-section is pipeline-authored and unreviewed; resume of pipeline drafts goes through `.cw/plan-draft.md` only (the Step 1a.0 check above). Otherwise look for content constituting an implementation plan: file paths with described changes, a phased approach (tests + implementation), estimated scope (files/lines), sections titled "Plan"/"Implementation Plan"/"Approach", or a clear actionable description of what to change and where.

2. **Free text input:** No existing plan — proceed to Step 1b.

3. **Decision:**
   - **Plan found (sufficient) + a later non-pipeline comment present** (a live-fetched comment newer than the plan content, excluding this file's own `## Multi-Marker Gate Blocked` and `## Pending Verification Scan` fixed-header comments): Extract the existing plan — do **not** proceed to Step 1b. AUTO-SKIP plan approval entirely, same as the branch below. Keep `plan_source: github_issue_existing` in the Stage 1 Completion sentinel — this is still an extraction, never a regeneration. Proceed to Step 1c: the live-fetched comments already include the later comment, so the ambiguity/premise scan re-evaluates against it — no separate "apply the delta" step is needed. If the later comment answers a previously-posted ambiguity/premise, fold it into the plan's `## Decisions` section at Step 1g per the existing interactive-path convention. Log: "Found existing plan + later comment on ticket — merging, not regenerating; plan pre-approved."
   - **Plan found (sufficient) (no later comment):** Extract it. AUTO-SKIP plan approval entirely. Log: "Found existing plan on ticket — plan pre-approved." Proceed to scope classification.
   - **Partial plan found** (approach but no file paths or phases): Note what exists, proceed to Step 1b with context.
   - **No plan found:** Proceed to Step 1b.

### Step 1b: Generate Plan (Agent)

**Step 1b setup — Pre-flight Resolution pre-extraction (orchestrator, before spawning the Plan agent).** Grep the **Stage-1 live-fetched ticket comments** for the marker `<!-- auto-dev-preflight-resolutions -->` (the `/harden-ticket` resolution comment), AND ALSO grep the live-fetched issue BODY's resolutions section for the same marker — the operator may fold resolutions into the body instead of, or in addition to, a comment, and both channels use the identical marker (see `harden-ticket/SKILL.md`). Grep that live fetch — **NEVER the `.cw/context.json` `comments` array or cached `body` field** (#952, #980). When tallying, exclude any comment bearing the pipeline's own blocker header `## Multi-Marker Gate Blocked` — those are self-authored gate diagnostics, never operator resolutions, so a stray marker inside one must never inflate the count and re-trip the gate (defense in depth, #967).

**Body/comment precedence (the #980 body-fold rule).** The >1-marker tally stays scoped to comments only — body markers are EXCLUDED from that tally, checked separately as at most a single authoritative source. The body is the preferred channel: if the body's resolutions section carries the marker, the body's copy is authoritative — use the body's list and do not separately inject the comment's copy, even when a marker-bearing comment with the same or older resolutions is also present. That pairing (#929) is the expected steady state and must NOT trip the gate; the >1-marker branch absorbs genuinely conflicting submissions via newest-wins (#1654), not this sanctioned echo. Treat `updatedAt` as at most a coarse re-read trigger — the failure asymmetry favors over-reading (a redundant fetch) over missing a response (a burned round).

Branch on the resulting count of operator marker comments (body markers excluded per above):
- **>1 marker comment** → **newest-wins, no exit (#1654).** Treat the marker-bearing comment with the latest created timestamp as the sole authoritative comment-channel source and ignore every older marker-bearing comment. Log a warning and append a `friction_highlights` entry in the form `multi-marker: <N> resolution comments found — newest (by timestamp) used; consolidate via /harden-ticket when convenient` (`<N>` is a literal placeholder — substitute the tallied count before emitting). Then proceed under the "exactly 1 marker comment" branch below with the newest comment as the marker comment. Do NOT exit, post a blocker comment, or demand a manual consolidation round: `harden-ticket/SKILL.md` already declares the newest superseding comment "the single source of truth" — the former hard EXIT (`ambiguities_pending_resolution`, message `multiple resolution comments detected — re-run /harden-ticket to consolidate.`) contradicted that rule. Timestamp selection only — do NOT build content-merge/supersession logic here; consolidating the text is still `/harden-ticket`'s job. Historical note (#967 defense in depth): tickets from the hard-EXIT era carry `## Multi-Marker Gate Blocked` blocker comments, which the tally excludes; such a comment MUST NOT contain the literal pre-flight resolutions marker string, so it can never inflate the count.
- **0 marker comments AND no marker-bearing body resolutions section** → proceed normally; no resolutions section is injected into the plan-agent prompt.
- **exactly 1 marker comment, OR 0/1 marker comments with a marker-bearing body resolutions section** → parse the authoritative source's numbered resolution items — the body's copy when the body carries the marker, the comment's copy otherwise — and inject them into the plan-agent prompt as a dedicated `## Binding Pre-flight Resolutions` numbered list (one line per resolution, preserving the `R<n>` numbering). Instruct the plan agent to treat every item as a **binding constraint** on the plan — not advisory context — and to prove conformance via the `## Pre-flight Resolution Conformance` producer rule below.

Spawn a **Plan** agent (`subagent_type: "Plan", model: "sonnet"`) synchronously — the orchestrator must consume the plan result (and the `## Ambiguities` section) in Step 1c, so `run_in_background: true` is intentionally NOT used. Background dispatch ends the parent's turn and trips Stop-hook session-completion (#151), orphaning the agent — the orphan hazard every synchronous-spawn rule below refers back to. Sonnet, not Opus: the plan is generated by the weaker model so the Sonnet review stations (Step 1f.2) are calibrated against Sonnet-level output.

**Prompt must include:**
- Ticket description / user description **and ALL ticket comments in chronological order** (via the active tracker's fetch ops)
- Any partial plan context from Step 1a (if applicable)
- Instruction to read CLAUDE.md and ARCHITECTURE.md if present
- Instruction to read actual model/schema definitions — never guess field names
- **Pattern discovery (required before proposing any new abstraction).** Before the plan adds a new endpoint, route, cache table, model, repository method, UI component, settings surface, strategy hook, or service class, the agent MUST grep for sibling patterns solving a similar shape, and emit one `## Patterns Found` entry per new abstraction:
  ```
  ## Patterns Found

  - Proposed: <new thing being added, e.g. "POST /v1/widgets/{id}/preview endpoint">
    Searched for: <grep queries actually run, verbatim>
    Found: <sibling patterns located, with file:line refs — or "no sibling pattern in tree">
    Decision: USE_EXISTING <name+path> | EXTEND_EXISTING <name+path> | NEW_PATTERN
    Justification (if NEW_PATTERN): <why no existing pattern fits — be specific>
  ```
  If the plan adds *no* new abstraction, write `## Patterns Found\n\nN/A — no new abstraction proposed.` instead. A plan with new abstractions but no `Patterns Found` section is a friction-block (BLOCK level), not a warning.
- **Touch-point contract (required before asserting how existing code behaves).** `Patterns Found` covers code the plan *creates*; this covers code it *attaches to*. Before the plan calls a function, reads/writes a field, renders a template, accesses an attribute, or instruments a call site it does not create, the agent MUST read the touch-point and quote the contract verbatim, one `## Touch-point Contract` entry per touch-point:
  ```
  ## Touch-point Contract

  - Touch-point: <what is being attached to, e.g. "process_record return value">
    File:line: <where it lives, e.g. "src/services/record_service.py:142">
    Read: <verbatim quote of signature / field list / template body / attribute definition — long enough to prove the claim, not paraphrased>
    Plan asserts: <what the plan claims about it, e.g. "returns was_created: bool">
    Match: CONFIRMED | NEEDS_CHANGE <how the plan adapts> | NOT_FOUND <plan asserts something absent — STOP and revise the plan>
  ```
  If the plan touches no existing code, write `## Touch-point Contract\n\nN/A — no existing touch-points.` instead. A plan that attaches to existing code but omits `Touch-point Contract` is a friction-block (BLOCK level), not a warning.
- Instruction to produce a test-first plan: Phase 1 = tests to write/update BEFORE implementation (for larger scope, integration tests run in isolation); Phase 2 = implementation, specific file changes with paths.
- **File enumeration under an explicit `## Files Modified` heading (required, #1779).** List every file the implementation will modify or create as **one bullet per file**, in the form `- <repo-relative path> (~<N> lines)` — one path per bullet, path first, nothing else before it. `.claude/scripts/check_plan_scope_conformance.py` parses this section (by heading prefix, paths from bullets or a table) at Step 2.5 gate 2 to measure the delivered diff against the plan; a plan that buries its file list in prose gives that gate no anchor and the run ships unmeasured. A table is tolerated as a fallback; bullets remain required. **This heading is the plan's complete file inventory, not a source-only subset (#1881):** it MUST include every Phase 1 test file the plan adds or modifies — do not rely on the Phase 1 test enumeration alone, the gate parses only this heading — and every mechanical companion a planned source change requires, most commonly a package `__init__.py` re-export needed to expose a new or renamed symbol (see CLAUDE.md's Module Size convention). A file named only in Phase 1/Phase 2 prose and omitted here is invisible to the gate and reads as unplanned scope drift.
- **Pre-flight Resolution Conformance:** When the prompt contains a `## Binding Pre-flight Resolutions` section, the plan MUST include a `## Pre-flight Resolution Conformance` section placed immediately before `## Ambiguities`, one line per item in the format `- R<n>: <short restatement> — <how the plan honors it> [SATISFIED | NOT APPLICABLE]`. An item with no conformance line is MISSING (a plan-review MUST_FIX). **If no `## Binding Pre-flight Resolutions` was injected into this prompt, omit `## Pre-flight Resolution Conformance` entirely — do not emit an empty or `N/A` section.**
- **Ambiguity pre-flight:** after the plan, append a section titled `## Ambiguities` listing anything you had to interpret without an explicit answer (naming, edge-case behavior, scope boundaries, role/auth assumptions, error-handling defaults). Format each item exactly as the Product Manager Reviewer's Mode 1 output: question, plan's assumption, alternatives, why-it-matters, ticket quote, and a `Recommendation:` sub-bullet. **Recommendation is mandatory on every item — never omit it.** Use the typed form `Recommendation: ADOPT — <why safe to auto-adopt> | PARK — <why a human must decide>`, matching `product-manager-reviewer.md`'s Mode 1 contract. A missing or malformed `Recommendation` line is treated as PARK downstream — a deliberate fail-closed default, never a shortcut for writing ADOPT. If you made no interpretive choices, write exactly `NO_AMBIGUITIES`.
- **Pre-flight verification:** read the targeted files / artifacts and check whether the requested change is already in the desired state. If ALL targeted changes are already applied, the agent MUST report this under `**Discoveries**` with the exact phrase `pre-flight: already satisfied` (verbatim, lowercase, including the colon) plus a per-artifact rundown. **The phrase is matched literally** — paraphrases like "already done" will NOT trigger the `no_op` exit and the pipeline will implement the empty plan redundantly. If only partially satisfied, produce a normal plan covering the gap and do NOT use the phrase.
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

   **Headless only — checkpoint the draft before review runs (#1778).** Immediately after the Plan agent returns (before the `stage.entered` telemetry below and before Step 1c's ambiguity scan or Step 1f's review stations run), write the plan's current text to `.cw/plan-draft.md` — best-effort: if the write fails, log and continue; do not treat it as blocking. This draft is unreviewed at capture time: neither Step 1c's ambiguity scan nor Step 1f's review stations have run yet, so it must never be treated as final or pre-approved, and it does not weaken, replace, or race the existing Step 1f.3 blocked-exit draft writes. Step 1a.0's resume check, the supersession guard, Step 1e, and Step 1g best-effort deletions already handle a checkpoint-origin draft like any other. Skip this checkpoint when Step 1b did not run this invocation.

   **Headless only — after plan agent returns, emit `stage.entered` (`s1_plan_generated`):**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_generated\",\"prev_stage\":\"s0_intake\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```

### Step 1c: Ambiguity Verification

After a plan is in hand (extracted or generated), run an ambiguity scan against the ticket BEFORE scope classification. This runs in every case, including the auto-skip path — a pre-approved plan is not a plan free of ambiguity.

**This step is non-negotiable.** Do NOT do an "inline scan" instead. None of these are valid reasons to skip the agent spawn:

| Rationalization | Why it's wrong |
|---|---|
| "Ticket is highly prescriptive — file paths, exact code, test cases" | Detail creates false confidence; implicit assumptions go unstated precisely because the author thought everything was covered. |
| "User said move without pausing / don't ask questions" | That governs clarifying questions to the user. The PM Reviewer runs in background and asks nothing of anyone. |
| "I can scan it faster myself" | The agent is cheap; a missed ambiguity is rework or a wrong implementation. |
| "Ticket is short / scope is small" | Small scope ≠ unambiguous scope. |

If you catch yourself drafting prose that explains *why* the agent isn't needed this time, that IS the signal — spawn it.

**Settlement marker grammar (`plan-stage-settled`, #1683).** A settled plan item is recorded as one HTML-comment line appended to `.cw/plan-draft.md`, immediately after the round-counter line. Settlement markers live only in `.cw/plan-draft.md`: no tracker comment this pipeline writes — `## Pending Verification Scan` included — may carry a settlement marker of any kind. The grammar is closed and exhaustive; these three forms are the ENTIRE grammar, and nothing else is valid:

```
A<n>: ADOPTED
A<n>: ALT-<x>
P<n>: CONFIRMED | REFUTED | DEFERRED
```

Persisted form — one line per settled item:

```
<!-- plan-stage-settled: A1: ADOPTED -->
<!-- plan-stage-settled: A3: ALT-b -->
<!-- plan-stage-settled: P2: CONFIRMED -->
```

A line whose content is anything other than the exact grammar above (trailing text, a label, operator prose) is non-conforming and MUST NOT be written — the transcription step treats an item it cannot express in this grammar as unmappable, never as an approximation. The grammar has no optional position, no label position, and no trailing-text position.

**Round counter (`plan-stage-scan-round`, #1683).** `.cw/plan-draft.md`'s literal first line, in the form `<!-- plan-stage-scan-round: N -->`, cap 2, incremented once per park EXIT — only the three Step 4c EXIT-bullet outcomes ever increment it; AUTO-CONTINUE never does, since nothing is parked.

**Step 1c.0 — Round-cap read + settlement folding (resumed rounds only).** Fires only when Step 1a.0's resume branch fired this dispatch; on a fresh (non-resumed) dispatch skip straight to step 1 below.

   1. Read the round counter from `.cw/plan-draft.md`'s first line (`<!-- plan-stage-scan-round: N -->`, default 0 when absent).
   2. Locate the newest `## Pending Verification Scan` comment in the live-fetched comments. It defines the currently-open numbered items and each item's **plan-authored** content: its question/claim, its "Plan currently assumes"/stated-fact text, and — for ambiguities — its lettered `(a)/(b)/(c)...` alternatives.
   3. Locate the newest ordinary ticket comment posted after that park comment, from the ticket's author/operator. No marker, no required format — plain natural language. If none exists, nothing settles this round; proceed to step 6.
   4. **Transcription (R4).** For each still-open item from step 2, read the comment from step 3 in full and determine whether it answers that specific item, classifying strictly against that item's own enumerated options (`ADOPTED`/`ALT-<x>` for ambiguities, where `<x>` MUST be one of that item's own lettered alternatives from step 2; `CONFIRMED`/`REFUTED`/`DEFERRED` for premises). Anything that does not map cleanly to exactly one option — unaddressed, hedged, off-topic, or outside the closed set — is **unmappable**: default to unmappable on any doubt. An unmappable item is not settled; it stays open and is scanned as ordinary ticket text like any other comment in the spawn below — no special handling, no partial credit. The only permitted output of this step is one closed token from the grammar above, per item. **`DEFERRED` (R7, active registration):** transcribes identically to `CONFIRMED` for re-raise-suppression purposes — the recorded marker value (`DEFERRED` vs `CONFIRMED`) is audit-only and carries no different settlement behavior at this step. Settling a premise `DEFERRED` never itself writes an `In-implementation check:`/`On mismatch:` pair from operator prose; that pair, if one is ever produced, comes only from the agent's own classification on a later scan (see step 5).
   5. For each settled (non-unmappable) item: append the marker line (grammar above) to `.cw/plan-draft.md`; append one entry to `## Settled Plan Items` quoting ONLY the item's own plan-authored question/claim (and, for `ALT-<x>`, the matched alternative's own text) exactly as it appeared in the step-2 park comment — never the operator's reply text; and append one `friction_highlights` line of the form `plan-stage item settled: <item id> → <marker value> — round <N>` (also never quoting operator text; both `<...>` tokens are literal placeholders — substitute the computed values). **`DEFERRED` wiring (R7, active registration):** when the settled item is a premise marked `DEFERRED`, this step ALSO writes a stub entry to `## Deferred Premises` at settlement time — the stub carries only the plan-authored claim text (quoted from the step-2 park comment, never operator prose) plus a placeholder `In-implementation check:`/`On mismatch:` pair marked `PENDING — agent must supply on next scan`. The stub is not itself a runtime check — it exists only to guarantee the claim is mechanically fed forward. The settled-items-by-identity exclusion suppresses only re-raising the *identical parked question*; it never exempts the underlying claim from the Product Manager Reviewer's own classification work. The PM Reviewer prompt directs the agent to re-classify the stubbed claim's `Verified:` status on the *immediately next* scan — enforced by the presence of the `PENDING` stub, not merely invited. On that next scan the agent supplies its own `In-implementation check:`/`On mismatch:` pair (never transcribed from operator prose) and classifies `Verified: DEFER` (replacing the stub's placeholder pair, confirming the halt-check is live) or `Verified: NO` (the stub is removed — the claim did not hold up to scrutiny and reopens as an ordinary unverified premise, subject to Step 4c gating like any other). A stub that survives past its immediately-next scan without being resolved is a defect in this mechanism, not an accepted steady state — the pre-branch stub check below hard-blocks the round rather than letting it pass. **Resolution-evidence candidate (#1896).** When step 4/5's transcription settles ≥1 item in this round's own transcription pass, additionally record that this round settled ≥1 item, together with the step-3 comment's id/URL and the settled item ids — this becomes the round's `resolution_evidence` candidate, attached to the sentinel only if the round still exits paused via one of the three Step 4c EXIT bullets below. Scoped strictly to items settled by step 4's transcription in this round's own pass: a `plan-stage-settled` marker merely found already present from a prior round is not among the "still-open items from step 2" this round's step 4 evaluates, so it can never mint a second `resolution_evidence` candidate on a later scan.
   6. Persist the updated draft via the draft-persistence rule below. The round counter is unchanged by this step — it is bumped only on a park EXIT, by the pre-branch cap check.

   `## Settled Plan Items` anchors by the same Step 4b chain as the other plan-body accumulator sections: immediately after `## Deferred Premises` / `## Self-Verified Premises` if present, else immediately after `## Adopted Assumptions` if present, else immediately before `## Ambiguities` if present, else as the first section after the plan's title/summary. Its entries carry **only plan-authored content** — the item's own question/claim/alternative text quoted from the park comment, plus the closed-vocab marker value — never operator prose, not even an excerpt of the operator's decision sentence.

   **No redaction, anywhere (R3).** The ticket-comment text handed to the Product Manager Reviewer prompt is the complete, unredacted live-fetched stream, always — including every operator settlement reply located in step 3, verbatim. Nothing in Step 1c.0 removes, truncates, or placeholders any span of ticket-comment text. The only addition to the prompt is additive: `## Settled Plan Items`' plan-authored content, passed alongside (never instead of) the full stream. Stated explicitly: a factual claim inside an operator's settlement reply is re-scrutinized by every subsequent scan, forever — this is intended, not a residual gap.

1. **Source the ambiguity list.**
   - If the Plan agent ran in Step 1b and emitted a `## Ambiguities` section, use that list directly — do NOT re-spawn an agent. Skip to step 2 below.
   - Otherwise (plan extracted in Step 1a): you **MUST** spawn a **Product Manager Reviewer** agent in **ambiguity scan** mode (`model: "sonnet"`). No inline shortcut is permitted.
     - **Interactive mode:** `subagent_type: "Product Manager Reviewer"`, `run_in_background: true`.
     - **`--headless` mode:** `subagent_type: "Product Manager Reviewer"`, **synchronous** (omit `run_in_background`) — same orphan hazard as Step 1b (`750ea77`) and Step 1f.2, issues #175 / #176.

   **Prompt must include:**
   - Mode declaration: `Mode: ambiguity scan`
   - The ticket: description + ALL ticket comments in chronological order, or the free-text description if no ticket
   - The plan: full text, file list, phases — including any `## Adopted Assumptions` / `## Self-Verified Premises` / `## Deferred Premises` sections already present from a prior round's Step 4b partition. Do not trim the plan in a way that drops these — the agent cross-checks candidate findings against them per `product-manager-reviewer.md` Mode 1, and a truncated prompt silently reopens the gap this fixes (#1593).
   - **Settled items from prior rounds (do not re-raise):** the full `## Settled Plan Items` text (#1683). This list identifies which items are closed by identity only — it never redacts, exempts, or pre-verifies any ticket-comment text. The agent receives the complete comment stream above and must evaluate every claim in it on its own merits, including inside any operator reply that produced a settlement. If `## Deferred Premises` contains any entry whose check pair is marked `PENDING — agent must supply on next scan`, the agent MUST classify that exact claim's `Verified:` status in this scan's output (`DEFER`, supplying its own `In-implementation check:`/`On mismatch:` pair, or `NO`) — it is a required classification target, not an optional re-discovery.
   - Instruction to output either `AMBIGUITIES — N items` (question format per the agent spec) or exactly `NO_AMBIGUITIES`
   - The friction protocol block
   - The Health Check block (verbatim from Step 1b)

2. Parse the agent's output.

   The agent emits up to two blocks — an ambiguities verdict (`NO_AMBIGUITIES` or `AMBIGUITIES — N items`) and, optionally, a `PREMISES TO VERIFY — N items` block. Handle both whenever present.

   **Pinned exit-comment header (both outcomes).** Whichever headless exit fires below — `ambiguities_pending_resolution` or `premises_pending_verification` — post the exit comment under the fixed header `## Pending Verification Scan`, mirroring the `## Multi-Marker Gate Blocked` fixed-header idiom (retired at #1654; the header remains excluded from scans as historical). One shared, greppable header for this step's two headless exits, with the JSON `status` field distinguishing which fired. Interactive AskUserQuestion prompts are unaffected.

   - **`NO_AMBIGUITIES`** and no premises block → proceed to Step 1d. Log: "Ambiguity scan: clean."
   - **`AMBIGUITIES — N items`** → present each ambiguity to the user via AskUserQuestion (one question per ambiguity, with the plan's current interpretation as the first / recommended option and the alternatives listed). Collect answers, append them as decision context to the plan.
   - **`PREMISES TO VERIFY — N items`** → for each premise, AskUserQuestion with options: *verify before continuing* / *confirmed true — proceed* / *skip ticket*. A premise is a factual claim, not a preference — on "verify" the human checks it (or you spawn an exploration agent) and the outcome is appended as decision context; on "skip", EXIT. Do NOT route premises to the plan-revision loop — revising a plan does not make a false premise true.
   - Proceed to Step 1d only once both blocks are resolved.

3. **Free-text tickets:** if no ticket and no description was supplied, skip the ambiguity scan entirely. Log: "Ambiguity scan: skipped (no ticket context)."

4. **Headless mode:**

   **Draft-persistence rule (every Stage-1 human-gated headless exit with a plan in hand).** Before ANY headless EXIT that leaves Stage 1 while a draft plan exists — generated by Step 1b or extracted in Step 1a this invocation — write the plan's current text to `.cw/plan-draft.md`. This covers the Step 4c `ambiguities_pending_resolution` and `premises_pending_verification` exits (including the combined exit) and Checkpoint 1's headless `plan_pending_approval` exit, in addition to the two Step 1f.3 `plan_unreviewable`/`plan_unsound` writes, which are instances of the same rule. The write captures the plan **as it stands at the moment of exit**, including every section inserted so far (`## Adopted Assumptions`, `## Self-Verified Premises`, `## Deferred Premises`, `## Settled Plan Items`, merged resolutions, any already-appended signoff marker), so the next dispatch's Step 1a.0 resume check picks up where this round left off. The draft's own leading bookkeeping lines are part of the same capture (#1683): the `<!-- plan-stage-scan-round: N -->` round-counter first line and every `<!-- plan-stage-settled: ... -->` marker line beneath it are written and preserved with the plan text, never dropped on a rewrite. Queue re-dispatch reuses the ticket's worktree, so the draft survives; a path that builds a fresh worktree finds nothing and Step 1b regenerates.

   **Consolidated park (single-exit rule, #1650).** When Step 4c below (or Checkpoint 1's headless large-scope clause, or the Pre-branch integrity checks' stub/cap hard-EXITs below, #1683) decides to exit for a human AND a draft plan exists in hand, do NOT exit carrying only that gate's findings — each serial gate costs one operator round (mean park latency 9–13h). Finish ALL remaining plan-phase analysis first:

   1. Run Step 1d scope classification on the draft (if not already run this invocation).
   2. Run the Step 1f stations (Plan Reviewer + Plan Soundness Reviewer, serially per the existing headless dispatch rules, honoring the Step 1f.1 marker skip) in **advisory mode**: findings are collected only — no signoff marker is appended, no revision cycle (Step 1f.4) runs, and a station MUST_FIX must NOT convert the park into `blocked`; the fixes land next round together with the operator's answers. A station that friction-BLOCKs is skipped with a note in the comment — never escalated to `agent_block` from this path.
   3. Post ONE comment under the `## Pending Verification Scan` header containing, in order: the numbered parked ambiguities and/or unverified premises (existing shapes, renumbering, and the malformed-recommendation note, all unchanged); `### Advisory plan-review findings (address in the same round)` with each station finding verbatim (omit this sub-section when both stations returned NO_ISSUES or were marker-skipped); when Step 1d classified the draft Large, `### Approval requested` — an approving reply alongside the answers clears both gates on re-entry; and `### Draft plan (unreviewed — context only)` with the full draft text.
   4. Persist the draft per the draft-persistence rule above.

   The exit **status** is unchanged by consolidation — Step 4c's precedence still picks `premises_pending_verification` over `ambiguities_pending_resolution`, and a park with nothing parked/unverified but Large scope still exits `plan_pending_approval` via Checkpoint 1; only the comment gets richer. Sentinel: append `consolidated park: <a> ambiguities, <p> premises, <f> advisory findings, scope <tier>` to `friction_highlights` (placeholders — substitute computed values; no schema change). Guard: the advisory station run happens only when a draft plan exists in hand — an exit with no plan keeps its existing comment shape. Result-payload rules are untouched: `ambiguities`/`premises` arrays carry the same parked/unverified-only subsets; advisory findings travel in the comment and `friction_highlights` only.

   **Step 4a — Partition (adopt-assumption fast path, headless only).** Before any exit/continue decision, if an `AMBIGUITIES — N items` block is present, split its items by each item's `Recommendation:` sub-bullet into `adopted` (leading token exactly `ADOPT`, case-insensitive) and `parked` (everything else — explicit `PARK`, a missing `Recommendation:` sub-bullet, or any unparseable/malformed value all default-safe to parked, no exceptions). If no `AMBIGUITIES` block is present, both are empty — the unchanged `NO_AMBIGUITIES` path. Scoped to this headless branch only; the interactive branch (Step 1c.2) is untouched.

   **Same partition, mirrored for premises — three ways (#1651).** Before any exit/continue decision, if a `PREMISES TO VERIFY — N items` block is present, split its items by each item's `Verified:` sub-bullet into `self_verified` (leading token exactly `YES`, case-insensitive, backed by a quoted citation), `deferred` (leading token exactly `DEFER`, case-insensitive, AND both required sub-bullets — `In-implementation check:` and `On mismatch:` — present and non-empty), and `unverified` (everything else — explicit `NO`, a missing `Verified:` sub-bullet, a `DEFER` missing either required sub-bullet, or any unparseable/malformed value all default-safe to unverified, no exceptions). `deferred` never parks — a runtime-only premise meeting the DEFER bar is verified at implementation start, not by a human who cannot check it statically either. If no `PREMISES TO VERIFY` block is present, all three buckets are empty. Scoped to this headless branch only.

   **Malformed-recommendation tally (additive, no new bucket).** Alongside the `adopted`/`parked` partition, tally `malformed_recommendation_count` — items in `parked` whose `Recommendation:` sub-bullet was missing or did not parse to a leading `ADOPT`/`PARK` token (excluding items that parked via a well-formed, explicit `Recommendation: PARK — ...` line). Telemetry only: it does not change `adopted`/`parked` membership and does not introduce a third partition bucket. Used in Step 4c's `stage.entered` payload and the `## Pending Verification Scan` comment note below.

   **Step 4b — Plan-body disposition.**
   - If `adopted` is non-empty, insert a `## Adopted Assumptions` section into the plan body — immediately AFTER `## Pre-flight Resolution Conformance` if that section is present, else immediately before `## Ambiguities` — one entry per adopted item: the question, the chosen interpretation, and the one-line ADOPT rationale.
   - If the plan body contains a producer-emitted `## Ambiguities` section (the Step 1b self-emission case), rewrite that section's contents in place: literal `NO_AMBIGUITIES` when `parked` is empty, or the renumbered `parked` items only (adopted items fully removed, never left showing) when `parked` is non-empty. The original section must never be left displaying the full unpartitioned list alongside `## Adopted Assumptions`.
   - If the list came from a standalone Product Manager Reviewer spawn (no pre-existing `## Ambiguities` section), there is no section to rewrite — `## Adopted Assumptions` is still inserted fresh, and `parked` is carried forward for Step 4c only. Anchor the fresh insertion by the same rule; if the plan body has **neither** section, insert `## Adopted Assumptions` as the first section immediately after the plan's title/summary — never silently drop it.
   - If `self_verified` is non-empty, insert a `## Self-Verified Premises` section — immediately after `## Adopted Assumptions` if present, else immediately after `## Pre-flight Resolution Conformance` if present, else immediately before `## Ambiguities` if present, else as the first section after the plan's title/summary — one entry per premise: the claim, the quoted evidence from its `Verified: YES` sub-bullet, and what the plan depends on it for. **Stub-identity match (#1683).** Before inserting the entry, check whether `## Deferred Premises` already contains a stub whose check pair still reads `PENDING — agent must supply on next scan` and whose claim text matches this premise's claim by identity (exact-string claim-text match is sufficient identity — no separate fingerprint field). If a matching stub exists: remove that stub entry from `## Deferred Premises` entirely — a `Verified: YES` reclassification fully confirms the claim, so there is nothing to update in place. If no matching stub exists: insert normally, unchanged from the existing logic.
   - For each `self_verified` premise, append one line to `friction_highlights` in the form `self-verified premise: <claim> — <evidence citation>`, so the operator can audit every settled claim after the fact rather than blocking on it (no schema change — an existing `list[str]`).
   - If `deferred` is non-empty, insert a `## Deferred Premises` section — immediately after `## Self-Verified Premises` if present, else at the same anchor chain — one entry per premise: the claim, the exact `In-implementation check:`, the `On mismatch:` halt condition, and what the plan depends on it for. ALSO insert an explicit leading step into the plan's implementation phase (Phase 2's first step, before any dependent work): run every `## Deferred Premises` check first; on any mismatch, halt via the impl stage's existing `blocked` exit with the premise named in the blocker message — never a silent fallback. A deferred check that cannot even be run (endpoint unreachable, fixture absent) is a mismatch for this purpose, not a pass. **Stub-identity match (#1683).** Before inserting the entry, check whether `## Deferred Premises` already contains a stub whose check pair still reads `PENDING — agent must supply on next scan` and whose claim text matches this premise's claim by identity (exact-string claim-text match is sufficient identity — no separate fingerprint field). If a matching `PENDING` stub exists: update that entry **in place** — overwrite the placeholder `In-implementation check:`/`On mismatch:` pair with the agent-supplied pair from this scan's `Verified: DEFER` classification and remove the `PENDING` marker — never append a second, duplicate entry for the same claim. If no matching stub exists (an ordinary premise deferred fresh this round): insert normally, unchanged from the existing logic.
   - For each `deferred` premise, append one line to `friction_highlights` in the form `deferred premise: <claim> — checked at implementation start; on mismatch: <halt condition>` (no schema change).
   - **`unverified`-bucket stub-identity match (#1683).** If an `unverified` item's claim matches an existing `PENDING — agent must supply on next scan` stub by the same claim-text identity rule — a `Verified: NO` classification of a previously-stubbed claim — remove that stub entry from `## Deferred Premises` entirely. The claim reopens as an ordinary unverified premise and is gated by Step 4c like any other, never left behind as an orphaned `PENDING` stub sitting alongside it.
   - **Completed no-orphan invariant (#1683).** After Step 4b runs, no bucket outcome — `deferred` (stub updated in place), `unverified` (stub removed, claim reopens as an ordinary premise), or `self_verified` (stub removed, claim confirmed) — can leave a matched `PENDING` stub behind.

   **Pre-branch integrity checks (single seam, #1683).** Fires exactly ONCE, immediately after Step 4b's disposition pass (stub-identity match included) completes and before Step 4c's branch selection begins — covering all four Step 4c outcomes, AUTO-CONTINUE included. Both checks evaluate here, in this fixed order — stub check first, then cap check — instead of being duplicated at the three Step 4c EXIT bullets, which carry no inline check text of their own.

   > **Stub check (first).** Scan the plan body's `## Deferred Premises` section for any entry whose check pair still reads `PENDING — agent must supply on next scan`. If none remain, the check passes silently and evaluation proceeds to the cap check below — a fully-resolved round never trips it. If one or more remain: the round does NOT proceed to ANY of Step 4c's four outcomes, AUTO-CONTINUE (`parked` empty AND `unverified` empty) included. Headless mode hard-EXITs **through the consolidated park above** — instead of whichever outcome was about to fire — with `status: "blocked"`, `blocker.reason: "deferred_stub_unresolved"`, `blocker.stage: "stage1_plan"`, `blocker.details` naming the unresolved stub(s) verbatim (claim text plus the round it was settled), and `retry_eligible: true`; the same single-comment bundling the three Step 4c EXIT bullets already use — any parked ambiguities, unverified premises, or advisory Step 1f findings surfaced this same round are folded into the one park comment, never dropped behind a stub-only block. Emit the `stage.errored` block below before the `blocked` sentinel. Interactive mode instead **AskUserQuestion:** "One or more `## Deferred Premises` stubs were not classified on their required next scan (the PM Reviewer's scan missed or mishandled them). Re-run the scan for these claims / accept the current state (unresolved stubs move to a new `## Open Questions` section, flagged as un-enforced) / abandon the ticket?"
   >
   > ```bash
   > cw event record stage.errored \
   >   --correlation-id "$TICKET" \
   >   --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"deferred_stub_unresolved\"}" || true
   > ```
   >
   > **Cap check (second).** Reachable only when the stub check passed AND the impending Step 4c outcome is one of the three EXIT bullets — i.e. `parked` non-empty OR `unverified` non-empty. AUTO-CONTINUE never reaches this check, since nothing is being parked or blocked for the round cap to bound. Read `.cw/plan-draft.md`'s round-counter line (`<!-- plan-stage-scan-round: N -->`, default 0 if absent). If `N < 2`: increment it to `N+1`, persist the incremented line to `.cw/plan-draft.md`, and proceed with whichever EXIT bullet Step 4c selects, as written below. If `N >= 2` (cap reached): headless mode does NOT proceed with that EXIT — instead hard-EXIT **through the consolidated park above** with `status: "blocked"`, `blocker.reason: "ambiguity_scan_unconverged"`, `blocker.stage: "stage1_plan"`, `blocker.details` naming the still-open item(s) verbatim and the round count (`N` at exhaustion — always 2, the fixed cap value from R6), and `retry_eligible: true`; the same single-comment bundling the three Step 4c EXIT bullets and the stub check already use — any parked ambiguities, unverified premises, or advisory Step 1f findings surfaced this same round are folded into the one park comment, never dropped behind a cap-only block. Emit the `stage.errored` block below before the `blocked` sentinel. Interactive mode does NOT proceed with that EXIT either — instead **AskUserQuestion:** "This ticket's ambiguity scan has parked for 2 consecutive rounds without converging. Continue for one more round / accept the current state (still-open items move to a new `## Open Questions` section in the plan body, unresolved but non-blocking) / abandon the ticket?"
   >
   > ```bash
   > cw event record stage.errored \
   >   --correlation-id "$TICKET" \
   >   --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"ambiguity_scan_unconverged\"}" || true
   > ```

   **Step 4c — Exit/continue decision.** Keys on `parked` and `unverified` ONLY — `deferred` never gates (#1651) — not the raw block presence: an all-adopt ambiguity scan (`parked` empty) or a premises scan fully absorbed by `self_verified` + `deferred` (`unverified` empty) is functionally `NO_AMBIGUITIES`/`no premises pending` even though the raw scans returned items:
   - `parked` empty AND `unverified` empty → AUTO-CONTINUE to Step 1d. (A premises scan whose items all landed in `self_verified` and/or `deferred` is functionally `no premises pending` for gating purposes — the plan already carries `## Self-Verified Premises` / `## Deferred Premises` and the `friction_highlights` lines from Step 4b.)

   **Headless only — on AUTO-CONTINUE path, emit `stage.entered` (`s1_ambiguity_scan_complete`):**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_complete\",\"prev_stage\":\"s1_plan_generated\",\"adopted_count\":<N>,\"malformed_recommendation_count\":<M>,\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```
   `<N>` is a literal placeholder — substitute the computed `len(adopted)` integer directly into the payload string before running the command. It is not a shell variable: nothing exports an `ADOPTED_COUNT` env var, so a literal `$ADOPTED_COUNT` token would expand to empty and produce invalid JSON. `<M>` is likewise a literal placeholder for the `malformed_recommendation_count` from Step 4a; on this AUTO-CONTINUE emission it is always `0` (malformed items are a subset of the empty `parked`), and the non-zero case surfaces via the `## Pending Verification Scan` comment note below.
   - `parked` non-empty AND `unverified` empty → EXIT `ambiguities_pending_resolution` **through the consolidated park above**. Persist the draft per the draft-persistence rule above (write the plan's current text to `.cw/plan-draft.md`) before posting. Post only the `parked` items (renumbered) under the `## Pending Verification Scan` header (one numbered question per item, with the plan's current interpretation and the alternatives) and include only the `parked` items in the result payload under `ambiguities` — never the raw N-item list. The branch is NOT created. If this round settled ≥1 item via Step 1c.0 step 5, include `resolution_consumed: true` and `resolution_evidence`; otherwise omit both keys.
   - `unverified` non-empty AND `parked` empty → EXIT `premises_pending_verification` **through the consolidated park above**. Persist the draft per the draft-persistence rule above before posting. Post ONLY the `unverified` premises (renumbered) under the `## Pending Verification Scan` header (one numbered item per premise, with what the plan depends on it for and how to verify) and include only the `unverified` items in the result payload under `premises` — never `self_verified` items, and never the raw N-item list. The branch is NOT created. If this round settled ≥1 item via Step 1c.0 step 5, include `resolution_consumed: true` and `resolution_evidence`; otherwise omit both keys.
   - `unverified` non-empty AND `parked` non-empty → EXIT `premises_pending_verification` (still the stronger signal — a false premise invalidates the ambiguity resolutions too) **through the consolidated park above**. Persist the draft per the draft-persistence rule above before posting, then post BOTH lists under the same `## Pending Verification Scan` header, where the premises half is the `unverified`-only subset and the ambiguities half is the `parked`-only subset — never either raw N-item list. If this round settled ≥1 item via Step 1c.0 step 5, include `resolution_consumed: true` and `resolution_evidence`; otherwise omit both keys.

   **Malformed-recommendation note (#1274).** Whenever `parked` items are posted to the `## Pending Verification Scan` comment, if `malformed_recommendation_count` is non-zero, prepend a one-line note above the numbered list: `Note: <malformed_recommendation_count> of these <parked_count> item(s) parked because the Recommendation field was missing or malformed, not because of a genuine PARK decision.` Both `<...>` tokens are literal placeholders — substitute the computed integers before posting. A count-only note — no per-item classification, no new partition bucket.

### Step 1d: Scope Classification

From the plan (existing or generated), classify scope:

1. Count planned files and estimated line changes
2. Check if any planned files touch forbidden areas (migrations, auth/security core, CI/CD, shared base classes with 3+ consumers):
   - **Step 1d.2a — path pre-filter:** flag any planned file under `migrations/**`, auth/security-core paths, `.github/workflows/**`, or shared base classes with 3+ consumers as a **candidate** forbidden-area hit.
   - **Step 1d.2b — content gate (CI/CD only):** for every candidate under `.github/workflows/**`, inspect the diff hunk, not just the path, and set `forbidden_touched=true` only if the hunk touches a YAML **key** defining pipeline behavior: `steps:`, `run:`, `uses:`, `permissions:`, `on:`/trigger blocks, matrix definitions, or `env:` when consumed by a step. A hunk confined to a string/comment payload inside a step (a `body:`/`echo`/markdown value, a `# comment`, example text) does NOT, even though the path pre-filter matched — e.g. #1091's `release.yml` edit inside `steps[].with.body:` is `forbidden_touched: false`, while a `run: uv publish --token ...` edit is `true`. Migrations, auth/security-core, and shared-base-class candidates are unaffected; this gate narrows CI/CD only.
3. Classify:
   - **Small:** ≤10 files AND ≤500 lines AND no forbidden-area touches
   - **Large:** >10 files OR >500 lines OR touches forbidden areas

4. **Constraint enforcement** (if `--scope-limit small` is active): if classified Large, **AskUserQuestion:** "Ticket <id> exceeds scope limit (estimated N files, ~M lines). Skip this ticket, or abort pipeline?"

5. **Constraint enforcement** (if `--forbidden <areas>` is active): if the plan touches any forbidden area, **AskUserQuestion:** "Ticket <id> touches forbidden area (<area>). Skip this ticket, or abort pipeline?"

### Checkpoint 1 (Plan Approval)

**If plan was auto-skipped** (existing plan found): Skip this checkpoint entirely.

**If plan was generated or built on partial:** present ticket summary, plan source, file list + estimated scope, scope classification, Phase 1 test approach, Phase 2 implementation approach, and friction highlights (skip if NONE). Then **AskUserQuestion:** "Approve plan, adjust, or skip ticket?"

- **Approve** → proceed to Stage 2
- **Adjust** → re-plan with user's adjustments, re-present
- **Skip** → move to next ticket in queue

**Headless** (clauses evaluated in order; first match wins): If plan agent reported `pre-flight: already satisfied` → EXIT `no_op` (see Step 1e) — this preempts every clause below, including scope and forbidden-area rejections, since there is nothing to implement. Otherwise: if plan in Linear or resumed from `.cw/plan-draft.md` → AUTO-SKIP plan-approval question (Step 1c's ambiguity scan still runs and may exit `ambiguities_pending_resolution`). **Large-scope carve-out on the resumed-draft path (#1650):** when the plan was resumed from `.cw/plan-draft.md` AND Step 1d classifies it Large, the AUTO-SKIP additionally requires approval evidence in the live-fetched comments — an operator reply approving the plan posted after the prior round's `## Pending Verification Scan` comment (the `### Approval requested` ask). Absent that evidence, EXIT `plan_pending_approval` again (persist the draft, reference the existing park comment rather than re-posting the full draft — do not re-run the advisory stations just to re-ask). A resumed draft that was parked for approval must not slip through approval by being resumed. If plan generated + small → AUTO-APPROVE and proceed (Step 1c still gates on ambiguities). If plan generated + large → EXIT `plan_pending_approval` **through the Step 1c consolidated park** (post the single enriched `## Pending Verification Scan` comment — advisory station findings, `### Approval requested`, and the full draft — no branch; ambiguity scan results ride along in the same comment when present; persist the draft to `.cw/plan-draft.md` per the Step 1c draft-persistence rule before exiting). If `--scope-limit small` rejects → EXIT `scope_exceeded`. If `--forbidden` rejects → EXIT `forbidden_area`.

### Step 1e: Pre-flight Already-Satisfied Check

Before Step 1f or Step 1g, check the plan agent's friction report for the exact phrase `pre-flight: already satisfied` under `**Discoveries**`. **Match literally** — a case-insensitive substring match is acceptable, but never infer the signal from a paraphrase; if you suspect the intent, re-spawn the plan agent with a corrected prompt rather than guess. The signal means all targeted changes are already in the desired state.

**If the signal is present:**
- Do NOT create or push a branch.
- Do NOT post a plan to Linear (the ticket is being closed, not implemented).
- Headless: EXIT immediately with `status: "no_op"`, `blocker: null`, `next_actions: ["close_issue_as_completed"]`, and `health.recommendation: "EXIT_FOR_HUMAN_REVIEW"` (a human still closes the ticket).
- Interactive: surface the per-artifact rundown and **AskUserQuestion:** "Plan agent reports the requested work is already in the desired state. Close ticket as completed, re-plan (in case the agent missed something), or skip?"
- Best-effort clear the draft: if `.cw/plan-draft.md` is present, delete it on the way out — this exit neither writes `.cw/plan.md` nor intends a resume, so a stale draft is worse than none. A pre-existing draft must not survive a `no_op` exit. Deletion is best-effort and must not fail this exit.

`no_op` is distinct from `blocked` + `agent_block`: "already satisfied" is a healthy outcome, not a failure needing attention, and routing it through `blocked` produces alerting noise.

A third option exists for a narrower case (#1862): if the work is *not*
already merged but this ticket **already has an open, unmerged PR** from an
earlier dispatch, the correct exit is `status: "stale_dispatch"` with
`blocker.reason: "pr_already_open"`, `blocker.details` naming the PR, `pr:
null`, and `next_actions: []`. Neither `no_op` (nothing is complete) nor
`blocked` (nothing is broken) fits. This normally never reaches Stage 1 — the
Stage 0 intake self-check and `cw`'s own pre-dispatch gate both catch it
first — but a resume path that skipped Stage 0 can surface it here. See
`.claude/commands/auto-dev-intake.md`'s "Step 3 open-PR self-check" for the
exact sentinel shape and the fail-open rule for an unreliable `gh` answer.

**If the signal is absent:** proceed to Step 1f (Plan Quality Review).

### Step 1f: Plan Quality Review

Fires after Checkpoint 1 (approval), after the Step 1e `no_op` short-circuit, and after Step 1c ambiguity resolutions are merged into the plan body. Two stations, two lenses, run in parallel:

- **Plan Reviewer** — *is the plan specified well enough to implement?* Catches under-specification.
- **Plan Soundness Reviewer** — *is the plan's chosen direction sound?* Catches a well-specified plan that builds the wrong thing — a direction contradicting a codified `ARCHITECTURE.md` §7/§8 rule, or matching a known high-blast-radius shape.

With Step 1c (Product Manager Reviewer Mode 1 — "did the ticket leave gaps?"), these are the plan-time pre-review: requirements, specification, direction. All three run.

**Step 1f.1 — Signoff marker check (cheap, runs first):**

Each station has its own independently-versioned marker, so soundness can go stale without forcing a spec re-review:

```
<!-- plan-spec-reviewed: YYYY-MM-DD vN -->
<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->
```

For each marker independently:
- **Present AND version current** → trust, AUTO-SKIP that station.
- **Absent OR version stale** → that station runs in Step 1f.2.

If both markers are present and current → log `Plan signoff valid (spec vN, soundness vN).` and proceed to Step 1g. Current versions: `plan-spec` `v2`, `plan-soundness` `v1`. Bumping a version constant invalidates that station's markers and forces re-review, independently per station. (`plan-spec` `v2` adds the `## Touch-point Contract` check to Plan Reviewer's Contract Specificity verification — see `agents/plan-reviewer.md` Check 1.)

**Step 1f.2 — Spawn the stale/missing stations:**

Dispatch shape depends on mode (see issues #175 / #176 for the orphan hazard this avoids):

- **Interactive mode:** spawn both stations in parallel, both `run_in_background: true`, in a single message — the Stop-hook session-completion path does not auto-transition USER-origin sessions.
- **`--headless` mode:** spawn the stations **serially** (no `run_in_background: true`). Block on each result before dispatching the next, and do NOT end the parent turn between them — background dispatch here trips the cw-side Stop-hook session-completion and orphans the run with no sentinel (`750ea77`).

Stations (whichever Step 1f.1 did not skip):
- **Plan Reviewer** (`subagent_type: "Plan Reviewer", model: "sonnet"`) — 4 checks in `agents/plan-reviewer.md`: Contract Specificity, File Enumeration, Test Helper Inventory, Observability Call Inventory. Verdicts: **NO_ISSUES**, **SHOULD_FIX**, **PRINCIPLE**, **MUST_FIX**.
- **Plan Soundness Reviewer** (`subagent_type: "Plan Soundness Reviewer", model: "sonnet"`) — two tiers in `agents/plan-soundness-reviewer.md`: Tier 1 codified violations of `ARCHITECTURE.md` §7/§8, Tier 2 Risk Radar shapes. Verdicts: **NO_ISSUES**, **RISK**, **MUST_FIX**.

**Each prompt must include:** full plan text (with Step 1c ambiguity resolutions merged in), Linear ticket ID + description, target repo path, that station's current marker version, the friction protocol block, the standard health check block.

**Step 1f.3 — Gating:**

Gate each station independently; the plan proceeds to Step 1g only when **both** markers are present (skipped-as-current or freshly appended). A station's marker is appended only when that station clears per the rules below.

**Blocking-findings comment rule (#1815).** When a headless EXIT below leaves Stage 1 carrying a persisting MUST_FIX (`blocker.reason: "plan_unreviewable"` or `"plan_unsound"`), post the persisting station's MUST_FIX finding(s) — verbatim, per the Step 1f.4 convention — as a tracker comment under the fixed header `## Blocking Review Findings`, one `### <Station Name> — MUST_FIX` sub-section per persisting station. This header is distinct from `## Pending Verification Scan` (Step 1c's park header) and `## Multi-Marker Gate Blocked` (the historical diagnostic): a hard block, not a park, and it must never post the plan text itself. `<Station>` is a literal placeholder — substitute `Plan Reviewer` for `plan_unreviewable` or `Plan Soundness Reviewer` for `plan_unsound`. Sentinel: append `blocking findings posted: <station>` to `friction_highlights`, mirroring the #1650 `consolidated park: ...` idiom.

*Plan Reviewer verdicts:*
- **NO_ISSUES / SHOULD_FIX only / PRINCIPLE only** (any scope, any mode) → log findings (do NOT re-argue PRINCIPLE), append the `plan-spec-reviewed` marker.
- **MUST_FIX where every persisting finding's category is exactly `Format-Only`, format-only cycle not yet used (any scope, any mode)** → spawn plan-revision agent (Step 1f.4) in **format-only mode**, scoped to just the format-only findings; does not require or consume the standard revision-cycle budget. Re-review only Plan Reviewer. Skips the Large-scope AskUserQuestion gate deliberately: this branch only fires for a finding whose regression made it unverifiable, the fix is a deterministic schema re-emit capped at 1 attempt, and a failure falls through to the same Large-scope gate below rather than skipping it permanently.
- **MUST_FIX, 1st cycle, Small or interactive** → spawn plan-revision agent (Step 1f.4), re-review once.
- **MUST_FIX, 1st cycle, Large + interactive** → AskUserQuestion: revise / surface to human / skip ticket. On "revise" → Step 1f.4.
- **MUST_FIX persists after 1 revision cycle, interactive** → AskUserQuestion: post stale plan to Linear anyway / abandon ticket.
- **MUST_FIX persists after 1 revision cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unreviewable"`. Do NOT post the stale plan to Linear. Before exiting, write the plan's current text — as it stands at the moment of exit, including any already-appended signoff marker — to `.cw/plan-draft.md`, so a subsequent retry can resume from it instead of regenerating. Also post the persisting `Plan Reviewer` MUST_FIX finding(s) — verbatim, per the blocking-findings comment rule above — as a tracker comment before exiting.
- **MUST_FIX persists after the format-only cycle (still all `Format-Only`)** → falls through to the standard "persists after 1 revision cycle" branches above (same AskUserQuestion / `blocked` `plan_unreviewable` EXIT, including the existing `stage.errored` emission) — the format-only path is a one-shot, not a second standard cycle.

*Plan Soundness Reviewer verdicts:*
- **NO_ISSUES** (any scope, any mode) → append the `plan-soundness-reviewed` marker.
- **RISK, interactive** → AskUserQuestion per finding: acknowledge & proceed / treat as MUST_FIX & revise / codify as a §7 principle. On "revise" → Step 1f.4; on acknowledge or codify → append the marker (record acknowledged RISKs and any `codify:` proposals in `friction_highlights`).
- **RISK, headless** → append each finding's `codify:` line to `friction_highlights`, append the marker, continue. RISK is advisory and never blocks a headless run on its own.
- **MUST_FIX, 1st cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unsound"`. Do NOT post the plan to Linear. Before exiting, write the plan's current text — as it stands at the moment of exit, signoff marker included — to `.cw/plan-draft.md` for a later resume. Also post the persisting `Plan Soundness Reviewer` MUST_FIX finding(s) — verbatim, per the blocking-findings comment rule above — as a tracker comment before exiting.
- **MUST_FIX, 1st cycle, interactive** → AskUserQuestion: revise approach / accept with explicit override / skip ticket. On "revise" → Step 1f.4; on "override" → append the marker and record the override verbatim in `friction_highlights`; on "skip" → abandon the ticket per the existing skip handling.
- **MUST_FIX persists after 1 revision cycle, interactive** → AskUserQuestion: accept with explicit override / abandon ticket.
- **MUST_FIX persists after 1 revision cycle, headless** → EXIT `blocked` with `blocker.reason: "plan_unsound"`. Before exiting, write the plan's current text — as it stands at the moment of exit, signoff marker included — to `.cw/plan-draft.md` for a later resume. Also post the persisting `Plan Soundness Reviewer` MUST_FIX finding(s) — verbatim, per the blocking-findings comment rule above — as a tracker comment before exiting.

**Codify lessons → wiki inbox:** every RISK finding's `codify:` proposal is written to `~/.claude/wiki/local/inbox/` as `lesson-soundness-codify-<shape>-<YYYY-MM-DD>.md`, with the standard `source` / `date` / `topic` frontmatter — the *durable* sink, where `friction_highlights` dies with the result payload. Route to `wiki/local/inbox/`, not the tracked `wiki/inbox/`: a codify proposal names a specific repo's architecture. This write happens for every RISK finding, in both modes, however the human dispositioned it.

If revision was performed, a marker reflects the revised plan, not the original.

**Step 1f.4 — Plan revision (when MUST_FIX from either station):**

Re-spawn the **Plan** agent (`model: "sonnet"`) with the current plan, the verbatim findings from *every* station that returned MUST_FIX (and any RISK the user chose to "treat as MUST_FIX"), and an instruction to revise addressing each one. Route the returned plan text back to Step 1f.2, re-running **only the stations that triggered the revision** (a clean station's marker stays valid). Maximum **1 revision cycle** — if a revised plan still has MUST_FIX, exit per the gating rules above.

**Format-only revision (defense-in-depth).** Fires only when Step 1f.3 routes here because every persisting Plan Reviewer MUST_FIX carries the `Format-Only` category (rare — a compliant reviewer raises such findings as SHOULD_FIX per plan-reviewer.md's severity floor). Spawns the same Plan agent, scoped to just the format-only findings, with instruction to re-emit the affected section(s) in the required schema and make no other content changes. Tracked on an **independent axis** from, and does **not** decrement, the standard 1-cycle budget — it may run even when that budget is spent. This format-only cycle is itself capped at **1 attempt**; if exhausted with MUST_FIX (still all `Format-Only`) persisting, fall through to the standard "persists after 1 revision cycle" exit branches.

**Headless only — checkpoint the revised draft (#1778).** Immediately after the plan-revision agent returns a new plan text — from either the standard MUST_FIX revision cycle or the format-only revision cycle — write the plan's current text to `.cw/plan-draft.md` — best-effort: if the write fails, log and continue; do not treat it as blocking. This checkpoint is not the fully-re-reviewed, authoritative version: Step 1f.2 still re-reviews the revised plan, and the Step 1f.3 blocked-exit writes remain the authoritative draft writes if the revision cycle is exhausted — this checkpoint only ensures a revised draft survives an unexpected interruption in between.

**Headless only — if the 1 revision cycle is exhausted and MUST_FIX persists, emit `stage.errored` before exiting:**
```bash
cw event record stage.errored \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_reviewed\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"plan_revision_failed\"}" || true
```

**Friction & health check note:** if either reviewer's friction is **BLOCK** (couldn't access the repo, plan malformed), treat as `agent_block` per the existing escalation path — do NOT treat agent failure as a clean review.

### Step 1g: Persist Plan + Post to Linear

After plan is approved (or auto-skipped with existing plan) AND Plan Quality Review has passed (Step 1f):

**FIRST, persist the plan file (#943 — the stage's primary artifact, not optional):** Write the full reviewed plan text verbatim — including both signoff markers below — to `.cw/plan.md` in the worktree, BEFORE posting to the tracker and BEFORE emitting the sentinel. Stage 2 (`auto-dev-impl.md`) hard-requires this file; a plan that exists only as a tracker comment leaves the ticket unimplementable. Verify the write (`test -s .cw/plan.md`) as part of this step.

**Then, best-effort clear the draft:** if `.cw/plan.md` was written successfully, delete `.cw/plan-draft.md` if present — a stale draft would otherwise resurface on the next Step 1a resume check even though the plan it drafted has been approved and superseded. Deletion is best-effort: if it fails, log and continue — a failed deletion must NOT fail Step 1g.

**THEN** post the same plan as a comment on the Linear issue (skip for free-text tickets — but never skip the `.cw/plan.md` write). The tracker comment is the audit copy, the file is the pipeline artifact.

**Marker requirement:** The plan body posted to Linear MUST include both signoff markers, each on its own line near the top, exactly:

```
<!-- plan-spec-reviewed: YYYY-MM-DD v2 -->
<!-- plan-soundness-reviewed: YYYY-MM-DD v1 -->
```

Use today's date and each station's current marker version. By the time Step 1g runs both markers are settled — a station that did not clear (and was not overridden) exits before this step. The markers are the contract that lets future `/auto-dev` runs skip Step 1f.2's expensive reviewer spawns.

**Scope tier marker requirement:** the persisted `.cw/plan.md` MUST also include a single canonical scope-tier line, near the scope classification, in the exact form:

```
**Scope tier:** <small|large> (<N> files, ~<M> lines, forbidden_touched=<bool>)
```

This is the one reliable, machine-greppable location the three downstream readers (`auto-dev-impl.md:59`, `auto-dev-review.md`'s tier-resolution step, `auto-dev-finalize.md:31` — all already loosely matching this shape via "or similar Stage-1c marker") depend on, rather than relying on free-text presentation happening to be greppable. Exactly one occurrence must exist in the persisted plan — a later stage that rewrites it (see `auto-dev-review.md`'s one-time tier downgrade) replaces it in place rather than appending a second occurrence.

If the plan was loaded from Linear in Step 1a and already contained a current marker, preserve it as-is. If the plan was revised in Step 1f.4, stamp with today's date.

If Step 1c surfaced ambiguities AND the user resolved them (interactive path), OR a later ticket comment resolved a previously-posted ambiguity/premise per Step 1a's "later non-pipeline comment" merge branch (headless re-entry), include the resolved answers in the Linear comment under a `## Decisions` section, so the same questions don't get re-asked in a future re-run.

**Headless only — after plan is posted / confirmed, emit `stage.entered` (`s1_plan_reviewed`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_plan_reviewed\",\"prev_stage\":\"s1_ambiguity_scan_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

---

## Stage 1 Completion (headless only)

After all Stage 1 steps complete successfully in headless mode, emit the `AUTO_DEV_RESULT` sentinel.

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
  "next_actions": [],
  "resolution_consumed": false,
  "resolution_evidence": null
}
AUTO_DEV_RESULT>>>
```

**`resolution_consumed`/`resolution_evidence` emission rule (#1896).** These two keys must include both keys only if this round settled ≥1 item via Step 1c.0 step 5 (the round's `resolution_evidence` candidate — see that step) AND the round still exits paused through one of the three Step 4c EXIT bullets; must not include them otherwise. This is what makes the anti-gaming guarantee hold: only step 5's own current-round transcription can produce a candidate, so a round with nothing settled this pass — including a round that merely finds a prior round's marker already present — never emits `resolution_consumed: true`.

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output plan`.
