# Installing claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

## Prerequisites

Install these before installing cw:

| Tool | Required | Purpose | Install |
|------|----------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Yes | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Python 3.13+](https://python.org/) | Yes | Runtime | Via uv: `uv python install 3.13` |
| [Zellij](https://zellij.dev/) | Yes | Terminal multiplexer | `cargo install zellij` or package manager |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Yes | AI coding assistant | `npm install -g @anthropic-ai/claude-code` |
| [Yazi](https://yazi-rs.github.io/) | No | File tree pane | `cargo install yazi-fm` or package manager |

### Verifying Prerequisites

```bash
# Check each prerequisite
uv --version          # >= 0.4.0
python3 --version     # >= 3.13
zellij --version      # any recent version
claude --version      # any recent version
yazi --version        # optional
```

## Installation

### From GitHub (recommended)

```bash
uv tool install git+https://github.com/mattwwarren/claude-workspace.git
```

Pin to a specific release:

```bash
uv tool install git+https://github.com/mattwwarren/claude-workspace.git@v0.3.0
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

This launches a Zellij layout with panes for each session purpose (impl, idea, debt).

## File Locations

| File | Location | Purpose |
|------|----------|---------|
| Client config | `~/.config/cw/clients.yaml` | Project definitions |
| Session state | `~/.local/share/cw/sessions.json` | Active session tracking |
| Zellij layouts | `~/.config/zellij/layouts/cw-*.kdl` | Generated layout files |
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
notifications: true  # global default

clients:
  my-project:
    workspace_path: /home/user/projects/my-project
    default_branch: main
    auto_purposes: [impl, idea, debt]
    notifications: true
    auto_background_threshold: 40
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

### Zellij not launching

Verify Zellij is installed and accessible:

```bash
zellij --version
which zellij
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
