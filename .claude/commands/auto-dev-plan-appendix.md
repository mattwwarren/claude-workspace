> Companion appendix to /auto-dev-plan. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 1 Plan — Appendix

Interactive-only procedures and design rationale extracted from
`.claude/commands/auto-dev-plan.md` (#1879). Each section is reached from a named
trigger sentence in the core doc; a headless run on the common path needs none of
it.

---

## Step 1a.0b: binding-resolutions delta + approved-fingerprint fast path (resumed rounds only)

Reached from Step 1a's item "0b." in the core doc, and only when Step 1a.0's resume branch fired this dispatch. A fresh dispatch never reaches here.

**Step 1a.0b — binding-resolutions delta + approved-fingerprint fast path (resumed rounds only).** Fires only when Step 1a.0's resume branch fired this dispatch; on a fresh (non-resumed) dispatch skip straight to step 1 below.

**Bookkeeping-line grammar.** The complete marker grammar and ordering are defined once by the core doc's **Leading bookkeeping-line grammar and order** rule (`.claude/commands/auto-dev-plan.md`); this section does not duplicate those forms. Step 1a.0b uses that rule's `plan-stage-resolutions-applied`, `plan-stage-resolutions-attempted`, and `plan-stage-approval-revoked` lines for source identity, recoverable attempt state, and the draft-local approval-revocation audit trail.

1. **Resolutions-delta detection.** Re-run Step 1b setup's marker-discovery procedure (`.claude/commands/auto-dev-plan.md`, "Step 1b setup — Pre-flight Resolution pre-extraction") — the comment-marker grep, the body-marker grep, newest-wins across >1 marker comment, and body-over-comment precedence — fresh against the live-fetched comments/body. Cited by name here, not restated. The result is the current authoritative resolutions source identity: `comment:<id>`, `body:<sha of body's resolutions section>`, or `none`.

2. **Delta comparison.** Compare the current source identity against the persisted `plan-stage-resolutions-applied` marker (absent ⇒ "never evaluated"):
   - marker absent + current source `none` → no delta; persist the marker as `source=none` (bootstrap), continue to step 4.
   - marker absent + current source concrete → **delta** (this resolutions source has never been folded in).
   - marker present with value X, current source Y, X ≠ Y → **delta** (a newer resolutions source, or a body edit).
   - X == Y → no delta; continue to step 4.

3. **On delta: revise.** Use Step 1f.4's re-spawn contract, modeled on the Format-only revision (defense-in-depth) precedent; this is independent of, and does not consume, the standard 1-cycle MUST_FIX revision budget, and has 1 attempt per detected source identity, not a per-dispatch cap. Checkpoint 1 MUST honor it before evaluating row-path evidence; a later `plan_approved_at` supersedes it as a fresh approval. Invalidates BOTH `plan-spec-reviewed` and `plan-soundness-reviewed`; a successful revision requires fresh row-path approval via `cw dev-queue approve`. First, if the row-path fields are non-null, call `cw dev-queue revoke-plan-approval <ticket> -c <client>` through the queue's durable mutation path. It atomically clears both row fields under the queue lock; fail closed if that mutation fails. Then persist a durable `<!-- plan-stage-approval-revoked: at=<UTC>; fingerprint=<sha> -->` line, where `<sha>` is the pre-clear `queue_metadata.plan_approved_fingerprint` and `<UTC>` is the current time. This draft marker is audit/recovery evidence only: Checkpoint 1 MUST consume the durable cleared-row state, and a later `plan_approved_at` supersedes the marker as a fresh approval. Next, inspect `plan-stage-resolutions-attempted`. A `started` marker is a reservation lease, not a consumed attempt: its `lease_until` is exactly 15 minutes after reservation; if it is still in the future, do not spawn and EXIT `blocked` with `blocker.reason: "agent_block"` naming the active reservation; if it is expired (including an interrupted prior dispatch), reconcile it by replacing it with a fresh `started` marker and lease before spawning. A matching `outcome=failed` or `outcome=succeeded` marker is the only consumed attempt state; persist the draft and EXIT `blocked` for that source identity without spawning again. Otherwise persist a `started` marker with the 15-minute lease, then spawn the **Plan** agent (`subagent_type: "Plan", model: "sonnet"`) on an independent axis inside Step 1f.4 (`.claude/commands/auto-dev-plan.md`) — modeled on the Format-only revision (defense-in-depth) precedent there — independent of, and does not consume, the standard 1-cycle MUST_FIX revision budget. Prompt: the current draft, plus a `## Binding Pre-flight Resolutions` injection (Step 1b setup's exact shape, cited by name, not restated), plus an instruction to revise the draft and re-emit/extend `## Pre-flight Resolution Conformance`. Invalidates BOTH `plan-spec-reviewed` and `plan-soundness-reviewed` signoff markers on the resulting draft — a resolutions redirect can implicate either station's prior verdict. The cap is **1 attempt per detected source identity**, persisted by that marker; an expired interrupted-dispatch lease is recoverable and does not consume it. Only after a valid revised draft is successfully checkpointed may the stage change the attempt marker to `outcome=succeeded` and persist `plan-stage-resolutions-applied` = the source identity just applied. If the revision agent fails, returns invalid plan text, or the checkpoint fails, change the attempt marker to `outcome=failed`, persist it together with the approval-revocation marker under the Draft-rewrite rule (`.claude/commands/auto-dev-plan.md`), EXIT `blocked` with `blocker.reason: "agent_block"`, and do not retry that unchanged source identity on a later dispatch; a newly detected source identity gets its own one-shot attempt. A successful revision still requires fresh row-path approval via `cw dev-queue approve` before Checkpoint 1 may treat that evidence as present. **Telemetry:** none beyond the best-effort checkpoint write — a bare successful revision emits no new `stage.entered`/`stage.errored`; the failed-attempt blocked exit is the specified durable error path. If the revision leaves a persisting MUST_FIX that later exhausts Step 1f.3's own cycle, Step 1f.3's existing `stage.errored` emission (unchanged) covers it — no gap.

The durable revocation is the approval source of truth: Checkpoint 1 MUST honor it before evaluating row-path evidence, while consuming the cleared queue-row fields; it must not restore approval from the draft-local marker. A later `plan_approved_at` supersedes it as a fresh approval.

4. **Fingerprint fast-path check (runs regardless of whether step 3 fired).** Compute `draft_fp` per the *Plan-draft fingerprint rule* (`.claude/commands/auto-dev-plan.md`) of the draft as it now stands. Reuse Checkpoint 1's existing row-path evidence check by name — do not re-derive it here (`.claude/commands/auto-dev-plan.md`, Checkpoint 1 — non-null `queue_metadata.plan_approved_at` **and** non-null `queue_metadata.plan_approved_fingerprint`, **and** either a fingerprint equal to `draft_fp` or the *Operator-authority delta* alternate, whose two-branch test finds no newer operator-authority comment).
   - **Match → fast path.** Skip Step 1c's ambiguity/premise re-scan AND Step 1c.0's round-cap/settlement-folding machinery entirely — no Product Manager Reviewer spawn, nothing rewrites the draft — and proceed straight to Step 1d. Emit:
     ```bash
     cw event record stage.entered \
       --correlation-id "$TICKET" \
       --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_skipped\",\"prev_stage\":\"s1_plan_generated\",\"reason\":\"approved_fingerprint_match\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
     ```
     — in place of, not in addition to, `s1_ambiguity_scan_complete`.
   - **Operator-authority-delta alternate → fast path.** When Checkpoint 1's alternate row-path condition holds — a durable `plan_approved_at` exists AND the *Operator-authority delta* rule finds no newer operator-authority comment — take the same fast path independently of fingerprint equality. Skip Step 1c's ambiguity/premise re-scan AND Step 1c.0's round-cap/settlement-folding machinery entirely and proceed straight to Step 1d. Emit:
     ```bash
     cw event record stage.entered \
       --correlation-id "$TICKET" \
       --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s1_ambiguity_scan_skipped\",\"prev_stage\":\"s1_plan_generated\",\"reason\":\"operator_approval_no_delta\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
     ```
     — in place of, not in addition to, `s1_ambiguity_scan_complete`. Any operator-authority comment newer than `plan_approved_at` disqualifies this branch and falls through to Step 1c.0 / Step 1c as today. Like the existing fingerprint-match branch, this branch never emits `resolution_consumed` or `resolution_evidence`.

     **This branch fires ONLY when ALL three conditions hold (#2433 fix cycle 6)** — the two above plus a third the prior wording omitted:
     1. a durable `plan_approved_at` **and** its paired `plan_approved_fingerprint` both exist on the row (Checkpoint 1's alternate row-path condition);
     2. the *Operator-authority delta* rule (`.claude/commands/auto-dev.md`) finds no operator-authority comment newer than `plan_approved_at`;
     3. the persisted `body_sha` — read from `.cw/plan-draft.md`'s `plan-stage-last-evaluated` marker (absent ⇒ treat as no persisted value, condition fails) — equals a freshly computed SHA-256 of the live-fetched issue body, using the same *body_sha* definition Step 1c.0 uses.

     A `body_sha` mismatch disqualifies this branch exactly as a newer operator-authority comment does (condition 2), falling through to Step 1c.0 / Step 1c as today; that fallthrough takes Step 1c.0's own existing tracker-state-delta reset on the mismatch, so this composition invents no new branch for it. Condition 3 is never covered by the *Operator-authority delta* rule itself — that rule stays comment-scoped only (see its definition in `.claude/commands/auto-dev.md`) and is never, by itself, disqualified by a body edit; this fast path composes that rule with the separate `body_sha` check as an independent, additional gate, so the comment-scoped rule never exempts the fast path from the body-edit check.
   - **No match (or absent evidence) → proceed to Step 1c.0 / Step 1c as today**, now evaluated against the (possibly just-revised) draft.

**Scope note on evidence source.** Step 4's existing fingerprint-equality evidence check is scoped to the row-path only (`cw dev-queue approve`) — matching the ticket's literal wording ("the row's `plan_approved_fingerprint` matches"). The new no-delta alternate is also anchored to the row's durable `plan_approved_at`; a comment-path-token-approved draft is unaffected by this section and continues through today's slower path (Step 1c re-scan runs, Checkpoint 1 re-evaluates the comment-path token as it does today).

Because step 3 (delta/revision) runs strictly before step 4 (fingerprint fast-path check), a new resolutions-marker comment or a body edit that changes the resolutions source after approval automatically takes precedence over the fast path: step 3 revises the draft and durably revokes the approval, so the freshly-computed `draft_fp` no longer equals the (now-stale) `plan_approved_fingerprint`, and the fast path falls through to the normal ambiguity re-scan → Checkpoint 1 re-park, with a freshly computed fingerprint. An ordinary ticket body edit that does not change the resolutions source does not, by itself, disqualify the Operator-authority delta alternate; that rule is comment-scoped only, so such a body edit remains a separate `body_sha` tracker-state change rather than an operator-authority delta.

---

## Step 1c.0: round-cap read and settlement folding (resumed rounds only)

Reached from `### Step 1c: Ambiguity Verification` in the core doc, and only when Step 1a.0's resume branch fired this dispatch. A fresh dispatch never reaches here. Reached only when Step 1a.0b's fast path (above) did not already apply — a fast-path match this round skips this section entirely.

**Step 1c.0 — Round-cap read + settlement folding (resumed rounds only).** Fires only when Step 1a.0's resume branch fired this dispatch; on a fresh (non-resumed) dispatch skip straight to step 1 below.

   0. **Fingerprint read (#2154).** Read `.cw/plan-draft.md`'s `plan-stage-last-evaluated` marker (its second line, immediately after the round-counter line; absent ⇒ treat as no prior fingerprint — nothing to compare against yet). The fingerprint is **exactly two components**: `operator_comment` — the `id` of the newest live-fetched comment that survives the *Comment provenance rule* in `.claude/commands/auto-dev.md`, or `none` when no such comment exists; and `body_sha` — a SHA-256 of the live-fetched issue body text (github: `gh issue view <n> --json body`; linear: `get_issue`), computed over the raw string. Neither component derives from the issue's raw `updated_at`/`updatedAt`. If either component differs from the persisted fingerprint, this round's cap check (core doc, Pre-branch integrity checks) evaluates with `N` reset to 0 regardless of the persisted round counter. The fingerprint itself is persisted at the same point the round counter is written — the cap check's own tracker-state-delta reset step — not here.
   1. Read the round counter from `.cw/plan-draft.md`'s first line (`<!-- plan-stage-scan-round: N -->`, default 0 when absent).
   2. Locate the newest `## Pending Verification Scan` comment in the live-fetched comments. It defines the currently-open numbered items and each item's **plan-authored** content: its question/claim, its "Plan currently assumes"/stated-fact text, and — for ambiguities — its lettered `(a)/(b)/(c)...` alternatives.
   3. Locate **all** ordinary ticket comments posted after that park comment, from the ticket's author/operator, in chronological order. No marker, no required format — plain natural language. **Provenance gate (#2097):** apply the *Comment provenance rule* in `.claude/commands/auto-dev.md` first — a comment it marks agent-authored (the `<!-- cw-agent-authored -->` marker line, a pipeline fixed header, or the plan-of-record post) is this pipeline's own analysis and can never supply this decision. Keep every surviving comment as an independent candidate: a later comment-path approval token must not hide an earlier answer comment or answers in the token-bearing comment itself. If none exists, nothing settles this round; proceed to step 6. **Cross-reference (#2074):** these comments are also the ones Checkpoint 1's Large-scope carve-out checks for comment-path plan-approval evidence — transcribe valid answers from token-bearing comments before applying the no-double-duty approval exclusion; any comment that settles ≥1 item at step 5 is disqualified from ALSO counting as approval; see Checkpoint 1's no-double-duty rule.
   4. **Transcription (R4).** For each still-open item from step 2, read each independent candidate comment from step 3 in full — including a comment carrying the exact comment-path approval token — and determine whether it answers that specific item, classifying strictly against that item's own enumerated options (`ADOPTED`/`ALT-<x>` for ambiguities, where `<x>` MUST be one of that item's own lettered alternatives from step 2; `CONFIRMED`/`REFUTED`/`DEFERRED` for premises). First transcribe any valid closed-vocabulary answer from a token-bearing comment and persist it through step 5; the token alone does not settle an item. Only after that transcription does Checkpoint 1 apply the no-double-duty rule to decide whether the token-bearing comment may also count as approval. Anything that does not map cleanly to exactly one option — unaddressed, hedged, off-topic, or outside the closed set — is **unmappable**: default to unmappable on any doubt. An unmappable item is not settled; it stays open and is scanned as ordinary ticket text like any other comment in the spawn below — no special handling, no partial credit. The only permitted output of this step is one closed token from the grammar above, per item. **`DEFERRED` (R7, active registration):** transcribes identically to `CONFIRMED` for re-raise-suppression purposes — the recorded marker value (`DEFERRED` vs `CONFIRMED`) is audit-only and carries no different settlement behavior at this step. Settling a premise `DEFERRED` never itself writes an `In-implementation check:`/`On mismatch:` pair from operator prose; that pair, if one is ever produced, comes only from the agent's own classification on a later scan (see step 5).
   5. For each settled (non-unmappable) item: insert the new `plan-stage-settled` marker line (grammar above) immediately before the existing `plan-stage-resolutions-applied` line, if present; otherwise append it after the other leading bookkeeping lines. This preserves the resolutions marker as the always-last line even when a later round settles another item. Then append one entry to `## Settled Plan Items` quoting ONLY the item's own plan-authored question/claim (and, for `ALT-<x>`, the matched alternative's own text) exactly as it appeared in the step-2 park comment — never the operator's reply text; and append one `friction_highlights` line of the form `plan-stage item settled: <item id> → <marker value> — round <N>` (also never quoting operator text; both `<...>` tokens are literal placeholders — substitute the computed values). **`DEFERRED` wiring (R7, active registration):** when the settled item is a premise marked `DEFERRED`, this step ALSO writes a stub entry to `## Deferred Premises` at settlement time — the stub carries only the plan-authored claim text (quoted from the step-2 park comment, never operator prose) plus a placeholder `In-implementation check:`/`On mismatch:` pair marked `PENDING — agent must supply on next scan`. The stub is not itself a runtime check — it exists only to guarantee the claim is mechanically fed forward. The settled-items-by-identity exclusion suppresses only re-raising the *identical parked question*; it never exempts the underlying claim from the Product Manager Reviewer's own classification work. The PM Reviewer prompt directs the agent to re-classify the stubbed claim's `Verified:` status on the *immediately next* scan — enforced by the presence of the `PENDING` stub, not merely invited. On that next scan the agent supplies its own `In-implementation check:`/`On mismatch:` pair (never transcribed from operator prose) and classifies `Verified: DEFER` (replacing the stub's placeholder pair, confirming the halt-check is live) or `Verified: NO` (the stub is removed — the claim did not hold up to scrutiny and reopens as an ordinary unverified premise, subject to Step 4c gating like any other). A stub that survives past its immediately-next scan without being resolved is a defect in this mechanism, not an accepted steady state — the pre-branch stub check below hard-blocks the round rather than letting it pass. **Resolution-evidence candidate (#1896).** When step 4/5's transcription settles ≥1 item in this round's own transcription pass, additionally record that this round settled ≥1 item, together with the supplying comment(s)' ids/URLs and the settled item ids — this becomes the round's `resolution_evidence` candidate, attached to the sentinel only if the round still exits paused via one of the three Step 4c EXIT bullets below. Scoped strictly to items settled by step 4's transcription in this round's own pass: a `plan-stage-settled` marker merely found already present from a prior round is not among the "still-open items from step 2" this round's step 4 evaluates, so it can never mint a second `resolution_evidence` candidate on a later scan.
   6. Persist the updated draft via the draft-persistence rule below. The round counter is unchanged by this step — it is bumped only on a park EXIT, by the pre-branch cap check.

   `## Settled Plan Items` anchors by the same Step 4b chain as the other plan-body accumulator sections: immediately after `## Deferred Premises` / `## Self-Verified Premises` if present, else immediately after `## Adopted Assumptions` if present, else immediately before `## Ambiguities` if present, else as the first section after the plan's title/summary. Its entries carry **only plan-authored content** — the item's own question/claim/alternative text quoted from the park comment, plus the closed-vocab marker value — never operator prose, not even an excerpt of the operator's decision sentence.

   **No redaction, anywhere (R3).** The ticket-comment text handed to the Product Manager Reviewer prompt is the complete, unredacted live-fetched stream, always — including every operator settlement reply located in step 3, verbatim. Nothing in Step 1c.0 removes, truncates, or placeholders any span of ticket-comment text. The only addition to the prompt is additive: `## Settled Plan Items`' plan-authored content, passed alongside (never instead of) the full stream. Stated explicitly: a factual claim inside an operator's settlement reply is re-scrutinized by every subsequent scan, forever — this is intended, not a residual gap.

---

## Consolidated park (single-exit rule, #1650)

Reached from Step 1c's headless mode in the core doc, and only when a gate has decided to exit for a human with a draft plan in hand. A round that converges to AUTO-CONTINUE never reaches here.

**Consolidated park (single-exit rule, #1650).** When Step 4c below (or Checkpoint 1's headless large-scope clause, or the Pre-branch integrity checks' stub/cap hard-EXITs below, #1683) decides to exit for a human AND a draft plan exists in hand, do NOT exit carrying only that gate's findings — each serial gate costs one operator round (mean park latency 9–13h). Finish ALL remaining plan-phase analysis first:

   1. Run Step 1d scope classification on the draft (if not already run this invocation).
   2. Run the Step 1f stations (Plan Reviewer + Plan Soundness Reviewer, serially per the existing headless dispatch rules, honoring the Step 1f.1 marker skip) in **advisory mode**: findings are collected only — no signoff marker is appended, no revision cycle (Step 1f.4) runs, and a station MUST_FIX must NOT convert the park into `blocked`; the fixes land next round together with the operator's answers. A station that friction-BLOCKs is skipped with a note in the comment — never escalated to `agent_block` from this path.
   3. Post ONE comment under the `## Pending Verification Scan` header containing, in order: the numbered parked ambiguities and/or unverified premises (existing shapes, renumbering, and the malformed-recommendation note, all unchanged); `### Advisory plan-review findings (address in the same round)` with each station finding verbatim (omit this sub-section when both stations returned NO_ISSUES or were marker-skipped); when Step 1d classified the draft Large, `### Approval requested` — an approving reply alongside the answers clears both gates on re-entry (say so, and name `cw dev-queue approve <ticket> -c <client>` as the equivalent that needs no tracker comment: it stamps `plan_approved_at` **and** `plan_approved_fingerprint` on the dev-queue row, which Checkpoint 1 reads from `queue_metadata` on the next dispatch). **Say what the approval is bound to (#2102):** the row-side approval covers *this* draft specifically — `plan_approved_fingerprint` records the draft's fingerprint, and if the draft changes before the next dispatch, Checkpoint 1 detects the mismatch and re-asks rather than proceeding on a stale approval. **Comment-path token (#2074):** also print, verbatim, the literal line an operator can paste in reply to approve without the CLI — `<!-- auto-dev-comment-approval -->` — stated plainly that it must be its own comment, separate from any reply resolving a parked ambiguity or premise (a comment doing both does not count; see Checkpoint 1's no-double-duty rule), and that it stops counting once any later `## Pending Verification Scan` or `## Blocking Review Findings` comment posts (see Checkpoint 1's staleness guard); and `### Draft plan (unreviewed — context only)` with the full draft text. **Provenance marker (#2097):** end the comment body with the line `<!-- cw-agent-authored -->` on its own line after a blank line, per the *Comment provenance rule* in `.claude/commands/auto-dev.md` — it is what stops a later stage reading this pipeline's own analysis as an operator decision.
   3a. **Park marker (#2135):** once the step 3 comment has posted successfully (a failed or skipped post is not stamped), run `cw signal-park` once, from the cw session worktree root (the directory holding `.claude/cw-context.json`; not a gate worktree and not a nested agent worktree). It takes no arguments. Never run it for a `tool_denied` exit (that sentinel is emitted immediately, with no further tool call). If it is denied, exits non-zero, is an unknown command (an older `cw`), or says `park marker NOT recorded`, ignore that and emit the exit sentinel unchanged. See the *Park-comment stamp rule* in `.claude/commands/auto-dev.md`.
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
