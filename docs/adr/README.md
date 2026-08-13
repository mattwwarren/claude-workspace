# Architecture Decision Records

This directory holds ADRs — durable records of architectural decisions whose
consequences ripple across multiple modules or future work items.

## When to write one

Write an ADR when a decision:

- introduces an invariant that more than one module must honor
- constrains how future tickets can be implemented
- closes off alternatives that a reasonable future engineer might otherwise
  re-open
- is referenced from multiple GitHub issues as a "see ADR-XXXX"

Plain implementation choices, file-level refactors, and one-off design calls
do NOT need an ADR — they belong in the ticket that drives them.

## File naming

`NNNN-kebab-case-title.md` where `NNNN` is the next zero-padded integer in
sequence. Do not renumber existing ADRs.

## How to add one

1. Pick the next number from the index below.
2. Copy `template.md` to `NNNN-<title>.md`.
3. Fill it in. Keep it short — an ADR is reference material, not a tutorial.
4. Add a line to the index below, in number order.
5. Link to it from the relevant GitHub issue(s) so the connection is durable.

## Status values

- **Proposed** — under discussion; do not act on yet.
- **Accepted** — current law of the land.
- **Superseded by ADR-NNNN** — replaced; kept for history.
- **Deprecated** — no longer applies; not (yet) superseded.

When an ADR is superseded, edit the old one's status line — don't delete it.

## Index

| # | Title | Status |
|---|---|---|
| [0000](0000-native-supervisor-migration.md) | Migrate session lifecycle to `claude --bg` + supervisor | Accepted |
| [0001](0001-parked-tasks-pin-their-session.md) | Parked tasks pin their Session | Accepted |
| [0002](0002-blocker-retry-policy-pair.md) | Blocker carries an explicit retry policy | Accepted |
| [0003](0003-stop-hook-canonical-completion-signal.md) | Stop hook is the canonical worker-completion signal | Accepted |
| [0004](0004-stage-events-on-orchestrator-bus.md) | Stage-transition events on the orchestrator event bus | Accepted |
| [0005](0005-single-state-lock.md) | All `sessions.json` mutations go through a single state lock | Proposed |
| [0006](0006-reaping-is-gated-by-an-authority.md) | Reaping is gated by an authority, not automatic | Accepted |
| [0007](0007-reconcile-cadence-and-ownership.md) | Reconcile cadence and ownership: on-demand, ticker, or daemon primary runner | Proposed |
| [0008](0008-tracker-resolution-is-a-typed-seam.md) | Tracker resolution is a declared descriptor, not bespoke code | Proposed |
| [0009](0009-branch-absence-is-diagnostic-not-completion.md) | Branch-absence is a diagnostic signal, never a completion signal | Accepted |
| [0010](0010-live-dashboard-extends-orchestrate-watch.md) | The live work dashboard extends `cw orchestrate watch`, not a new surface *(deprecated — see `cw board`)* | Accepted |
| [0011](0011-ticket-status-transitions-through-one-seam.md) | TicketTask status transitions go through a single seam | Accepted |
| [0012](0012-cw-never-grants-github-review-approvals.md) | cw never grants a GitHub review approval | Accepted |
| [0013](0013-agent-delegated-ticket-work.md) | Provider-portable ticket work is agent work; cw keeps one GitHub-only programmatic client | Accepted |
| [0014](0014-timers-never-destroy-work.md) | Timers never destroy work — elapsed time is a signal, not a trigger | Accepted |
| [0015](0015-voided-finding-suppression-is-content-anchored.md) | Voided-finding suppression is content-anchored, never positional | Accepted |

ADR-0000 is the foundational record — the trajectory it captures is
assumed as ground truth by every subsequent ADR.
