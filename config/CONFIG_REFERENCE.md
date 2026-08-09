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
| `lanes` | list[LaneConfig] | `[]` | Named dispatch lanes (a scheduling boundary for dev-queue tickets; manage with `cw lane add/ls/pause/resume/rm`, target with `cw dev-queue add --lane` / `cw dev-queue move`). Each lane has `name` (required), `max_parallel: int = 1`, `priority: int = 0`, `paused: bool = false`, `description: str = ""`, `reap_policy: "signal_only" | "auto" | null = null` (null inherits the global `reap_policy` from `orchestrator.yaml`), `pipeline: PipelineConfig | null = null` (per-lane per-stage executor override — see [Pipeline Configuration](#pipeline-configuration--per-stage-model-pinning) below), `signoff: "operator" | null = null` (RFC 0007 Phase 3 — see [Operator Signoff Gates](#operator-signoff-gates-rfc-0007-phase-3) below), `gate_recipes: dict[str,bool] | null = null` (RFC 0009 Phase 4 — per-lane gate-recipe enablement; see [Gate Recipe Enablement](#gate-recipe-enablement-rfc-0009-phase-4) below), `review_recipes: dict[str,bool] | null = null` (RFC 0010 Phase 3 — per-lane review-recipe enablement; see [Review Recipe Enablement](#review-recipe-enablement-rfc-0010-phase-3) below), `codex_fix_loop_enabled: true | null = null` (#1553 — lane override for the codex backend's autonomous MUST_FIX fix loop; `null` defers to the global `default_codex_fix_loop_enabled` in `orchestrator.yaml`; see [Codex Fix-Loop Gate](#codex-fix-loop-gate-1465) below). When no lanes are declared, a single implicit `default` lane is synthesized. |
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

Each `backend` here corresponds to a harvest-authority mechanism documented in
[`docs/headless-contract.md` §11](../docs/headless-contract.md#11-result-publishing-harvest-authority-rfc-0012)
— `claude-native` harvests via the Stop hook (`stop_hook_harvest`), `codex`
and `local` harvest via their supervised-child executor (`executor_direct` /
`git_synthesis`) — for how each backend's end state ultimately reaches
`Session.last_result`.

### Local Backend (aider + LM Studio)

Set `backend: local` to delegate a stage to `aider` running against a local
OpenAI-compatible endpoint (e.g. LM Studio). The local model never emits a
sentinel — `cw` synthesises an `AutoDevResult` from git state after aider
commits.

> **Edit-format warning:** aider falls back to the **model default** edit
> format when it doesn't recognize the configured model id — which for any
> non-Claude / OpenAI-compatible model is `whole`. In `whole` mode aider
> regenerates the entire target file on every edit, which can blow a stage
> timeout on a large file and present as a **model/timeout failure** rather
> than the real cause. Set `edit-format: diff` in `~/.aider.conf.yml` (read
> per-exec, so it applies to an already-running dispatcher with no restart)
> — or `AIDER_EDIT_FORMAT=diff` in the *dispatcher's own* environment before
> it starts (a shell-profile export will not reach an already-running
> dispatcher).

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
- `edit-format` is not a `cw` config field — set `edit-format: diff` in `~/.aider.conf.yml` (or `AIDER_EDIT_FORMAT=diff` in the dispatcher's own environment) to avoid aider's `whole`-mode default on unrecognized model ids.

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

### Codex Fix-Loop Gate (#1465)

`CodexExecutor.spawn`'s Step 3 (`review` stage, `backend: codex`) delegates to
`run_review_with_fix_loop`, which runs a bounded, autonomous fix-cycle loop
(codex writes to the worktree and commits) whenever cycle 0's review blocks on
a MUST_FIX finding. The lane-scoped `codex_fix_loop_enabled` gates that loop
(#1553; see the `lanes` field above), falling through to the global
`default_codex_fix_loop_enabled` in `orchestrator.yaml` when the lane sets no
override. Both default to `false` as a fail-safe: turning on `review:
{backend: codex}` should not implicitly grant codex the ability to commit
fixes to the worktree on its own. With the gate off, a blocking cycle-0
review parks on `CODEX_MUST_FIX_FINDINGS` immediately — the pre-#1392
park-on-MUST_FIX behavior — with zero fix cycles attempted.

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    lanes:
      - name: codex-trial
        codex_fix_loop_enabled: true
```

```yaml
# orchestrator.yaml — flip the loop on for every lane that sets no override
default_codex_fix_loop_enabled: true
```

**Operator callout (#1553 migration):** `ClientConfig.codex_fix_loop_enabled`
no longer exists — the gate moved to the lane-scoped
`LaneConfig.codex_fix_loop_enabled` shown above, with
`OrchestratorConfig.default_codex_fix_loop_enabled` as the global fallback.
Delete any `codex_fix_loop_enabled: <bool>` line set directly on a client
block in `~/.config/cw/clients.yaml` — not a repo file, so this change does
not touch it automatically. A client whose value was already `false` (the
prior schema default, and also `default_codex_fix_loop_enabled`'s default)
needs no lane entry — deleting the line is a no-op change in effective
behavior. A client that relied on `codex_fix_loop_enabled: true` must move
it onto the relevant lane instead, per the first `yaml` block above.

**Cost posture for metered plans:** each selected reviewer role costs one
codex invocation per review pass (`_select_reviewer_roles` picks at most 4
roles on a small-tier pass — Code Quality, SysAdmin, Data Safety, Product
Manager — and at most 9 on a large-tier pass, adding Architecture, Test,
Performance, API Contract Validator, and Deployment). With the fix loop
enabled, a ticket that blocks on cycle 0 runs up to 5 more cycles, each one
fix invocation plus another full re-review pass, on top of the cycle-0
review: `6 × review_pass_size + 5` codex calls in the worst case — **29 calls
for a small-tier ticket, 59 for a large-tier ticket** — before the loop gives
up. On a metered GPT plan (e.g. a $20/mo trial), that is the number to check
against remaining quota before enabling the loop.

**Recommended posture:** for operators on a metered codex/GPT plan, leave
`codex_fix_loop_enabled: false`. This is already the schema default
(`LaneConfig.codex_fix_loop_enabled` / `default_codex_fix_loop_enabled`),
but the recommendation here is about invocation cost, not just
accidental-commit safety — with the gate off, a blocking cycle-0 review
parks on `CODEX_MUST_FIX_FINDINGS` at 1× review_pass_size codex calls
instead of up to `6 × review_pass_size + 5`.
Only enable the loop once the worst-case call count above has been checked
against the plan's remaining quota for the billing period.

**Design option, not implemented:** a per-lane cap on codex invocations per
ticket (e.g. a hypothetical `codex_invocation_ceiling` on the lane config)
could hard-stop the fix loop before it reaches the worst case above,
independent of `_MAX_FIX_CYCLES`. No such field exists today — this is
flagged as a possible follow-up, not a commitment.

### Codex Reviewer Profile (Lean Profile, #1711)

A `codex exec` invocation launched by `cw` reads more than the prompt `cw`
gives it. By default it also picks up the operator's `~/.codex/config.toml`,
the repo's own `AGENTS.md`/project doc, whatever MCP servers the operator has
configured, and an evolving set of optional feature surfaces. None of those
are inputs `cw` chose, so two operators running the same review can get
different reviewers. The lean profile closes those channels unconditionally on
both the read-only reviewer path (`_build_generic_codex_argv`) and the
write-capable fix path (`_build_fix_codex_argv`).

**`reasoning_effort` (`StageExecutorConfig` field, default `"high"`)**

Pins `-c model_reasoning_effort=<value>` on reviewer and fix invocations. It
resolves through
`resolve_executor_config()`'s **three-level** lane > client > default
precedence — the same path `backend` and `endpoint` take (see that function's
docstring). This is *not* the 4-level precedence documented under "4-Level
Precedence" above: that chain is specific to the `model` field and its
`client.worker_model` fallback, which `CodexExecutor` never reads.

The field default `"high"` **is** the "default" tier — a bare
`StageExecutorConfig()` already resolves to `"high"`. Setting it explicitly to
`null` means "do not pin it", leaving codex's own default in force.

`"high"` is a **starting position, not a benchmarked optimum.** No measurement
justifies it yet; `cw.codex_review._benchmark.run_reasoning_effort_benchmark`
exists to produce that measurement (it runs the same roles and prompts once per
effort value and compares findings, degraded roles, wall clock, and token
totals). Revisit the default once there are numbers.

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    pipeline:
      executors:
        review:
          backend: codex
          reasoning_effort: high
    lanes:
      - name: cheap-review
        pipeline:
          executors:
            review:
              backend: codex
              reasoning_effort: medium   # lane wins over the client value
```

**What the profile disables**

The complete 104-feature inventory and default-enabled set captured from
codex-cli 0.147.0 are versioned in code. The lean profile explicitly denies
the following eleven ticket-defined surfaces via `--disable <feature>`
(documented sugar for `-c features.<name>=false`):
`hooks`, `memories`, `multi_agent`,
`multi_agent_v2`, `plugins`, `plugin_sharing`, `browser_use`,
`browser_use_external`, `browser_use_full_cdp_access`, `computer_use`,
`personality`. A captured, complete `codex features list` contract test pins
that inventory to its CLI version. Before any reviewer role launches, `cw`
records the floating runtime CLI version in profile diagnostics. Strict config
validation, rather than an exact-version gate, rejects profile flags that the
installed CLI does not recognize.

**MCP servers are disabled separately**, via `-c mcp_servers={}` — *not* via
`--disable`. MCP servers are not a member of codex's `features` list, so
`--disable`, being `features.<name>=false` sugar, structurally cannot reach
them; a direct config override is the only mechanism that can. This is why the
flag is listed apart from the eleven above rather than folded in with them.

Plus two whole-channel closures on both paths:
- `--ignore-user-config` — drops `~/.codex/config.toml`.
- `-c project_doc_max_bytes=0` — stops codex inlining the repo's
  `AGENTS.md`/project doc. `cw` already inlines every instruction the reviewer
  should see; a second, unversioned instruction channel is the thing this
  profile exists to close.

**Why the `-c` overrides are trustworthy: `--strict-config`**

Every invocation also carries `--strict-config`, which makes codex *reject* an
unknown `-c` key rather than silently ignoring it. Verified against codex-cli
0.147.0: `-c bogus_key_xyz=1` exits 1 with

```
Error loading config.toml: unknown configuration field `bogus_key_xyz` in -c/--config override
```

Without that flag, a typo'd or removed config key would leave the profile
quietly not applied. With it, a stale override fails the run loudly instead.

**Per-session diagnostics: `codex-review-profile.json`**

Every review pass writes one profile artifact under the session's diagnostics
dir (`~/.local/share/cw/sessions/<session_id>/diagnostics/`), answering "what
profile did *this* review actually run under":

- `profile_version` — bumped when the argv block changes shape.
- `reasoning_effort` — the resolved value (`null` if unpinned).
- `effective_model` — the model `cw` resolved and passed on the argv. No codex
  event carries a model field, so this is `cw`'s answer, not codex's.
- `cli_version` — parsed `codex --version`.
- `feature_inventory_cli_version` — the CLI version whose captured feature
  inventory was used; `null` when no inventory matches the probed CLI.
- `enabled_tool_classes` — actual codex JSONL tool-call classes available to
  the read-only reviewer for the matching CLI-versioned inventory. The
  `0.147.0` inventory reports only `command_execution`; this field is `null`
  when no inventory matches the probed CLI. Feature-flag names are not tool
  classes and are kept in the separately versioned feature inventory above.
- `instruction_sources` — which prompt-instruction channels actually
  contributed content, unioned across every role in the pass, in a fixed
  canonical order. Vocabulary: `role_spec`, `output_format_supplement`,
  `ticket_context`, `approved_plan`, `project_rubrics`, `repo_policy`,
  `lint_grounding`, `sensitive_files`. A channel appears only when its content
  was non-empty — a zero-byte `.cw/plan.md` does not put `approved_plan` in the
  list. `null` means the caller did not compute source provenance; `[]` is
  reserved for a computed result in which no channel contributed content.

Writing this artifact never blocks a review: a failed write is logged at
WARNING and swallowed, matching the capability-probe diagnostics' contract.
The reasoning-effort benchmark preserves both passes as
`codex-review-profile-medium.json` and `codex-review-profile-high.json` under
the same real session directory.

## Orchestrator Configuration (`~/.claude-workspace/orchestrator.yaml`)

Controls the autonomous dispatch loop. Created with defaults on first run.

```yaml
tick_interval_seconds: 30

# Two-knob scheduler (RFC 0004 Phase 2). default_ceiling caps concurrent
# sessions per client (per_client_ceiling overrides per client);
# max_parallel_clients caps how many clients are eligible per tick
# (null = no limit). The legacy names default_max_parallel /
# per_client_max_parallel are deprecated aliases, still read on load.
default_ceiling: 1
per_client_ceiling: {}
max_parallel_clients: null

# Host-capacity admission gate (#1444). Fleet-wide ceiling on concurrently-
# running DAEMON sessions across the WHOLE HOST, independent of (and folded
# into, via min()) the per-client available_client_slots math above. null
# (default) = feature off, byte-identical to pre-#1444 behavior. When set,
# excess PENDING work is never rejected/killed — a client whose turn this
# tick would exceed the remaining host budget is simply skipped with
# skip_reason=host_capacity_gated and retried on a later tick. The count
# excludes any DAEMON session whose owning TicketTask has been parked
# BLOCKED_ON_USER / AWAITING_OPERATOR_SIGNOFF by reconcile, so a stalled
# ("ghost") session under the default signal_only reap policy does not
# permanently strand a unit of host budget.
host_session_budget: null

linear_prefix_map: {}
# ^ routes a ticket-id prefix to the client/repo that owns it in a
# multi-client cw deployment — it is not provider selection. cw does not
# choose between tracker implementations in Python; the daemon's only
# programmatic tracker client is gh.py (GitHub-only), and all
# provider-portable ticket I/O is delegated to agent-native tools. See
# ADR-0013 (../docs/adr/0013-agent-delegated-ticket-work.md) for the full
# boundary.

operator_github_login_by_repo: {}
# ^ repo-keyed override for the GitHub login treated as "the operator" at
# client-less entry points (RFC 0011 follow-up, #1171) — see "Operator
# GitHub Login Override" below.

# REMOVED — process-kill timeouts (ADR-0014). The wall-clock budget and
# idle-watchdog machinery no longer exists: sessions are never dispositioned
# (TIMED_OUT, reverted, parked, daemon-stopped, worktree-removed) on elapsed
# time or transcript quietness. The following keys are ignored if present in
# an existing config (a one-time warning is logged at load): 
#   headless_timeout_by_tier, headless_timeout_by_stage,
#   idle_watchdog_by_tier, idle_watchdog_by_stage, idle_watchdog_seconds,
#   idle_retry_cap_by_tier, stalled_retry_cap_by_tier,
#   idle_confirm_observations, park_veto_cap,
#   salvage_skip_attention_threshold
# (`cw dev-queue add --timeout` was removed with them.)
# What replaces the signal those timers provided: the liveness-bucket ladder
# (`liveness_buckets_minutes` below) emits session.liveness_changed on
# transcript-staleness crossings, and a crossing into the top bucket by a
# roster-present session with no sentinel and no pending subagent emits a
# signal-only session.needs_attention (paused_status=session_unresponsive)
# plus a push notification — the operator decides what happens next.

# Absolute ceiling on task.attempts across ALL causes (#786) — a spawn-time
# admission gate (parks BLOCKED_ON_USER instead of spawning again), not a
# process-kill timer.
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

# session.needs_attention escalation latch: consecutive per-client
# freshness-gate blocks (RFC 0007 W2) at which a needs_attention event fires
# exactly once.
freshness_block_attention_threshold: 5

# Maximum consecutive sentinel-stage-mismatch vetoes the phantom sweep grants a
# single already_refused session before letting the pending CRASH_COMPLETE
# proceed (#1449). Counts only vetoes that fire while the
# transcript is still LIVE on a session whose most recent tick refused a
# stage-mismatched sentinel, so 2 already reproduces the #1281 "would have
# crashed two sweeps after the refusal" window this bound fixes. On cap-fire an
# immediate session.needs_attention is emitted. Resets for free per pipeline
# episode (fresh Session object).
sentinel_mismatch_veto_cap: 2

# Review-recipe repeat-fire burst detector (RFC 0010 anomaly layer, #1201).
# When a single (ticket, recipe) records this many PR_ACTION_TAKEN events
# within review_recipe_repeat_fire_window_minutes, one
# session.needs_attention (paused_status="review_recipe_repeat_fire") fires on
# the exact crossing (no re-fire once past it) — surfacing a recipe thrashing
# on a PR that never clears its attention_state. The sibling `cw doctor`
# liveness check needs no config.
review_recipe_repeat_fire_threshold: 5
review_recipe_repeat_fire_window_minutes: 20

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

# SSH-agent-key preflight gate operator escape hatch (GitHub #1437). Default
# true (gate stays enforced) -- contrast concierge_enabled/gate_recipes_enabled/
# review_recipes_enabled above, all default-false because they gate NEW
# automation opting in. This field gates an already-live safety probe (#927):
# when the fleet-wide `ssh-add -l` probe reports no key loaded, every client
# stays PENDING (no claim, no attempts consumed) rather than risk a
# guaranteed-failing spawn. Setting this false bypasses that skip fleet-wide
# whenever the probe reports unavailable -- each bypass records a
# gate.ssh_key_bypassed event (forwarded to the operator channel by default,
# same as gate.auto_approved) so the operator sees every suppressed gate.
ssh_key_gate_enabled: true

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

# Global default for the proactive finalize hold (RFC 0011 A3, GitHub
# #1160), used when neither the ticket (hold_finalize) nor its lane
# (finalize_gate) sets an override. "auto" (default): no hold. "manual":
# every ticket stops at the REVIEW->FINALIZE checkpoint with disposition
# `finalize_gate_held`, released by an explicit `cw dev-queue approve`.
# Invalid values raise loudly (same fail-closed rationale as
# default_signoff above).
default_finalize_gate: auto

# Retention window (hours) for per-session executor-diagnostics bundles;
# dispatch_tick's cleanup pass removes older bundles (GitHub #1239 — see
# docs/diagnostics-retention.md).
diagnostics_retention_hours: 24

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
    - gate.auto_approve_held    # RFC 0011 A3 — a finalize hold declined an auto-approval
    - pr.action_taken           # RFC 0010 — a review recipe acted on a PR
    - pr.action_failed
    - gate.ssh_key_bypassed     # GitHub #1437 — operator bypassed the ssh-key gate probe
  task_transition_statuses:
    - blocked_on_user
    - awaiting_operator_signoff
    - completed
    - failed
    - cancelled
  liveness_min_bucket: stale_30m

# Attention Digest Window (RFC 0011 A6, #1162) -- shown with its defaults;
# omit these fields entirely to use them. A held (awaiting_operator /
# finalize_gate_held) session.needs_attention park buffers on the
# cw-operator SSE channel instead of paging immediately; the buffer flushes
# to one digest push per batch once BOTH the local-timezone delivery window
# below is open AND attention_digest_idle_floor_seconds has elapsed since
# the most recently buffered arrival. See docs/operator-channel.md's
# "Digest coalescing" section for the full contract.
#
# attention_digest_window_tz is fail-loud (like default_signoff, not
# reap_policy): an unresolvable IANA zone raises ValidationError at config
# load rather than silently falling back to UTC -- a typo here would
# otherwise silently open the window at the wrong local hour.
attention_digest_window_tz: America/New_York
attention_digest_window_start_hour: 8   # local to attention_digest_window_tz
attention_digest_window_end_hour: 20    # local to attention_digest_window_tz
attention_digest_idle_floor_seconds: 60

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

Override a single ticket's scope tier at enqueue time (the former
`--timeout` flag was removed with the process-kill timeouts — see ADR-0014):

```bash
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

- `address_review` — PR review came back `changes_requested` (a formal GitHub
  review OR a plain issue/PR comment carrying cw's own blocking-review
  vocabulary, #1195); dispatch an `/address-review` session to mechanically
  work the requested changes.
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

## Operator GitHub Login Override (RFC 0011 follow-up, #1171)

`ClientConfig.operator_github_login` (see [Client Fields](#client-fields)
above) overrides the GitHub login treated as "the operator" — but it only
applies at call sites that have a `ClientConfig` in hand. Three call sites are
client-less by construction (a PR-scoped or webhook entry point with no
client context to resolve): `cw review register <pr>`, the `review_requested`
webhook, and `hydrate_pr_states`'s serve-tick PR poll. For these,
`orchestrator.yaml`'s `operator_github_login_by_repo` (a `dict[str, str]`
keyed by exact-string `"owner/repo"`, case-sensitive, default `{}`) fills the
same role, keyed by the PR's repo instead of by client.

Precedence (highest to lowest):

1. `ClientConfig.operator_github_login` — wins when a `ClientConfig` exists
   and sets it. Not consulted by the three client-less call sites above (no
   `ClientConfig` exists for them to read).
2. `orchestrator.yaml`'s `operator_github_login_by_repo[repo]` — consulted
   only by the three client-less call sites, via
   `cw.operator_identity.resolve_operator_login_for_repo`.
3. `cached_gh_login()` — the runtime-resolved, process-cached `gh api user`
   login. Final fallback when neither of the above applies.

```yaml
# orchestrator.yaml
operator_github_login_by_repo:
  acme/widgets: alice-alt-account
```

`resolve_operator_login_for_repo` is a pure dict lookup — it never calls
`cached_gh_login()` itself, so populating this map adds no extra `gh api
user` subprocess calls even though `hydrate_pr_states` may call it once per
candidate PR repo per tick (see the #1195 RISK note on
`cw.pr_hydrate.hydrate_pr_states`).

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
- `wall_clock_budget_seconds` — always `null` since the process-kill-timeout
  removal (ADR-0014): no session is handed a kill deadline and the
  orchestrator never reaps on elapsed time. The key is retained for
  schema-shape compatibility only.
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
  - `headless_timeout_override` — always `null` (deprecated-inert; the
    per-ticket timeout was removed with the process-kill timeouts).
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
  "wall_clock_budget_seconds": null,
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
