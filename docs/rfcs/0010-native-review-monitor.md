# RFC 0010 — Native Review-Monitor (PR-attention reactor on the cw event bus)

| Field | Value |
|-------|-------|
| Status | Accepted |
| Owner | Matt Warren |
| Date | 2026-07-09 |
| Supersedes | — |
| Related | RFC 0006 (review system in cw), RFC 0008 (orchestrator push channel), RFC 0009 (gate-recipe automation), ADR-0006 (reap-policy fail-safe) |

## Summary

PR-attention monitoring today lives in a **global** Claude-Code skill — `global-claude/commands/review-monitor.md` (945 lines) over a `global-claude/scripts/review_monitor.py` state machine (**2812 lines**) — with its own JSON state file, run by a human typing `/review-monitor`. cw is only *loosely* wired to it: the `cw-orchestrator` agent (`.claude/agents/cw-orchestrator.md`, 24 lines) reacts to `pr.ci_failed` / `pr.review_received` webhook events by **re-dispatching the entire ticket** to auto-dev — no attention-state nuance, no role-aware action.

This RFC makes PR-attention monitoring a **first-class cw concern**: a config-gated **detect→act reactor** — modeled directly on RFC 0009's `gate_recipes.py` — that reads the attention state cw *already computes* (`pr_hydrate._compute_attention_state`), maps each state to a native cw action, emits an auditable event, and runs on the same reconcile tick as the other reactors. It is fed by the `pr.*` event bus that already exists (`pr_hydrate._diff_transitions` emits it; the operator channel already forwards it).

**Scope (resolved, operator decision 2026-07-09):** native review-monitor covers **only auto-dev-originated PRs** — the set `pr_hydrate.hydrate_pr_states()` already tracks (dev-queue tasks with a `pr_url`). Arbitrary registered/human-authored PRs stay on the global skill and are a candidate for a *later* RFC. This confines the sprint to the reactor + action layer and explicitly excludes the net-new PR-tracking store (`register`/`discover`) that is the bulk of `review_monitor.py`.

The wave metric this targets: **mandatory operator touches for PR monitoring → 0** for auto-dev PRs whose attention state maps to a mechanical action, leaving human moments only for genuine merge blocks and product/scope forks.

## Motivation

**The monitoring touch is mechanical and already half-automated.** When an auto-dev PR goes CI-red, gets changes requested, or loses its reviewer, the operator's response is a fixed function of the PR's state — the same "predicate, not a decision" pattern RFC 0009 removed for approval gates. cw already *detects* these transitions and emits `pr.ci_failed` / `pr.review_received` / `pr.mergeable` / `pr.merged` (`OrchestratorEventType`, `models.py:214-218`); what's missing is a native *reactor* richer than the coarse whole-ticket redispatch.

**The attention logic is already ported into cw — a native reactor should call it, not reimplement it a third time.** `pr_hydrate._compute_attention_state` (`pr_hydrate.py:97-143`) is an *adapted* port of `review_monitor.py`'s attention derivation (its docstring, `pr_hydrate.py:108-111`, notes it deliberately drops the original's role/status/unaddressed-count/comment-review inputs — no cw subsystem exists for them yet). Its sibling `_summarize_status_checks` (`pr_hydrate.py:55-94`) *is* a verbatim port. The precedence chain it returns (7 effective rows) is:

```
draft            → None                    (pr_hydrate.py:126, drafts never escalate)
merge_blocked    → "merge_blocked"         (:128, _ROW1_MERGE_BLOCKING_STATES)
ci_failing       → "ci_failing"            (:130, not ci_ok)
changes_requested→ "changes_requested"     (:132, review_decision == CHANGES_REQUESTED)
no_reviewer      → "no_reviewer"           (:134, REVIEW_REQUIRED and reviewer_count == 0)
BLOCKED sub-rows → _blocked_attention_state(...)  (:139-143 → :146-157, rows 5a/5b/5c)
otherwise        → "ready_to_approve"      (:143)
```

**The bus and the poller already exist.** `pr_hydrate.hydrate_pr_states()` (`pr_hydrate.py:505-528`) polls `load_dev_queue().tasks` filtered by `_is_candidate` (`pr_hydrate.py:257-261`), and `_diff_transitions` (`pr_hydrate.py:207-254`) emits the `pr.*` events on state change. A separate webhook path — `cw_pr_events_server.py` (`POST /pr-event`, `_VALID_EVENT_TYPES` at `:40`, HMAC at `:241-246`) — dual-writes the same state via `observe_pushed_event` (`:258-270`). Both feed the same attention state; this RFC adds a *consumer*, not a new bus.

**There is a proven detect/act template to copy.** RFC 0009's `gate_recipes.py` (820 lines) runs config-gated `_detect_*` (pure) / `_act_*` (mutating) pairs — `_detect_auto_approve_review` (`gate_recipes.py:367-414`) / `_act_auto_approve_review` (`:580-684`) — combined in `run_gate_recipes` (`:785`), each gated by `resolve_gate_recipe_enabled` (`:95-99`, 3-tier ticket→lane→floor precedence at `:114-129`) under a master `gate_recipes_enabled=False` opt-in (`models.py:882`), emitting its evidence event before mutating. It is called from the reconcile tick at `reconcile/core.py:104`, one line after `run_concierge_recoveries` (`:103`). RFC 0010 is the same shape applied to PR-attention states instead of approval gates.

## Design

### W1 — The `review_recipes` module

New `src/cw/reconcile/review_recipes.py`, structured 1:1 on `gate_recipes.py`:

- A frozen `ReviewRecipeCandidate` dataclass (mirror `GateRecipeCandidate`): `ticket_id, client, lane, recipe, attention_state, pr_url, evidence, session_id`.
- Recipe-name constants, one per actionable attention state (W2).
- One `_detect_*` / `_act_*` pair per recipe (pure classification vs mutating action under `dev_queue_lock()`).
- A `run_review_recipes(*, now, config)` entry point called from `reconcile/core.py`, in the same tick that already runs `run_gate_recipes` and `run_concierge_recoveries`.

**Detect scope:** `TicketTask` rows that `_is_candidate` accepts (a `pr_url` + non-terminal `pr_state`), whose freshly-hydrated `pr_state` yields a non-`None` attention state, on a lane where the matching recipe is enabled (W3). The reactor reads the attention state cw already computed during `hydrate_pr_states` — it does **not** re-poll GitHub itself.

### W2 — The attention-state → action recipes

Each non-draft attention state maps to exactly one native action. **A recipe fires only when (a) the attention state matches and (b) its per-lane flag is enabled.** Draft PRs (`_compute_attention_state` → `None`) are never candidates — inherited for free.

| Attention state | Recipe | Native action |
|-----------------|--------|---------------|
| `ci_failing` | `auto_fix_ci` | Dispatch a scoped auto-fix — re-enter the ticket's auto-dev at the fix stage. (Distinct from the coarse whole-ticket redispatch; see W6.) |
| `changes_requested` | `address_review` | Dispatch the vendored `address-review` skill (W5) — targeted "resolve the review feedback," not a full redispatch. |
| `no_reviewer` | `request_reviewer` | Request a reviewer on the PR (`gh pr edit --add-reviewer`), emit evidence, no ticket state change. |
| `ready_to_approve` | *(delegated)* | **No new recipe.** This is exactly RFC 0009's `auto_approve_clean_review` gate — do not duplicate the auto-approve path. W6 documents the seam. |
| `merge_blocked` / BLOCKED sub-rows | `escalate_merge_block` | Park + escalate to the operator (a merge block on an auto-dev PR is rarely mechanically fixable — treat as human-gated, RFC-0009-latch precedent). |

Following RFC 0009's "config toggles enablement, code owns criteria" split: the **state→action mapping is hardcoded**, not config-tunable. Config decides *whether* a lane auto-acts on `changes_requested`; the code decides *what* acting means. Tightening/loosening an action is a reviewed code change, not a silent config widening of an auto-actor.

Act phase, per firing candidate: re-load state under `dev_queue_lock()`, re-validate the attention state against the current `pr_state` (guard against a race with a human who acted, or a newer CI run that went green), **emit the W4 evidence event, then perform the action.** A state that no longer holds on re-check is skipped silently (no event, no action) — the `gate_recipes` act-phase re-validation pattern (`gate_recipes.py:580-684`).

> **Deadlock note (inherited from RFC 0009 errata):** any action that mutates dev-queue state must call a **lock-free** helper from inside the recipe's own `dev_queue_lock()` — never the public wrapper that re-acquires the same `flock` (self-deadlocks, `gate_recipes.py` uses `_approve_ticket_locked` for exactly this).

### W3 — Per-lane config gate

Reuse the exact RFC 0009 plumbing rather than invent a parallel one. Two viable shapes (Open Question 1):

- **(a) Extend the existing `gate_recipes` map** — `LaneConfig.gate_recipes` (`models.py:607`) and `TicketTask.gate_recipes` (`models.py:472`) already exist; add the review-recipe names as keys and reuse `resolve_gate_recipe_enabled` (`gate_recipes.py:95-99`) verbatim. Fewer moving parts; but conflates approval-gate enablement with PR-action enablement under one map.
- **(b) A sibling `review_recipes: dict[str,bool] | None` field** on `LaneConfig` + `TicketTask` with a `resolve_review_recipe_enabled` mirroring the 3-tier `resolve_gate_recipe_enabled` precedence (ticket → lane → hardcoded-off floor), under a master `OrchestratorConfig.review_recipes_enabled: bool = False` (mirror `gate_recipes_enabled` at `models.py:882`).

Both keep the **default-off floor** — a fresh install auto-acts on nothing until a lane opts in — and the master opt-in short-circuit.

> **Resolved (operator, 2026-07-09): (b) — the sibling `review_recipes` map.** PR-action risk is a different risk class than approval-gate risk (dispatching an auto-fix vs clearing a ship gate), and an operator will want to arm them independently. Add `review_recipes: dict[str,bool] | None` to `LaneConfig` and `TicketTask`, a `resolve_review_recipe_enabled` mirroring `resolve_gate_recipe_enabled`'s 3-tier precedence (`gate_recipes.py:95-99,114-129`), and a master `OrchestratorConfig.review_recipes_enabled: bool = False` mirroring `gate_recipes_enabled` (`models.py:882`). This matches RFC 0009's own reasoning for choosing per-lane `resolve_signoff`-style plumbing over the flat `concierge_recoveries` dict.

### W4 — Evidence & operator visibility

New `OrchestratorEventType.PR_ACTION_TAKEN` (and a `PR_ACTION_FAILED` sibling, mirroring `GATE_AUTO_APPROVED` / `GATE_AUTO_APPROVE_FAILED` at `models.py:654,659`), emitted by the act phase **before** the mutation, `correlation_id = ticket_id`, payload `{client, lane, recipe, ticket_id, pr_url, attention_state, session_id, evidence_snapshot}` where `evidence_snapshot` records the attention-state fields that licensed the action (audit: *why* it fired).

Add both to `_DEFAULT_OPERATOR_EVENT_TYPES` (`models.py:633-661`) — which already forwards all five `pr.*` types + `SESSION_NEEDS_ATTENTION` (`:637`) + `GATE_AUTO_APPROVED` (`:654`) — so an autonomous PR action is something the operator *sees* on the `cw-operator` channel, consistent with RFC 0008/0009.

### W5 — First native reaction skill: vendor `address-review`

`global-claude/skills/address-review.md` (50 lines) already documents itself as "Invoked by the cw orchestrator daemon when `pr.review_received` is detected" — it is the closest existing seam and the smallest slice that proves the `changes_requested` → native-cw-action path end-to-end. **P1 vendors it into `.claude/skills/` and wires it as the `address_review` recipe's action**, before any of the richer recipes. This is the incremental "one slice, end-to-end" opening move.

### W6 — Interaction with the existing coarse consumers

Two existing consumers overlap and must be reconciled, not left to double-fire:

- **`cw-orchestrator` agent** (`.claude/agents/cw-orchestrator.md:13-18`) currently maps `pr.ci_failed` and `pr.review_received` to an identical whole-ticket `cw dev-queue add … && run --once`. Once `review_recipes` handles these states natively, the agent's coarse rows are **superseded** for auto-dev PRs.
  > **Resolved (operator, 2026-07-09): retire them.** In P4, remove the `pr.ci_failed` / `pr.review_received` rows from the agent's decision table for auto-dev PRs — `review_recipes` owns those states. During the transition, a double-fire guard (e.g. the recipe is authoritative when `review_recipes_enabled` is on for the lane; the agent row is a no-op) prevents a single `pr.review_received` from triggering both a coarse redispatch *and* the native `address_review` recipe. The `pr.merged` → `cw orchestrate retire` and `pr.mergeable` → log-only rows stay (no `review_recipes` overlap).
- **RFC 0009 `auto_approve_clean_review`** owns the `ready_to_approve` terminal state. `review_recipes` must **not** add an approve action — it detects `ready_to_approve` only to *hand off* (or no-op), never to double-clear. Document the boundary in both modules.

### Explicitly out of scope (Option A boundary + net-new deferrals)

- **Arbitrary / human-authored PRs** — the `register`/`discover` PR-tracking store (`review_monitor.py`'s bulk). Auto-dev PRs only this sprint.
- **Thread-level state** — individual review-thread tracking, `delta_base_sha` delta-review baselines, `ack-delta`/`confirm-thread` — the majority of `review_monitor.py`'s 2812 lines. `_compute_attention_state` already *dropped* these inputs (`pr_hydrate.py:108-111`); this RFC does not restore them.
- **Outbound nudge/DM/channel-bump drain queue** — the operator channel is push-only observability, not an outbound-message queue. Auto-actions here mutate cw state or the PR directly; they do not send nudges.
- **Making the state→action mapping configurable** — see W2.

## Phasing

| Phase | Contents | Gate |
|-------|----------|------|
| P1 | Vendor `address-review.md` (W5); `review_recipes.py` skeleton + `ReviewRecipeCandidate` + `run_review_recipes` wired into `reconcile/core.py` behind `review_recipes_enabled=False`; `address_review` recipe (detect `changes_requested` → dispatch vendored skill) | Unit: detect returns candidates only for `changes_requested` on candidate tasks; disabled master switch = no-op; draft PR never a candidate |
| P2 | `PR_ACTION_TAKEN` / `PR_ACTION_FAILED` events + operator-channel forward; act-phase re-validation + lock-free mutation | Tests: event emitted before mutation; stale attention state (green on re-check) skips silently; no self-deadlock under `dev_queue_lock()` |
| P3 | `W3` config plumbing — `review_recipes` per-lane map + `resolve_review_recipe_enabled` 3-tier precedence + master switch; docs in `config/CONFIG_REFERENCE.md` | Tests: ticket>lane>floor resolution; default-off |
| P4 | Remaining recipes: `auto_fix_ci`, `request_reviewer`, `escalate_merge_block`; W6 reconciliation (narrow/retire the coarse `cw-orchestrator` rows) | Tests: each attention state routes to exactly one recipe; `ready_to_approve` adds no approve action; no double-fire with the coarse agent |
| P5 | Migrate the `global-claude/wiki/review-monitor.md` operational lessons (60+ dated entries — race conditions, baseline anchoring, draft guards) into the cw domain so the port does not re-learn them | Review: lessons that still apply are captured as code comments / tests / docs |

## Open questions

1. ~~**Config surface (W3): reuse `gate_recipes` map or add a sibling `review_recipes` map?**~~ **Resolved (operator, 2026-07-09):** sibling `review_recipes` map — independent risk class, independent arming. See W3.
2. **`auto_fix_ci` action semantics.** Is "re-enter auto-dev at the fix stage" a real entry point, or does cw only support whole-ticket redispatch today? If the latter, `auto_fix_ci` degrades to the coarse behavior until a scoped-fix entry point exists — decide whether to ship the coarse version behind the gate or defer `auto_fix_ci` past P4.
3. ~~**Fate of the coarse `cw-orchestrator` rows (W6).**~~ **Resolved (operator, 2026-07-09):** retire the `pr.ci_failed` / `pr.review_received` rows for auto-dev PRs in P4; keep a double-fire guard during the transition. See W6.
4. **`ready_to_approve` handoff (W6).** Does `review_recipes` ignore `ready_to_approve` entirely (let RFC 0009 own it), or emit an informational-only event so the operator sees "PR reached ready_to_approve" even on a lane where `auto_approve_clean_review` is disabled?
5. **Small-scope PRs never park.** Per the RFC 0009 dogfood finding, Small tickets auto-advance and never park at approval gates (`dispatch.py:1790,1894`). Does the same "small auto-advances" reality bypass any PR-attention state a recipe would act on, and if so which recipes only ever see Large-scope PRs? Validate the candidate set during P1.
6. **`evidence_snapshot` retention.** Is the snapshot in the `PR_ACTION_TAKEN` payload sufficient audit, or should the action also leave a PR comment (parity with a human acting leaving a trail)?

## References

- `src/cw/pr_hydrate.py:97-143` — `_compute_attention_state`, the reused attention derivation (precedence `:126-143`; BLOCKED sub-rows via `_blocked_attention_state` `:146-157`)
- `src/cw/pr_hydrate.py:55-94` — `_summarize_status_checks` (verbatim port of `review_monitor.py`)
- `src/cw/pr_hydrate.py:257-261` — `_is_candidate` (the auto-dev-PR candidate set, structurally)
- `src/cw/pr_hydrate.py:207-254` — `_diff_transitions` (emits `pr.*` on transition)
- `src/cw/pr_hydrate.py:505-528` — `hydrate_pr_states` (polls `load_dev_queue().tasks`)
- `src/cw/reconcile/gate_recipes.py:367-414,580-684,785,95-99,114-129` — the detect/act/entry/resolve template to mirror
- `src/cw/reconcile/core.py:103-104` — the reconcile tick that runs the reactors
- `src/cw/models.py:214-218` — `OrchestratorEventType` `pr.*` members
- `src/cw/models.py:633-661` — `_DEFAULT_OPERATOR_EVENT_TYPES` (operator-channel forward set)
- `src/cw/models.py:607,472,882` — `LaneConfig.gate_recipes`, `TicketTask.gate_recipes`, `gate_recipes_enabled` (config plumbing to mirror)
- `src/cw/cw_pr_events_server.py:40,241-246,258-270` — webhook path, HMAC, dual-write to `observe_pushed_event`
- `.claude/agents/cw-orchestrator.md:13-18` — the coarse consumer to reconcile (W6)
- `global-claude/skills/address-review.md` — the first native reaction skill to vendor (W5)
- `global-claude/commands/review-monitor.md` (945), `global-claude/scripts/review_monitor.py` (2812), `global-claude/wiki/review-monitor.md` — the global source being partially ported

Issues: (to be filed per phase)
