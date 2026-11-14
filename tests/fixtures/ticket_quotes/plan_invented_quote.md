<!-- plan-spec-reviewed: 2026-11-14 v2 -->
<!-- plan-soundness-reviewed: 2026-11-14 v1 -->

# Plan: add retry budget to the dispatch loop

## Evidence from the ticket

- Ticket quote: "The dispatch loop should stop retrying after three attempts."
- Ticket quote: "The loop must never retry after two failures."

The genuine quote above matches the ticket. The second quote was invented by
the planner and appears nowhere in the ticket, its comments or the resolutions.

## Files Modified

- src/cw/dispatch.py
