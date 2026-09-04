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
- `[OK/WARN] skills-commands-drift` — repo-tracked `.claude/skills`/`.claude/commands`/`.claude/scripts` files compared against `~/.claude`; `[WARN]` means at least one tracked file is missing, content differs, or its `~/.claude` counterpart is a symlink pointing somewhere other than this checkout. A `differ` on `.claude/scripts/prep_pr_state.py` is the #2090 shape: a stale copy of a cw-owned script at `~/.claude/scripts/` — re-run `scripts/install-skills.sh`.
- `[OK/WARN] agent-spec-drift/<client>` — per-client reviewer agent-spec resolution (repo-local / global fallback / absent); `[WARN]` names any reviewer role with no usable spec.

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

The install script runs `uv tool install --from "$PROJECT_DIR" --force --reinstall --no-cache "claude-workspace[mcp]"`, making `cw` globally available, and then syncs cw's bundled skills, commands, and helper scripts into `~/.claude/` via `scripts/install-skills.sh`.

### How skills/commands/scripts stay in sync

`~/.claude/skills/<name>`, `~/.claude/commands/<name>`, and `~/.claude/scripts/<name>` are **symlinks** into this checkout's `.claude/` tree, not copies — one copy of each file exists on disk, so drift between the two trees is structurally impossible.

Scripts are linked **per file** (including `utils/`), never per directory: `~/.claude/scripts/` is typically the `global-claude` checkout's own `scripts/` directory, which also holds scripts that exist only there (`generate_handoff.py`, `session_complete.py`, ...), and those must keep working. Only the scripts this repo tracks under `.claude/scripts/` are cw's to install — the ones its commands invoke (`prep_pr_state.py`, `prep_pr_finalize.py`, `review_monitor.py`, `post_review.py`, ...). `check_imports.py` is this repo's CI gate and is skipped via `scripts/excluded-scripts.txt`, the same mechanism as `excluded-commands.txt`.

**If `global-claude` still tracks a copy of a cw-owned script** (the #2090 incident: its `prep_pr_state.py` was three versions behind this repo's and silently lacked `/prep-pr`'s `gate-timeout` subcommands), the installer replaces that regular file with the symlink and lists it under `scripts replaced` in its summary. Finish the hand-over in `global-claude` so the symlink stops reading as a modified tracked file: `git rm --cached scripts/<name>` and add `scripts/<name>` to the cw block of its `.gitignore`, exactly as was done for the commands cw owns.

Re-running `scripts/install-skills.sh` (directly, or via `./scripts/install.sh`) migrates an existing copy-based install automatically: a stale regular file or directory at the destination is replaced with a symlink, and a symlink already pointing at the wrong target is repointed. No manual `rm` is required. For commands and skills, uncommitted hand-edits to such a copy are lost in that replacement — commit them (or fold them into this repo) first. For scripts, a replaced copy whose bytes differ from cw's source is kept beside the link as `<name>.pre-symlink.bak` and named in the summary; a byte-identical copy is simply replaced.

**Trade-off, stated plainly, not as a free win:** once installed as a symlink, an "edit to a local skill" *is* an edit to this repo's working tree. It shows up in `git status`, can be committed from any directory, and can be clobbered by a `git checkout`, `git stash`, or branch switch performed in this repo. Dispatched (`cw`-spawned) workers are unaffected — they resolve skills from their own worktree's committed state — but any interactive session running elsewhere sees uncommitted edits to this checkout live.

Agents (`~/.claude/agents`) are unaffected by this — they are still copied (not symlinked), and on a typical setup the destination itself is a symlink into the `global-claude` repo. That layout is unchanged and out of scope for this doc.

Because agents are copied rather than symlinked, `install-skills.sh` guards against silently clobbering a hand-edit: it keeps a baseline shadow copy of each agent file at `~/.claude/.cw-agents-baseline/`, and refuses (non-zero exit, naming the file and both paths) to overwrite a destination that diverges from both the source and that baseline. Pass `--force` to overwrite anyway.

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
