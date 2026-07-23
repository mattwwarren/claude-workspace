# cw Architecture

This document is the **Plan Soundness Reviewer's Tier-1 source of truth**
(`.claude/agents/plan-soundness-reviewer.md`, "Source of Truth" section:
`ARCHITECTURE.md` in the target repo — specifically §7 (principles) and §8
(anti-patterns)). A plan whose chosen direction contradicts a §7 principle or
§8 anti-pattern is a MUST_FIX at plan time, before any code is written.

The `§7`/`§8` numbering is a **structural contract**, not a stylistic
choice: `.claude/commands/auto-dev-plan.md` and `.claude/commands/auto-dev.md`
hard-code the literal string `§7/§8` at multiple call sites. Do not renumber
or retitle those two sections — inserting, removing, or reordering a section
elsewhere in this document is fine as long as §7 stays "Principles" and §8
stays "Anti-patterns".

## §1 System Overview

`cw` (claude-workspace) is a multi-session workspace orchestrator for Claude
Code. It lets an operator drive parallel autonomous Claude workers across
their repos — enqueue tickets, dispatch background workers, monitor progress
to terminal, triage gates, and ship with auto-merge — while the operator stays
the coordinator rather than the implementer. The core loop is: harden a
ticket, dispatch it, let workers implement/review/ship it, then triage gates
and clean up. Workers are spawned via `claude --bg` and tracked by a short hex
session id in `~/.claude/daemon/roster.json`; no multiplexer is required.
Durable architectural decisions — the ones whose consequences ripple across
multiple modules or future tickets — are recorded as ADRs; see
`docs/adr/README.md` for the index and the criteria for writing one.

## §2 State & Locking Model

Two ADRs govern how `cw` mutates its own state, and both close a class of bug
that showed up as silent, hard-to-reproduce corruption before they landed.

**ADR-0005 — single state lock.** Every mutation of `sessions.json`
(`CwState`) MUST go through a `mutate_state()` helper that holds an exclusive
file lock (`STATE_DIR/.state.lock`, `fcntl.flock`) across the entire
`load_state → mutate → save_state` cycle. Bare `load_state(); …;
save_state()` sequences are no longer permitted outside that helper. This
closed a real race: the worker Stop-hook and the orchestrator loop
(`reconcile()` / `spawn_create_impl()`) are concurrent writers by design, and
without a lock spanning the read-modify-write cycle the last writer's save
silently clobbers the other's (a completion write lost, a freshly-spawned
session orphaned, a `TIMED_OUT` revert overwritten). Read-only callers may
still use bare `load_state()` — a stale read is acceptable; a lost write is
not.

**ADR-0011 — ticket-status transition seam.** Every dev-queue `TicketTask`
status change goes through one function — `transition_task_status(task,
new_status, ...)` in `src/cw/dev_queue.py`. Code does not assign
`task.status = ...` directly. The seam is both the single authority for
status and the one hook point for any logic that must fire on a transition
(e.g. disposition stamping/clearing), so a cross-cutting behavior change is a
one-site edit instead of an N-site sweep across scattered assignments.

See `docs/adr/0005-single-state-lock.md` and
`docs/adr/0011-ticket-status-transitions-through-one-seam.md` for the full
decisions, invariants, and alternatives considered.

## §3 Session/Task Lifecycle

**ADR-0006 — reaping is gated by an authority.** `reconcile()` splits into a
**detect** phase (classify phantoms, budget-exceeded, silently-idle sessions —
no mutation) and an **act** phase (revert `TicketTask` RUNNING→PENDING, stop
the daemon surface, force-remove the worktree). The act phase is gated by a
`reap_policy` that defaults to `signal_only`: detection emits a distress event
and routes the owning task to `BLOCKED_ON_USER`, but performs no destructive
mutation until an authority — the lane's long-lived `ORCHESTRATE` session, or
an explicit operator command (`cw doctor --reap`, `cw reconcile --apply`) —
authorizes it. Automatic reaping (`reap_policy: auto`) is opt-in per lane, not
the default. Callers must count `BLOCKED_ON_USER`/`REAP_PROPOSED` sessions as
occupying capacity — a stalled, signal-only session does not free its slot
just because it stopped making progress.

**ADR-0009 — branch-absence is diagnostic, not completion.** When the reaper
times out a headless session with no parseable sentinel and no merged PR, an
absent dev branch is surfaced only as a diagnostic annotation
(`branch_state: "absent_no_merged_pr"`) on the `SESSION_TIMED_OUT` event — it
is never used to infer the work completed. Branch-absence never routes a
session to `COMPLETED`; a timed-out session with no merged PR reverts to
`PENDING` for retry regardless of whether its branch exists. The check fails
open: a `gh` error or absent `gh` never blocks the disposition and never adds
the tag.

See `docs/adr/0006-reaping-is-gated-by-an-authority.md` and
`docs/adr/0009-branch-absence-is-diagnostic-not-completion.md`.

## §4 Dispatch & Admission

Two admission mechanisms gate how many sessions the dispatch loop lets run
concurrently, and both matter for correctly reading "is the queue stuck or
just full."

**Host-wide capacity budget (#1444, `src/cw/dispatch/host_capacity.py`).**
This module is "a single optional ceiling on how many DAEMON sessions may run
concurrently across the whole host, independent of (and folded into) the
existing per-client ceiling in `lanes.py`." It is a pure in-memory computation
over state and queue snapshots the caller already loaded that tick — no
subprocess, no sidecar file, nothing worth memoizing across ticks. A session
whose owning task is confirmed parked (`BLOCKED_ON_USER` or
`AWAITING_OPERATOR_SIGNOFF`, per ADR-0006) is excluded from the host-running
count via a join on `TicketTask.session_id == Session.id` — otherwise a
"ghost" session parked under `signal_only` would permanently consume one unit
of host budget until an operator intervened.

**Lane occupancy (`src/cw/dispatch/claim.py`).** `_lane_stats_for_client`'s
docstring states the rule directly: "BLOCKED_ON_USER occupies its lane slot
per ADR-0006, so `running + blocked + signoff` is the total occupied count."
Occupancy counting is task-join based (not session-based) per ADR-0006 /
RFC 0004 Phase 4a scope. A dispatch admission check that only counts `RUNNING`
tasks as occupying a lane will over-admit into a lane that is actually full of
parked, awaiting-operator work.

See `src/cw/dispatch/host_capacity.py` and `src/cw/dispatch/claim.py` module
and function docstrings for the full reasoning, and `docs/dispatch-runbook.md`
for the operator-facing dispatch procedure.

## §5 Result Publishing & Events

**RFC 0012 — one door, per-backend harvest authorities.** Before this RFC,
every backend delivered its `AutoDevResult` end state through a different
mechanism, each carrying its own copy of the don't-clobber guard, with no
record of which mechanism wrote the result. The invariant this RFC
establishes: "every backend has a designated harvest authority that pushes
through one validated door" — an importable `emit_result()` extracted from
`cw result emit`'s internals. Supervised-child backends (codex, aider/local)
harvest directly in their executor; the detached Claude daemon harvests via
the Stop hook; reconcile's transcript scraping remains only as explicitly
labeled salvage. "Consumers read `session.last_result` only — never
transcripts." The door also stamps a `Session.last_result_source` provenance
field so operators can tell how a wedged ticket's result got there.

**Stop hook is the sole completion-event source.** The door is deliberately
write-only: "it emits no events and routes no tasks (the #536 separation —
the Stop hook remains the sole completion-event source, `_apply_sentinel_to_task`
stays with its callers)." A new mechanism that both writes `last_result` and
emits a completion event would duplicate that separation and is out of scope
for any future backend integration.

See `docs/rfcs/0012-unified-result-publishing.md` for the full design, and
`docs/headless-contract.md` for the `AUTO_DEV_RESULT` sentinel schema and
event taxonomy that the door validates against.

## §6 Review/Ship Pipeline

**ADR-0012 — cw never grants a GitHub review approval.** No code path in
`src/` may invoke `gh pr review --approve`, the GraphQL
`addPullRequestReview` mutation with `event: APPROVE`, or REST `POST
/pulls/{n}/reviews` with `"event": "APPROVE"`. This is a tested invariant
(`tests/test_review_approval_guard.py`), not an emergent property of current
code, and it has no escape hatch: no config flag, per-lane override, or scoped
exception may reintroduce an approving-review call path. A genuinely
legitimate future need requires a new ADR that explicitly supersedes this
one. Arming auto-merge on an already-human-approved PR (`gh pr merge --auto`)
is not an approval and is unaffected by this invariant. See
`docs/adr/0012-cw-never-grants-github-review-approvals.md`.

**Module size.** Source modules follow the package-split convention described
in `CLAUDE.md`'s "Module Size" section (`CLAUDE.md:80-101`): keep modules
under ~1000 lines, treating that as a ceiling rather than a target, and split
an oversized module into a package with an `__init__.py` that re-exports the
public surface (`cw.cli` and `cw.reconcile` follow this shape) rather than
letting individual functions or a single file grow unbounded. That section
owns the full prose; this document does not duplicate it.

## §7 Principles

Each principle below is a single quotable sentence with its source. These are
the codified rules the Plan Soundness Reviewer holds plans accountable to —
read `.claude/agents/plan-soundness-reviewer.md` for how a Tier 1 finding
cites one of these.

1. Reap-policy authority: reaping is gated by an authority; the default
   `reap_policy` is `signal_only` (no destructive mutation without
   authorization); automatic reaping is opt-in. — Source:
   `docs/adr/0006-reaping-is-gated-by-an-authority.md`
2. Harvest authority / one-door result publishing: each backend has one
   designated harvest authority pushing through one validated door;
   consumers read `session.last_result` only, never transcripts. — Source:
   `docs/rfcs/0012-unified-result-publishing.md`
3. Dispatch admission, host budget: dispatch admission enforces a single
   optional host-wide ceiling on concurrent DAEMON sessions, folded into
   the per-client ceiling. — Source: `src/cw/dispatch/host_capacity.py`
4. Dispatch admission, lane occupancy: `BLOCKED_ON_USER` tasks continue to
   occupy their lane slot; occupied count = running + blocked + signoff. —
   Source: `src/cw/dispatch/claim.py`
5. Sentinel/emit contract: the Stop hook is the sole completion-event
   source; the result-publishing door emits no events and routes no
   tasks. — Source: `docs/rfcs/0012-unified-result-publishing.md`
6. Module-size / package-split convention: modules stay under ~1000 lines;
   exceeding it means splitting into a package with an `__init__.py` that
   re-exports the public surface. — Source: `CLAUDE.md`
7. cw never grants a GitHub review approval; there is no escape hatch
   without a superseding ADR. — Source:
   `docs/adr/0012-cw-never-grants-github-review-approvals.md`
8. Single state lock: every mutation of `sessions.json` goes through
   `mutate_state()`; bare `load_state()`/`save_state()` sequences are
   disallowed outside it. — Source:
   `docs/adr/0005-single-state-lock.md`
9. Branch-absence is diagnostic, not completion: branch-absence never
   routes a session to `COMPLETED`; a timed-out session with no merged PR
   reverts to `PENDING` for retry. — Source:
   `docs/adr/0009-branch-absence-is-diagnostic-not-completion.md`
10. Ticket status transitions through one seam: every `TicketTask` status
    change goes through `transition_task_status()`; direct `task.status =
    ...` assignment is disallowed. — Source:
    `docs/adr/0011-ticket-status-transitions-through-one-seam.md`

## §8 Anti-patterns

Each anti-pattern below is the direct inversion of the same-numbered §7
principle, grounded in the same source document.

1. Destructive reap under `signal_only` with no authority sign-off —
   force-removing a worktree, stopping a daemon, or reverting
   RUNNING→PENDING without a `reap_policy: auto` lane or an explicit
   operator command (`cw doctor --reap`, `cw reconcile --apply`) behind it.
   — Source: `docs/adr/0006-reaping-is-gated-by-an-authority.md`
2. A second `session.last_result =` write path outside the RFC 0012 door —
   any new or existing writer that assigns `last_result` directly instead
   of going through `emit_result()`, bypassing first-writer-wins
   arbitration and provenance stamping. — Source:
   `docs/rfcs/0012-unified-result-publishing.md`
3. A dispatch admission check that ignores the host-wide ceiling — code
   that computes lane/client capacity without folding in
   `resolve_host_capacity()`'s fleet-wide budget. — Source:
   `src/cw/dispatch/host_capacity.py`
4. A dispatch admission check that treats parked (`BLOCKED_ON_USER`) tasks
   as free lane capacity — counting only `RUNNING` tasks as occupying a
   lane and admitting new work into a lane that is actually full of
   awaiting-operator work. — Source: `src/cw/dispatch/claim.py`
5. A new completion-event source competing with the Stop hook — any code
   path that both writes a result and emits its own completion event
   instead of routing through the existing Stop-hook /
   `_apply_sentinel_to_task` seam. — Source:
   `docs/rfcs/0012-unified-result-publishing.md`
6. A source module silently growing past ~1000 lines with no package
   split — accreting unrelated concerns into one file instead of
   extracting helpers or splitting into a package with a re-exporting
   `__init__.py`. — Source: `CLAUDE.md`
7. `gh pr review --approve` / GraphQL `addPullRequestReview(APPROVE)` /
   REST approve call anywhere in `src/` — reintroducing an
   approval-granting path via a flag, override, or "just this once"
   workaround. — Source:
   `docs/adr/0012-cw-never-grants-github-review-approvals.md`
8. Bare `load_state()` → mutate → `save_state()` outside `mutate_state()`
   — an inlined read-modify-write that skips the exclusive file lock,
   reopening the lost-write race between the Stop hook and the
   orchestrator loop. — Source: `docs/adr/0005-single-state-lock.md`
9. Routing a task to `COMPLETED` on branch absence — inferring "the work
   shipped" from a missing dev branch instead of treating it as a
   diagnostic-only anomaly annotation. — Source:
   `docs/adr/0009-branch-absence-is-diagnostic-not-completion.md`
10. Direct `task.status = ...` assignment outside `transition_task_status()`
    — a new call site that mutates `TicketTask.status` in place instead of
    routing through the seam, re-scattering transition logic across the
    codebase. — Source:
    `docs/adr/0011-ticket-status-transitions-through-one-seam.md`

## Reference Table

The ADRs below are not required §7 entries — listed here for completeness.
See `docs/adr/README.md` for the full index and the criteria for writing a
new one.

| ADR | Title | Status |
|---|---|---|
| [0000](docs/adr/0000-native-supervisor-migration.md) | Migrate session lifecycle to `claude --bg` + supervisor | Accepted |
| [0001](docs/adr/0001-parked-tasks-pin-their-session.md) | Parked tasks pin their Session | Accepted |
| [0002](docs/adr/0002-blocker-retry-policy-pair.md) | Blocker carries an explicit retry policy | Accepted |
| [0003](docs/adr/0003-stop-hook-canonical-completion-signal.md) | Stop hook is the canonical worker-completion signal | Accepted |
| [0004](docs/adr/0004-stage-events-on-orchestrator-bus.md) | Stage-transition events on the orchestrator event bus | Accepted |
| [0007](docs/adr/0007-reconcile-cadence-and-ownership.md) | Reconcile cadence and ownership: on-demand, ticker, or daemon primary runner | Proposed |
| [0008](docs/adr/0008-tracker-resolution-is-a-typed-seam.md) | Tracker resolution is a declared descriptor, not bespoke code | Proposed |
| [0010](docs/adr/0010-live-dashboard-extends-orchestrate-watch.md) | The live work dashboard extends `cw orchestrate watch`, not a new surface *(deprecated — see `cw board`)* | Accepted |
| [0013](docs/adr/0013-agent-delegated-ticket-work.md) | Provider-portable ticket work is agent work; cw keeps one GitHub-only programmatic client | Accepted |

**Footnote:** `docs/adr/README.md`'s index table lists ADR-0005 as
"Proposed", but ADR-0005's own file has `**Status:** Accepted — implemented
(single state lock + mutate_state, #387/#563; released v1.1.0)`. This
document treats ADR-0005 as Accepted — the file's own status line is
authoritative; the index table is stale.
