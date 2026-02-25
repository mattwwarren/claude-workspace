---
description: Install or upgrade claude-workspace (cw) CLI tool
allowed-tools: Bash, Read, Write, AskUserQuestion
---

# Install claude-workspace (cw)

Install the `cw` multi-session workspace orchestrator for Claude Code.

## Step 1: Check Prerequisites

Verify each prerequisite is installed. Run these checks:

```bash
uv --version 2>/dev/null || echo "MISSING: uv"
python3 --version 2>/dev/null || echo "MISSING: python3"
zellij --version 2>/dev/null || echo "MISSING: zellij"
claude --version 2>/dev/null || echo "MISSING: claude"
```

If any required tools are missing, stop and tell the user what needs to be installed:

- **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Python 3.13+**: `uv python install 3.13`
- **Zellij**: Install from https://zellij.dev/documentation/installation (cargo, brew, or package manager)
- **Claude Code**: `npm install -g @anthropic-ai/claude-code`

Optional: check for yazi (`yazi --version`) - not required but enhances the file tree pane.

## Step 2: Install cw

Ask the user which installation method they prefer:
1. **From GitHub** (recommended for users): `uv tool install git+https://github.com/mattwwarren/claude-workspace.git`
2. **From local clone** (for development): Clone the repo, then run `./scripts/install.sh`
3. **Development mode** (editable): Clone the repo, run `uv sync`, use `uv run cw`

Run the appropriate installation command based on their choice.

## Step 3: Verify Installation

```bash
cw --version
cw --help
```

Confirm `cw` is on PATH. If not found, check `~/.local/bin` is in PATH:
```bash
echo $PATH | tr ':' '\n' | grep -q "$HOME/.local/bin" && echo "OK" || echo "Add ~/.local/bin to PATH"
```

## Step 4: Initial Configuration

Run `cw init` to set up the first project, or guide the user through manual config:

```bash
cw init
```

If the user wants to configure manually, create `~/.config/cw/clients.yaml`:

```yaml
clients:
  project-name:
    workspace_path: /path/to/repo
    default_branch: main
```

## Step 5: Install Skills (Slash Commands)

Install the cw slash commands to `~/.claude/commands/` so they're available globally in Claude Code:

```bash
./scripts/install-skills.sh
```

Or if working from a non-local install, find the project source and run the script. The skills include:
- `/session-done` - Wrap up work session with handoff generation
- `/handoff` - Generate session handoff for abnormal endings
- `/queue-plan` - Queue a plan for implementation
- `/queue-debt` - Queue a tech debt item
- `/pull-and-execute` - Pull and execute queue items
- `/orchestrate-phase` - Automated phase implementation with GitHub Issue integration

## Step 6: Shell Completion (Optional)

Ask the user's shell and provide the appropriate completion setup:

- **Bash**: `eval "$(_CW_COMPLETE=bash_source cw)"` in `~/.bashrc`
- **Zsh**: `eval "$(_CW_COMPLETE=zsh_source cw)"` in `~/.zshrc`
- **Fish**: `_CW_COMPLETE=fish_source cw | source` in `~/.config/fish/config.fish`

## Step 7: First Session

Start the user's first session:

```bash
cw start <project-name>
```

This launches Zellij with panes for impl, idea, and debt sessions.

## Output

After installation, summarize:
- cw version installed
- Config file location (`~/.config/cw/clients.yaml`)
- State file location (`~/.local/share/cw/`)
- Configured projects
- Next steps (e.g., `cw start <project>`)

Refer the user to `docs/INSTALL.md` and `config/CONFIG_REFERENCE.md` for full documentation.
