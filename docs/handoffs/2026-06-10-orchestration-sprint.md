# Handoff: Orchestration UX Sprint (2026-06-10)

**For:** a local Claude Code session managing this sprint on the operator's machine.
**Origin:** remote session analyzing Fable 5 ↔ cw integration (branch
`claude/fable-5-integration-giz1mp`).
**Sprint goal:** rock-solid orchestration ergonomics — add tickets, move
tickets, see status, see what's in review — with zero orientation tax for a
fresh Claude session.

---

## TL;DR

The issue tracker already contains most of this sprint as a milestone-1.1
cluster. One new ticket was filed this session (#507, `dev-queue move`). The
recommended order is three waves with one hard dependency chain:
**#506 → #507**, and **#308 → #310 → #479**. Two items are deliberately
*unticketed* (MCP tool server, `cw brief`) — file them when their
prerequisites land (see "Unticketed work").

## Sprint board

| Wave | Issue | Title (short) | Depends on | Notes |
|------|-------|---------------|------------|-------|
| 1 | #506 | claim loop ignores `--priority`; `wait` matches stale records | — | Correctness bug, filed 2026-06-09 by an external user after a real prod-fix failed to jump the queue. Consider splitting the `wait` stale-record observation into its own issue. |
| 1 | #507 | `cw dev-queue move` — reprioritize / requeue / reassign in place | #506 | Filed this session. Requeue-in-place structurally removes the duplicate-record class behind #506's `wait` bug. |
| 2 | #308 | `cw dev-queue list` — ticket↔session rows, active-only default | — | Owner-specced with acceptance criteria; start here for the view work. |
| 2 | #310 | disposition (shipped/blocked/no_op/timeout) + PR on completed rows | #308 | Issue recommends Path A: denormalize `disposition`/`pr_url`/`completed_at` onto `TicketTask` (schema v2→v3 bump + migration in `migrate_dev_queue`). |
| 3 | #476 | real PR CI status + mergeable in review-monitor section | — | Cross-cutting: touches `MonitoredPR` in `src/cw/orchestrate.py` **and** `~/.claude/scripts/review_monitor.py` on the local machine — this is why the sprint runs locally. |
| 3 | #479 | `dev-queue wait --json` surfaces PR/issue outcome | #310 | Joins queue → session → PR outcome. |
| Bridge | #238 | native inspection commands (`session show/list/wait`, `dev-queue tasks`) | — | Partially overlaps #308; dedupe scope before dispatch. |
| Bridge | #313 | `cw schema` — JSON Schemas for AutoDevResult/TicketTask/Session | — | Feeds the future MCP tool server (same `model_json_schema()` output). |

Adjacent, not in sprint: #380 (session.reaped events), #480/#257/#477
(wave-wait / wave_id / orchestrate-sprint skill), #127 (auto-promote deferred
tickets on PR_MERGED — same mutation as #507's requeue, daemon-initiated).

## Why this shape (context from the analysis)

The friction in "orienting around the cw toolkit" traced to three things:

1. **Missing verbs.** No `move`; "what's in review" data exists in
   `orchestrator_status().monitored_prs` but isn't joined to tickets/sessions.
2. **The agent-facing surface is Bash** — ~40 subcommands across 6 groups;
   every consumer hand-rolls jq over `sessions.json`/`dev_queue.json`
   (documented with transcript evidence in #238).
3. **Event-coverage gaps force polling** — `cw_queue_events_channel.py:31-32`
   documents that RUNNING→CANCELLED and RUNNING→BLOCKED_ON_USER emit nothing;
   the `cw-queue-peek` polling ladder exists to compensate.

The longer-term proposal (discussed, not ticketed): a `cw mcp serve` stdio
**tool** server exposing `queue_add/move/cancel/status`, `orchestrate_status`,
`review_status`, `ticket_wait`, `dispatch_once` as typed MCP tools built
directly on `dev_queue.py`/`orchestrate.py` functions. The repo already ships
the `mcp` extra and two notification-only channel proxies
(`cw_queue_events_channel.py`, `cw_pr_events_channel.py`); this is the third
leg. #313's schemas are the contract source.

## Key code pointers

- Claim loop (the #506 fix site): `src/cw/dispatch.py` `_claim_next_pending`
  (~line 108) — iterate PENDING sorted by `(-priority, enqueue_index)`;
  keep the `priority_ticket_ids` `--use-plan` path as explicit override.
- Stale-record match (the #506 secondary): `src/cw/dev_queue.py`
  `_find_ticket` / `wait_for_terminal` (lines ~301-358) — first-match over
  possibly-duplicated `(client, ticket_id)` records.
- Lock pattern to copy for `move`: `cancel_ticket` in `src/cw/dev_queue.py`
  (load → mutate → save under `_lock()`).
- Models / schema bump: `src/cw/models.py` — `TicketTask` (line ~162),
  `DEV_QUEUE_SCHEMA_VERSION = 2` (line 58), migration in
  `dev_queue.migrate_dev_queue`.
- Event types: `OrchestratorEventType` in `src/cw/models.py` (line ~112);
  `record_event` usage example in `cli.py` `dev_queue_add`.
- Status snapshot / review join: `src/cw/orchestrate.py`
  (`orchestrator_status`, `MonitoredPR`), human formatter in `cli.py`
  `_format_status_human`.
- CLI group: `src/cw/cli.py` `dev-queue` group starts ~line 1465.

## How to run the sprint (dogfood the pipeline)

1. Pre-flight each ticket with `/harden-ticket` before dispatch — #310's
   Path A/B choice and #238/#308 scope overlap are exactly the ambiguity
   class that bounces workers.
2. Dispatch waves with `/cw-fanout` (respect the dependency chain: don't
   fan out #507 with #506, or #479 with #310).
3. Monitor with `/cw-queue-peek`; disposition follow-ups via `/cw-followup`.
4. Quality gates per CLAUDE.md — all 7, in order; patch coverage ≥90%
   including error paths.

## Unticketed work — file when prerequisites land

- **Install wiring for agent onboarding** (replaces the MCP tool server as
  the near-term path — see Decisions): extend `cw init` / `install.sh` to
  (1) register the two channel MCP servers in `.mcp.json` instead of
  shipping `.example` files, (2) install cw skills/agents + a `Bash(cw *)`
  permission allowlist entry, (3) add a SessionStart hook running `cw brief`
  (interim: `cw orchestrate status --json`), (4) surface `cw schema` in the
  generated CLAUDE.md snippet. Folds into milestone 1.1 next to #238/#313.
- **`cw mcp serve` tool server** — DEFERRED, not cancelled. Revisit trigger:
  a session without shell access to the operator machine (remote/phone)
  needs to drive cw — that makes MCP-over-SSE a capability gap rather than
  a convenience. Until then the CLI + `--json` + allowlist path is
  equivalent and avoids maintaining every verb twice.
- **`cw brief`** (token-frugal agent-first snapshot + next-action hints) —
  file after #308/#310 so it can reuse their row data; pairs with a
  SessionStart hook that injects it (see `session-start-hook` skill).
- **Event-gap closure** (`queue.ticket_cancelled`, `queue.ticket_blocked`,
  `pr.ci_passed`) — partially covered by #380; audit
  `cw_queue_events_channel.py` instructions block for the full gap list.
- **#506 split** — if the `wait` stale-record observation isn't fixed inside
  #506, file it separately; #507's requeue-in-place is the structural fix.

## Decisions made this session

- Sprint anchors on existing tickets rather than new specs (only genuinely
  new verb, `move`, was filed as #507).
- Requeue mutates the existing record (preserve `attempts`,
  `total_cost_usd`); never create a duplicate `(client, ticket_id)` row.
- #310 Path A (denormalize onto `TicketTask`) endorsed — matches the issue's
  own recommendation.
- **No MCP milestone.** The orientation tax traced to missing information
  (undocumented schemas, no `--json` commands — see #238 evidence), not a
  missing transport. CLI + `cw schema` + `Bash(cw *)` allowlist + install
  wiring is equivalent for local sessions at a fraction of the maintenance
  cost (a tool server duplicates every verb). Event push already exists as
  MCP via the channel proxies; the install just needs to register them.

## Open questions for the operator

- Should #238 and #308 be merged into one worker dispatch? They overlap on
  the queue-side listing; #238 adds the session-side commands.
- Milestone hygiene: #506/#507 are unmilestoned; the rest of the cluster is
  1.1. Tag them?
- #476 requires editing `~/.claude/scripts/review_monitor.py` (outside this
  repo) — confirm that's in scope for an auto-dev worker or operator-manual.
