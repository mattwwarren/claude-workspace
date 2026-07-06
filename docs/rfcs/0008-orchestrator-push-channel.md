# RFC 0008 — Orchestrator Push Channel: Event-Complete Transitions, Liveness Signals, and the Operator Attention Filter

| Field | Value |
|---|---|
| Status | **Draft** |
| Owner | @mattwwarren |
| Date | 2026-07-05 |
| Supersedes | none |
| Related | RFC 0002 (SSE/channel infra), RFC 0007 (observability sprint), ADR 0006 (reaping authority), `docs/events.md`, wiki note `cw-stage-timing-baselines-2026-07-05` |

## Summary

The orchestrator session that drives dispatch waves still discovers queue and
session state by *polling*: bash loops that diff `dev_queue.json` rows every
45s, recompute transcript-mtime staleness, and hand-maintain an emit policy
separating "attention" from "routine". Every emitted line costs an orchestrator
model turn; every threshold lives in an untested shell script.

This RFC moves that monitoring into cw as push events and a server-side
attention filter, riding the SSE/channel infrastructure RFC 0002 shipped and
the event bus RFC 0007 populated. The orchestrator subscribes instead of
polling; the routine-vs-attention split becomes tested code.

## Motivation (2026-07-05 sprint evidence)

- A single three-ticket wave produced **35+ monitor events**, each a full
  orchestrator turn; only ~8 required action, and every actionable one was a
  queue status/disposition change — exactly the signal cw mutates internally
  but never emits.
- The monitor's emit policy was revised **three times in one day** (staleness
  bucketing, dropping sub-15m flips, per-stage thresholds) — policy iteration
  happening in bash instead of reviewed code.
- **Five false parks** were diagnosed by hand-correlating park events against
  transcript mtimes; a liveness producer would have carried that signal.
- Empirical baselines now exist (739 classified legs): healthy sessions write
  near-continuously (p95 gap ≤1m in every stage); real deaths cluster ≥60m;
  stage-specific idle tails differ 3× (impl p99 gap 31m vs review 9m). These
  belong in config, not in an orchestrator's memory.

## Design

### W1 — Transition producers (the missing events)

**`task.transition`.** `transition_task_status` (`dev_queue.py:86`) is the
single documented authority for TicketTask status changes. Emit one event
there: payload `{ticket_id, client, lane, stage, old_status, new_status,
disposition, session_id, pr_url}`. Stage advances that do not change status
(`_stage_advance_unchecked`, `_stage_regress`) emit a `task.stage_changed`
sibling from the same module. Every gate the operator closes
(`blocked_on_user` + disposition, `awaiting_operator_signoff`, terminal
completions) becomes push-visible with its reason attached.

**`task.deleted`.** Row removal emits an event (this is GitHub #978's fix,
absorbed here).

### W2 — Liveness producer

Reconcile already computes per-session transcript staleness for the idle
watchdog. Emit `session.liveness_changed` on threshold crossings —
`live → stale_15m → stale_30m → stale_45m` and the recovery edge back to
`live` — latched per bucket (no re-fire while a bucket holds). Bucket values
are `OrchestratorConfig` fields seeded from the 2026-07-05 empirical
baselines, with a per-stage override table (impl legs legitimately idle 3×
longer than review legs).

### W3 — Operator attention filter

A server-side filter over the existing queue-events SSE channel defines the
**operator channel**: forward only
- `task.transition` where the new status is a park, gate, or terminal,
- `task.deleted`,
- `session.needs_attention` (incl. RFC 0007 W2 counters),
- `pr.*` transition events,
- `session.liveness_changed` at/above the configured attention bucket.

Everything else (ticks, healthy-stage transitions, sub-threshold liveness)
stays on the bus for the board and forensics. The filter set is config, not
code. Consumption uses the channel-injection pattern the `cw-orchestrator`
agent already demonstrates for `cw-pr-events`: an orchestrator session
subscribes once and receives attention events as they occur — zero polling
turns. Poll remains the fallback when no server is running (same degradation
contract as RFC 0007 W2 push/poll coexistence).

### W4 — Docs + skills (adoption surface)

- `docs/events.md`: sections for the three new producers.
- `docs/dispatch-runbook.md`: the monitoring section rewritten
  subscribe-first (operator channel) with the poll ladder demoted to fallback.
- `.claude/skills/cw-fanout` Step 4: wave-watching converts from bash liveness
  monitors to operator-channel subscription (poll fallback retained verbatim).
- `.claude/skills/cw-session-watch`: consumes `task.transition` /
  `session.liveness_changed` instead of grepping events + transcripts.
- Skill copies in global-claude sync per the established mirror process.

### Explicitly out of scope

Adjudication stays with the operator/LLM: reading gate comments, answering
ambiguities, plan approval, false-park diagnosis. Software routes and filters;
judgment does not compile. The web board (#994) consumes the same channel but
remains its own RFC.

## Phasing

| Phase | Contents | Gate |
|---|---|---|
| 1 — Producers | `task.transition`, `task.stage_changed`, `task.deleted` (#978), `session.liveness_changed` | Events observable on the bus for a full wave; board unaffected |
| 2 — Filter | operator-channel filter set (config) over queue-events SSE | A subscribed session receives gates/parks/terminals only |
| 3 — Adoption | docs + skills rewrite, runbook subscribe-first | A wave driven end-to-end with zero poll-loop monitor turns |

## Open questions

None blocking — filter-set config naming and bucket defaults are pinned in the
implementation tickets from the empirical baselines.

## References

- `src/cw/dev_queue.py:86` — `transition_task_status` (W1 chokepoint)
- `src/cw/reconcile/` idle watchdog mtime math (W2 source)
- `src/cw/cw_queue_events_server.py` — SSE half (W3 transport)
- `.claude/agents/cw-orchestrator.md` — channel-injection consumption pattern
- wiki `cw-stage-timing-baselines-2026-07-05` — threshold data
- Issues: #978 (absorbed by W1), #976 (liveness veto uses W2's signal), #994 (web board, same bus)
