# claude-workspace Vision

**North star:** JARVIS. **Today's deliverable:** Tony's workbench.

This document holds the enduring *why* behind `cw` — the guiding principles and
the long-horizon vision. It is deliberately free of command-level detail and
delivered-milestone logs, which go stale quickly. For how the system is
actually built and why specific choices were made, read the architecture
records:

- **RFCs** (`docs/rfcs/`) — design proposals for major subsystems (native
  session backend, work lanes, staged pipeline, review system, observability,
  push channels).
- **ADRs** (`docs/adr/`) — the point decisions that shaped those subsystems
  (reaping authority, reconcile cadence, single state lock, canonical
  completion signal, …).

---

## Design Philosophy

### Context is the Bottleneck

The single most valuable resource in agentic work is context window capacity.
Everything `cw` does serves one goal: protect context by making it cheap to
spin work off, background it, and resume it later with full fidelity.

### Spin Off Anything Too Heavy

If a task is too significant for the current context (or a single context), it
should be trivially easy to:

- **Background it** (`cw bg`) — generate a handoff, free the context.
- **Delegate it** — a new session with fresh context and focused scope.
- **Fork it** — implementation discovers a review concern? Spin up a review
  session on the same branch.

The cost of starting a new session must approach zero.

### Native First

Claude Code's experience is the product. `cw` is scaffolding around it, never a
replacement. Every worker runs real `claude`. Every handoff uses the existing
`/session-done` pipeline. No wrappers, no proxies, no reimplemented UIs.

### Intra- vs Inter-Session Delegation

- **Intra-session** (Claude's `Task` tool): quick, parallel subtasks that share
  context (running tests, linting, exploring code).
- **Inter-session** (`cw`): larger, independent work streams that need their own
  context (full feature review, debt-paydown campaign, multi-file refactor).

Rule of thumb: if you'd use `/handoff` to pass the work, it belongs in a
separate `cw` session. If you'd use `Task` to delegate, it stays in the current
session.

---

## The Horizon: JARVIS

The long-term aim is an AI development environment that anticipates rather than
waits:

- **Voice interface** — "Hey cw, what's the status on sigma?"
- **Proactive suggestions** — "You haven't run debt paydown on this client in 5 days."
- **Smart scheduling** — run debt/review sessions during idle periods automatically.
- **Cross-client insights** — "The same pattern you fixed in sigma exists in meta-work."
- **Natural language orchestration** — "Start reviewing all open PRs across my clients."
- **Full IDE integration** — when Claude Code exposes a programmatic API, replace
  keystroke injection with proper IPC.
