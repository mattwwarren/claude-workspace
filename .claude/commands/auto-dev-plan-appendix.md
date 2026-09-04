> Companion appendix to /auto-dev-plan. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 1 Plan — Appendix

Interactive-only procedures and design rationale extracted from
`.claude/commands/auto-dev-plan.md` (#1879). Each section is reached from a named
trigger sentence in the core doc; a headless run on the common path needs none of
it.

---

## Step 1c.0: round-cap read and settlement folding (resumed rounds only)

Reached from `### Step 1c: Ambiguity Verification` in the core doc, and only when Step 1a.0's resume branch fired this dispatch. A fresh dispatch never reaches here.

**Step 1c.0 — Round-cap read + settlement folding (resumed rounds only).** Fires only when Step 1a.0's resume branch fired this dispatch; on a fresh (non-resumed) dispatch skip straight to step 1 below.

   1. Read the round counter from `.cw/plan-draft.md`'s first line (`<!-- plan-stage-scan-round: N -->`, default 0 when absent).
   2. Locate the newest `## Pending Verification Scan` comment in the live-fetched comments. It defines the currently-open numbered items and each item's **plan-authored** content: its question/claim, its "Plan currently assumes"/stated-fact text, and — for ambiguities — its lettered `(a)/(b)/(c)...` alternatives.
   3. Locate the newest ordinary ticket comment posted after that park comment, from the ticket's author/operator. No marker, no required format — plain natural language. **Provenance gate (#2097):** apply the *Comment provenance rule* in `.claude/commands/auto-dev.md` first — a comment it marks agent-authored (the `<!-- cw-agent-authored -->` marker line, a pipeline fixed header, or the plan-of-record post) is this pipeline's own analysis and can never supply this decision. If none exists, nothing settles this round; proceed to step 6.
   4. **Transcription (R4).** For each still-open item from step 2, read the comment from step 3 in full and determine whether it answers that specific item, classifying strictly against that item's own enumerated options (`ADOPTED`/`ALT-<x>` for ambiguities, where `<x>` MUST be one of that item's own lettered alternatives from step 2; `CONFIRMED`/`REFUTED`/`DEFERRED` for premises). Anything that does not map cleanly to exactly one option — unaddressed, hedged, off-topic, or outside the closed set — is **unmappable**: default to unmappable on any doubt. An unmappable item is not settled; it stays open and is scanned as ordinary ticket text like any other comment in the spawn below — no special handling, no partial credit. The only permitted output of this step is one closed token from the grammar above, per item. **`DEFERRED` (R7, active registration):** transcribes identically to `CONFIRMED` for re-raise-suppression purposes — the recorded marker value (`DEFERRED` vs `CONFIRMED`) is audit-only and carries no different settlement behavior at this step. Settling a premise `DEFERRED` never itself writes an `In-implementation check:`/`On mismatch:` pair from operator prose; that pair, if one is ever produced, comes only from the agent's own classification on a later scan (see step 5).
   5. For each settled (non-unmappable) item: append the marker line (grammar above) to `.cw/plan-draft.md`; append one entry to `## Settled Plan Items` quoting ONLY the item's own plan-authored question/claim (and, for `ALT-<x>`, the matched alternative's own text) exactly as it appeared in the step-2 park comment — never the operator's reply text; and append one `friction_highlights` line of the form `plan-stage item settled: <item id> → <marker value> — round <N>` (also never quoting operator text; both `<...>` tokens are literal placeholders — substitute the computed values). **`DEFERRED` wiring (R7, active registration):** when the settled item is a premise marked `DEFERRED`, this step ALSO writes a stub entry to `## Deferred Premises` at settlement time — the stub carries only the plan-authored claim text (quoted from the step-2 park comment, never operator prose) plus a placeholder `In-implementation check:`/`On mismatch:` pair marked `PENDING — agent must supply on next scan`. The stub is not itself a runtime check — it exists only to guarantee the claim is mechanically fed forward. The settled-items-by-identity exclusion suppresses only re-raising the *identical parked question*; it never exempts the underlying claim from the Product Manager Reviewer's own classification work. The PM Reviewer prompt directs the agent to re-classify the stubbed claim's `Verified:` status on the *immediately next* scan — enforced by the presence of the `PENDING` stub, not merely invited. On that next scan the agent supplies its own `In-implementation check:`/`On mismatch:` pair (never transcribed from operator prose) and classifies `Verified: DEFER` (replacing the stub's placeholder pair, confirming the halt-check is live) or `Verified: NO` (the stub is removed — the claim did not hold up to scrutiny and reopens as an ordinary unverified premise, subject to Step 4c gating like any other). A stub that survives past its immediately-next scan without being resolved is a defect in this mechanism, not an accepted steady state — the pre-branch stub check below hard-blocks the round rather than letting it pass. **Resolution-evidence candidate (#1896).** When step 4/5's transcription settles ≥1 item in this round's own transcription pass, additionally record that this round settled ≥1 item, together with the step-3 comment's id/URL and the settled item ids — this becomes the round's `resolution_evidence` candidate, attached to the sentinel only if the round still exits paused via one of the three Step 4c EXIT bullets below. Scoped strictly to items settled by step 4's transcription in this round's own pass: a `plan-stage-settled` marker merely found already present from a prior round is not among the "still-open items from step 2" this round's step 4 evaluates, so it can never mint a second `resolution_evidence` candidate on a later scan.
   6. Persist the updated draft via the draft-persistence rule below. The round counter is unchanged by this step — it is bumped only on a park EXIT, by the pre-branch cap check.

   `## Settled Plan Items` anchors by the same Step 4b chain as the other plan-body accumulator sections: immediately after `## Deferred Premises` / `## Self-Verified Premises` if present, else immediately after `## Adopted Assumptions` if present, else immediately before `## Ambiguities` if present, else as the first section after the plan's title/summary. Its entries carry **only plan-authored content** — the item's own question/claim/alternative text quoted from the park comment, plus the closed-vocab marker value — never operator prose, not even an excerpt of the operator's decision sentence.

   **No redaction, anywhere (R3).** The ticket-comment text handed to the Product Manager Reviewer prompt is the complete, unredacted live-fetched stream, always — including every operator settlement reply located in step 3, verbatim. Nothing in Step 1c.0 removes, truncates, or placeholders any span of ticket-comment text. The only addition to the prompt is additive: `## Settled Plan Items`' plan-authored content, passed alongside (never instead of) the full stream. Stated explicitly: a factual claim inside an operator's settlement reply is re-scrutinized by every subsequent scan, forever — this is intended, not a residual gap.

---

## Consolidated park (single-exit rule, #1650)

Reached from Step 1c's headless mode in the core doc, and only when a gate has decided to exit for a human with a draft plan in hand. A round that converges to AUTO-CONTINUE never reaches here.

**Consolidated park (single-exit rule, #1650).** When Step 4c below (or Checkpoint 1's headless large-scope clause, or the Pre-branch integrity checks' stub/cap hard-EXITs below, #1683) decides to exit for a human AND a draft plan exists in hand, do NOT exit carrying only that gate's findings — each serial gate costs one operator round (mean park latency 9–13h). Finish ALL remaining plan-phase analysis first:

   1. Run Step 1d scope classification on the draft (if not already run this invocation).
   2. Run the Step 1f stations (Plan Reviewer + Plan Soundness Reviewer, serially per the existing headless dispatch rules, honoring the Step 1f.1 marker skip) in **advisory mode**: findings are collected only — no signoff marker is appended, no revision cycle (Step 1f.4) runs, and a station MUST_FIX must NOT convert the park into `blocked`; the fixes land next round together with the operator's answers. A station that friction-BLOCKs is skipped with a note in the comment — never escalated to `agent_block` from this path.
   3. Post ONE comment under the `## Pending Verification Scan` header containing, in order: the numbered parked ambiguities and/or unverified premises (existing shapes, renumbering, and the malformed-recommendation note, all unchanged); `### Advisory plan-review findings (address in the same round)` with each station finding verbatim (omit this sub-section when both stations returned NO_ISSUES or were marker-skipped); when Step 1d classified the draft Large, `### Approval requested` — an approving reply alongside the answers clears both gates on re-entry (say so, and name `cw dev-queue approve <ticket> -c <client>` as the equivalent that needs no tracker comment: it stamps `plan_approved_at` on the dev-queue row, which Checkpoint 1 reads from `queue_metadata` on the next dispatch); and `### Draft plan (unreviewed — context only)` with the full draft text. **Provenance marker (#2097):** end the comment body with the line `<!-- cw-agent-authored -->` on its own line after a blank line, per the *Comment provenance rule* in `.claude/commands/auto-dev.md` — it is what stops a later stage reading this pipeline's own analysis as an operator decision.
   4. Persist the draft per the draft-persistence rule above.

   The exit **status** is unchanged by consolidation — Step 4c's precedence still picks `premises_pending_verification` over `ambiguities_pending_resolution`, and a park with nothing parked/unverified but Large scope still exits `plan_pending_approval` via Checkpoint 1; only the comment gets richer. Sentinel: append `consolidated park: <a> ambiguities, <p> premises, <f> advisory findings, scope <tier>` to `friction_highlights` (placeholders — substitute computed values; no schema change). Guard: the advisory station run happens only when a draft plan exists in hand — an exit with no plan keeps its existing comment shape. Result-payload rules are untouched: `ambiguities`/`premises` arrays carry the same parked/unverified-only subsets; advisory findings travel in the comment and `friction_highlights` only.

---

## Why an inline ambiguity scan is never a substitute for the agent spawn

None of these are valid reasons to skip the Step 1c agent spawn:

- *"Ticket is highly prescriptive — file paths, exact code, test cases."* Detail
  creates false confidence; implicit assumptions go unstated precisely because
  the author thought everything was covered.
- *"User said move without pausing / don't ask questions."* That governs
  clarifying questions to the user. The PM Reviewer runs in background and asks
  nothing of anyone.
- *"I can scan it faster myself."* The agent is cheap; a missed ambiguity is
  rework or a wrong implementation.
- *"Ticket is short / scope is small."* Small scope is not unambiguous scope.

If you catch yourself drafting prose that explains *why* the agent isn't needed
this time, that IS the signal — spawn it.

---

## Checkpoint 1 — interactive plan-approval gate

**If plan was auto-skipped** (existing plan found): skip this checkpoint
entirely.

**If plan was generated or built on partial:** present ticket summary, plan
source, file list + estimated scope, scope classification, Phase 1 test approach,
Phase 2 implementation approach, and friction highlights (skip if NONE). Then
**AskUserQuestion:** "Approve plan, adjust, or skip ticket?"

- **Approve** → proceed to Stage 2
- **Adjust** → re-plan with user's adjustments, re-present
- **Skip** → move to next ticket in queue

---

## Step 1f — the two review lenses and how they compose with Step 1c

Step 1f fires after Checkpoint 1 (approval), after the Step 1e `no_op`
short-circuit, and after Step 1c ambiguity resolutions are merged into the plan
body. Two stations, two lenses:

- **Plan Reviewer** — *is the plan specified well enough to implement?* Catches
  under-specification.
- **Plan Soundness Reviewer** — *is the plan's chosen direction sound?* Catches a
  well-specified plan that builds the wrong thing — a direction contradicting a
  codified `ARCHITECTURE.md` §7/§8 rule, or matching a known high-blast-radius
  shape.

With Step 1c (Product Manager Reviewer Mode 1 — "did the ticket leave gaps?"),
these are the plan-time pre-review: requirements, specification, direction. All
three run.
