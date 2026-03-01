# claude-workspace (`cw`)

Multi-session workspace orchestrator for Claude Code.

Manage multiple Claude Code sessions across projects and purposes (implementation, ideation, debt paydown) with the ability to background, switch, and resume without losing context.

## Installation

```bash
# Install from GitHub
uv tool install git+https://github.com/mattwwarren/claude-workspace.git

# Or pin to a specific release
uv tool install git+https://github.com/mattwwarren/claude-workspace.git@v0.4.0

# Or install from local clone
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
./scripts/install.sh
```

See [docs/INSTALL.md](docs/INSTALL.md) for full installation guide.

## Getting Started

```bash
# Add your first project
cw init my-project --path /path/to/repo

# Start working (launches Zellij with impl/idea/debt panes)
cw start my-project

# Background current session (auto-generates handoff context)
cw bg

# Resume later with handoff context injected
cw resume my-project/impl

# Check what's running
cw status
```

## Prerequisites

- [Zellij](https://zellij.dev/) - Terminal multiplexer
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) - AI coding assistant
- [uv](https://docs.astral.sh/uv/) - Python package manager
- [peon-ping](https://github.com/PeonPing/peon-ping) - Sound notifications when Claude needs attention (optional)

## Commands

| Command | Description |
|---------|-------------|
| `cw init <name> --path <path>` | Add a new project |
| `cw start <client>` | Start or resume sessions in Zellij |
| `cw bg` | Background current session (triggers handoff) |
| `cw resume <session>` | Resume a backgrounded session |
| `cw done <session>` | Mark a session as completed |
| `cw list` | List all sessions |
| `cw status` | Show session health dashboard |
| `cw queue add <client> "task"` | Queue work for later |
| `cw queue list <client>` | View queued items |
| `cw queue next <client>` | Claim next queued item |
| `cw config` | Show configuration |
| `cw run-claude` | Internal: pane command wrapper |
| `cw pane-exited` | Internal: pane exit handler |
| `cw completion <shell>` | Show shell completion snippet |

## Workflow

### For Humans

The core workflow is: **init** → **start** → **work** → **bg/resume** → **done**.

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ cw init  │───>│ cw start │───>│  (work)  │───>│  cw bg   │
└──────────┘    └──────────┘    └──────────┘    └──────────┘
                     ▲                               │
                     │          ┌──────────┐         │
                     └──────────│cw resume │<────────┘
                                └──────────┘
```

1. **`cw init`** registers a project (workspace path, branch, purposes)
2. **`cw start`** launches a Zellij session with one Claude Code pane per purpose (impl, idea, debt). Each pane runs `cw run-claude` which starts Claude with purpose-specific prompts.
3. **Work** happens inside each Claude Code pane — implementation, brainstorming, or debt cleanup.
4. **`cw bg`** backgrounds all panes: injects `/session-done` into each Claude instance, waits for handoff files, then marks sessions as backgrounded.
5. **`cw resume`** restarts a session with its handoff context auto-injected, so Claude picks up where it left off.
6. **`cw done`** marks a session as completed when the work is finished.

### For Agents (Claude Code Slash Commands)

Agents interact with `cw` through Claude Code slash commands that queue and execute work:

1. **`/queue-plan`** — Queue an approved plan for implementation. Adds it to the client's task queue with context files and success criteria.
2. **`/queue-debt`** — Queue a tech debt item for later cleanup.
3. **`/pull-and-execute`** — Pull the next queued item, spawn agent teams to implement it, review the results, and mark it complete.

This creates an async pipeline: humans (or planning sessions) populate the queue, and execution sessions drain it.

```
Planning Session          Queue              Execution Session
┌─────────────┐    ┌──────────────┐    ┌───────────────────┐
│ /queue-plan │───>│ cw queue add │───>│ /pull-and-execute │
│ /queue-debt │    │              │    │                   │
└─────────────┘    └──────────────┘    └───────────────────┘
```

### Multi-Client Workflow

```bash
# Start sessions for different projects
cw start client-a
cw start client-b

# Each gets its own Zellij tab with separate Claude panes
# Switch between clients using Zellij's tab navigation

# List all active sessions across clients
cw list
```

## Configuration

Config lives at `~/.config/cw/clients.yaml` (or `$XDG_CONFIG_HOME/cw/clients.yaml`).

```yaml
clients:
  my-project:
    workspace_path: /path/to/repo
    default_branch: main
    auto_purposes: [impl, idea, debt]
    purpose_prompts:
      impl: |
        Focus on implementation. Follow existing patterns.
```

See [config/CONFIG_REFERENCE.md](config/CONFIG_REFERENCE.md) for all options.

## How It Works

`cw` manages two things:

1. **Zellij layouts** - tabs per client, panes per purpose (impl, idea, debt)
2. **Session lifecycle** - start, background (with auto-handoff), and resume Claude Code sessions

Claude Code stays native in every terminal pane. `cw` just orchestrates around it.

## Shell Completion

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"
```

## License

This is free and unencumbered software released into the public domain. See [UNLICENSE](LICENSE).
