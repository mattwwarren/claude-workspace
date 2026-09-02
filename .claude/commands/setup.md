---
description: Discover project tools and write .claude/project-config.yaml
argument-hint: [--reset]
allowed-tools: Bash, Read, Write, Glob, Grep, Agent, AskUserQuestion
---

# /setup — Project Configuration Interview

Discover the current project's available tools, interview the user for preferences, and write `.claude/project-config.yaml`. Downstream commands like `/graduate-plan` read this config for tracking and plan-exit preferences. Quality gate detection is handled separately by `prep_pr_state.py`.

`.claude/` remains the canonical project-local namespace even when this workflow is reused from Codex or another wrapper. Do not introduce a parallel `.codex/` config tree for the same project concepts.

## Arguments

- **--reset**: Regenerate config from scratch (ignore existing config)

## Instructions

### Step 1: Check for Existing Config

Look for `.claude/project-config.yaml` in the current working directory. If it exists and `--reset` was not passed, read it and ask: "Found existing project config. Want to update it, or start fresh?"

### Step 2: Auto-Discovery (no user input needed)

Run these checks silently and collect results:

**Language/framework detection** — check which files exist:
- `pyproject.toml`, `setup.py`, `setup.cfg` → Python
- `package.json` → JavaScript/TypeScript
- `Cargo.toml` → Rust
- `go.mod` → Go
- `Gemfile` → Ruby
- `pom.xml`, `build.gradle` → Java
- `mix.exs` → Elixir

**MCP servers** — check for MCP configuration:
```bash
cat .mcp.json 2>/dev/null
cat .vscode/mcp.json 2>/dev/null
```
Parse the JSON to list available MCP server names.

**CLI tools** — check which are installed:
```bash
command -v gh && echo "gh:found"
command -v glab && echo "glab:found"
command -v linear && echo "linear:found"
command -v jira && echo "jira:found"
command -v cw && echo "cw:found"
```

**Quality gate tools** — check what's available, considering the detected language:

For Python:
```bash
command -v ruff || uv run ruff --version 2>/dev/null
command -v mypy || uv run mypy --version 2>/dev/null
command -v pytest || uv run pytest --version 2>/dev/null
command -v black || uv run black --version 2>/dev/null
```

For JavaScript/TypeScript:
```bash
npx eslint --version 2>/dev/null
npx tsc --version 2>/dev/null
npx jest --version 2>/dev/null || npx vitest --version 2>/dev/null
npx prettier --version 2>/dev/null
```

For Rust:
```bash
cargo clippy --version 2>/dev/null
cargo test --version 2>/dev/null
```

For Go:
```bash
go vet --help 2>/dev/null
go test --help 2>/dev/null
command -v golangci-lint
```

Also check `pyproject.toml`, `package.json`, etc. for configured tool sections (e.g., `[tool.ruff]`, `"eslintConfig"`, scripts like `"lint"`, `"test"`, `"typecheck"`).

### Step 3: Interview the User

Present discovery results and ask focused questions to fill gaps. Only ask what you can't determine automatically.

**Tracking system:**
- If MCP servers include notion/linear: "I found [Notion/Linear] MCP server. Want to use it for tracking work items?"
- If `gh` is installed: "I detected `gh` CLI. Want to use GitHub Issues for tracking?"
- If nothing found: "I didn't find a tracking system. Options: `github-issues` (install `gh`), `notion` (add Notion MCP), `linear` (add Linear MCP), or `local` (track in plan files only). Which do you prefer?"

**Quality gates:**
- Present what you found: "I detected these quality tools: [ruff, mypy, pytest]. I'd configure these gates:"
  - Show the proposed commands
- "Want to use these, or adjust?"
- If nothing found: "What commands verify your code is correct? (lint, typecheck, test)"

**PR creation:**
- If `gh` found: "Use `gh pr create` for PRs?" (default yes)
- If `glab` found: "Use `glab mr create` for merge requests?"
- If neither: "How do you create PRs? I can prepare the description for manual creation."

**PR conventions** (drives a project-local `/ship-it` — see `templates/ship-it-template.md`):
- Base branch: default to `main` unless `git symbolic-ref refs/remotes/origin/HEAD` points elsewhere. Confirm with user.
- Template: check for `.github/pull_request_template.md` or `docs/pr-template.md`. If found, ask "Use `<path>` as the PR body template?"; else leave blank (ship-it will generate a minimal body).
- Required labels: "Any labels every PR should carry (e.g., `automated`, `auto-dev`, team tags)? Comma-separated, or skip."
- Required reviewers: "Any GitHub usernames or team slugs that should be auto-requested? Comma-separated, or skip."
- Auto-merge: "Enable `gh pr merge --auto --squash` after creation? (yes/no, default yes for `/auto-dev` integration)"
- Draft by default: "Create PRs as draft? (yes/no, default no)"

**Plan exit behavior:**
- "When a plan is approved, how should I proceed?"
  - `suggest` (default): "I'll assess scope and suggest a track — you confirm"
  - `auto`: "I'll assess and dispatch automatically"
  - `ask`: "I'll always ask which track to take"

### Step 4: Build Quality Gate Commands

Based on discovery and user input, construct the exact commands. Use the project's runner where possible:

| Detected Setup | Lint Command | Type Command | Test Command |
|----------------|-------------|-------------|-------------|
| `pyproject.toml` + ruff + uv | `uv run ruff check .` | `uv run mypy .` | `uv run pytest -x` |
| `pyproject.toml` + ruff (no uv) | `ruff check .` | `mypy .` | `pytest -x` |
| `package.json` + eslint | `npm run lint` (if script exists) or `npx eslint .` | `npx tsc --noEmit` | `npm test` |
| `Cargo.toml` | `cargo clippy -- -D warnings` | (included in clippy) | `cargo test` |
| `go.mod` | `golangci-lint run` or `go vet ./...` | (included in vet) | `go test ./...` |

Check for `scripts` in `package.json` or `[tool.pytest]` etc. to use project-specific commands.

### Step 5: Write Config

Write `.claude/project-config.yaml` to the current project directory. Create the `.claude/` directory if it doesn't exist.

```yaml
# Written by /setup — edit freely, re-run /setup to regenerate
# Generated: <current date>

tracking:
  system: <github-issues|notion|linear|local>
  mcp_server: <server name if applicable, else null>
  auto_create_issues: true
  auto_close_on_complete: true

review:
  self_review: true
  max_cycles: 3

pr:
  tool: <gh|glab|local>
  auto_create: false
  branch_pattern: "{type}/{plan_id}"
  base: main                          # default base branch for PRs
  template_path: <path or null>       # e.g., .github/pull_request_template.md — null = generate minimal body
  labels: []                          # labels applied to every PR (e.g., ["automated", "auto-dev"])
  reviewers: []                       # GitHub usernames/team slugs auto-requested (e.g., ["my-team", "alice"])
  auto_merge: true                    # run `gh pr merge --auto --squash` after creation
  draft_default: false                # create PRs as draft
  title_format: "{ticket-id}: {summary}"  # tokens: {ticket-id}, {summary}, {type}, {branch}

plan_exit:
  mode: <suggest|ask|auto>
```

### Step 6: Bootstrap Per-Project `/ship-it`

`/prep-pr` and `/auto-dev` both require a per-project ship-it — there is no global fallback (by design: accidental loading was bad). It may be a command (`.claude/commands/ship-it.md`) or a skill (`.claude/skills/ship-it/SKILL.md`, `.agents/skills/ship-it/SKILL.md`); `/prep-pr` Step 8 probes all three. `/setup` offers to copy the command template into the current repo so these pipelines work out of the box.

1. **Check existence — probe every layout `/prep-pr` probes, not just the
   command path.** A repo whose ship-it is a skill already has a working
   ship-it; bootstrapping a command stub next to it would shadow the real one
   (Step 8 prefers the command when both exist), which is worse than the
   missing-ship-it case this step exists to fix.
   ```bash
   for candidate in .claude/commands/ship-it.md \
                    .claude/skills/ship-it/SKILL.md \
                    .agents/skills/ship-it/SKILL.md; do
     [ -f "$candidate" ] && echo "exists $candidate"
   done
   ```

2. **If no layout matched**, ask the user:
   > "This repo has no ship-it — probed `.claude/commands/ship-it.md`, `.claude/skills/ship-it/SKILL.md`, and `.agents/skills/ship-it/SKILL.md`. `/prep-pr` and `/auto-dev` need one to create PRs. Copy the command template from the installed Claude home or from the checked-out `global-claude/templates/ship-it-template.md`? (yes / no / show-me)"

   - **yes** → copy (not symlink — symlinks = de facto global fallback, which we explicitly reject):
     ```bash
     mkdir -p .claude/commands
     TEMPLATE_SRC="${GLOBAL_CLAUDE_TEMPLATE_PATH:-$HOME/.claude/templates/ship-it-template.md}"
     [ -f "$TEMPLATE_SRC" ] || TEMPLATE_SRC="$HOME/workspace/personal/global-claude/templates/ship-it-template.md"
     /bin/cp "$TEMPLATE_SRC" .claude/commands/ship-it.md
     ```
     Then confirm: "Copied. Edit `.claude/commands/ship-it.md` to add repo-specific logic (CI bootstrap, notifications, merge queue). The 6-step skeleton must stay intact so callers get consistent output."
   - **no** → Skip. Warn: "`/prep-pr` and `/auto-dev` will fail with BLOCK until a `ship-it.md` exists in this repo."
   - **show-me** → `cat "$TEMPLATE_SRC"` and re-ask yes/no.

3. **If any layout matched**, skip silently (don't overwrite user customizations on a re-run, and never add a command stub beside an existing skill). If `--reset` was passed, ask whether to overwrite — name the layout that already exists so the user is not offered a second, competing ship-it by accident.

### Step 7: Confirm

Show the user the generated config. Tell them:
- "Config written to `.claude/project-config.yaml`"
- "`/graduate-plan` reads this config for tracking and plan-exit preferences. Quality gates are auto-detected by `prep_pr_state.py`."
- "Edit the file anytime, or re-run `/setup` to regenerate."
