# RFC 0005 — Staged Pipeline & Heterogeneous Executors

| Field | Value |
|---|---|
| Status | **Partially Implemented — Phases 1–4 shipped in v1.2.0 (stage engine, per-stage commands, advance loop, `cw board`, schema seam, `ClaudeNativeExecutor`); Phase 5 (foreign executor backends) deferred to v1.2.x+** |
| Owner | @mattwwarren |
| Date | 2026-06-13 |
| Supersedes | none (extends the dispatch model on a new axis) |
| Related | RFC 0004 (work lanes — the *scheduling* axis), RFC 0006 (review system in cw — the *inbound* feedback loop; its REVIEW re-entries append to the stage ledger added below), ADR 0003 (stop-hook completion signal), ADR 0004 (stage events on the bus), ADR 0006 (reaping authority), `docs/events.md` |
| Branch | TBD |

## Summary

RFC 0004 redesigned the **scheduling axis** — how many parallel streams of
work run per client (`(client, lane)`, slot budgets, priority). This RFC
redesigns the orthogonal **pipeline axis** — the phases a single ticket moves
through on its way to a PR.

Today the plan→implement→review pipeline lives **entirely inside the
`/auto-dev` skill**, running as sequential steps in **one** daemon session
under **one** model (`ClientConfig.worker_model`, forwarded as `--model` at
spawn — `spawn.py:298`). cw spawns one session, the session stages internally,
and cw only ever sees an opaque start and a final sentinel. Two limits follow:

1. **No per-stage model heterogeneity.** A single session is a single
   executor. There is no way to let one model plan, a second implement, and a
   third review — which is the explicit goal as non-Claude agents (codex,
   GLM-class) come online in the 1.2 line.
2. **No pipeline observability.** cw owns *scheduling* position (which lane,
   running/blocked) but not *pipeline* position (planning vs implementing vs
   reviewing). The operator cannot see where a ticket actually is.

This RFC promotes **stage** to a first-class concept: a `TicketTask` carries a
`stage`, cw drives it through an ordered pipeline by spawning **one
short-lived session per stage** (each independently assignable to a different
**executor/model**), and the cross-stage handoff is an explicit, persisted,
**agent-agnostic** contract rather than shared in-session context. A live
in-terminal cockpit (`cw board`) renders the resulting `lane × stage` grid.

## The two axes (why this is a separate RFC, not an extension of 0004)

| Axis | First-class concept | Answers | Owned by |
|---|---|---|---|
| **Scheduling** (RFC 0004) | `lane` | *How many parallel streams run, who reaps stalls* | cw scheduler (`dispatch.py`) |
| **Pipeline** (this RFC) | `stage` | *Which phase a ticket is in, who executes that phase* | cw stage engine (new) |

These are independent. A ticket lives in exactly one `(client, lane)` and
walks the stage pipeline *within* that lane. Lanes do **not** become stages
(that would re-collapse the two axes); `LaneConfig` is untouched.

## Design decisions (locked)

| # | Decision | Choice |
|---|---|---|
| D1 | Stage as a new orthogonal axis | **Yes.** `TicketTask.stage`; lane semantics unchanged. Stage parameterizes *which executor/prompt/model* spawns — nothing else. |
| D2 | How stages advance | **Re-enter the normal `PENDING→RUNNING` cycle.** Advancing = mark the stage's session done → set `stage = next`, `status = PENDING` → next `dispatch_tick` re-claims and spawns the next stage. The existing Tier-2 per-lane `max_parallel` allocator is the concurrency guardrail *for free*; it never needs to know about stages. Cost: one tick of latency per handoff. |
| D3 | Where stage content lives | **Three legs.** Durable trace → ticket comments via the **active tracker** (`tracking.primary.system`), never a hardcoded forge — reuses `/auto-dev`'s Tracker Resolution dispatch (`github-issues`/`linear`/`notion`/`local`). Disposable detail → gitignored `.cw/` in the worktree. Contract → `cw schema`. cw owns *position* only; stages own *content*. |
| D4 | Executor model | **Pluggable `StageExecutor` seam** from day one. `ClaudeNativeExecutor` (wraps existing `spawn_create_impl` + `native_daemon`) is the only one shipped early; foreign backends (codex, GLM) are later phases against the same seam — required because `native_daemon` hardcodes `claude --bg`, so a non-Claude executor cannot be a flag. |
| D5 | Worktree per ticket | **One worktree per ticket, shared across all its stages.** Created at stage 1, removed at FINALIZE. Bounds disk (the real OOM axis); stage sessions are short-lived so live-RAM ≈ tickets-in-flight, unchanged from today. |
| D6 | PR lifecycle | REVIEW opens a **draft** PR + posts the review trace. FINALIZE scrubs ephemeral files, folds stray plan docs into real docs, marks the PR **ready-for-review**, and assigns reviewers. Splits "AI done self-reviewing" from "ready for humans." |

## Core model

### The `Stage` enum

```python
class Stage(StrEnum):
    HARDEN   = "harden"    # pre-flight: resolve ambiguities, post resolutions
    PLAN     = "plan"      # draft plan, approval gate
    IMPL     = "impl"      # implement, commit, push branch
    REVIEW   = "review"    # AI review pass → draft PR + deferred-work trace
    FINALIZE = "finalize"  # scrub .cw/, fold docs, un-draft PR, add reviewers
```

`Stage` is distinct from `SessionPurpose` (`models.py:15`), which classifies a
*worker* for reconcile sweeps and stays as-is. A stage session is
`origin=DAEMON`, `purpose=IMPL` (worker), and carries `stage=<phase>` for
observability — mirroring how `Session.lane` was added in schema v9
(`models.py:522`).

### `TicketTask` additions

```python
class TicketTask(BaseModel):
    ...
    stage: Stage = Stage.PLAN              # current pipeline position
    stage_base_ref: str | None = None      # commit each stage started from (idempotent re-run)
    stage_history: list[StageVisit] = []   # the ledger (see below)
```

`status` (PENDING/RUNNING/BLOCKED_ON_USER/…) continues to describe the *current
stage*. A task is e.g. "RUNNING in stage IMPL". HARDEN is opt-in per pipeline
config (see below); the default first stage is PLAN for parity with today.

### Stage history (the ledger) — effort & position accounting

*Added per the #678 design session: `stage` alone is point-in-time — it records
**where** a ticket is, not **how much** each phase cost or how many times it
looped. The operator requirement is both: "see how much planning, scoping,
implementation, review a ticket needed, and where in that process it lies."*

`stage` is the *current* position; an append-only `stage_history` ledger is the
record of how the ticket got there:

```python
class StageVisit(BaseModel):
    stage: Stage
    entered_at: datetime
    exited_at: datetime | None = None  # None = the open/current visit
    outcome: str | None = None         # "advanced" | "blocked" | "reworked"
    session_id: str | None = None      # links the visit to the worker → cost/audit
```

The ledger is maintained by the same advance loop that already moves `stage`
(`consume_completed_sessions`): entering a stage appends an open `StageVisit`;
advancing/blocking closes it (`exited_at`, `outcome`). `stage` stays as a cheap
denormalized read (= the open visit's `stage`) so existing readers are
untouched.

What it answers, with no extra state:

- **Where it lies** = the open visit (`exited_at is None`) — identical to
  `task.stage`, the value `cw board` already columns on.
- **How much each phase needed** = aggregate the ledger: *count of visits per
  stage* = rework rounds (three REVIEW visits = three review loops), *Σ
  (exited_at − entered_at) per stage* = time-in-phase, `session_id` links →
  cost via the session records. "This ticket: 2 plan passes, 1 impl, 3 review
  rounds, now in review."
- **Inbound review feedback re-enters here.** A human review round that
  triggers rework appends a fresh `StageVisit(stage=REVIEW, …)` to the *same*
  ticket — see RFC 0006. The PR feedback loop *is* the observability; it is
  **not** a separate PR-keyed work item (RFC 0006 rejects overloading
  `ticket_id`).

Idempotency: a re-run of a stage (reap recovery, retry) opens a new visit
rather than mutating a closed one, so the rework count is honest. Release
mapping: `stage_history` is an additive field with a safe default (`[]`),
landing with the other 1.1.x forward-compat seams below.

### Pipeline configuration (per-client, lane-overridable)

```python
class StageExecutorConfig(BaseModel):
    backend: str = "claude-native"      # 1.2: "codex", "glm", ...
    model: str | None = None            # --model; falls back to client.worker_model

class StagePipelineConfig(BaseModel):
    stages: list[Stage] = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
    executors: dict[Stage, StageExecutorConfig] = {}  # the heterogeneity map; default per stage = claude-native
```

The heterogeneous vision is literally:

```yaml
pipeline:
  stages: [harden, plan, impl, review, finalize]
  executors:
    plan:   { backend: claude-native, model: opus }
    impl:   { backend: glm }
    review: { backend: codex }
```

`StagePipelineConfig` lives on `ClientConfig`, overridable per `LaneConfig`.

### The `StageExecutor` seam

```python
class StageExecutor(Protocol):
    """Spawns one stage's work and knows how to recognise its completion."""
    def spawn(self, *, stage: Stage, task: TicketTask, worktree: Path,
              client: ClientConfig) -> Session: ...
    def stage_sentinel_schema(self, stage: Stage) -> dict: ...  # JSON Schema
```

- `ClaudeNativeExecutor` — v1, the only one shipped early. Wraps
  `spawn_create_impl` + `native_daemon`, forwards `--model`, recognises the
  stage sentinel. Stage prompts become **backend-agnostic templates** ("plan
  this ticket; emit artifact matching `cw schema stage-output plan`") so a
  non-Claude executor can translate them.
- `CodexExecutor` / `GlmExecutor` — later phases, same seam, own spawn backend.

## Data flow

Each `→` that re-queues is one `dispatch_tick`:

1. `cw dev-queue add <id> --lane X` → `TicketTask{stage=<first>, status=PENDING}`.
2. Tick claims it (consumes a lane slot), creates the per-ticket worktree
   (once), resolves the executor for the current stage, spawns the stage
   session. `status=RUNNING`. cw records `stage_base_ref = HEAD`.
3. The stage session does its work, **posts its own trace comment** (cw never
   touches content), then emits a stage-scoped sentinel validated against
   `cw schema stage-output <stage>`. Stop hook → `SESSION_COMPLETED`.
4. `consume_completed_sessions` (`dispatch.py:817`), generalized:
   - success + stages remain → advance `stage`, reset `status=PENDING`;
   - pause sentinel (`plan_pending_approval`, …) → `BLOCKED_ON_USER`
     (existing `PAUSED_FOR_USER_INPUT_STATUSES` path, verbatim);
   - failure / crash → `BLOCKED_ON_USER` (no blind auto-retry);
   - terminal stage → `COMPLETED`.
5. Repeat `PLAN → IMPL → REVIEW`, each a fresh session in the **same**
   worktree with its stage's resolved model/backend.
6. **REVIEW** pushes the branch, opens a **draft PR**, posts the review trace
   (deferred work especially).
7. **FINALIZE** scrubs `.cw/`, folds stray plan docs into real docs, marks the
   PR ready, assigns reviewers → `COMPLETED`. Worktree removed.

## Handoff protocol (the three legs)

### Leg 1 — Ticket trace comments (durable audit trail)

Each stage posts one marked comment before its sentinel, generalizing harden's
existing marker (`<!-- auto-dev-preflight-resolutions -->` →
`<!-- cw-stage:<name> -->`):

- `harden` → pre-flight resolutions (today's harden output, unchanged).
- `plan` → plan summary, key decisions, assumptions.
- `impl` → implementation status, edges/friction, deviations from plan.
- `review` → findings, **deferred work/decisions**, draft-PR link.
- `finalize` → final disposition, PR ready, reviewers assigned.

Stages read and post comments through the **active tracker**, never a
hardcoded forge. The tracker is resolved from `tracking.primary.system` in
`.claude/project-config.yaml` exactly as `/auto-dev`'s existing "Tracker
Resolution" dispatch table does (`github-issues` → `gh issue view/comment`,
`linear` → MCP `list_comments`/create-comment, `notion`/`local` → their
equivalents). cw's Python stays tracker-blind — it owns `ticket_id` as an
opaque string (`models.py:TicketTask`); all tracker I/O is the executor/skill
layer's job. The `<!-- cw-stage:<name> -->` markers are **plain-string
sentinels matched on read, not rendered HTML**, so they work uniformly across
GitHub, Linear, and Notion comments. The next stage consumes prior comments
natively, including non-Claude executors via whatever CLI/MCP the active
tracker exposes.

### Leg 2 — Ephemeral worktree files (disposable detail)

Verbose working detail too noisy for the ticket lives under a **gitignored
`.cw/` directory in the worktree** (`plan.md`, scratch). Consequences:

- Never reaches the PR — gitignored, so the scrub is automatic.
- A *performance cache, not source of truth*. If a worktree is recreated after
  a reap, the stage re-derives from the durable ticket comments and
  regenerates detail. **Ticket = truth, `.cw/` = cache.** This is what makes
  resume safe.
- FINALIZE's only manual scrub: if a stage wrote a plan doc into the *tracked*
  tree (e.g. `docs/plans/`), fold it into real docs and remove the stray.

**Enforcement of the `.cw/` ignore (not convention):**

- **Creation-time:** on worktree create (`worktree.py`), cw idempotently
  appends `.cw/` to `$GIT_COMMON_DIR/info/exclude` (check-then-append). This is
  git-native ignore-without-tracked-files — it never modifies the consumer
  project's committed `.gitignore`, so no diff/PR noise. Linked worktrees share
  `info/exclude` via the common dir, which is correct here (we want `.cw/`
  ignored across every worktree).
- **Finalize-time backstop:** FINALIZE asserts `git ls-files .cw/` is empty
  before readying the PR; a force-added ephemeral file routes `BLOCKED_ON_USER`
  with a clear reason. Converts "should be ignored" into "cannot leak."

### Leg 3 — The schema contract (`cw schema`)

What makes legs 1–2 work across heterogeneous agents. cw publishes every
Pydantic model as JSON Schema (`model_json_schema()`):

- `cw schema list` — enumerate available schemas.
- `cw schema show <name>` — one model's JSON Schema.
- `cw schema stage-output <stage>` — the sentinel + artifact a stage must emit.

An executor fetches the stage-output schema, the agent produces output,
validates against it, *then* emits the sentinel. A malformed codex/GLM output
fails fast at the source rather than corrupting the pipeline.

## Error handling, idempotency & reap interaction

- **Stage failure (non-pause)** leaves the task at its current stage and routes
  `BLOCKED_ON_USER` (per ADR-0006 `signal_only`) — never blind auto-retry,
  because a re-run impl stage could stack bad commits. The operator/authority
  chooses retry-stage or abandon.
- **Idempotent re-run.** Durable truth is ticket comments + the branch, so a
  re-run reads prior comments and current branch state:
  - **IMPL re-run** resets the branch to `stage_base_ref` (the recorded
    pre-stage commit) before re-implementing, not stacking on a half-done
    attempt.
  - **REVIEW re-run** updates the *existing* draft PR (looked up by branch),
    never creating a second.
  - **FINALIZE re-run** — un-draft + assign-reviewers are already idempotent.
- **Reap mid-pipeline (ADR-0006).** A stalled stage session → reconcile detects
  → `SESSION_REAP_PROPOSED` → `signal_only` parks the task `BLOCKED_ON_USER` at
  its current stage; the authority (`cw orchestrate run`) authorizes the revert
  → task returns to `PENDING` **at the same stage**. The worktree persists and
  prior artifacts are durable, so the next tick cleanly re-spawns that stage.
  The reap machinery is unchanged; it just operates on a task that carries a
  `stage`.

## Observability — `cw board` (live TUI)

A read-only, auto-refreshing **`lane × stage` grid**, a pure consumer of the
position-state above (no new state):

- **Rows** grouped `client → lane`; **columns** `HARDEN · PLAN · IMPL · REVIEW
  · FINALIZE`.
- **Cells** place each in-flight ticket in its current stage column — id,
  age-in-stage, resolved model/backend, PR link once present; color by status.
  Age-in-stage and a per-stage rework badge (e.g. `REVIEW ×3`) are read
  straight off `stage_history` — the ledger is what turns the board from "where
  is it" into "where is it, and how much has each phase cost so far."
- **Per-lane header:** slot usage `running/max_parallel` (the throughput
  guardrail at a glance).
- **Footer:** global live-session count vs ceiling (the OOM watch).
- **Data:** polls the same lock-free read path as `cw status` every N seconds.

**Tech:** `rich.Live` first (light dep, read-only covers the "where is my work"
need); `cw board --once` prints one frame for non-TTY/CI. `textual`
interactivity (drill into a ticket's trace; **re-prioritize from the board**)
is deferred. **Forward-compat constraint:** the board reads canonical state
mutated by existing commands (`cw lane` already carries `priority` + `paused`;
`cw dev-queue` handles ordering), so the future interactive write-path is
wiring keystrokes to commands that already exist — Phase 1 must keep the
prioritization levers first-class.

## harden-ticket → the HARDEN stage

harden-ticket is today an **orchestrator main-thread skill** that spawns a
read-only sweep subagent and posts a `Pre-flight Resolutions` comment — pure
stage shape (consume ticket+code → emit a durable ticket artifact the next
stage reads), but run in the operator's context, polluting it per ticket.

Under this RFC it becomes the **HARDEN stage**: a delegated stage session whose
executor runs the sweep and posts the `<!-- cw-stage:harden -->` comment via the
active tracker (the resolutions comment is just leg-1 content). This
removes the orchestrator-context cost and makes harden a uniform pipeline phase
rather than a manual pre-step. (Answers the originating question: harden is
**neither an agent nor a lane — it is a stage**, executed by an agent.)

## Phasing

1. **Stage engine, all-Claude/single-model.** `Stage`, `TicketTask.stage` +
   `stage_base_ref`, `StagePipelineConfig`, schema bumps, `/auto-dev`
   decomposed into per-stage entrypoints, advance loop in
   `consume_completed_sessions`, shared per-ticket worktree + `.cw/` exclude.
   **Exit bar: parity with today's monolith on a real ticket.**
2. **Handoff protocol + harden-as-stage + `cw schema`.** Trace comments,
   ephemeral `.cw/`, FINALIZE scrub/fold + draft→ready split, migrate
   harden-ticket into the HARDEN stage.
3. **Live TUI (`cw board`).** Reads Phase-1 state; can run alongside Phase 2.
4. **Per-stage model heterogeneity** (still Claude: opus-plan / sonnet-impl /
   sonnet-review) — proves the `executors` map end-to-end with no foreign
   backend.
5. **Foreign executor backends** (`CodexExecutor`, `GlmExecutor`) against the
   proven seam — the 1.2 enabler; hardest work last, on a stable contract.

## Release mapping

- **1.1.x (patch — forward-compat seams, dormant/additive, no behavior
  change):** additive model fields with safe defaults (`TicketTask.stage`,
  `stage_base_ref`, `stage_history`, `StagePipelineConfig`, `Session.stage`) + the schema-version
  bump; the `StageExecutor` Protocol + `ClaudeNativeExecutor` (unwired); `.cw/`
  worktree exclude; `cw schema` (independently useful now). Defaults preserve
  the current single-session monolith exactly, so 1.2 lands the engine without
  a big-bang migration.
- **1.2.0 (the feature — behavior change):** Phases 1–4 (engine wiring,
  handoff protocol, harden-as-stage, `cw board`, per-stage heterogeneity).
- **1.2.x / later:** Phase 5 foreign executor backends.

## Out of scope

- Foreign-backend *spawn implementation* (codex/GLM process management) beyond
  defining the seam — Phase 5 / 1.2.x.
- `textual` interactive board and board-driven mutation — deferred (forward-compat
  constraint recorded above).
- Lane/branch integration changes — RFC 0004 D4 stands; workers still PR into
  the client default branch.
- Reconciling the **pre-existing `project-config.yaml` divergence** — `/setup`
  writes flat `tracking.system` while the deployed config + `/auto-dev` read
  nested `tracking.primary.system` (and `queue-issues` reads the flat form).
  This RFC uses the canonical `tracking.primary.system` and does not depend on
  the flat path, but the divergence predates this work and should be fixed
  separately (a natural companion to the `cw schema` / `setup` surface).

## Testing

Project conventions (1:1 test↔module, `pytest`, `freezegun`, `CliRunner`,
`mock_native_daemon`):

- `test_stage.py` — advance state machine (success→advance, pause→blocked,
  failure→blocked, terminal→completed); `stage_base_ref` reset idempotency.
- `test_executor.py` — seam + `ClaudeNativeExecutor` via `mock_native_daemon`.
- `test_cli.py` — `cw schema` subcommands return valid JSON Schema; `cw board
  --once` frame.
- TUI tested as a **pure function** — `render_board(state) -> renderable`;
  never the `Live` loop.
- Integration — extend the tmux suite with one ticket walking
  `harden→…→finalize` driven by `FakeNativeDaemonClient`.
- Gates unchanged: ≥88% total / ≥90% patch, every new branch incl.
  `except`/error paths.
