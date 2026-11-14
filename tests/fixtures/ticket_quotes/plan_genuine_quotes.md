<!-- plan-spec-reviewed: 2026-11-14 v2 -->
<!-- plan-soundness-reviewed: 2026-11-14 v1 -->

# Plan: add retry budget to the dispatch loop

## Evidence from the ticket

- Ticket quote: "The dispatch loop should stop retrying after three attempts."
- The ticket says "Operators need a visible counter on cw status so they can tell a stuck ticket from a slow one" and the operator wrote "keep the cap configurable, but default to three attempts".
- Decided: R1 — "use the existing lane config rather than a new setting"

Per the acceptance criteria:

> AC2: The status output shows
> the remaining retry budget.

## Things that are not checked

The reviewer thought "this unattributed prose is never examined" in passing.

```text
Ticket quote: "this fenced invented quote is ignored entirely"
```

The command `grep "an inline invented quote"` is also ignored.

> A blockquote with no ticket lead-in is never checked even when invented.

## Files Modified

- src/cw/dispatch.py
