# RFC 0009 — Gate-Recipe Automation (auto-approve clean plans & reviews)

| Field | Value |
|-------|-------|
| Status | Accepted |
| Owner | Matt Warren |
| Date | 2026-07-08 |
| Supersedes | — |
| Related | RFC 0006 (review system), RFC 0007 (sprint observability & operator gates), RFC 0008 (orchestrator push channel), ADR-0006 (reap-policy fail-safe) |

## Summary

The auto-dev pipeline pauses Small/Medium tickets at two operator-approval gates that, in practice, an operator clears **mechanically** — without adding judgment — whenever the pipeline's own structured evidence already says "clean":

1. `review_pending_approval` — the Stage 3 review passed with zero must-fixes, zero deferrals, a `PROCEED` health recommendation, and no forbidden-area edits, yet the ticket still parks for a human to click approve.
2. `plan_pending_approval` — the Stage 1 plan cleared both review stations with no MUST_FIX, yet still parks for a human to adopt it.

This RFC adds **config-gated "gate recipes"** — a detect→act pattern modeled directly on `concierge.py` — that auto-clear exactly these two gates when a fixed, evidence-derived safety predicate holds, emit an auditable event, and defer to the `#1015` escalation latch as a backstop. The wave metric this targets: **mandatory operator touches → 0 for Small/Medium tickets** whose plan and review are clean, leaving human moments only for genuine product/scope forks.

Explicitly *not* in scope: `AWAITING_OPERATOR_SIGNOFF` (the RFC 0007 W3 ship checkpoint — a deliberate human-eyes gate), and the judgment pauses `ambiguities_pending_resolution` / `premises_pending_verification`. Those keep parking for a human.

## Motivation (2026-07-08 recovery-gap wave evidence)

**The clean-review approval this wave was purely mechanical.** #1030's `review_pending_approval` gate was cleared by the operator against exactly four structured fields — `must_fix_initial=0`, `deferred=0`, `recommendation=PROCEED`, `forbidden_touched=false` — with no added human judgment. That is a predicate, not a decision.

**The evidence is already structured and already on `session.last_result`.** The AUTO_DEV_RESULT sentinel parses to typed Pydantic models at `src/cw/auto_dev_result.py`: `Review` (`auto_dev_result.py:285-289`) carries `must_fix_initial` / `deferred`; `Health` (`auto_dev_result.py:311-321`) carries `recommendation: Literal["PROCEED", "EXIT_FOR_HUMAN_REVIEW"]` (`:315`); `Scope` (`auto_dev_result.py:251-256`) carries `forbidden_touched`. The parsed dict lands on `session.last_result`, so a recipe reads `session.last_result["review"]["must_fix_initial"]`, `["health"]["recommendation"]`, `["scope"]["forbidden_touched"]` directly — no transcript parsing.

**The mechanical clear path already exists.** `approve_ticket()` (`dev_queue.py:849-970`) is what `cw dev-queue approve` invokes: it validates `task.status in _APPROVABLE_STATUSES` and `session.last_result["status"] in SCOPE_GATED_APPROVAL_STATUSES` (`dev_queue.py:924-935`), then advances the stage pointer (`_advance_task_pointer`, `dev_queue.py:952-959`). A recipe replaces only the *trigger* (its own detect predicate) — not the state transition — so it inherits every existing guard.

**There is a proven detect/act template to copy.** `concierge.py` runs config-gated recovery recipes as `_detect_*` (pure) / `_act_*` (mutating) pairs combined in `run_concierge_recoveries()` (`concierge.py:693-739`), each gated by `resolve_concierge_recipe_enabled()` (`concierge.py:119-130`) against a sparse override dict, under a master `concierge_enabled=False` opt-in (`models.py:805`), emitting its evidence event *before* mutating (`concierge.py:439-448`). RFC 0009 is the same shape applied to approval gates.

## Design

### W1 — The `gate_recipes` module

New `src/cw/reconcile/gate_recipes.py`, structured 1:1 on `concierge.py`:

- A frozen `GateRecipeCandidate` dataclass (mirror `ConciergeCandidate`, `concierge.py:133-156`): `ticket_id, client, lane, recipe, evidence, session_id`.
- Recipe-name constants: `RECIPE_AUTO_APPROVE_REVIEW`, `RECIPE_AUTO_ADOPT_PLAN`.
- One `_detect_*` / `_act_*` pair per recipe (pure classification vs locked mutation).
- A `run_gate_recipes(state, tasks, *, now, config)` entry point called from the same reconcile tick that already runs `run_concierge_recoveries()` and `run_escalation_sweep()`.

Detect scope: `TicketTask` rows in `QueueItemStatus.BLOCKED_ON_USER` whose `session.last_result["status"]` is in `SCOPE_GATED_APPROVAL_STATUSES` (`auto_dev_result.py:100-102`), on a lane where the recipe is enabled (W3).

### W2 — The two recipes and their fixed predicates

**`auto_approve_clean_review`** — candidates with `last_result["status"] == "review_pending_approval"`. Fires only when the predicate holds:

```
review.must_fix_initial == 0
  and review.deferred == 0
  and health.recommendation == "PROCEED"
  and scope.forbidden_touched is False
```

**`auto_adopt_clean_plan`** — candidates with `last_result["status"] == "plan_pending_approval"`. Fires only when the plan cleared both review stations cleanly: both signoff markers present (`plan-spec-reviewed`, `plan-soundness-reviewed`) and no MUST_FIX recorded for the plan. (The plan sentinel exposes the review outcome the same way; W2 implementation pins the exact field during the plan-of-record read.)
>
> **Errata (2026-07-08, OQ1 resolved):** the plan sentinel does **not** expose a structured plan-review field — `AutoDevResult.review` is hardcoded to zeros at plan-stage exit (`.claude/commands/auto-dev-plan.md:360`, schema filler). `auto_adopt_clean_plan` must read the two signoff markers from the plan-of-record (tracker comment via `gh.py:284`, or `.cw/plan.md`). This is fine: the markers are written *only* on a clean pass (`plan-spec-reviewed` on NO_ISSUES/SHOULD_FIX/PRINCIPLE-only `auto-dev-plan.md:272`; `plan-soundness-reviewed` on NO_ISSUES `:279`), so **both-present ⟺ the "no MUST_FIX, both stations clean" predicate**.

Act phase, for a firing candidate: re-load state under `dev_queue_lock()`, re-validate the predicate against the *current* `last_result` (guard against a race with a concurrent human approve / re-dispatch), **emit the W4 evidence event, then call `approve_ticket(ticket_id, client)`** — the identical mutation a human approve performs. A predicate that no longer holds on re-check is skipped silently (no event, no mutation).

> **Errata (2026-07-08, impl #1065):** calling the *public* `approve_ticket()` from inside the recipe's `dev_queue_lock()` **self-deadlocks** — `approve_ticket()` acquires the same `_lock()` internally (`dev_queue.py:876`; `dev_queue_lock` *is* `_lock`, `:209`), and two `flock` acquisitions on the same file from one process block forever. Correct implementation: extract a lock-free `_approve_ticket_locked(...)` from `approve_ticket`'s body (public `approve_ticket()` keeps wrapping it in `with _lock():`), and have the recipe act acquire the lock itself and call the lock-free helper — the pattern `concierge.py` `_act_*` (`:405/:564/:661`) already uses.

The predicate is **hardcoded**, not config-tunable (operator decision, 2026-07-08): config toggles *enablement*, code owns *criteria* — the concierge split. Tightening/loosening the predicate is a code change with a review, not a config knob that can silently widen a ship gate.

### W3 — Per-lane config gate

Add to `LaneConfig` (`models.py:546-570`):

```python
gate_recipes: dict[str, bool] | None = None
```

and a `resolve_gate_recipe_enabled(config, client, lane, recipe_name)` mirroring `resolve_signoff()`'s three-tier most-specific-wins precedence (`dispatch.py:1551-1578`): `TicketTask` override → `LaneConfig.gate_recipes` → `OrchestratorConfig` global default. A master `OrchestratorConfig.gate_recipes_enabled: bool = False` short-circuits the whole module (opt-in fail-safe, exactly like `concierge_enabled`). Default per-recipe map is all-`False` — a fresh install auto-approves nothing until a lane opts in.

This is deliberately the `resolve_signoff` (per-lane, 3-tier) plumbing rather than the flat global `concierge_recoveries` dict, because approval-gate risk is lane-specific: an operator may auto-approve on `default` but never on a lane touching shared infra.

### W4 — Evidence & operator visibility

New `OrchestratorEventType.GATE_AUTO_APPROVED`, emitted by the act phase **before** the `approve_ticket` transition (concierge ordering, `concierge.py:439-448`), `correlation_id = ticket_id`, payload: `{client, lane, recipe, ticket_id, session_id, predicate_snapshot}` where `predicate_snapshot` records the exact field values that licensed the auto-approve (audit: *why* it fired).

It is a **distinct** type from the human-path `TICKET_APPROVED` (`cli/dev_queue.py:204-213`) so audit trails separate "operator clicked approve" from "recipe auto-approved," and it is **forwarded to the operator channel by default** (add to `_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES`'s sibling forward set, `models.py:599-607`) — an auto-approval is something the operator should *see*, unlike the silent `CONCIERGE_RECOVERED`.

### W5 — Interaction with the #1015 escalation latch

The escalation latch (`reconcile/escalation.py`) parks eligible `BLOCKED_ON_USER` rows and fires `OPERATOR_ESCALATION` once after `ESCALATION_PARK_MINUTES = 45` (`escalation.py:47`); `plan_pending_approval` / `review_pending_approval` are in its eligible set. Its clear-site is `transition_task_status()` (`dev_queue.py:125-138`), which unconditionally nulls the latch fields on any status change.

Therefore a gate recipe interacts with the latch **for free**: because `run_gate_recipes` runs every reconcile tick (far inside the 45-minute window), a clean gate is auto-approved long before escalation would fire, and the `approve_ticket` → `transition_task_status` path resets the latch clock automatically. The latch remains the backstop for the case the recipe *cannot* act (predicate not met, recipe disabled): those still page at 45 min. The only race — a recipe acting in the same tick the 45-minute fire triggers — costs at most one redundant `OPERATOR_ESCALATION` page; acceptable, and no special suppression logic is added.

### Explicitly out of scope

- `AWAITING_OPERATOR_SIGNOFF` (RFC 0007 W3) — a deliberate ship checkpoint; auto-clearing defeats its purpose.
- `ambiguities_pending_resolution`, `premises_pending_verification` — judgment pauses; they park because a human answer changes the outcome.
- Making the safety predicate configurable — see W2.

## Phasing

| Phase | Contents | Gate |
|-------|----------|------|
| P1 | `gate_recipes.py` module skeleton, `GateRecipeCandidate`, `run_gate_recipes` wired into the reconcile tick behind `gate_recipes_enabled=False` | Unit tests: detect returns candidates only for in-scope statuses; disabled master switch = no-op |
| P2 | `auto_approve_clean_review` detect+act with the fixed predicate; `GATE_AUTO_APPROVED` event; operator-channel forward | Tests: predicate boundary (each field flipped independently blocks the fire); act calls `approve_ticket`; event emitted before transition |
| P3 | `auto_adopt_clean_plan` detect+act; plan-clean predicate | Tests: clean plan adopts, MUST_FIX plan does not |
| P4 | `LaneConfig.gate_recipes` + `resolve_gate_recipe_enabled` 3-tier precedence; docs in `config/CONFIG_REFERENCE.md` | Tests: ticket>lane>global resolution; default-off |

## Open questions

1. **Plan-clean predicate exact fields.** W2 pins `auto_approve_clean_review` precisely; the `auto_adopt_clean_plan` predicate is specified as "both signoff markers + no MUST_FIX" — confirm whether the plan sentinel exposes a structured `must_fix`/review-outcome field the recipe can read, or whether it must grep the persisted `.cw/plan.md` markers.
2. **`predicate_snapshot` retention.** Is the snapshot in the `GATE_AUTO_APPROVED` payload sufficient audit, or should the recipe also write it into the ticket as a comment (parity with the human approve leaving a trail)?
3. **Re-check window.** Is a single re-validate under `dev_queue_lock()` at act time enough, or should the recipe require the gate to have been stable for N seconds before firing (guard against approving a result a human is mid-review on)?
4. **Rollout lane.** Which lane opts in first for a dogfood — `default` at `max_parallel` with verified-clean tickets, or a dedicated low-risk lane?

## References

- `src/cw/reconcile/concierge.py:693-739` — `run_concierge_recoveries`, the detect/act template
- `src/cw/reconcile/concierge.py:119-130` — `resolve_concierge_recipe_enabled`, config-gate pattern
- `src/cw/reconcile/concierge.py:439-448` — evidence-event-before-mutation ordering
- `src/cw/auto_dev_result.py:100-102` — `SCOPE_GATED_APPROVAL_STATUSES`
- `src/cw/auto_dev_result.py:285-289, 311-321, 251-256` — `Review` / `Health` / `Scope` sentinel models
- `src/cw/dev_queue.py:849-970` — `approve_ticket`, the shared mutation
- `src/cw/dev_queue.py:97-158` — `transition_task_status` (event emit + latch clear)
- `src/cw/reconcile/escalation.py:47,73-88,105-149` — the #1015 escalation latch
- `src/cw/models.py:546-570` — `LaneConfig`
- `src/cw/dispatch.py:1551-1578` — `resolve_signoff`, the 3-tier precedence to mirror
- `src/cw/models.py:599-607` — operator-channel forward status set
- `src/cw/cli/dev_queue.py:204-213` — human-path `TICKET_APPROVED` (contrast)

Issues: #1015, #1020, #1030, #1032
