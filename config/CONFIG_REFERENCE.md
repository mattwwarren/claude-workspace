# cw Configuration Reference

Complete reference for `cw` client configuration.

## Prerequisites

- **[Zellij](https://zellij.dev/)** - Terminal multiplexer (required)
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** - AI coding assistant (required)
- **[uv](https://docs.astral.sh/uv/)** - Python package manager (for installation)

## Quick Start

```bash
# Install
uv tool install git+https://github.com/mattwwarren/claude-workspace.git

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
| `auto_purposes` | list | `[idea, impl, debt]` | Session purposes to auto-start with `cw start` |
| `purpose_prompts` | dict | `{}` | Custom prompts per session purpose |
| `worktree_base` | path | *none* | Base directory for git worktree isolation |
| `worker_model` | string \| null | `null` | Pin the model for DAEMON-origin worker spawns (auto-dev). Forwarded as `--model <id>` to `claude --bg` from both initial spawn and DAEMON-origin resume. USER-origin sessions (interactive `cw start` / `cw resume`) always inherit the operator's logged-in default model. Opaque string — no validation. |
| `repo_path` | path | *none** | Shared repo path (worktree mode) |
| `branch` | string | *none** | Branch name (worktree mode) |
| `lanes` | list[LaneConfig] | `[]` | Named dispatch lanes. Phase 1 (data model only); dispatch wiring in #558. Each lane has `name` (required), `max_parallel: int = 1`, `priority: int = 0`, `paused: bool = false`, `description: str = ""`, `reap_policy: str = "signal_only"`. |
| `pipeline` | PipelineConfig \| null | `null` | Per-stage executor configuration (RFC 0005). See [Pipeline Configuration](#pipeline-configuration--per-stage-model-pinning) below. |

\* Either `workspace_path` OR both `repo_path` + `branch` must be set.

## Session Purposes

Each client can have sessions for different purposes:

| Purpose | Description |
|---------|-------------|
| `impl` | Implementation — writing features, fixing bugs |
| `idea` | Ideation — brainstorming, architecture, design |
| `debt` | Debt — refactoring, cleanup, tech debt |
| `explore` | Exploration — research, codebase navigation |

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
default_max_parallel: 2
per_client_max_parallel: {}
linear_prefix_map: {}

# Per-tier headless timeout budgets (seconds). Sessions whose scope.tier is
# known (from the auto-dev sentinel scope field) are budgeted by this map.
# Sessions without a known tier fall back to the global HEADLESS_TIMEOUT_SECONDS.
# Explicit per-ticket overrides (cw dev-queue add --timeout <s>) always win.
headless_timeout_by_tier:
  small: 1800   # 30 min — tight cap for small-scope tickets
  large: 5400   # 90 min — room for 11-file, 600-line implementations

# Per-tier idle-watchdog budgets (seconds). After this window of silence
# (no terminal sentinel emitted), a DAEMON session is flagged as
# BLOCKED_ON_USER and a push notification fires. Large-tier sessions can
# legitimately stall on slow test runs or mypy before emitting any
# sentinel. Sessions whose scope_hint is unknown fall back to the global
# IDLE_WATCHDOG_SECONDS (900s). See GitHub issues #326, #340.
idle_watchdog_by_tier:
  large: 3600   # 60 min — above worst-case FINALIZE gate-run (pytest+mypy); #918

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
```

Override a single ticket's budget at enqueue time:

```bash
cw dev-queue add GEN-123 --client my-project --timeout 7200
cw dev-queue add GEN-456 --client my-project --idle-watchdog 600
```

## Worktree Context File (`.claude/cw-context.json`)

Written by `cw` into each DAEMON worktree at spawn time. The Stop hook reads it
to emit `SESSION_COMPLETED` events, and the `/auto-dev` worker reads it for
operational context. All fields are present in every context; optional fields
are `null` when not applicable.

### Always-present fields

- `schema_version` — integer, currently `1`. Increment when the shape changes.
- `session_id` — cw session ID (UUID).
- `session_name` — human-readable `<client>/<label>`.
- `client` — client name from `clients.yaml`.
- `purpose` — session purpose string (e.g. `"impl"`).
- `ticket_id` — Linear/GitHub issue ID, or `null` for non-ticket sessions.
- `headless` — `true` when spawned by the orchestrator dispatch loop.
- `worktree_path` — absolute, canonicalized path to the worktree root.

### DAEMON task fields (present when `headless: true` and a `TicketTask` exists)

- `attempt` — 1-indexed current attempt number at the moment of spawn. `1` on
  the first spawn (`_claim_next_pending` increments `TicketTask.attempts` before
  calling `spawn_create_impl`); `2` on the first retry, etc.
- `wall_clock_budget_seconds` — seconds this session is allowed to run before
  the orchestrator reaps it. Computed by `resolve_headless_budget` (#314):
  priority is (1) per-ticket override, (2) last sentinel's `scope.tier`, (2.5)
  `task.scope_hint`, (3) global default.
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
  - `plan_source` — always `null` today; reserved for a future `cw dev-queue plan` command.
  - `headless_timeout_override` — per-ticket timeout in seconds, or `null`.
- `world_state_snapshot` — git context captured at spawn:
  - `origin_main_sha_at_spawn` — SHA of `origin/main` at spawn time, or `null` if the git call fails.
  - `origin_main_branch` — always `"main"`.
  - `prior_attempts_summary` — always `[]` today; reserved for retry context.

### Example (DAEMON, scope_hint=large)

```json
{
  "schema_version": 1,
  "session_id": "a1b2c3d4-...",
  "session_name": "my-project/auto-dev-GEN-314",
  "client": "my-project",
  "purpose": "impl",
  "ticket_id": "GEN-314",
  "headless": true,
  "worktree_path": "/path/to/.claude/worktrees/my-worktree",
  "attempt": 0,
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
