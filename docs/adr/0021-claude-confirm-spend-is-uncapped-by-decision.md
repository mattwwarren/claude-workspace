# The Claude confirm round's spend is uncapped by decision

**Status:** Accepted
**Driven by:** #2812 (follow-up to #2791)

## Decision

The Claude confirm round runs `claude -p` once per reviewer role under the
operator's own login, on the codex-lane clean-mode gate and on
`cw review confirm --backend claude`. There is no per-run or per-ticket spend
ceiling and no `review` setting for one. The only cost figure that exists is
the CLI's self-reported `total_cost_usd`, and it is not recorded anywhere a
ticket's history can show it.

This was a deliberate call on #2812, not an omission: the operator chose a
documented decision over a cap knob.

## Invariant

1. The confirm round passes no spend flag to `claude -p`. In particular it
   never passes `--max-budget-usd`.
2. The bounds that exist are `max_cycles`, the wall-clock budget and the loop
   iteration backstop (`effective_max_cycles + 1` loop runs, 9 at the clean
   default). The wall-clock budget applies only when one is passed
   (`cw codex run --wall-clock-budget-seconds`). Dispatched lanes pass none, so
   there the bounds are `max_cycles`, oscillation and the backstop.
3. A reviewer role's cost is logged at INFO
   (`claude review role %r finished in %.1fs (total_cost_usd=%s)`, logger
   `cw.claude_review._roles`) and is not recorded in the convergence ledger,
   `cw review history` or the history comment. The figure is self-reported by
   the CLI and is not audited by cw.

## What this means for callers

- An operator who wants a cost bound today sets `review.confirm_backend: none`
  (or `codex`) or `review.convergence: blocking`.
- The decision covers both the codex-lane gate and
  `cw review confirm --backend claude`. Neither has a ceiling.
- A change that adds a spend setting, a budget argv, a budget-stop classifier or
  a spend-park blocker reason reopens this decision and must supersede this ADR.
  A future cap would need a typed, retry-eligible, fail-closed blocker so that a
  parked ticket can be requeued after the operator raises the limit.

## Consequences

- Worst-case spend per ticket is roughly (loop runs) x (reviewer roles)
  `claude -p` calls, and it is unmetered.
- No spend visibility in the ticket's own history: the figure lives only in the
  INFO log line. The confirm round's cost is not on any result object that
  reaches the history renderer, so showing it needs new plumbing that this
  decision does not add.
- A spend ceiling can be revisited once cost is persisted and real cost data
  exists. Persisting it is out of scope here.

## Alternatives considered

- A per-ticket cumulative cap with a typed, requeueable fail-closed blocker.
  It needs ledger counters, budget-stop classification and a new blocker
  reason.
- Passing `--max-budget-usd` on each reviewer call.
- Recording the confirm round's cost in the history.

All three were rejected, following the operator's decision on #2812: they are
larger and riskier than a documented decision, and unneeded while confirm cost
is unobserved.

## Referenced by

- #2812, #2791
