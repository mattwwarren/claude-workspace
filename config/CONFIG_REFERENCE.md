# cw Configuration Reference

Complete reference for `cw` client configuration.

## Prerequisites

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** - AI coding assistant (required)
- **[uv](https://docs.astral.sh/uv/)** - Python package manager (for installation)

No terminal multiplexer is required — cw 1.0 removed the tmux/cmux backend and
spawns workers directly via `claude --bg` (see
[docs/MIGRATION-0.x-to-1.0.md](../docs/MIGRATION-0.x-to-1.0.md)).

## Quick Start

```bash
# Install
uv tool install "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git"

# Add your first project
cw init my-project --path /path/to/repo

# Start working
cw start my-project
```

## Config File Location

```
~/.config/cw/clients.yaml
```

Or, if `XDG_CONFIG_HOME` is set:

```
$XDG_CONFIG_HOME/cw/clients.yaml
```

State is stored at `~/.local/share/cw/` (or `$XDG_DATA_HOME/cw/`).

## Configuration Fields

### Client Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace_path` | path | *required* | Absolute path to the project repository |
| `default_branch` | string | `"main"` | Default git branch |
| `feature_branch_prefix` | string | `"dev"` | Prefix for the per-ticket feature branch the staged pipeline provisions and auto-dev workers push to: `<prefix>/<ticket_id>` (e.g. `dev/662`) |
| `auto_purposes` | list | `[idea, impl, debt]` | Session purposes to auto-start with `cw start` |
| `purpose_prompts` | dict | `{}` | Custom prompts per session purpose |
| `worktree_base` | path | *none* | Base directory for git worktree isolation |
| `notifications` | bool | `false` | Desktop notifications on session events. A top-level `notifications: true` key in `clients.yaml` (sibling of `clients:`) turns it on for every client that doesn't set it explicitly |
| `auto_background_threshold` | int \| null | `null` | Auto-background the session after N conversation turns |
| `worker_model` | string \| null | `null` | Pin the model for DAEMON-origin worker spawns (auto-dev). Forwarded as `--model <id>` to `claude --bg` from both initial spawn and DAEMON-origin resume. USER-origin sessions (interactive `cw start` / `cw resume`) always inherit the operator's logged-in default model. Opaque string — no validation. |
| `operator_github_login` | string \| null | `null` | Override the runtime-resolved GitHub login used for counterparty/self-identity resolution (RFC 0011 S1). Rare multi-account case; the runtime `gh api user` login is authoritative when unset. |
| `repo_path` | path | *none** | Shared repo path (worktree mode) |
| `branch` | string | *none** | Branch name (worktree mode) |
| `lanes` | list[LaneConfig] | `[]` | Named dispatch lanes (a scheduling boundary for dev-queue tickets; manage with `cw lane add/ls/pause/resume/rm`, target with `cw dev-queue add --lane` / `cw dev-queue move`). Each lane has `name` (required), `max_parallel: int = 1`, `priority: int = 0`, `paused: bool = false`, `description: str = ""`, `reap_policy: "signal_only" | "auto" | null = null` (null inherits the global `reap_policy` from `orchestrator.yaml`), `pipeline: PipelineConfig | null = null` (per-lane per-stage executor override — see [Pipeline Configuration](#pipeline-configuration--per-stage-model-pinning) below), `signoff: "operator" | null = null` (RFC 0007 Phase 3 — see [Operator Signoff Gates](#operator-signoff-gates-rfc-0007-phase-3) below), `gate_recipes: dict[str,bool] | null = null` (RFC 0009 Phase 4 — per-lane gate-recipe enablement; see [Gate Recipe Enablement](#gate-recipe-enablement-rfc-0009-phase-4) below), `review_recipes: dict[str,bool] | null = null` (RFC 0010 Phase 3 — per-lane review-recipe enablement; see [Review Recipe Enablement](#review-recipe-enablement-rfc-0010-phase-3) below). When no lanes are declared, a single implicit `default` lane is synthesized. |
| `pipeline` | PipelineConfig | standard 4-stage pipeline, no per-stage models | Per-stage executor configuration (RFC 0005): `stages` (default `[plan, impl, review, finalize]`) and `executors` (default `{}`). See [Pipeline Configuration](#pipeline-configuration--per-stage-model-pinning) below. |

\* Either `workspace_path` OR both `repo_path` + `branch` must be set.

## Session Purposes

Each client can have sessions for different purposes:

| Purpose | Description |
|---------|-------------|
| `impl` | Implementation — writing features, fixing bugs |
| `idea` | Ideation — brainstorming, architecture, design |
| `debt` | Debt — refactoring, cleanup, tech debt |

(A fourth purpose, `orchestrate`, exists for the session created by
`cw orchestrate start` / `cw orchestrator-start`; it is excluded from the
worker purposes and is not meant for `auto_purposes`.)

## Modes

### Standard Mode

Point directly to a project directory:

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    default_branch: main
```

### Worktree Mode

For multi-branch workflows from a shared repository. Each session gets its own git worktree:

```yaml
clients:
  feature-work:
    repo_path: /home/user/projects/shared-repo
    branch: feature/my-feature
```

## Example Configurations

### Single Project

```yaml
clients:
  my-app:
    workspace_path: /home/user/projects/my-app
    default_branch: main
```

### Multiple Projects

```yaml
clients:
  frontend:
    workspace_path: /home/user/projects/frontend
    default_branch: main
    auto_purposes: [impl, idea]

  backend:
    workspace_path: /home/user/projects/backend
    default_branch: main
    notifications: true
```

### Custom Session Prompts

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    default_branch: main
    purpose_prompts:
      impl: |
        You are working on the backend API.
        Follow the patterns in src/api/.
      idea: |
        Brainstorm features for the next sprint.
        Focus on user experience improvements.
```

### Worktree Mode with Base Directory

```yaml
clients:
  feature-a:
    repo_path: /home/user/projects/monorepo
    branch: feature-a
    worktree_base: /home/user/worktrees
```

### Cost Control — Pin Worker Model

`worker_model` constrains the model used by autonomous workers (auto-dev,
dispatch, planner) without affecting your interactive sessions:

```yaml
clients:
  thrifty-project:
    workspace_path: /home/user/projects/thrifty-project
    default_branch: main
    # Workers run on Sonnet (cheaper than Opus) for auto-dev tasks.
    # Interactive `cw start thrifty-project` still uses your default model.
    worker_model: claude-sonnet-4-6-20251015

  exploratory-project:
    workspace_path: /home/user/projects/exploratory-project
    default_branch: main
    # Workers pinned to Haiku — fast/cheap for simple debt tickets.
    worker_model: claude-haiku-4-5-20251001
```

Scope: forwarded as `--model <id>` to `claude --bg` from both
`spawn_create_impl` (initial DAEMON spawn) and `resume_session` (DAEMON-origin
resume of a dead surface). USER-origin sessions ignore this field.

Permission mode: some models (notably Haiku) do not support
`--permission-mode auto`, which would hang the `claude --bg` spawn
indefinitely (#1111). When a DAEMON-origin worker is pinned to a
non-auto-capable model, cw spawns it with `--permission-mode bypassPermissions`
instead — the same non-interactive posture already-supported Sonnet/Opus
workers run under. This requires the bypass-permissions disclaimer to have
been accepted once (`claude --dangerously-skip-permissions` interactively);
an unaccepted disclaimer surfaces as a clear `DisclaimerNotAcceptedError`
rather than a silent hang. Auto-capable pins (Sonnet 4.6+, Sonnet 5, Opus
4.6+) and unpinned clients are unaffected and continue to use `auto`.

## Pipeline Configuration — Per-Stage Model Pinning

RFC 0005 adds a `pipeline:` block that lets you assign a different model to each
pipeline stage (PLAN, IMPL, REVIEW, FINALIZE) for a single client. The `model`
field is an opaque string — no validation; any value is forwarded as-is to
`claude --bg --model <value>`.

```yaml
clients:
  staged-project:
    workspace_path: /path/to/repo
    default_branch: main
    pipeline:
      executors:
        plan:   { backend: claude-native, model: claude-opus-4-8 }
        impl:   { backend: claude-native, model: claude-sonnet-4-6-20251015 }
        review: { backend: claude-native, model: claude-sonnet-4-6-20251015 }
```

`backend` defaults to `claude-native` and can be omitted.

### Local Backend (aider + LM Studio)

Set `backend: local` to delegate a stage to `aider` running against a local
OpenAI-compatible endpoint (e.g. LM Studio). The local model never emits a
sentinel — `cw` synthesises an `AutoDevResult` from git state after aider
commits.

```yaml
clients:
  my-client:
    workspace_path: /home/user/projects/my-project
    pipeline:
      executors:
        impl:
          backend: local
          model: qwen2.5-coder-32b-instruct   # aider model; 'openai/' prefix added automatically
          endpoint: http://localhost:1234/v1   # LM Studio default
```

Requirements:
- `endpoint` must be set — a missing endpoint blocks with `endpoint_not_configured`.
- `aider` must be on `$PATH` — missing binary blocks with `aider_not_found` (retry-eligible).
- `.cw/plan.md` must exist in the worktree — absent plan blocks with `plan_missing`.
- `OPENAI_API_KEY` env var is optional; defaults to `"local"` (LM Studio ignores the value).

Limitations:
- Synchronous executor: blocks the dispatch tick for the full aider run. Set
  `max_parallel: 1` on lanes using `local` backend.
- Suitable for `impl` stage only; not yet validated for `plan`, `review`, or `finalize`.
- Review and finalize stages continue to use `claude-native` unless configured explicitly.

### 4-Level Precedence (highest to lowest)

- **Lane stage model** — `lanes[].pipeline.executors[stage].model`
- **Client stage model** — `pipeline.executors[stage].model`
- **`client.worker_model`** — client-level model fallback
- **Operator logged-in default** — no `--model` flag emitted; inherits the
  operator's active model

Resolution rule: `stage_config.model or client.worker_model`. If the stage has
no model configured (`null`) it falls through to `worker_model`. If neither is
set, no `--model` flag is emitted and the worker inherits the operator default.

**Known gap:** `cw resume` (the interactive USER-origin path) does not forward
the stage-resolved model. The autonomous pipeline routes through
`executor.spawn` and is unaffected. See #626 follow-ons.

### Recommended Per-Stage Defaults

Recommended models per stage:
- **plan/impl/review**: `claude-sonnet-4-6`
- **finalize**: `claude-haiku-4-5-20251001`

**Cost Rationale**:
- `haiku` is cheaper than `sonnet` ($0.25/$1.25 vs $3/$15 per 1M tokens (input/output)).
- `finalize` is mostly mechanical and can handle the reduced quality of `haiku`.

**Watch-finalize-on-haiku caveat**:
- PR-description synthesis, merge-gate adjudication, CI-failure triage are unhappy-path risks.
- Bump to `sonnet` if quality degrades.

**Intake Note**:
- No separate `intake` executor stage — intake (Stage 0) runs within the PLAN session.
- Dedicated cheap-intake tracked as #895.

## Orchestrator Configuration (`~/.claude-workspace/orchestrator.yaml`)

Controls the autonomous dispatch loop. Created with defaults on first run.

```yaml
tick_interval_seconds: 30

# Two-knob scheduler (RFC 0004 Phase 2). default_ceiling caps concurrent
# sessions per client (per_client_ceiling overrides per client);
# max_parallel_clients caps how many clients are eligible per tick
# (null = no limit). The legacy names default_max_parallel /
# per_client_max_parallel are deprecated aliases, still read on load.
default_ceiling: 2
per_client_ceiling: {}
max_parallel_clients: null

linear_prefix_map: {}

# Per-tier headless timeout budgets (seconds). Sessions whose scope.tier is
# known (from the auto-dev sentinel scope field) are budgeted by this map.
# Sessions without a known tier fall back to the global HEADLESS_TIMEOUT_SECONDS.
# Explicit per-ticket overrides (cw dev-queue add --timeout <s>) still win over
# both this and the per-stage map below. Per-stage budgets
# (`headless_timeout_by_stage`, see below) are consulted before this per-tier
# map; a stage present in that map fully overrides the per-tier lookup below
# for sessions at that stage.
headless_timeout_by_tier:
  small: 1800   # 30 min — tight cap for small-scope tickets
  large: 5400   # 90 min — room for 11-file, 600-line implementations

# Per-stage wall-clock timeout budgets (seconds), consulted BEFORE the
# per-tier default above. Keyed by Stage (plan/impl/review/finalize);
# HARDEN is a dormant stage and intentionally has no entry. A stage absent
# from this map falls through to headless_timeout_by_tier / the global
# HEADLESS_TIMEOUT_SECONDS fallback unchanged. Seeded from empirical stage
# timing baselines (wiki cw-stage-timing-baselines-2026-07-05, n=739 legs).
# Explicit per-ticket overrides (cw dev-queue add --timeout <s>) still win
# over both this and the per-tier default.
headless_timeout_by_stage:
  plan: 3600      # 60 min — wall-clock p99 38.5m, max 47.8m
  impl: 4200      # 70 min — wall-clock p99 38.6m, max 50.5m
  review: 7200    # 120 min — wall-clock p99 72.0m (matches prior -t 7200 workaround)
  finalize: 5400  # 90 min — matches existing large-tier ceiling (CI-wait tail)

# Per-tier idle-watchdog budgets (seconds). After this window of silence
# (no terminal sentinel emitted), a DAEMON session is flagged as
# BLOCKED_ON_USER and a push notification fires. Large-tier sessions can
# legitimately stall on slow test runs or mypy before emitting any
# sentinel. Sessions whose scope_hint is unknown fall back to the global
# IDLE_WATCHDOG_SECONDS (900s). See GitHub issues #326, #340.
idle_watchdog_by_tier:
  large: 3600   # 60 min — above worst-case FINALIZE gate-run (pytest+mypy); #918

# Global idle-watchdog budget (seconds) when a session has no per-tier
# budget (e.g. it stalled before Stage 1 set a scope_hint).
# null falls back to the built-in 900s constant.
idle_watchdog_seconds: null

# Number of consecutive failed idle-watchdog observations before a session
# is dispositioned (confirm-before-reap, #545). 1 reproduces the old
# single-observation behavior.
idle_confirm_observations: 2

# Retry caps. idle_retry_cap_by_tier / stalled_retry_cap_by_tier cap
# idle-stall (#384) and wall-clock-budget (#756) auto-retries per scope
# tier before a ticket is parked BLOCKED_ON_USER (built-in default: 2 for
# unknown tiers). global_attempt_ceiling is the absolute ceiling on
# task.attempts across ALL kill causes (#786).
idle_retry_cap_by_tier: {}
stalled_retry_cap_by_tier: {}
global_attempt_ceiling: 10

# Consecutive spawn errors at which a lane's circuit breaker trips and
# pauses the lane (resume via `cw lane resume`). See #875.
lane_circuit_breaker_threshold: 3

# Seconds to wait after a usage-limit cutoff before retrying.
usage_limit_backoff_seconds: 3600

# Elapsed seconds before reconcile routes an emitted-but-unrouted sentinel
# (the Stop hook never fired). See #578.
sentinel_unrouted_check_seconds: 300

# Transcript-staleness ladder for Session.liveness_bucket (RFC 0008 W2):
# thresholds in minutes for [stale_15m, stale_30m, stale_45m], plus a
# per-stage override of the entry-point floor (default: impl 35 min, so
# normal impl-stage quiet spells don't emit spurious stale_15m noise).
liveness_buckets_minutes: [15, 30, 45]
liveness_first_bucket_by_stage:
  impl: 35

# session.needs_attention escalation latches: consecutive per-client
# freshness-gate blocks (RFC 0007 W2) / consecutive per-session salvage
# skips (#974) at which a needs_attention event fires exactly once.
freshness_block_attention_threshold: 5
salvage_skip_attention_threshold: 5

# Reap policy: controls whether the reconciler destroys a stalled session
# or only signals for human intervention (ADR-0006 invariant 4).
#
# signal_only (default): when a session is detected as stalled/phantom,
#   the owning queue task is routed to BLOCKED_ON_USER. Session status,
#   worktree, and daemon surface are left intact for operator review.
#   Re-detection on subsequent ticks is an idempotent no-op.
#
# auto: pre-#554 self-healing — stop the daemon surface, revert the queue
#   task to PENDING for retry, and remove the worktree.
#
# MIGRATION: existing deployments wanting self-healing must set:
#   reap_policy: auto
reap_policy: signal_only

# Daemon-side mechanical recovery reactor (RFC 0008 capstone, GitHub #1015).
# Opt-in, default false: the 3 recipes below requeue/restore TicketTasks in
# ways adjacent to reap_policy's own destructive-action gate above (ADR-0006
# invariant 4) -- nothing fires without an explicit operator opt-in, same
# fail-safe posture as reap_policy's signal_only default.
#
# When enabled, 3 recipes run every reconcile tick, each individually
# toggleable via concierge_recoveries (merged onto the all-true defaults --
# NOT a full-replace map; setting one key never silently disables the
# others):
#   false_park_requeue        -- requeue a stalled_retry_cap_parked (or
#                                 null-disposition) BLOCKED_ON_USER row once
#                                 its owning session is confirmed dead
#                                 (absent from the daemon roster, transcript
#                                 flat).
#   park_marker_poison_clear   -- close + requeue a row behind a session
#                                 whose park marker (silently_idle /
#                                 needs_salvage) has persisted for
#                                 consecutive_salvage_skips >= 1 and whose
#                                 transcript is confirmed dead.
#   cancelled_row_restore      -- restore a CANCELLED row to PENDING when its
#                                 worktree still has committed work ahead of
#                                 its base branch, so work is never silently
#                                 lost to a stray cancel.
#
# The first two recipes gate on attempts < global_attempt_ceiling; at the
# ceiling the row is refused and left parked rather than requeued. See
# docs/dispatch-runbook.md's "Concierge & Watchdog" section (#11) for the
# full recipe preconditions and docs/events.md for the concierge.recovered
# event each recovery emits.
concierge_enabled: false
concierge_recoveries: {}
#   false_park_requeue: true
#   park_marker_poison_clear: true
#   cancelled_row_restore: true

# Gate-recipe automation master switch (RFC 0009, GitHub #1065/#1067). Default
# false, mirroring concierge_enabled's fail-safe posture: a gate recipe
# auto-clears an approval gate with NO human review, so nothing fires without
# an explicit operator opt-in. This is a hard top-level short-circuit -- when
# false, the whole gate-recipes module is a no-op regardless of any per-lane
# or per-ticket enablement. When true, each recipe is still gated per-lane /
# per-ticket via the 3-tier resolution below (both recipes default OFF). See
# Gate Recipe Enablement below.
gate_recipes_enabled: false

# Review-recipe automation master switch (RFC 0010, GitHub #1096/#1097).
# Default false, mirroring gate_recipes_enabled's fail-safe posture: when
# true, enabled review recipes react to PR review/CI/merge feedback with NO
# human in the loop (e.g. dispatching an /address-review session). Per-recipe
# enablement is still resolved per-lane / per-ticket -- see Review Recipe
# Enablement below.
review_recipes_enabled: false

# Minimum elapsed seconds between PR-state hydration passes in the serve tick
# (GitHub #929). Gated off max(pr_state.hydrated_at) across dev-queue tasks —
# no separate timer state. Each pass fetches `gh pr view` for every open PR
# referenced by a dev-queue task, so lowering this increases gh API load.
pr_hydration_interval_seconds: 150

# Thresholds `cw doctor` checks events/inbox.jsonl against (GitHub #856).
# The inbox grows unbounded by default; when either threshold is exceeded,
# `cw doctor` reports the "inbox-size" check as failing and suggests
# `cw event prune`. Read-only: doctor never mutates or prunes the inbox
# itself. See docs/events.md's "Prune events" section for `cw event prune`.
inbox_size_warn_bytes: 5000000    # 5 MB
inbox_line_count_warn: 15000

# Global default operator-signoff gate (RFC 0007 Phase 3, GitHub #990).
# "none" (default): no gate — the existing staged-advance rules apply.
# "operator": every ticket pauses at AWAITING_OPERATOR_SIGNOFF at the
#   REVIEW->FINALIZE checkpoint (the ship point) pending an explicit
#   `cw dev-queue approve`, unless overridden per-lane or per-ticket.
# Unlike reap_policy, an invalid value here raises loudly rather than
# silently falling back — a config typo must never silently disable the
# gate an operator is relying on. See Operator Signoff Gates below.
default_signoff: none

# Operator-attention forward-set for the cw-operator SSE channel (RFC 0008
# W3, GitHub #1002) -- a declarative filter over the orchestrator event bus.
# Shown here with its defaults; omit this block entirely to use them.
# Like default_signoff (not reap_policy), an invalid value here -- an
# unknown event type, QueueItemStatus, or LivenessBucket -- raises loudly
# and crashes `cw queue-channel serve` at startup rather than silently
# under-forwarding. See docs/operator-channel.md for the full reference.
operator_channel_forward:
  event_types:
    - task.transition
    - task.deleted
    - session.needs_attention
    - pr.registered
    - pr.ci_failed
    - pr.review_received
    - pr.mergeable
    - pr.merged
    - session.liveness_changed
    - operator.escalation
    - gate.auto_approved        # RFC 0009 — a gate recipe approved with no human review
    - gate.auto_approve_failed
    - pr.action_taken           # RFC 0010 — a review recipe acted on a PR
    - pr.action_failed
  task_transition_statuses:
    - blocked_on_user
    - awaiting_operator_signoff
    - completed
    - failed
    - cancelled
  liveness_min_bucket: stale_30m

# Tool-name patterns denied to EVERY DAEMON worker spawn, forwarded as a single
# `--disallowed-tools=<comma-joined>` token (GitHub #726/#733). Default empty:
# cw imposes no tool restriction on workers. Global by design — one fleet-wide
# policy, no per-lane/per-client override. Patterns use claude's
# `--disallowed-tools` glob syntax; entries must be non-blank and comma-free
# (comma is the join delimiter). Replaces the former hard-coded, tracker-gated
# Linear-MCP block.
#
# MIGRATION: a github-issues client that depended on the old automatic
#   mcp__plugin_linear_linear__* block (added in #726 to avoid a headless
#   Linear-OAuth stall) must now opt in explicitly:
#   disallowed_mcp_tools: ["mcp__plugin_linear_linear__*"]
disallowed_mcp_tools: []
```

Override a single ticket's budget or tier at enqueue time (there is no
per-ticket idle-watchdog flag — the idle budget resolves from
`idle_watchdog_by_tier` / `idle_watchdog_seconds` above):

```bash
cw dev-queue add GEN-123 --client my-project --timeout 7200
cw dev-queue add GEN-456 --client my-project --scope large
```

## Operator Signoff Gates (RFC 0007 Phase 3)

Lets an operator require an explicit signoff before a ticket ships, gating the
REVIEW→FINALIZE transition (the point at which a ticket would otherwise
auto-advance or complete unattended). Resolved with 3-tier precedence,
highest first:

1. **Per-ticket** — `cw dev-queue add GEN-123 --client my-project --signoff operator`
2. **Per-lane** — `signoff: operator` on a `LaneConfig` entry (see the `lanes`
   field above)
3. **Global default** — `default_signoff: operator` in `orchestrator.yaml`
   (see above)

When a ticket is gated, it parks at `AWAITING_OPERATOR_SIGNOFF` instead of
advancing (`cw dev-queue wait` exits `4`; `cw dev-queue status`'s lane
breakdown shows a `signoff=N` count alongside `blocked=N`). Clear the gate
with the same command used for an ordinary approval gate:

```bash
cw dev-queue approve GEN-123 --client my-project
```

A large-tier ticket with signoff configured requires **two** approvals at the
REVIEW stage: the first (ordinary `review_pending_approval`) approval re-routes
it to `AWAITING_OPERATOR_SIGNOFF` instead of advancing straight to FINALIZE; a
second `approve` clears the gate. To reject a signoff-parked ticket instead of
clearing it forward, regress it back to an earlier stage:

```bash
cw dev-queue requeue GEN-123 --client my-project --stage impl --regress
```

## Gate Recipe Enablement (RFC 0009 Phase 4)

Gate recipes (`cw.reconcile.gate_recipes`) auto-clear an approval gate with **no
human review** when a fixed predicate holds — `auto_approve_clean_review`
auto-approves a clean review, `auto_adopt_clean_plan` auto-adopts a
double-signed plan. Whether a recipe fires for a given ticket is resolved with
3-tier precedence, highest first:

1. **Per-ticket** — a `gate_recipes` map on the `TicketTask` (e.g.
   `{auto_approve_clean_review: true}`). There is **no CLI flag** for this tier
   yet; it is a data-model surface only.
2. **Per-lane** — a `gate_recipes` map on a `LaneConfig` entry (see the `lanes`
   field above).
3. **Hardcoded default** — both recipes default **OFF**. A recipe absent from
   the ticket map and the lane map is disabled.

Independently, the module-wide master switch `gate_recipes_enabled` (in
`orchestrator.yaml`, default `false`) is a hard top-level short-circuit: when
`false`, **no** recipe fires regardless of any per-lane or per-ticket setting.
The per-lane resolution above only matters once the master switch is `true`.

```yaml
# clients.yaml — enable auto-approve on one lane, leave the other off
clients:
  my-project:
    workspace_path: /path/to/repo
    lanes:
      - name: fastlane
        gate_recipes:
          auto_approve_clean_review: true
          auto_adopt_clean_plan: true
      - name: default   # both recipes stay off (hardcoded default)
```

Unrecognized recipe keys fail loud at config-load time (a typo like
`auto_aprove_clean_review` raises rather than silently no-opping).

## Review Recipe Enablement (RFC 0010 Phase 3)

Review recipes (`cw.reconcile.review_recipes`) are the review-feedback analogue
of the gate recipes above: each recipe reacts to a distinct PR **attention
state** (derived by `cw.pr_hydrate._compute_attention_state`) and takes a
matching action. The routing is 1:1 — a single PR is never a candidate for more
than one recipe. Recognized recipe keys (RFC 0010 P4, #1099):

- `address_review` — PR review came back `changes_requested`; dispatch an
  `/address-review` session to mechanically work the requested changes.
- `auto_fix_ci` — PR CI is failing (`ci_failing`); re-enqueue the ticket and run
  a dispatch tick to re-enter auto-dev (coarse re-dispatch, not a scoped fix).
- `request_reviewer` — PR needs a reviewer (`no_reviewer`); request one per the
  repo's [Review Strategy Config](#review-strategy-config-rfc-0010-phase-4)
  (a `ci`-mode repo silent-skips — it relies on CI, requesting no reviewer).
- `escalate_merge_block` — PR is `merge_blocked`; emit one durable
  `PR_ACTION_TAKEN` escalation per merge-blocked episode (a one-shot latch on
  `TicketTask.escalate_merge_block_fired_at` clears when the PR leaves the state).

They are a sibling of the gate recipes, **not** a reuse — a review recipe acts on
review/CI/merge feedback, a distinct action class from advancing an approval
gate, and operators configure the two independently. Whether a recipe fires for a
given ticket is resolved with 3-tier precedence, highest first:

1. **Per-ticket** — a `review_recipes` map on the `TicketTask` (e.g.
   `{address_review: true}`). There is **no CLI flag** for this tier yet; it is a
   data-model surface only.
2. **Per-lane** — a `review_recipes` map on a `LaneConfig` entry (see the `lanes`
   field above).
3. **Hardcoded default** — the recipe defaults **OFF**. A recipe absent from the
   ticket map and the lane map is disabled.

Independently, the module-wide master switch `review_recipes_enabled` (in
`orchestrator.yaml`, default `false`) is a hard top-level short-circuit: when
`false`, **no** review recipe fires regardless of any per-lane or per-ticket
setting. The per-lane resolution above only matters once the master switch is
`true`.

```yaml
# clients.yaml — enable review recipes on one lane, leave the other off
clients:
  my-project:
    workspace_path: /path/to/repo
    lanes:
      - name: fastlane
        review_recipes:
          address_review: true
          auto_fix_ci: true
      - name: default   # recipes stay off (hardcoded default)
```

Unrecognized recipe keys fail loud at config-load time (a typo like
`adress_review` raises rather than silently no-opping).

## Review Strategy Config (RFC 0010 Phase 4)

The `request_reviewer` review recipe (above) needs to know **who** to request as
a PR reviewer. That policy lives in the repo's `.claude/project-config.yaml`
(repo-committed, sibling to the `tracking:` / `review:` / `pr:` keys) — **not**
in `clients.yaml`, because it is a per-repo property, and **not** in the existing
`review:` key (that is auto-dev's own self-review loop, a different concern).

```yaml
# .claude/project-config.yaml
review_strategy:
  mode: ci                       # ci | repo_owner | reviewer_team
  repo_owner: <gh-login>         # required when mode: repo_owner
  reviewer_team: <org>/<team>    # required when mode: reviewer_team
```

- `mode: ci` (the default when the key or file is absent) — rely on CI; the
  `request_reviewer` recipe silent-skips, requesting no reviewer.
- `mode: repo_owner` — request the `repo_owner` GitHub login.
- `mode: reviewer_team` — request the `reviewer_team` `org/team` slug.

Resolution is fail-safe (`cw.review_strategy.resolve_review_strategy`): a missing
file, malformed YAML, or an unrecognized `mode` degrades silently to `ci` so
reconcile never wedges on a typo. A `repo_owner` / `reviewer_team` mode whose
handle is missing is surfaced by `cw doctor` as a **warning** (not a hard fail),
and the recipe's act phase records a `PR_ACTION_FAILED` correction rather than
requesting a bogus reviewer. For `claude-workspace` itself the key is set to
`mode: ci`, so `request_reviewer` is a documented no-op here.

## Sprint Buildout Config

`cw sprint plan` (driven by `/sprint-buildout`) turns an RFC's `## Tickets`
section into GitHub milestone/epic/ticket drafts. It needs to know this repo's
title/label/footer conventions — the `sprint_buildout:` block in
`.claude/project-config.yaml` (repo-committed, sibling to `tracking:` /
`review_strategy:` / `pr:`) records them so buildout is transcription, not
archaeology; it does not rediscover these conventions by reading prior issues
each time.

```yaml
# .claude/project-config.yaml
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
    labels: []                          # epics are deliberately unlabeled
    children_marker: "<!-- children -->"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    labels: [feature]
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})"
    footer_epic_clause: ", Epic #{epic}"  # appended only when the ticket has an epic
  notion:                               # omit this block ⇒ the Notion phase skips
    data_source: "collection://673ac7cd-797a-4c76-b9eb-fb5bc7ee050a"
    project_page: "38b59b27-0a42-81da-b234-ea951daa0216"
    sprint_page_properties:
      Type: Sprint
      Status: Planning
      Repo: claude-workspace
```

Every key is required except `notion:` and the two `labels:` lists (`epic.labels`,
`ticket.labels`), which default to `[]` when omitted — an empty label set is a
valid convention, not a config error. Omitting the `notion:` block entirely is
how the skill knows to skip its Notion mirroring phase — there is no separate
boolean flag; presence of the block is the enablement signal, but a *present*
`notion:` value that isn't a mapping (e.g. a stray `notion: true`) is treated
as malformed, not as an opt-out. A missing `sprint_buildout:` block itself, a
missing/malformed `milestone:`, `epic:`, or `ticket:` section, a missing
required key within one of those sections (e.g. `epic:` without
`children_marker`), or a malformed `notion:` value is a hard refusal
(`RfcContractError`), not a silent default — buildout output would otherwise
depend on guessed conventions.

`ticket.footer_epic_clause` is appended to `ticket.footer_pattern` **only**
when a ticket declares an epic. An epic-less ticket's footer omits the clause
entirely — it is not a template substitution rendering an empty or em-dash
placeholder value. For example, with the config above, a ticket with no epic
gets `Part of RFC 0011 Wave 0 (Sprint 0)`; a ticket with `Epic: I` gets `Part
of RFC 0011 Wave 1 (Sprint 1), Epic #I`.

## Worktree Context File (`.claude/cw-context.json`)

Written by `cw` into each DAEMON worktree at spawn time. The Stop hook reads it
to emit `SESSION_COMPLETED` events, and the `/auto-dev` worker reads it for
operational context. All fields are present in every context; optional fields
are `null` when not applicable.

### Always-present fields

- `schema_version` — integer, currently `2`. Increment when the shape changes.
- `session_id` — cw session ID (8-char hex).
- `session_name` — human-readable `<client>/<label>`.
- `client` — client name from `clients.yaml`.
- `purpose` — session purpose string (e.g. `"impl"`).
- `ticket_id` — Linear/GitHub issue ID, or `null` for non-ticket sessions.
- `headless` — `true` when spawned by the orchestrator dispatch loop.
- `worktree_path` — absolute, canonicalized path to the worktree root.
- `workspace_path` — the operator's main checkout — the FORBIDDEN path for any
  git mutation from a dispatch worker (guards read it to block `git commit`/
  `push` against the shared checkout, #766). `null` for USER-origin sessions
  without a client workspace.

### DAEMON task fields (present when `headless: true` and a `TicketTask` exists)

- `attempt` — 1-indexed current attempt number at the moment of spawn. `1` on
  the first spawn (`_claim_next_pending` increments `TicketTask.attempts` before
  calling `spawn_create_impl`); `2` on the first retry, etc.
- `wall_clock_budget_seconds` — seconds this session is allowed to run before
  the orchestrator reaps it. Computed by `resolve_headless_budget` (#314):
  priority is (1) per-ticket override, (1.5) `headless_timeout_by_stage`
  (#1020), (2) last sentinel's `scope.tier`, (2.5) `task.scope_hint`,
  (3) global default.
- `stage_started_at` — ISO 8601 UTC timestamp (`datetime.now(UTC).isoformat()`)
  written at spawn. Workers can use it to compute elapsed time without relying
  on wall-clock calls.
- `expected_sentinel_schema_ref` — pointer to the sentinel schema the worker
  must emit:
  - `command` — `"cw schema show auto-dev-result --format=tldr"`
  - `model` — `"AutoDevResult"`
  - `version` — `4`
- `queue_metadata` — snapshot of the task's scheduling fields at spawn:
  - `scope_hint` — `"small"` | `"large"` | `null`
  - `plan_source` — the task's plan provenance (e.g. `"generated"`,
    `"github_issue_existing"`), carried through from a prior stage's sentinel
    or a `cw dev-queue plan` run; `null` when none is set.
  - `headless_timeout_override` — per-ticket timeout in seconds, or `null`.
- `world_state_snapshot` — git context captured at spawn:
  - `origin_main_sha_at_spawn` — SHA of `origin/<default_branch>` at spawn time, or `null` if the git call fails.
  - `origin_main_branch` — the client's `default_branch` (usually `"main"`).
  - `prior_attempts_summary` — compact outcome summaries (status,
    stage_reached, blocker reason/details, friction highlights) of this
    ticket's prior terminal sessions, oldest first; `[]` on the first attempt.

### Example (DAEMON, scope_hint=large)

```json
{
  "schema_version": 2,
  "session_id": "a1b2c3d4",
  "session_name": "my-project/auto-dev-GEN-314",
  "client": "my-project",
  "purpose": "impl",
  "ticket_id": "GEN-314",
  "headless": true,
  "worktree_path": "/path/to/.claude/worktrees/my-worktree",
  "workspace_path": "/home/user/projects/my-project",
  "attempt": 1,
  "wall_clock_budget_seconds": 5400,
  "stage_started_at": "2026-06-10T14:32:00.123456+00:00",
  "expected_sentinel_schema_ref": {
    "command": "cw schema show auto-dev-result --format=tldr",
    "model": "AutoDevResult",
    "version": 4
  },
  "queue_metadata": {
    "scope_hint": "large",
    "plan_source": null,
    "headless_timeout_override": null
  },
  "world_state_snapshot": {
    "origin_main_sha_at_spawn": "c2e9096...",
    "origin_main_branch": "main",
    "prior_attempts_summary": []
  }
}
```

## MCP Server Configuration

`cw init` automatically writes `cw-queue-events` and `cw-pr-events` entries into
`<workspace>/.mcp.json`. The files `config/cw-queue-events.mcp.json.example` and
`config/cw-pr-events.mcp.json.example` are for manual wiring only and are not
required when using `cw init`.

`cw-operator` (see [`docs/operator-channel.md`](../docs/operator-channel.md))
is **manual wiring only** — `cw init` does not auto-wire it into `.mcp.json`.
Copy `config/cw-operator-events.mcp.json.example` in by hand. It shares the
same host/port as `cw-queue-events` (no separate `serve` process).

## Managing Configuration

```bash
# Add a new client
cw init my-project --path /path/to/repo

# Add with custom branch
cw init my-project --path /path/to/repo --branch develop

# Add with specific purposes
cw init my-project --path /path/to/repo --purposes impl,idea

# Interactive setup
cw init

# View current configuration
cw config
```

## Shell Completion

Enable tab completion for client names and commands:

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"

# Fish (~/.config/fish/config.fish)
_CW_COMPLETE=fish_source cw | source
```
