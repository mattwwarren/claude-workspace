# Stage-transition events on the orchestrator event bus

**Status:** Accepted
**Driven by:** #173 (event taxonomy + `last_stage` derivation), #213 (rollup PR)
**Builds on:** [`docs/headless-contract.md`](../headless-contract.md) §10
(stage event taxonomy), [`docs/events.md`](../events.md) (event-bus
storage and CLI).

## Decision

The `/auto-dev` skill emits `stage.entered` and `stage.errored` events on
the existing orchestrator event bus (`OrchestratorEventType.STAGE_ENTERED`
/ `STAGE_ERRORED`) at each stage boundary while running headless. cw
consumers derive a per-session `last_stage` by walking `STAGE_ENTERED`
events only — `STAGE_ERRORED` is observability-only and never redefines
the current stage.

## Invariant

For every event recorded with type `STAGE_ENTERED` or `STAGE_ERRORED`:

1. The `payload.stage` value MUST be one of the closed enum in
   [headless-contract §10.2](../headless-contract.md#102-stage-identifiers-closed-enum)
   (`s0_intake` … `done`). Unknown stages are a producer bug; consumers
   that gate on stage MUST tolerate them by treating the event as
   `last_stage = None` rather than crashing.
2. `STAGE_ERRORED.payload.error_kind` is an *open* enum — consumers MUST
   accept unknown values without crashing.
3. `cw.orchestrate._derive_last_stage_by_session` MUST ignore
   `STAGE_ERRORED` when computing `last_stage`. A transient `agent_block`
   that the skill later recovers from MUST NOT displace the real current
   stage in the operator-facing rollup.
4. `last_stage` is a *render-time derivation*, NOT a persisted field on
   the `Session`. The event log is the source of truth. If events are
   pruned, `last_stage` correctly reverts to `None`.

## What this means for callers

- **`cw orchestrate status`** (CLI text) and **`cw orchestrate watch`**
  (TUI) read `SessionSummary.last_stage`, which is populated from
  `_derive_last_stage_by_session` at status-snapshot time. Both surfaces
  must tolerate `None` (sessions with no stage events render as `—`).
- **Future stage-aware surfaces** (e.g. a notification when
  `s3_review_complete` is reached, or a dashboard heatmap) MUST consume
  `STAGE_ENTERED` from the event log via `read_events` + cursor
  advancement, identical to how the daemon already consumes PR events.
  They MUST NOT cache stage state on the Session model.

## What this means for producers

- The `/auto-dev` skill (in `mattwwarren/global-claude`) MUST emit
  `stage.entered` at each transition, with `session_id` set to
  `$CW_SESSION_ID` and `ticket_id` matching the run. Emission goes
  through `cw event record stage.entered --payload '…'` — see
  [headless-contract §10.4](../headless-contract.md#104-producer-invocation).
- `stage.errored` is reserved for transient setbacks (agent_block, parse
  failure) that do NOT end the run. A run-ending failure still emits the
  canonical `<<<AUTO_DEV_RESULT` sentinel with `status: blocked` per §3 —
  stage events do not replace the sentinel.

## Consequences

- The event log grows linearly with stage transitions: ~10 stages × 1
  transition each × N daily tickets. Well below the PR-event volume the
  bus already carries; no pruning needed in the foreseeable horizon.
- A producer that lies about its `prev_stage` (emits `s4_pr_created`
  immediately after `s0_intake`) is accepted by the consumer — the enum
  is validated, the sequence is not. Worth it: enforcing sequence in cw
  would require duplicating the skill's state machine across repos.
- The producer lives in `global-claude` while the consumer lives in cw.
  Any change to the stage enum (§10.2) requires coordinated PRs in both
  repos. The closed enum is the only schema field with this constraint;
  payload additions (e.g. new optional fields) are non-breaking.

## Alternatives considered

- **Dedicated stage channel separate from the orchestrator bus.**
  Rejected. The bus already handles correlation IDs, cursors, and
  multi-consumer reads; a parallel channel would duplicate infrastructure
  for a small event volume.
- **Persist `last_stage` directly on the `Session` model.** Rejected.
  Stage transitions arrive from the headless worker out-of-process.
  Updating the Session each transition either requires the producer to
  hold a state-write capability (defeats the event-log model) or a
  consumer bridge that writes events → state (defeats the
  derive-at-render simplicity). Render-time derivation keeps `state.json`
  small and stage logic in one place.
- **Use `STAGE_ERRORED` to update `last_stage`.** Rejected. A producer
  that hits a transient block at `s2_impl_started` and recovers would
  briefly render as "errored at s2" before re-entering — confusing for
  operators glancing at the dashboard. `STAGE_ERRORED` belongs in
  `recent_events` and `cw event tail --type stage.errored` for
  diagnosis, not in the current-stage rollup.

## Referenced by

- #173, #213
