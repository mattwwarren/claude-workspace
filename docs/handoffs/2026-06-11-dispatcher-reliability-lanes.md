# Handoff: 1.1 Dispatcher Reliability + Lanes — reconciled with PR #386

**For:** a local Claude Code orchestrator session on the operator's machine.
**Origin:** remote session, branch `claude/milestone-1-1-queues-1g09ig`.
**Supersedes:** the first draft of this handoff (standalone "lane matrix"
ADRs), which was written before discovering **PR #386**. This version
**reconciles** that work with #386 rather than duplicating it.

---

## The authoritative base is PR #386

PR #386 (`claude/queue-system-redesign-7vG0t`, opened 2026-05-30) already
holds the deep design for the matrix + reliability work:

- **RFC 0004 — Work Lanes** (`docs/rfcs/0004-work-lanes-queue-redesign.md`).
  *This is the "Q matrix."* It locks the `(client, lane)` partition,
  `LaneConfig` in `clients.yaml`, a two-knob concurrency model
  (`max_parallel_clients` + `per_client_ceiling` + per-lane `max_parallel`),
  a **non-destructive scheduler** (Tier-1 client / Tier-2 lane allocation —
  *this is the leapfrog*), and first-class `SessionPurpose.ORCHESTRATE`
  sessions bound one-per-lane.
- **ADR 0005 — single state lock** (`mutate_state()` over all
  `sessions.json` writes; the S1 fix).
- **State-integrity audit S1–S6**, already ticketed (see below).

**Do not re-spec lanes.** RFC 0004 is the design of record. The only thing
this branch adds on top is the **reap-gating** layer (ADR-0006) and two
proposed **RFC-0004 amendments**.

## What changed on this branch after the reconcile

- **Dropped** the standalone lane ADR (it contradicted RFC 0004 D4 —
  per-lane worktree isolation, "channel" naming, `lane=client` migration).
  RFC 0004 owns the lane decision.
- **Renumbered** the reap-gating ADR to **ADR-0006**
  (`docs/adr/0006-reaping-is-gated-by-an-authority.md`); 0005 belongs to
  #386. Reframed so it *composes* with ADR-0005 + RFC 0004 instead of
  standing parallel.
- **Reconciled the tickets** (#552, #554–#561) against #386 — see the map.

## The one net-new thread: reap-gating (ADR-0006)

The operator's four beliefs, verified against #386:

| Belief | Verdict |
|---|---|
| Lanes/leapfrog already covered | ✓ RFC 0004 scheduler |
| Distress signal covered | ✓ event bus + push |
| **Auto-reaping must stop** | **✗ not covered — RFC 0004 *keeps* auto-revert; this is the gap** |
| Client≠filesystem | hinted; RFC 0004 D4 deliberately kept client=filesystem (amendment below) |

ADR-0005 (state lock) fixes the *lost-write* class (S1: "clobbered
TIMED_OUT revert ⇒ no retry"). It does **not** make reaping opt-in.
**ADR-0006** does: detect→signal, `reap_policy` default `signal_only`,
reap authority = the lane's ORCHESTRATE session. #542 (reaped-mid-wait) and
#402 (isolation breach) are the live bugs in this gap.

## Two proposed amendments to RFC 0004 (for the operator to fold into #386)

1. **Per-lane `reap_policy`.** Add `reap_policy: signal_only | auto` to
   `LaneConfig`; the Tier-2 allocator counts a `signal_only`-blocked
   session as occupying a slot. (RFC 0004 §"Reconciler interaction"
   currently says "no change to the revert path" — ADR-0006 amends that.)
2. **Client↔filesystem decoupling (open question, not yet a decision).**
   The operator's SaaS framing — *"client nomenclature isn't a useful tie
   to the filesystem; a service with many integrations has channels of work
   in each"* — goes **further** than RFC 0004 D4, which keeps
   client = repo/workspace and lane = sub-stream within it. Capture as an
   RFC-0004 open question: *should a lane be able to resolve to a different
   workspace than its client, making `client` a pure logical grouping?*
   Decide before Phase 3 (CLI/routing) hardens the `(client, lane)`
   assumption. **Operator decision required — do not implement either way.**

## Ticket reconciliation map

**RFC 0004 Phase 0 (state integrity) — already ticketed, NOT mine. Do these first; they unblock everything:**
- #387 (S1 state lock) · #388 (S1 sweep) · #389 (S2 hot-reload) ·
  #391 (S4 throttle lock) · #392 (S5 snapshot) · #393 (S6 inbox).
  #387 is milestone **1.0** — likely the session already in flight.

**My reap-gating tickets — KEEP (net-new, reframed to compose):**
| Ticket | Reframe |
|---|---|
| #552 (A1 split detect/act) | Land *after/with* #387 — the act phase writes via `mutate_state()`; do not add a second write path. |
| #554 (A2 `reap_policy`) | Per-lane field on RFC 0004 `LaneConfig`, not a standalone global only. |
| #555 (A3 `REAP_PROPOSED` + authz) | Payload carries `lane`; authority = ORCHESTRATE session. Fixes #542. |
| #556 (A4 doc inversion) | Also amends RFC 0004 §Reconciler interaction. |
| #560 (B4 reap authority ↔ lane) | The join. Authority = RFC 0004 ORCHESTRATE session; depends on #554/#555 + RFC 0004 Phase 4. |

**My lane tickets — REWRITE to conform to RFC 0004 (they were the first lane impl tickets, but mis-framed):**
| Ticket | Was (wrong) | Should be (RFC 0004) |
|---|---|---|
| #557 (B1 lane field) | `lane=client` migration, state+queue schema bump | `lane="default"` migration, dev-queue v2→v3 only (RFC 0004 Phase 1) |
| #558 (B2 lane dispatch) | `per_lane_max_parallel`, lane→worktree isolation | Tier-1/Tier-2 scheduler, two-knob model, `worktree.py` untouched (RFC 0004 Phase 2) |
| #559 (B3 client advertises lanes) | ad-hoc `--lane` | `cw lane add/ls/rm/pause`, `LaneConfig` in clients.yaml (RFC 0004 Phase 3) |
| #561 (B5 consumer audit) | re-point client→lane | grouping by `(client → lane)` per RFC 0004 CLI section (Phase 3) |

**Adjacent existing tickets to cross-link (don't refile):**
- #542 reaped-mid-wait → ATTENTION (the reap-gating bug) ·
  #402 isolation breach · #438 doctor --reap unguarded write ·
  #366 global concurrency ceiling (overlaps RFC 0004 knob A) ·
  #257 wave_id concurrency (overlaps lane policy) ·
  #507 dev-queue move (RFC 0004 lane re-routing) ·
  #209 doctor surface effective cap.

## Combined dispatch order

```
Wave 0 — RFC 0004 Phase 0 (state integrity):  #387 → #388, #389, #391, #392, #393
Wave 1 — RFC 0004 Phase 1 (lane data model):  #557 (rewritten)
Wave 2 — RFC 0004 Phase 2 (scheduler):        #558 (rewritten)   ← the leapfrog
Wave 3 — RFC 0004 Phase 3 (CLI/routing):      #559, #561 (rewritten)
─ reap-gating layer (parallel to Phases 1-3, gated on #387) ─
       #552 (A1) → #554 (A2), #555 (A3) → #556 (A4)
Wave 4 — RFC 0004 Phase 4 + the join:         ORCHESTRATE sessions → #560 (B4)
```

Reap-gating (#552/#554/#555/#556) only needs ADR-0005's `mutate_state`
(#387) — it can run alongside the lane phases. #560 is the capstone: it
needs both the gate (#554/#555) and the lane owner (RFC 0004 Phase 4).

## Open decisions for the operator

1. **Fold the two RFC-0004 amendments into #386**, or carry them as ADR-0006
   + separate follow-ups? (Recommend: per-lane `reap_policy` into #386;
   client↔filesystem stays an open question until you decide.)
2. **Client↔filesystem decoupling** — yes/no/defer (amendment 2 above).
3. **Reap-gating before or after the lane scheduler?** It only needs #387,
   so it can land early and de-risk the dogfood (no reaped workers mid-wave)
   before the bigger scheduler change.

## Pointers (verified on this branch)

- Reaper / sweeps: `reconcile.py` — `reconcile()`/`_reconcile_locked()`,
  `compute_drift`, `revert_stalled_headless_sessions`,
  `flag_silently_idle_daemon_sessions`, `_cleanup_timed_out_worktree`
  (the #404/#425 refuse-and-signal prototype ADR-0006 generalizes).
- Dispatch: `dispatch.py` `_claim_next_pending` (81), `dispatch_tick` (130).
- State lock target (ADR-0005/#387): `config.py` `load_state`/`save_state`
  (240/393) — `mutate_state()` lands here.
- Models: `models.py` `TicketTask` (163), `Session` (322),
  `OrchestratorConfig` (237), `OrchestratorEventType` (113).
- RFC 0004 + ADR 0005 live on PR #386's branch, not this one.
</content>
