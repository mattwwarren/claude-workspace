# claude-workspace (`cw`)

Multi-session workspace orchestrator for Claude Code.

Manage multiple Claude Code sessions across projects and purposes (implementation, ideation, debt paydown, exploration) with the ability to background, switch, and resume without losing context.

## Installation

```bash
# Install from GitHub
uv tool install git+https://github.com/mattwwarren/claude-workspace.git

# Or pin to a specific release
uv tool install git+https://github.com/mattwwarren/claude-workspace.git@v0.3.0

# Or install from local clone
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
./scripts/install.sh
```

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
- [Yazi](https://yazi-rs.github.io/) - Terminal file manager (optional, for tree pane)

## Commands

```bash
cw init <name> --path <path>   # Add a new project
cw start <client>              # Start or resume sessions
cw bg                          # Background current session
cw resume <session>            # Resume a backgrounded session
cw list                        # List all sessions
cw status                      # Dashboard view
cw hand <purpose> "message"    # Send message to another session
cw delegate <client> "task"    # Spawn task in new pane
cw queue add <client> "task"   # Queue work for daemon
cw config                      # Show configuration
cw dashboard                   # Interactive TUI
```

## Configuration

Config lives at `~/.config/cw/clients.yaml` (or `$XDG_CONFIG_HOME/cw/clients.yaml`).

```yaml
clients:
  my-project:
    workspace_path: /path/to/repo
    default_branch: main
```

See [config/CONFIG_REFERENCE.md](config/CONFIG_REFERENCE.md) for all options including custom prompts, notifications, worktree mode, and auto-background.

## How It Works

`cw` manages two things:

1. **Zellij layouts** - tabs per client, panes per purpose (impl, idea, debt, explore)
2. **Session lifecycle** - start, background (with auto-handoff), and resume Claude Code sessions

Claude Code stays native in every terminal pane. `cw` just orchestrates around it.

## Shell Completion

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"
```

See [ROADMAP.md](ROADMAP.md) for the full vision (v1 through JARVIS).
