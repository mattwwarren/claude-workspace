# The live work dashboard extends `cw orchestrate watch`, not a new surface

**Status:** Accepted
**Driven by:** #537 (epic "Live work dashboard"); split into #308, #834, #833, #310, #842

> **Note:** `cw orchestrate watch` is deprecated (RFC 0007 Phase 4) and will
> be removed in the release after v1.14.0. Use `cw board` instead.

## Decision

The operator-facing "live work dashboard" is the existing `cw orchestrate watch`
TUI (`src/cw/cli/orchestrate.py` → `tui_watch` in `src/cw/tui.py`), incrementally
extended with the signals it lacked — **not** a new command or web surface. Each
missing signal ships as its own small ticket against that one surface.

## Invariant

1. New dashboard signals are added to the existing per-client `tui.py` tables
   and their `OrchestratorStatus` / `SessionSummary` / `MonitoredPR` models —
   not to a parallel surface. There is one live-dashboard surface.
2. Render-layer data is computed at **snapshot time** in `orchestrate.py`'s
   builder (`_summarise_session`, `orchestrator_status`), stamped onto the
   summary models, and rendered by `tui.py`. The render layer does not reach
   back into live state per row.
3. The attention indicator **dedupes** repeated `session.needs_attention`
   per distinct `(session_id, paused_status)` and aggregates the affected-session
   count. Noise-collapse and signal-surfacing are the same feature: a storm of
   identical re-fires renders as one actionable row, never N rows.

## What this means for callers / future tickets

- Want a new column or signal on the dashboard? Extend `tui.py` + the summary
  model, behind the existing `DetailLevel` (COMPACT stays counts-only). Do not
  build a second dashboard.
- Event-bus reads for the dashboard that must survive a storm (e.g. attention)
  use a **dedicated** filtered `read_events(event_types=[...])` — not the
  bounded `recent_events` tail, which a storm can evict.

## Consequences

- The four-signal delta (transcript-freshness heartbeat, sentinel/paused status,
  CI/mergeable, storm-deduped attention) landed as small reviewable tickets on a
  stable surface, rather than a large greenfield rebuild — at the cost of working
  within the existing TUI's structure.
- Disposition surfacing (#310) required denormalizing `disposition`/`pr_url`/
  `completed_at` onto `TicketTask` (schema v5), stamped through the single status
  seam — see ADR-0011.

## Alternatives considered

- **Greenfield dashboard (rejected):** a new `cw watch` command or local web
  view per the original #537 sketch. Discovery found `orchestrate watch` already
  provided ~70% of the vision; a new surface would duplicate it and fragment the
  operator's mental model.

## Referenced by

- #537, #308, #834, #833, #310, #842, ADR-0011
