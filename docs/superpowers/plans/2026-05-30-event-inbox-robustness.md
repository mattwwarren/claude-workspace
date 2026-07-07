# Event Inbox Robustness & Compaction — Implementation Plan

> **◐ PARTIALLY SHIPPED (archived plan).** The robustness edges (S6a silent-wedge replay,
> S6c torn-read locking) landed via **#393** and are live in `events.py`. The **S6b
> compaction/rotation** half was deferred and is tracked separately by the open **#856**
> (the inbox is still "never truncated or rotated"). Retained as the historical record. —
> noted during the 2026-07-07 ticket audit.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Driven by:** RFC 0004 State-integrity finding **S6**. Tracked by **#393 (T7)** (the three inbox edges). This plan exists because finding **S6b (compaction)** has a genuine design fork — compacting a shared append-only log without orphaning any consumer's cursor is not mechanical.

**Goal:** Harden `src/cw/events.py` (append-only inbox, per-consumer cursors) against three failure modes the audit found:
- **S6a — silent wedge:** if a consumer's cursor id is no longer present in the inbox, `read_events` never flips `past_cursor` true and returns `[]` *forever* (`events.py:150-153`).
- **S6b — unbounded cost:** the entire inbox is re-read and re-parsed on every `read_events` call, every tick — O(n) in total events, growing without bound.
- **S6c — torn read:** reads run *outside* `_inbox_lock` (only `record_event` holds it, `events.py:71`); a concurrent append that leaves a partial trailing line crashes the consuming tick's JSON parse.

**Current shape (grounding):** `events.py` is a 166-line module. Inbox is JSONL at `_inbox_path()`. Per-consumer cursors are `events_dir()/cursors/<consumer>.json` holding `{"cursor": <event_id>}`. `record_event` appends under `_inbox_lock` (exclusive flock). `read_events` scans from the top, skipping until `event.id == cursor`, then yields the rest. No compaction exists today — the file only grows.

---

## Design fork — compaction strategy (decide before coding S6b)

The wedge (S6a) and compaction (S6b) are coupled: compaction is the *only* thing that removes a cursor's target event, so compaction is what *creates* the wedge condition. Two viable strategies:

**Option A — Offset cursors + truncating compaction.** Change cursors from "last event id" to a monotonic sequence number / byte offset. Compaction drops events below `min(all consumer cursors)`. Cursors survive compaction because they're positions, not content references.
- *Pro:* O(1) resume, true bound on file size.
- *Con:* cursor schema change (migration of existing `cursors/*.json`); must enumerate *all* live consumers to compute the safe low-water mark; a stale/abandoned consumer cursor pins the whole log (needs a cursor TTL/GC).

**Option B — Keep id cursors, make read tolerant + compact conservatively.** Leave the cursor-as-id scheme. Fix S6a so a missing cursor falls back (see Task 1). Compact only events older than a wall-clock retention window (e.g. 7d) AND below all known cursors, so a fallback after compaction loses at most already-consumed history.
- *Pro:* no cursor schema change, smaller blast radius, composes with the S6a fallback.
- *Con:* fallback-on-missing-cursor can re-deliver events if a consumer is far behind and its cursor got compacted; relies on consumers being idempotent (which S5 work is already pushing toward).

**Recommendation:** **Option B.** It is the proportional fix, needs no cursor migration, and the S6a fallback (Task 1) is required regardless. Option A is the right move only if a single inbox grows large enough that wall-clock retention is insufficient — defer until measured. **Confirm with the maintainer before implementing Task 3.**

---

## File Structure

**Files to modify:**
- `src/cw/events.py` — (T1) cursor-not-found fallback in `read_events`; (T2) tolerate a torn trailing line + read under `_inbox_lock`; (T3, pending fork) a `compact_inbox(retain)` function + call site.
- `src/cw/daemon.py` *or* `src/cw/dispatch.py` — invoke `compact_inbox` on a low cadence (e.g. once per N ticks) if Option B is chosen. (Decide call site with maintainer.)

**Tests:**
- `tests/test_events.py` — fallback on missing cursor (with a log line); torn-trailing-line read does not raise; concurrent append+read serialize; compaction drops only safe events and never orphans a live cursor.

---

## Task 1: Cursor-not-found fallback (S6a) — correctness, ship first

**Why first:** this is a live silent-wedge bug independent of compaction. Small, high-value.

- [ ] **Step 1:** Failing test: seed an inbox, set a consumer cursor to an id that is NOT in the inbox, assert `read_events` returns *something sensible* (current behavior: `[]` forever) and emits a warning log.
- [ ] **Step 2:** In `read_events`, after the scan, if `cursor is not None` and the cursor id was never matched (`past_cursor` still false at end), fall back to returning from the start (or from the oldest event) and log `WARNING` "cursor <id> not found in inbox; replaying from oldest". Make the fallback explicit, not silent.
- [ ] **Step 3:** Decide + document the fallback semantics (replay-all vs replay-from-oldest-retained) so it composes with whatever compaction lands. Default: replay-from-oldest-retained.

## Task 2: Read robustness (S6c) — tolerate torn line + lock the read

- [ ] **Step 1:** Failing test: write an inbox whose final line is a truncated/partial JSON record; assert `read_events` skips it with a log rather than raising.
- [ ] **Step 2:** Wrap the per-line parse in a guard: on `JSONDecodeError` for the *final* line only, skip + log; a malformed *interior* line is a real corruption and should still surface (don't silently swallow everything).
- [ ] **Step 3:** Take `_inbox_lock` (shared/`LOCK_SH` if you split read/write locks, else the existing exclusive lock) around the read so a concurrent `record_event` append cannot interleave a torn read. Confirm no lock-ordering conflict with `state_lock` (events are recorded outside the state lock today — keep it that way).

## Task 3: Compaction (S6b) — PENDING design fork above

**Do not start until the Option A/B fork is confirmed with the maintainer.**

- [ ] **Step 1:** (Option B assumed) Add `compact_inbox(retain: timedelta)` that, under `_inbox_lock`, rewrites the inbox keeping events newer than `retain` OR at/after `min` of all known consumer cursors. Atomic temp+rename, same pattern as `save_state`.
- [ ] **Step 2:** Enumerate live consumers from `cursors/*.json`; never drop an event still referenced by any cursor. A cursor with no matching event (already wedged) does not pin the log.
- [ ] **Step 3:** Wire a low-cadence call site (every N dispatch/watcher ticks). Test that compaction + the Task 1 fallback together never lose an unconsumed event for a healthy consumer.

---

## Risks & callouts

- **Idempotency dependency:** Option B's fallback can re-deliver. The queue-events consumers must tolerate replays. This is the *same* property S5 (snapshot atomicity) is driving toward — coordinate so both land on "consumers are idempotent" rather than each assuming exactly-once.
- **Interior vs trailing malformed line:** only the *trailing* partial line is an expected concurrent-append artifact. Swallowing interior parse errors would hide real corruption — keep that distinction.
- **Scope discipline:** Tasks 1 and 2 are self-contained correctness/robustness fixes and can ship as #393 (T7) without Task 3. Compaction (Task 3) is optional until inbox growth is actually measured to be a problem — do not gold-plate.

## Referenced by

- RFC 0004 (State integrity, S6), issue #393.
