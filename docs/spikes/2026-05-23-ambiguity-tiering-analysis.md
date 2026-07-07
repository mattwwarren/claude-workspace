# Analysis: Pre-scan + Decisions Doesn't Pre-empt Plan-time Emergent Ambiguities

**Date:** 2026-05-23
**Provenance:** promoted from issue #210 (a discussion ticket, closed during the 2026-07-07 ticket audit). The rationale below is largely **embodied by the shipped `harden-ticket` skill** (per-ticket resolve-upfront) and the Decisions pattern; kept here as the record of *why* that shape was chosen. The related three-axis scoring proposal is still tracked as open work in #211.
**Status:** Rationale — superseded in practice by `harden-ticket`.

## Observation

In the 2026-05-23 dogfood wave, every ticket dispatched after a complete pre-scan +
Decisions reply *still* emerged from `/auto-dev` Stage 1c with **another batch** of
ambiguities (typically 5–7 items). #177 was the first example: orchestrator-side pre-scan
caught 4 structural items, the user dispositioned them, and the Plan agent then surfaced 6
more — wholly different in shape.

| Layer | What it catches |
|---|---|
| Pre-scan (orchestrator-side) | CLI shape, predicate choice, error-on-miss, file placement — *"what should the API look like"* |
| Plan agent Stage 1c | Return type, exact error wording, flag-vs-subcommand, order-of-operations, choice-set membership — *"what should the implementation detail look like"* |

The two layers catch genuinely different things — pre-scan can't see the Stage-1c set
because it emerges during plan generation, not from reading the ticket. So
"pre-scan → Decisions → dispatch" doesn't single-round-trip; it adds a third round.

## Why it mattered

- **Round-trip cost.** Each emerged-ambiguity batch is another disposition + re-queue +
  dispatch + re-plan. For wholesale-acceptable batches this is pure overhead.
- **Defaults are usually fine.** The `wholesale-accept` reply pattern suggests plan-picked
  defaults are usually adequate; it's the *opt-out* path that was missing.
- **The contract treated all ambiguities the same** — architectural ("raise vs return")
  and cosmetic ("exact error string") shared one gate.

## Directions considered (at the time)

1. **Pre-flight contract change** — `Decisions:` blocks carry a wildcard
   `implementation=wholesale-accept-plan-defaults`; Plan skips the emerging-ambiguity gate.
2. **Tier ambiguities** — Plan classifies emerged ambiguities `architectural` vs `cosmetic`
   and only halts on architectural.
3. **Status quo + auto-accept skill** — a skill that posts the wholesale-accept reply and
   re-queues.
4. **Accept the round-trip** as the cost of safety.

## Outcome

The project converged on **resolve-upfront**: the `harden-ticket` skill sweeps a ticket for
implementation-determining ambiguities *before* dispatch and posts a single Pre-flight
Resolutions comment, so the worker passes plan review on the first try — collapsing the
multi-round-trip this ticket described. The finer **three-axis (Reversibility / Blast /
Convention) auto-tiering** idea remains open design in #211.
