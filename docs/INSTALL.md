# Installing claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

## Prerequisites

Install these before installing cw:

| Tool | Required | Purpose | Install |
|------|----------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Yes | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Python 3.13+](https://python.org/) | Yes | Runtime | Via uv: `uv python install 3.13` |
| [cmux](https://github.com/cmuxio/cmux) (macOS) | Yes on macOS | Terminal multiplexer backend | See cmux install guide |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Yes | AI coding assistant | `npm install -g @anthropic-ai/claude-code` |
| [peon-ping](https://github.com/PeonPing/peon-ping) | No | Sound notifications when Claude needs attention | `curl -fsSL peonping.com/install \| bash` |

### Verifying Prerequisites

```bash
# Check each prerequisite
uv --version          # >= 0.4.0
python3 --version     # >= 3.13
claude --version      # any recent version
# On macOS, verify cmux is running (its daemon socket exists)
test -S ~/Library/Application\ Support/cmux/cmux.sock && echo cmux-ok
peon status           # optional
```

## 1.0 Prerequisites — One-time Setup Steps

### Accept the bypass-permissions disclaimer

Before `cw` can spawn background Claude sessions, the `claude` binary requires you to accept its bypass-permissions disclaimer. Run this once interactively:

```bash
claude --dangerously-skip-permissions
```

This persists `skipDangerousModePermissionPrompt: true` in `~/.claude/settings.json`. You only need to do this once; subsequent `cw` sessions will pick it up automatically.

### Verify with `cw doctor`

After installation, run `cw doctor` to confirm your environment is ready:

```
cw doctor
```

Expected output for a healthy setup:

```
  [OK]   bypass-disclaimer — accepted
  [OK]   claude-version — 2.1.150 (Claude Code)
  [WARN] daemon-reachable — roster.json not found at ... — daemon not started?
```

- `[OK] bypass-disclaimer — accepted` — disclaimer accepted; `cw` can spawn sessions.
- `[WARN] bypass-disclaimer — ...` — disclaimer not yet accepted; run `claude --dangerously-skip-permissions` interactively.
- `[OK] claude-version` — `claude` binary found and responsive.
- `[WARN] daemon-reachable` — the Claude native daemon has not been started yet; this resolves automatically when `cw` first spawns a worker session.

## Installation

### From GitHub (recommended)

```bash
uv tool install git+https://github.com/mattwwarren/claude-workspace.git
```

Pin to a specific release:

```bash
uv tool install git+https://github.com/mattwwarren/claude-workspace.git@v0.4.0
```

### From Local Clone

```bash
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
./scripts/install.sh
```

The install script runs `uv tool install --from . --force claude-workspace`, making `cw` globally available.

### For Development

```bash
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
uv sync                    # Install dependencies
uv run cw --help           # Run without global install
```

### Upgrading

```bash
# From GitHub (latest)
uv tool install --force git+https://github.com/mattwwarren/claude-workspace.git

# From local clone
cd claude-workspace
git pull
./scripts/install.sh
```

## Post-Install Setup

### 1. Verify Installation

```bash
cw --version    # Should print version
cw --help       # Should show available commands
```

### 2. Add Your First Project

```bash
# Interactive setup
cw init

# Or with arguments
cw init my-project --path /path/to/your/repo
```

This creates `~/.config/cw/clients.yaml` with your project configuration.

### 3. Enable Shell Completion

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"

# Fish (~/.config/fish/config.fish)
_CW_COMPLETE=fish_source cw | source
```

### 4. Start Your First Session

```bash
cw start my-project
```

This launches a multiplexer workspace with panes for each session purpose (impl, idea, debt).

## File Locations

| File | Location | Purpose |
|------|----------|---------|
| Client config | `~/.config/cw/clients.yaml` | Project definitions |
| Session state | `~/.local/share/cw/sessions.json` | Active session tracking |
| Event history | `~/.local/share/cw/history/` | Session event log |

All paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME` if set.

## Configuration Reference

See [config/CONFIG_REFERENCE.md](../config/CONFIG_REFERENCE.md) for all configuration options.

### Minimal Configuration

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
```

### Full Configuration

```yaml
clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    default_branch: main
    auto_purposes: [impl, idea, debt]
    purpose_prompts:
      impl: |
        Focus on implementation. Follow existing patterns.
      idea: |
        Brainstorm and explore design options.
      debt: |
        Address tech debt and refactoring.
```

## Troubleshooting

### `cw: command not found`

The `uv tool install` bin directory is not in your PATH.

```bash
# Check where uv installs tools
uv tool dir

# Add to PATH (bash/zsh)
export PATH="$HOME/.local/bin:$PATH"
```

### `No module named 'cw'`

The package wasn't installed correctly. Reinstall:

```bash
uv tool install --force git+https://github.com/mattwwarren/claude-workspace.git
```

### `Python 3.13 required`

cw requires Python 3.13+. Install it via uv:

```bash
uv python install 3.13
```

### Multiplexer not launching

On macOS, verify cmux is running and its socket is reachable:

```bash
test -S ~/Library/Application\ Support/cmux/cmux.sock && echo cmux-ok
```

### Permission errors on `~/.config/cw/`

```bash
mkdir -p ~/.config/cw ~/.local/share/cw
```

## Uninstalling

```bash
# Remove the tool
uv tool uninstall claude-workspace

# Optionally remove config and state
rm -rf ~/.config/cw ~/.local/share/cw
```
