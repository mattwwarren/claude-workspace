# Installing claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

## Prerequisites

Install these before installing cw:

| Tool | Required | Purpose | Install |
|------|----------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Yes | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Python 3.13+](https://python.org/) | Yes | Runtime | Via uv: `uv python install 3.13` |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Yes | AI coding assistant | `npm install -g @anthropic-ai/claude-code` |
| [peon-ping](https://github.com/PeonPing/peon-ping) | No | Sound notifications when Claude needs attention | `curl -fsSL peonping.com/install \| bash` |

### Verifying Prerequisites

```bash
# Check each prerequisite
uv --version          # >= 0.4.0
python3 --version     # >= 3.13
claude --version      # any recent version
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
uv tool install "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git"
```

Pin to a specific release:

```bash
uv tool install "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git@v1.20.0"
```

### From Local Clone

```bash
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
./scripts/install.sh
```

The install script runs `uv tool install --from "$PROJECT_DIR" --force --reinstall --no-cache "claude-workspace[mcp]"`, making `cw` globally available, and then syncs cw's bundled skills and commands into `~/.claude/` via `scripts/install-skills.sh`.

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
uv tool install --force "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git"

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

#### What `cw init` wires

By default, `cw init` also runs four agent-onboarding steps against the target
workspace:

- **MCP servers** (`.mcp.json`) — merges `cw-queue-events` and `cw-pr-events`
  entries so background agents receive queue and PR events via MCP.
  The files `config/cw-queue-events.mcp.json.example` and
  `config/cw-pr-events.mcp.json.example` are for manual wiring only; `cw init`
  generates these entries automatically.
- **Bash allowlist** (`~/.claude/settings.json`) — adds `"Bash(cw:*)"` to
  `permissions.allow` so agents can call `cw` commands without prompting.
- **SessionStart hook** (`<workspace>/.claude/settings.json`) — adds
  `cw orchestrate status --json || true` so each new Claude session sees the
  current dispatch state.
- **CLAUDE.md snippet** (`<workspace>/.claude/CLAUDE.md`) — appends a brief
  `<!-- cw-onboarding -->` section documenting the MCP channels and `cw schema`
  usage. When `cw schema list` is unavailable the snippet is still written but
  the schema-specific lines are omitted.

All four steps are idempotent — re-running `cw init` or `cw init --onboard-only`
is safe.

**Flags:**

- `--no-onboarding` — skip all four onboarding steps (useful for scripted
  installs where you manage these files yourself). Note: the client is not
  runnable until you follow with `cw init <client> --onboard-only`.
- `--onboard-only` — re-run onboarding only; skip creating a new client entry
  (client must already exist in `clients.yaml`).

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

This spawns background Claude daemon sessions for each session purpose (impl, idea, debt).

### 5. Read the Operator Guide

```bash
cw guide
```

Prints the built-in operator guide — how to drive a sprint with cw
(dev-queue dispatch, monitoring, and follow-up).

## File Locations

| File | Location | Purpose |
|------|----------|---------|
| Client config | `~/.config/cw/clients.yaml` | Project definitions |
| Orchestrator config | `~/.claude-workspace/orchestrator.yaml` | Dispatch-loop tuning (created with defaults on first run) |
| Session state | `~/.local/share/cw/sessions.json` | Active session tracking |
| Dev-queue state | `~/.local/share/cw/dev_queue.json` | Orchestrator ticket queue |
| Event inbox | `~/.local/share/cw/events/inbox.jsonl` | Orchestrator event bus (prune with `cw event prune`) |
| Event history | `~/.local/share/cw/history/` | Session event log |
| Daemon roster | `~/.claude/daemon/roster.json` | Claude-owned registry of `claude --bg` workers |

The `~/.config/cw/` and `~/.local/share/cw/` paths respect `XDG_CONFIG_HOME`
and `XDG_DATA_HOME` if set; `orchestrator.yaml` is always at
`~/.claude-workspace/`.

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
uv tool install --force "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git"
```

### `ModuleNotFoundError` after pulling new dependency-adding source changes

If `cw` is installed via `uv tool install` (editable/local source), the tool's
own venv is not automatically re-synced when the source `pyproject.toml`
gains a new runtime dependency — only the next `uv tool upgrade` or
`--reinstall` picks it up. Merging a PR that adds a dependency can therefore
break every `cw` invocation, including a running `cw dev-queue serve` loop,
until you run:

```bash
uv tool upgrade claude-workspace
# or
uv tool install --reinstall claude-workspace
```

`cw doctor` flags this drift (`cw-deps` check) by comparing declared
dependencies in `pyproject.toml` against installed distributions, before it
manifests as a crash.

### `Python 3.13 required`

cw requires Python 3.13+. Install it via uv:

```bash
uv python install 3.13
```

### Daemon not starting

The Claude native daemon starts automatically when `cw` first spawns a worker
session. Verify liveness via:

```bash
claude agents --json   # lists running background sessions
```

If no workers appear after `cw start`, check that the `claude` binary is on
your `PATH` and the bypass-permissions disclaimer has been accepted.

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
