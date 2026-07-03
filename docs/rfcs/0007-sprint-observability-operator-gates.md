# RFC 0007 — Sprint Observability + Operator Gates

| Field | Value |
|---|---|
| Status | **Draft** |
| Owner | @mattwwarren |
| Date | 2026-07-03 |
| Supersedes | none |
| Related | RFC 0004 (work lanes), RFC 0005 (staged pipeline), RFC 0002 (Agent SDK / MCP push channel), RFC 0006 (review system), ADR 0006 (reaping authority), `docs/events.md` |

## Summary

The 2026-07-01/03 reliability bug sprint exposed two gaps:

1. **Observability fragility.** The operator burned context re-deriving PR and queue state every prompt. Two distinct queue freezes — a freshness-gate stall (#940) and a session-limit lane wedge — were silent for hours, requiring manual inspection to detect.
2. **Missing operator gate.** There is no personal-signoff checkpoint before a ticket ships. The `--scope large` flag (#926) is ineffective when the worker reclassifies scope; PR creation happens at finalize (late), not at review end as draft.

This RFC closes both gaps across four workstreams: board consolidation, PR-state push events, operator signoff gates, and worker-context correctness (the data-integrity floor the board depends on).

## Motivation

**Ad-hoc polling tax.** Nothing persists PR state between orchestrator prompts; `gh pr view` subprocess calls happen in the hot path. `OrchestratorEventType` defines `pr.registered`, `pr.ci_failed`, `pr.review_received`, `pr.mergeable`, `pr.merged` (`models.py:207–211`) — zero emit sites exist. `retire_merged_prs` (`orchestrate.py:259`) consumes `PR_MERGED` events nothing produces.

**Silent freezes.** When #940's worktree escape left `main` diverged, the freshness gate correctly refused all dispatch (`skip=freshness_gate`) — but emitted no attention signal. Hours passed before an operator polled and noticed.

**TUI sprawl without consolidation.** Three overlapping read surfaces exist:
- `src/cw/board.py` (277 lines) — `cw board`, RFC 0005 D1 lane×stage cockpit.
- `src/cw/tui.py` (837 lines) — `cw orchestrate status/watch`, the surface #854 identified as unreadable (`dispatch.tick` floods the recent-events panel; every session renders `last_stage=unknown`).
- Plain-text `cw dev-queue status` / `cw status` / `cw list`.

The "Live work dashboard" milestone closed without consolidating these.

**Operator gates are advisory, not enforced.** `--scope large` (#926) does not guarantee a pause before IMPL when a worker classifies scope as SMALL/MEDIUM: `apply_staged_decision` (`dispatch.py:1324`) advances on `stage_complete` regardless of the operator flag. There is no signoff gate between REVIEW completion and PR readying.

**Workers run blind.** `context.json` materialized with zero comments on the debt lane (#952) caused three wasted plan rounds. Empty `ambiguities` items (`{question: null}`) pass validation and park tickets with nothing for the operator to answer (#953).

## Design

### W1 — Board consolidation

`board.py` becomes the single live read surface. Current `cw board` renders the lane×stage grid (RFC 0005 D1); this workstream extends it with:

- **PR/CI column.** Hydrated from `TicketTask.pr_state` (new field, see W2); renders CI status, review decision, and attention badges without any subprocess call.
- **AWAITING SIGNOFF cell state.** New visual status alongside RUNNING/BLOCKED/PENDING for tickets parked at the W3 gate.
- **Session age + last stage.** Pulled from `stage_history` (RFC 0005 ledger), same as the rework badge already planned.
- **Attention/park badges from the event bus.** `session.needs_attention`, `session.reap_proposed`, and the new `freshness_gate.blocked` event (see W2) surface inline without polling.

`tui.py`'s session-grouping view and worktree-contention column migrate into `board.py` as a toggle panel (`cw board --detail`). The `dispatch.tick` flood (#854) is suppressed by default in the board's event feed (aggregate consecutive ticks as `dispatch.tick ×N over Xm`); `cw board --raw-events` restores full stream. `last_stage=unknown` (#854) resolves when stage events are wired in Phase 2.

`cw orchestrate watch` is deprecated in Phase 4 after parity. `cw orchestrate status` retains its `--json` path (the machine-readable surface used by tests and the future tuner). Absorbed legibility tickets: #854, #813, #824.

### W2 — PR state + push events

**Poll layer (#929):** each Nth serve/dispatch tick hydrates open PRs via `gh pr view --json state,mergeable,mergeStateStatus,statusCheckRollup,reviewDecision`. Result persisted as `pr_state` on `TicketTask` (`models.py:268`, after `pr_url` at line 324). On state transition only: emit `pr.merged`, `pr.ci_failed`, `pr.review_received`, `pr.mergeable` — first producers for the enum at `models.py:207–211`. Transition-dedup guards against double-emit. `retire_merged_prs` (`orchestrate.py:259`) begins actually firing.

**Freshness-gate signal.** Emit `session.needs_attention` (already defined, `models.py:203`) when the freshness gate blocks more than N consecutive ticks (proposed default: 5). Board surfaces this as an attention badge. Resolves the silent freeze observed in #940.

**Push layer (#930):** GitHub Actions workflow POSTs to `cw_pr_events_server.py`'s existing `POST /pr-event` endpoint (RFC 0002 Phase 3 shipped the receiving half). Relay via smee.io / cloudflared tunnel (URL a config value). Reuses #929's transition-dedup so push and poll coexist. HMAC secret added to the currently-unauthenticated endpoint. Push reduces latency to seconds; poll remains the reconciliation layer for missed webhooks.

### W3 — Operator signoff gates

**New field:** `TicketTask.signoff: Literal["none", "operator"] = "none"` (`models.py:268`). Set via `cw dev-queue add --signoff operator` or a per-lane `default_signoff: operator` in `clients.yaml`.

**Enforcement:** `apply_staged_decision` (`dispatch.py:1324`) checks `task.signoff == "operator"` when the incoming status is a review-completion signal (i.e., the stage would advance past REVIEW with a draft PR present). Instead of advancing to FINALIZE, route `task.status = "awaiting_operator_signoff"` — a new entry in `PAUSED_FOR_USER_INPUT_STATUSES` (`auto_dev_result.py:104`).

**Clearance:** `cw dev-queue approve <ticket>` (existing command, `cli/dev_queue.py:167`) gains an `awaiting_operator_signoff` arm: flips the draft PR to ready (via `gh pr ready`) and sets `status = PENDING` at stage FINALIZE. This composes with RFC 0005 C3 (#621, draft PR opened at REVIEW end) and C2 (#622, FINALIZE scrub + draft→ready + guard): the operator gate sits between C3's draft-PR creation and C2's ready-flip, making the finalize guard redundant for signoff-gated tickets.

The gate is code-enforced in `apply_staged_decision`, not a prompt instruction. This is the #926 lesson: operator flags must be respected regardless of worker classification. A `--scope large` ticket with `signoff=operator` will always park before ship — the plan-approval gate (`plan_pending_approval`) continues to operate independently for scope classification.

### W4 — Worker-context correctness

Data-integrity floor: the board is only as trustworthy as the state workers write.

**#952 — zero-comment context.** `local_runner.build_task_message` is NOT the cause — it is the LocalExecutor/aider prompt builder and does not sit on the #949 claude-session execution path this bug travels. Nor does `.cw/context.json` have any Python writer: it is a bash heredoc the worker fills per the prose instructions in `auto-dev-intake.md` Step 0d. The real defect is **staleness**, not a fetch bug. A plan-round re-dispatch invokes `/auto-dev-plan` standalone, and that command only re-materializes context when `.cw/context.json` is absent — never when it is stale. So the #837 requeue re-fetch guard, which lives entirely in intake Step 0d, is skipped on plan-round re-dispatches, and comments posted after Stage 0 (e.g. operator "Pre-flight Resolutions") never reach the plan stage. Fix: plan Stage 1 now live-fetches the ticket comments on every invocation and pins the Step 1b marker grep to that live fetch (not the cached `comments` array); intake Step 3 now requests `comments`; and a `session.needs_attention` WARN fires when the comments fetch fails.

**#953 — empty ambiguity items.** Add a `field_validator` on `AutoDevResult.ambiguities` (in `auto_dev_result.py`) rejecting items where `question` is null or empty. An ambiguity without a question is unresolvable by construction; it must never reach `ambiguities_pending_resolution`.

**#536 Phase 1 — `cw result emit`.** New strict CLI command: `cw result emit --status <status> --pr <url> …` writes the full `AutoDevResult` directly to `session.last_result` in cw state (accepted design: Design A from the #536 comment, 2026-07-01). Validates hard at the callback boundary via `cw result validate` (`result.py:69`); returns actionable errors for in-turn correction. Eliminates the emit→record window that produces the `needs_salvage` class (reconcile's `_has_terminal_sentinel` at `reconcile/_shared.py:1010` begins reading from direct-write path). Transcript sentinel demoted to forensic fallback.

## Phasing

| Phase | Contents | Gate |
|---|---|---|
| **1 — Data correctness** | #929 (pr_state hydration + pr.* emit), #952 (zero-comment fix), #953 (empty-ambiguity validator); #536-P1 (`cw result emit`) in parallel | Board renders accurate data; no silent freeze for open PRs |
| **2 — Board consolidation** | Extend `board.py` with PR/CI column, AWAITING SIGNOFF cell, session age, attention badges; `tui.py` toggle panel; #854 dispatch.tick suppression | `cw board` replaces `cw dev-queue status` as primary read surface |
| **3 — Signoff gates** | `TicketTask.signoff`, `apply_staged_decision` gate, `awaiting_operator_signoff` status, `cw dev-queue approve` clearance; compose with RFC 0005 C3/C2 (#621/#622) | `--signoff operator` reliably parks before ship, code-enforced |
| **4 — Push + deprecation** | #930 (GitHub Actions → relay → `POST /pr-event`); freshness-gate `session.needs_attention`; `cw orchestrate watch` deprecation notice | Zero-latency PR signal; `tui.py` marked deprecated, removed in following release |

Phase 1 is the prerequisite for Phase 2 (board accuracy) and Phase 3 (correct gate state). Phases 2 and 3 are independent and can ship in either order.

## Open questions — RESOLVED (operator, 2026-07-03)

1. **Signoff default granularity.** ~~Per-ticket, per-lane, or global?~~
   **Resolved: all three levels ship**, hierarchy `ticket > lane > global`
   (per-ticket `--signoff` at enqueue overrides `default_signoff` in
   `clients.yaml`, which overrides the `orchestrator.yaml` flag). Most
   specific wins.
2. **Board web variant.** ~~In-scope for Phase 4 or a follow-on?~~
   **Resolved: follow-on RFC.** This sprint stays terminal-only; file a
   separate ticket/RFC for the SSE-consuming HTML page after Phase 4.
3. **`cw status` / `cw list` fate.** ~~Aliases or separate?~~
   **Resolved: keep plain-text paths** for scripting; `cw board` becomes the
   interactive default read surface.
4. **`tui.py` migration completeness.** ~~Full parity vs. partial + flag?~~
   **Resolved: neither — a usefulness bar, not a parity bar.** Port the
   features actually in operator use, delete the rest (git history is the
   archive). Additionally: session-grouping logic must not live in a display
   module — extract it to a non-display module (e.g. alongside the reconcile
   or models layer) as part of the Phase 2 port. Assess the keep-list at
   Phase 2 entry.

## References

- `src/cw/board.py` — 277 lines, `cw board`, RFC 0005 D1 live cockpit
- `src/cw/tui.py` — 837 lines, `cw orchestrate watch/status`
- `src/cw/models.py:207–211` — `OrchestratorEventType` pr.* enum (zero emit sites)
- `src/cw/models.py:268` — `TicketTask` (new fields: `pr_state`, `signoff`)
- `src/cw/models.py:324` — `TicketTask.pr_url` (existing; pr_state lands here)
- `src/cw/dispatch.py:1324` — `apply_staged_decision` (W3 gate enforcement point)
- `src/cw/auto_dev_result.py:70–104` — `PAUSED_FOR_USER_INPUT_STATUSES`, status set
- `src/cw/auto_dev_result.py` — ambiguities validator target (#953)
- `src/cw/cli/dev_queue.py:167` — `cw dev-queue approve` (W3 clearance)
- `src/cw/result.py:69` — `cw result validate` (reused by W4 `cw result emit`)
- `src/cw/reconcile/_shared.py:1010` — `_has_terminal_sentinel` (W4 write path)
- `src/cw/orchestrate.py:259` — `retire_merged_prs` (begins firing after W2)
- `cw_pr_events_server.py` — RFC 0002 Phase 3 receiving half (W2 push target)
- Issues: #929, #930, #940, #952, #953, #536, #854, #926, #621, #622, #813, #824
