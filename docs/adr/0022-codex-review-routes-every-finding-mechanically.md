# Codex REVIEW gives every finding a terminal disposition by mechanical routing

**Status:** Accepted
**Driven by:** #2464 (slice 1: disposition accounting)

## Decision

The codex REVIEW path has no coordinating session, so it cannot exercise the
Claude-native Checkpoint 3a judgment (FIX NOW / REJECT / DEFER / OPERATOR
ACTIONABLE). Instead, `cw.codex_review._verdict._route.route_codex_dispositions`
applies the parts of that contract that are decidable from a finding's own
fields and the plan text, on every codex-synthesized pass (cycle 0, each
fix-loop re-review, and the Claude confirm round), after every suppression seam
and before the block-reason table:

| Finding (still at the `"fixed"` default) | Disposition | Pass outcome |
|---|---|---|
| `plan_requirement_quote` found verbatim in `.cw/plan.md`, any severity | `operator_actionable`, detail `plan_deviation: …` | blocked / `plan_deviation` |
| MUST_FIX with `no_diff_anchor` | `operator_actionable`, detail `no_diff_anchor: …` | blocked / `review_operator_actionable` |
| any other SHOULD_FIX | `deferred`, detail `auto_deferred: …` | unchanged (ships; ticketed on merge) |
| any other MUST_FIX | `fixed` (unchanged) | fix loop or `codex_must_fix_findings` |

The SHOULD_FIX default was an operator decision (2026-11-05): auto-defer and
ticket, rather than feed the fixer or park.

## Invariant

1. Every accepted MUST_FIX/SHOULD_FIX on the codex path ends *fixed* (by the
   fix loop or a park that names it), *deferred and recorded* in
   `.cw/deferred-findings.md`, or *handed to the operator* through a
   `plan_deviation` / `review_operator_actionable` park. None passes silently
   at the optimistic `"fixed"` default.
2. An operator-routed finding never reaches the autonomous fixer. The fix loop
   does not enter on an operator-routed park, even when sibling MUST_FIX
   findings keep `blocking` True, and a re-review that surfaces one parks the
   loop before another fix cycle.
3. A finding someone already decided (an operator void, a ledger
   adjudication, a settled suppression) is never re-routed.
4. A plan-deviation claim is trusted only when its quote is a clause of the
   plan (whitespace-collapsed substring, at least 8 characters). An
   unverifiable quote is logged and the finding routes as ordinary, so a
   reviewer cannot park a pass by inventing a plan clause.
5. Deferrals are recorded after every codex pass, not only from the terminal
   verdict: a fix-loop re-review covers only its delta, so the terminal
   verdict alone would lose cycle 0's deferrals. The record is written before
   the result door, so FINALIZE is never dispatched ahead of it.

## What this means for callers

- Dispatch routes both new codex parks to `BLOCKED_ON_USER` through the generic
  rules: the reason strings are the Claude-native Stage 3 exit reasons
  verbatim (`plan_deviation`, `review_operator_actionable`), so an
  orchestrator reads one vocabulary whichever backend reviewed.
- `cw dev-queue must-fix-override` and the confirm-feedback re-entry key on
  `codex_must_fix_findings` and so do not apply to an operator-routed park; the
  operator settles the finding or changes the plan, then requeues REVIEW.
- FINALIZE (`auto-dev-finalize.md` Step 4d) already copies
  `.cw/deferred-findings.md` into the PR body for any backend; the codex path
  is now a producer of it.
- The clean-mode ledger now records the router's `auto_deferred:` rationale for
  a codex-synthesized SHOULD_FIX, where it previously recorded the round's own
  (`decide_verdict` passes an already-deferred finding through unchanged).

## What this means for producers

- `cw.review_adjudication.write_deferred_findings` is the one writer of
  `.cw/deferred-findings.md`; `cw review adjudicate --deferred-findings-out`
  and the codex recorder both call it, so round stamping, projection and
  content dedup are identical. This makes the codex path the second consumer
  of the deferred-findings seam that `cw.review_adjudication`'s package
  docstring said would need an ADR. It reuses the **writer and the artifact**,
  not `apply_adjudication`: the codex fix loop's capped-survivor state has been
  `"unresolved"` since #2352, so `"deferred"` now means the same thing on both
  paths ("may ship un-fixed, ticketed on merge"), which is what makes sharing
  the artifact sound.
- `.cw/deferred-findings.md` is exempt from the fix loop's clean-start check
  and unstaged from every cycle commit (`cw.codex_fix_loop.baseline`, the #2689
  mechanism), so a worktree whose `.cw/` exclude registration failed still
  never parks `codex_fix_dirty_start` on it.
- A reviewer field added to `Finding` that the router reads must default to
  blank and carry a `None` normalizer (the strict-schema contract pinned by
  `tests/test_openai_strict_schema.py`), as `plan_requirement_quote` does.

## Consequences

- A codex pass with a fixable MUST_FIX and an operator item no longer fixes the
  former first: it parks immediately, and the fix loop runs after the operator
  resolves the item and requeues. This spends an operator round-trip to keep
  the fixer away from findings it must not touch; the Claude path, which can
  carry the decision across cycles in its `ADJUDICATIONS` array, still fixes
  first.
- A SHOULD_FIX is never fixed by the codex path. Lanes that want SHOULD_FIX
  fixed use the Claude-native review.
- Plan-deviation detection depends on the reviewer quoting the plan. A
  deviation the reviewer does not flag routes as an ordinary finding, which is
  no worse than before.
- Out-of-plan-scope findings (`in_plan_scope: false`, Checkpoint 3a (4d)) are
  not routed here: deciding DEFER versus a `## Decisions` carve-out needs plan
  reading this slice does not attempt. They keep their pre-#2464 behavior.

## Alternatives considered

- **Feed SHOULD_FIX to the fixer when the loop is enabled.** Rejected for this
  slice by the operator: it widens the fix loop's scope (growth guards, cycle
  budget) and is independent of accounting.
- **Park on any SHOULD_FIX.** Rejected: it would park most codex reviews.
- **Let the fix loop fix siblings before parking on an operator item.** Rejected:
  a delta-only re-review never re-raises an unchanged operator item, so the
  loop would need cross-cycle carry state, and the pass parks regardless.

## Referenced by

- #2464, #1817, #2352, #2689, ADR-0015
