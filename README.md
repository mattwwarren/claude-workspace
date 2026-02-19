# claude-workspace (`cw`)

Multi-session workspace orchestrator for Claude Code.

Manage multiple Claude Code sessions across clients and purposes (implementation, review, debt paydown, exploration) with the ability to background, switch, and resume without losing context.

## Quick Start

```bash
# Install
./scripts/install.sh

# Configure clients
vim ~/.config/cw/clients.yaml

# Start working
cw start personal              # Start impl session for 'personal' client
cw start sigma --purpose review # Start review session for 'sigma'
cw bg                          # Background current session (auto-handoff)
cw switch personal             # Switch to personal tab
cw resume sigma/review         # Resume backgrounded session
cw list                        # See all sessions
cw status                      # Dashboard view
```

## Prerequisites

- [Zellij](https://zellij.dev/) - Terminal multiplexer
- [Yazi](https://yazi-rs.github.io/) - Terminal file manager (optional, for tree pane)
- [Claude Code](https://claude.ai/claude-code) - The AI coding assistant

## How It Works

`cw` manages two things:

1. **Zellij layouts** - tabs per client, panes per purpose (impl, review, debt, explore)
2. **Session lifecycle** - start, background (with auto-handoff), and resume Claude Code sessions

Claude Code stays native in every terminal pane. `cw` just orchestrates around it.

See [ROADMAP.md](ROADMAP.md) for the full vision (v1 through JARVIS).
