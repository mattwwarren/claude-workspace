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
# legitimately stall >5min on slow test runs or mypy before emitting any
# sentinel. Sessions whose scope_hint is unknown fall back to the global
# IDLE_WATCHDOG_SECONDS (300s). See GitHub issue #326.
idle_watchdog_by_tier:
  large: 600    # 10 min — large-tier sessions may stall on slow builds
```

Override a single ticket's budget at enqueue time:

```bash
cw dev-queue add GEN-123 --client my-project --timeout 7200
cw dev-queue add GEN-456 --client my-project --idle-watchdog 600
```

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
