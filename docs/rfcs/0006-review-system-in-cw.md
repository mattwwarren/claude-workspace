# RFC 0006 — The Review System Lives in `cw` (Deterministic Brain), Skill Keeps Judgment

| Field | Value |
|---|---|
| Status | **Proposed** |
| Owner | @mattwwarren |
| Date | 2026-06-15 |
| Supersedes | none (promotes out-of-tree `.claude/scripts/review_monitor.py` into the package) |
| Related | RFC 0004 (work lanes — supplies the slot budget this RFC routes review work through), RFC 0005 (staged pipeline — the *outbound* sibling; shares the `cw schema` contract pattern), ADR 0006 (reaping authority — applies to review workers once they are lane sessions), ADR 0008 (#675 tracker seam — the deferred-thread filing path), #673 (`cw queue peek` — the promotion pilot), #674 (the `.claude/scripts` ↔ cw boundary), #664 (de-triplication), #668/#670/#671/#672 (import/launch fragility) |
| Branch | `claude/design-session-675-678-bty63i` |

## Summary

PR-feedback handling is core dev-flow responsibility — the same domain `cw`
already owns (dispatch, reconcile, dev-queue, lanes, board). Yet the
review-monitor logic lives as **out-of-tree `.claude/scripts/review_monitor.py`**:
~3,033 LOC of stateful argparse with ~19 `cmd_*` entrypoints
(register/check/status/nudge-ok/discover/ack-delta/…), JSON state under
`~/.claude/review-monitor/`, a `sys.path.insert` + `utils.runtime_paths` import
that is fragile by construction (#668/#671/#672), triplicated across repos
(#664), and symlink-deployed from a different repo than it is developed in.

This RFC **splits the system by nature**, exactly as RFC 0005 split the
outbound pipeline:

1. **The deterministic brain → `cw`.** Promote the state machine into a
   `cw.review` module behind a `cw review` subcommand group
   (`register`/`check`/`status`/`nudge-ok`/`discover`/`tick`). It becomes
   typed, unit-tested under `--cov=cw`, `mypy --strict`-clean, installed with
   the package (no triplication), importable without PYTHONPATH/symlink
   gymnastics.
2. **The LLM judgment → stays the `/review-monitor` skill.** Delta-review,
   approve/nudge/reply are model work. The skill stays but gets **thinner**: it
   calls `cw review check`/`status` and consumes their JSON instead of shelling
   a fragile script.
3. **The cron → a one-liner.** `cw review tick` replaces
   `~/.claude/scripts/review_monitor.py`.
4. **The resource governor → RFC 0004's lanes.** Because the brain now lives in
   cw, the work it dispatches (auto-fix re-pushes, delegated delta-review
   sessions) becomes **`TicketTask`s routed to a dedicated `review` lane**,
   capped by the existing Tier-2 allocator instead of an unbounded cron
   `claude -p`. This is the new capability the owner asked for: *intelligently
   use task delegation and queue lanes to keep resource limits on review
   sessions.*

This is the same architecture as RFC 0005: **cw owns deterministic state;
skills own LLM judgment; a typed `cw schema` contract sits between them.**
Where RFC 0005 is the *outbound* pipeline (plan→impl→review→PR), this RFC is
the *inbound* feedback loop (human review → auto-fix → re-push).

## What this absorbs or obsoletes

Per the ticket, promoting the brain dissolves a cluster of point-fixes:

| Issue | Today | After this RFC |
|---|---|---|
| #670 (CI import-smoke gate) | bespoke gate for a loose script | moot — in-package code is gated by the existing suite + `mypy --strict` |
| #671/#672 (launch fragility, `runtime_paths` bug) | `sys.path.insert` + env-var path resolution | moot — imported as `cw.review.*` |
| #674 (define `.claude/scripts` ↔ cw boundary) | open question | **this is the answer**: deterministic state → cw; judgment → skill |
| #673 (`cw queue peek`) | the pilot of this pattern | precedent; `cw review` is the second, larger application |
| #664 (single home / de-triplication) | three copies | solved by construction — one package |
| #668 (dead import) | latent breakage class | gone with the import model |

## Design decisions (locked)

| # | Decision | Choice |
|---|---|---|
| D1 | Where the brain lives | **`cw.review` module + `cw review` subcommand group.** Click, under `--cov=cw` and `mypy --strict`, same `@main.group(...)` idiom as `cw lane`/`cw queue`. |
| D2 | Subcommand surface name | **`cw review`** (not `cw pr`). Mirrors the `/review-monitor` skill and the "review" domain; avoids colliding with RFC 0005 FINALIZE's PR-open/ready operations. |
| D3 | State-file home | **Move under cw's state dir.** `~/.claude/review-monitor/*.json` → `state_dir()/"review-monitor"/` (`~/.local/share/cw/review-monitor/`) with a one-time idempotent migration. Aligns with cw owning its state; completes #664 by construction. |
| D4 | LLM judgment | **Stays the `/review-monitor` skill.** Delta-review and approve/nudge/reply are model work and cannot be a pure CLI subcommand. The skill calls `cw review` and consumes its JSON. |
| D5 | Skill ↔ cw contract | **`cw schema`.** Register the review state/output models in `schema.REGISTRY` so the skill consumes a typed, versioned contract — mirroring RFC 0005 leg 3. |
| D6 | Resource governance | **Review work flows through RFC 0004 lanes.** Auto-fix / delegated-review sessions are `TicketTask`s in a dedicated `review` lane with its own `max_parallel`, dispatched by the existing `dispatch_tick`. No new scheduler. The reap authority (ADR 0006) and `cw board` observability apply for free. |

## Core model

### The `cw.review` module

The deterministic brain — pure state machine + forge/git gathering, **no LLM
calls**:

- `MonitoredPR`, `MonitorState`, `ThreadStatus`, `CommentReviewRef` become
  typed Pydantic models (today plain classes in the script), so they serialize
  through the same `model_json_schema()` path as `TicketTask`/`Session`.
- The ~19 `cmd_*` entrypoints collapse into module functions invoked by Click
  subcommands. The argparse mutually-exclusive command categories
  (mutation/query/discovery/status) map onto `cw review <verb>`.
- Forge/git access (`_run_gh`, `_run_git`) routes through the **ADR-0008
  `Tracker` seam** for the tracker-specific reads (PR state, threads), so the
  review system is not independently gh-wired.

### The `cw review` subcommand surface

```
# Deterministic state machine (pure CLI — no model)
cw review register <pr> [--repo R]      # begin monitoring a PR
cw review check <pr> [--json]           # gather CI + threads + SHA-delta; emit actionable state
cw review status [--all|--repo R] [--json]
cw review nudge-ok <pr>                 # rate-limit gate for a nudge
cw review discover [--repo R]           # find author-role PRs to monitor
cw review tick                          # one full poll cycle (the cron entrypoint)
cw review drop|complete <pr>            # lifecycle
cw review ack-delta <pr> --sha <sha>    # re-baseline after a push
# (the remaining mutators — record-nudge, confirm-thread, set-status,
#  mark-* — port 1:1 as subcommands or `cw review state set` verbs)
```

`cw review check`/`status` emit the JSON the skill consumes; everything else
mutates state under cw's single state-lock discipline (the same
`mutate_state()` path used for `sessions.json`).

### The skill, thinner

`/review-monitor` keeps the judgment loop (delta-review a new SHA, decide
approve vs nudge vs reply, draft the reply) but its *plumbing* changes:

- Today: shells `~/.claude/scripts/review_monitor.py <cmd>` ~12 places.
- After: calls `cw review <cmd>` and consumes `cw schema show review-state` /
  `cw schema show review-check` for the contract.

The skill's GitHub *writes that require judgment* (posting a reply, approving)
stay in the skill/agent. The deterministic *reads and state transitions* move
to cw.

### The cron, a one-liner

```bash
# was: claude --dangerously-skip-permissions -p "Run /review-monitor…" …
# now: deterministic poll in cw; the skill is only invoked when tick says so
cw review tick
```

`cw review tick` does the deterministic poll (CI/threads/delta) for every
monitored PR and **enqueues actionable work** (see governance below). The
expensive LLM judgment is no longer run unconditionally every cron fire — it is
dispatched only when `tick` finds a PR that needs a model decision.

## Resource governance — review work on RFC 0004 lanes

This is the capability the promotion unlocks. Today the cron spawns **one
unbounded `claude -p /review-monitor`** that does the whole cycle and fans out
auto-fix agents inline — no slot budget, no reap authority, no visibility. Once
the brain is in cw, the work it produces is ordinary dispatchable work:

1. `cw review tick` polls deterministically (no model) and, for each PR that
   needs model work (a CI failure to auto-fix, a review thread needing a
   judgment reply), **enqueues a `TicketTask`** with `ticket_id` = the PR ref,
   routed to a dedicated **`review` lane**.
2. The existing `dispatch_tick` Tier-2 allocator spawns the
   delta-review/auto-fix worker **under `review_lane.max_parallel`**, inside
   the client ceiling and the global `max_parallel_clients` cap. Review work
   can no longer oversubscribe the host or starve impl work — it competes for a
   declared budget like everything else.
3. The worker is a normal DAEMON session, so **ADR-0006 reaping** (a stalled
   auto-fix session signals + parks rather than self-destructs) and **`cw
   board`** observability (RFC 0005) apply with zero new code.

```yaml
# clients.yaml — review gets its own slot budget, independent of impl
clients:
  my-client:
    lanes:
      - { name: default, max_parallel: 2, priority: 10 }   # impl work
      - { name: review,  max_parallel: 1, priority: 50 }   # PR feedback — preempts for free slots, capped at 1
```

`priority` lets review feedback take the *next free* slot ahead of steady-state
impl (non-destructive preemption, RFC 0004 invariant 1) without ever killing an
in-flight impl worker; `max_parallel: 1` caps concurrent review sessions
regardless of free ceiling (invariant 4).

**Design note — PR-as-work-item.** Review work reuses `TicketTask` with the PR
ref as `ticket_id` rather than introducing a parallel review-queue. This is the
DRY choice: it inherits the lane scheduler, the state lock, reap authority, and
board rendering wholesale. The cost is that `ticket_id` now sometimes denotes a
PR rather than an issue — acceptable because ADR-0008 already makes `ticket_id`
an opaque, tracker-grammar string. See [Open questions](#open-questions) Q1 for
whether review work wants a distinct queue field instead.

## The schema contract (`cw schema`)

`cw schema` already ships (`cli.py`, registry in `schema.py`). Register the
review models so the skill consumes a typed contract instead of parsing ad-hoc
JSON:

- `cw schema show review-state` — the persisted `MonitorState` shape.
- `cw schema show review-check` — the actionable output of `cw review check`
  (CI status, unresolved threads, SHA-delta, recommended action).
- Versioned with the rest of cw's schemas, so a skill/cw drift is caught at the
  contract, not at runtime.

## State migration (D3)

```
~/.claude/review-monitor/<repo_slug>.json   (legacy)
        │  one-time, idempotent, on first `cw review` invocation
        ▼
$XDG_DATA_HOME/cw/review-monitor/<repo_slug>.json
```

- `config.py` gains a `review_state_dir()` accessor under `state_dir()`
  (mirroring the existing `review_monitor_dir()` it replaces), so tests pick it
  up via the autouse `tmp_config_dir` fixture with no per-test patching.
- Migration: if the new dir is empty and the legacy
  `~/.claude/review-monitor/` exists, copy each `*.json` forward and leave the
  legacy in place (read-only) for one release, then a `cw doctor` check nudges
  removal. Never destructive on first contact.
- The legacy `.claude/review-monitor-state.json` single-file form (already
  migrated once in the script) is handled by the same forward-copy.

## Sequencing vs RFC 0005 (the `cli.py` / `dispatch.py` contention)

Both this RFC and RFC 0005 add subcommand groups to `cli.py` and consume the
dispatch loop. To avoid merge contention and a half-wired engine:

- **`cw review` is additive to `dispatch.py`.** It enqueues ordinary
  `TicketTask`s into a lane; it needs **no** change to the Tier-2 allocator
  (RFC 0004 already shipped it). The only `dispatch.py` touch is that review
  workers are spawned by the existing loop — which already handles arbitrary
  lanes. So `cw review` can land **before, during, or after** RFC 0005's stage
  engine without colliding on the allocator.
- **Schema registry is shared but append-only.** RFC 0005 registers
  stage-output schemas; this RFC registers review schemas. Both only *add*
  keys to `schema.REGISTRY` — no contention.
- **Recommendation:** land `cw review` **as its own track**, gated only on
  RFC 0004 (shipped) and ADR-0008 (the tracker seam, for the deferred-thread
  filing and PR-state reads). It does not need to wait for RFC 0005.

## Phasing

1. **Module extraction (behavior-preserving).** Port `review_monitor.py` →
   `cw.review` with typed models; `cw review` Click group wrapping the existing
   `cmd_*` logic 1:1; state still read from the legacy dir. Exit bar: the
   skill, repointed at `cw review`, passes a real PR poll cycle identical to
   today. Land under `--cov=cw` + `mypy --strict`.
2. **State migration + schema contract.** `review_state_dir()` under
   `state_dir()`, one-time forward-copy migration, register `review-state` /
   `review-check` in `schema.REGISTRY`, doctor check for legacy-dir cleanup.
3. **Cron → `cw review tick`.** Replace the bash cron + `claude -p` with the
   deterministic `cw review tick`; repoint the deploy (kill the cross-repo
   symlink — the script now installs with the package).
4. **Lane-routed dispatch (the governance payoff).** `cw review tick` enqueues
   actionable PRs as `TicketTask`s into the `review` lane; the existing
   allocator caps and schedules the delegated review/auto-fix sessions. Add the
   `review` lane to the relevant clients. Exit bar: review sessions visible in
   `cw board`, capped by `review_lane.max_parallel`, reap-gated per ADR-0006.

Phases 1–3 are pure promotion (no behavior change beyond where state lives and
what invokes the cycle). Phase 4 is the new capability and can follow once the
brain is stable in-package.

## Out of scope

- **The LLM judgment logic itself** — delta-review heuristics,
  approve/nudge/reply decisions stay in the skill, unchanged in substance.
- **Rewriting the auto-fix agent.** Phase 4 changes *how it is dispatched and
  capped* (a lane-budgeted `TicketTask`), not what it does once spawned.
- **Slack/desktop action-queue plumbing** (`runtime_paths.desktop_queue_dir`,
  the canvas/cursor machinery) beyond following the same state-dir move —
  re-homing that surface is a follow-up, not a blocker for the review brain.
- **Tracker abstraction itself** — owned by ADR-0008 (#675); this RFC consumes
  the seam, does not define it.

## Testing

Project conventions (1:1 test↔module, `pytest`, `freezegun`, `CliRunner`,
`mock_native_daemon`):

- `test_review.py` — the state machine: register→check→nudge-ok rate gate,
  SHA-delta re-baseline (`ack-delta`), thread-status transitions,
  complete/drop lifecycle. Replaces the script's zero in-suite coverage.
- `test_review_migration.py` — legacy `~/.claude/review-monitor/` → cw state
  dir forward-copy is idempotent and non-destructive; empty-new-dir guard.
- `test_cli.py` — `cw review` subcommands return the contracted JSON; `cw
  schema show review-state`/`review-check` are valid JSON Schema.
- `test_dispatch.py` (extend) — a `review`-lane `TicketTask` is allocated under
  `review_lane.max_parallel` and respects the client ceiling (reuses the
  RFC-0004 worked-example harness).
- Gates unchanged: ≥88% total / ≥90% patch, every new branch incl.
  `except`/error paths — the 3,033-LOC script's error paths become covered
  surface for the first time.

## Open questions

1. **PR-as-`TicketTask` vs a distinct field.** Reusing `ticket_id` for a PR ref
   is DRY but overloads the field's meaning. Alternative: a `work_kind:
   issue|pr` discriminator on `TicketTask`. (Proposed: overload for now;
   add the discriminator only if a real ambiguity appears — e.g. a PR ref that
   collides with an issue id.)
2. **One `review` lane per client, or a shared system lane?** Per-client gives
   independent budgets but multiplies lanes; a single cross-client `review`
   lane is simpler but couples unrelated repos' review concurrency. (Proposed:
   per-client, default `max_parallel: 1`, since clients already own their
   lanes.)
3. **Does `cw review tick` enqueue, or dispatch inline?** Enqueuing (Phase 4)
   gets the lane budget but adds one tick of latency before a review worker
   spawns. Acceptable for an hourly cron; revisit if a low-latency
   reply-to-reviewer path is wanted.
4. **`nudge-ok` rate limiting vs lane scheduling.** The script's nudge
   rate-limit and the lane's `max_parallel` are two different throttles. Confirm
   they compose (rate-limit gates *whether* to enqueue; lane gates *how many
   run at once*) rather than fighting.
