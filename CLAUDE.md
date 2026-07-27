# claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

**For Python coding standards, see [PYTHON-PATTERNS.md](PYTHON-PATTERNS.md)**

## Project Structure

- `src/cw/` - Main package
  - `cli.py` - Click CLI dispatcher
  - `config.py` - Client config loading (~/.config/cw/clients.yaml)
  - `exceptions.py` - Custom exception hierarchy
  - `handoff.py` - Handoff document parsing
  - `history.py` - Event history tracking
  - `models.py` - Pydantic models (Session, Client, State)
  - `prompts.py` - Prompt generation for Claude sessions
  - `queue.py` - Task queue for inter-session messaging
  - `session.py` - Session lifecycle (start, bg, resume)
  - `worktree.py` - Git worktree management for parallel work
  - `native_daemon.py` - Native `claude --bg` daemon client and protocol
- `config/` - Example configuration files
- `tests/` - Test suite

## `.claude/skills`/`.claude/commands` — Repo Copy Is Authoritative

This repo's `.claude/skills` and `.claude/commands` directories are git-tracked
and are the **authoritative copy for anything a dispatched worker loads** —
`cw` spawns headless sessions rooted in a worktree of this repo, and those
sessions resolve skills/commands from the worktree's own `.claude/` tree.

`~/.claude/skills` and `~/.claude/commands` are symlinks into the separate
`global-claude` repo. They are what an **interactive** `claude` invocation
resolves when you're not inside a `cw`-managed worktree — a different
resolution path with a different source of truth.

Because both are single top-level symlinks, `readlink -f` on a path under
`~/.claude/skills/<name>` or `~/.claude/commands/<name>` proves nothing about
whether *this* repo's tracked copy matches it — the two trees can silently
drift apart. `git ls-files .claude/skills/ .claude/commands/` inside this repo
is the ground truth for what a worker will load. `cw doctor`'s
`skills-commands-drift` check automates the comparison — see
[docs/INSTALL.md](docs/INSTALL.md#verify-with-cw-doctor).

## Development

```bash
uv run cw --help                    # Run CLI
uv run pytest tests/ -v             # Run tests
uv run ruff check src/ tests/       # Lint
uv run mypy src/                    # Type check
uv run pytest tests/ --cov=cw      # Coverage report
```

## Quality Gates

Before committing, run **every** gate CI enforces (`.github/workflows/ci.yml`),
in order. The first five mirror CI exactly; passing only a subset is the #1
cause of a green local run that fails CI (see #436):

```bash
uv lock --check                                                  # 1. Lockfile in sync
uv run ruff check src/ tests/                                    # 2. Lint
uv run ruff format --check src/ tests/                           # 3. Format
uv run mypy --strict src/                                        # 4. Type check
uv run pre-commit run --all-files                                # 5. Hooks
uv run --extra mcp pytest tests/ -m 'not integration' \
  --cov=cw --cov-report=xml --cov-fail-under=88                  # 6. Unit + total cov ≥88%
uv run pytest tests/ -m integration                              # 7. tmux integration
uv run diff-cover coverage.xml --compare-branch=origin/main \
  --fail-under=90                                                # 8. Patch coverage ≥90%
```

Gate 1 must run **standalone and first**, exactly as in CI. `uv run` locks-and-
syncs implicitly, so any later gate silently repairs a stale `uv.lock` on disk —
leaving the drift uncommitted and CI red. Do not fold it into a `uv run …`
invocation.

Pre-commit hooks enforce gates 1–5 automatically on `git commit`
(`uv run pre-commit install`) — git invokes them directly, with no `uv run`
wrapper, so gate 1 is genuine on that path. Only running gate 5 **by hand**
via `uv run pre-commit run` masks it.

**Requirements:**
- `uv lock --check` - **ZERO drift**. Any `pyproject.toml` edit that moves the
  project version or its dependencies must carry the regenerated `uv.lock` in
  the same commit.
- `ruff check` - **ZERO violations allowed**. Function-level complexity is gated by
  the `PLR` rules: ≤12 branches (`PLR0912`), ≤50 statements (`PLR0915`), ≤6 returns
  (`PLR0911`). When a function trips these, **extract helpers** — don't suppress.
  `PLR0913` (too-many-arguments) is intentionally disabled: Click injects one
  parameter per option and the rule fights helper extraction (see `pyproject.toml`).
- `ruff format --check` - **ZERO reformats** (run `ruff format` to fix; `ruff check` does NOT enforce formatting)
- `mypy --strict` - **ZERO type errors allowed**
- Test suite - **100% pass rate required**
- Total coverage **≥88%**; new/changed lines (patch coverage) **≥90%** — cover every new branch, including `except`/error paths
- No suppressions (`# noqa`, `# type: ignore`) without explicit user approval

Report format: Only actionable problems. Zero praise, zero summaries.

## Module Size

Keep source modules under **~1000 lines**. This is a review-enforced convention
(ruff has no module-line rule) — treat it as a ceiling, not a target.

- **Approaching ~800 lines:** a yellow flag. Check whether the module still owns
  a single concern; if it has accreted several, plan a split.
- **Exceeding ~1000 lines:** split it into a package — one submodule per concern,
  with an `__init__.py` that re-exports the public surface so import sites stay
  stable. `cw.cli` and `cw.reconcile` follow this shape.
- **Cohesion beats raw count.** Do not split a cohesive module just to clear the
  number (e.g. `reconcile/_shared.py` is large but is shared infrastructure for a
  single concern). Conversely, a smaller module mixing unrelated concerns is
  still a smell.
- Extract helpers rather than letting individual functions grow unbounded; long
  functions with many branches or arguments are the usual driver of oversized
  modules.

Test files map 1:1 onto source modules (see Testing), so this ceiling targets
`src/` modules; an oversized test file mirrors an oversized source module and is
resolved by splitting the source.

## Testing

Tests across test files (see `tests/`). Test files map one-to-one
onto source modules (`test_cli.py` ↔ `cli.py`, `test_native_daemon.py` ↔
`native_daemon.py`, etc.).

**Patterns:**
- Isolation: the autouse `tmp_config_dir` fixture in `conftest.py` patches
  every `cw.config.*` path at module load; consumers read paths through
  accessor functions so no per-test module-local patching is needed
- Mock `cw.native_daemon.FakeNativeDaemonClient` via the `mock_native_daemon`
  fixture for daemon-origin spawn and reconcile tests
- Use `freezegun` for time-dependent assertions
- Use Click's `CliRunner` for CLI tests
- File-based locking for concurrent session state access

## Key Patterns

- State stored at `~/.local/share/cw/sessions.json`
- Client config at `~/.config/cw/clients.yaml`
- Integrates with existing handoff pipeline at `~/.claude/scripts/generate_handoff.py`
- File-based locking prevents concurrent state corruption
- Event history provides audit trail for session lifecycle

## Shell Completion

Enable tab completion for `cw` commands:

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"

# Fish (~/.config/fish/config.fish)
_CW_COMPLETE=fish_source cw | source
```

Run `cw completion <shell>` to see the activation snippet.

Completions provide:
- Client names for `start`
- Session names for `resume` (filters out completed sessions)

## Common Workflows

### Full session lifecycle

```bash
# Start a new session (spawns Claude daemon workers for impl/idea/debt)
cw start my-client

# Background when done (triggers /session-done, waits for handoff)
cw bg

# Resume later with handoff context injected
cw resume my-client/impl

# Check what's running
cw status
```

### Multi-client workflow

```bash
# Start sessions for different projects
cw start client-a
cw start client-b

# Switch between client workspaces
cw switch client-a
cw switch client-b

# List all active sessions
cw list
```

## Architecture Decisions

- **Keystroke injection**: `cw bg` injects `/session-done` into active Claude sessions. Fragile but zero-coupling to Claude Code internals.
- **Flat JSON state**: Simple, human-readable. Single-user tool.
- **Native daemon backend**: Workers are spawned via `claude --bg` and tracked by short hex session id in `~/.claude/daemon/roster.json`. No multiplexer required.
- **On-demand reconciliation**: `cw status`, `cw list`, `cw start`, and each `dispatch_tick` call `reconcile()` to detect phantoms (sessions in state but absent from the daemon roster). By default (`reap_policy: signal_only`, per ADR-0006) detection emits `SESSION_REAP_PROPOSED` and routes the task to `BLOCKED_ON_USER` — no destructive mutation. Destructive act (RUNNING→PENDING revert, daemon stop, worktree removal) requires `reap_policy: auto` for the lane, or an explicit `cw doctor --reap`. No background daemon needed.
- **File-based locking**: Prevents concurrent state corruption from parallel session operations.
- **Event history**: Audit trail for session lifecycle transitions.

**Operator runbooks:** [`docs/dispatch-runbook.md`](docs/dispatch-runbook.md) — end-to-end `cw dev-queue` dispatch procedure. [`docs/session-disposition.md`](docs/session-disposition.md) — how to read a session's outcome from the transcript sentinel.

---

# Model Usage & Cost Optimization

**Cost per 1M tokens:** Haiku $0.25/$1.25 | Sonnet $3/$15 | Opus $15/$75

| Model | When to Use |
|-------|-------------|
| **haiku** | Quick searches, simple edits, file operations, status checks |
| **sonnet** | Default - implementation, reviews, planning, multi-file analysis |
| **opus** | Only when explicitly requested or Sonnet fails on complex reasoning |

### Per-client worker pinning

Each client can pin the model used by autonomous workers (auto-dev,
dispatch) via the `worker_model` field in `clients.yaml`. When set, `cw`
forwards `--model <worker_model>` to `claude --bg` from two chokepoints:

- `src/cw/spawn.py:spawn_create_impl` — initial DAEMON spawn (auto-dev
  worker creation).
- `src/cw/session.py:resume_session` — DAEMON-origin resume of a dead
  surface (re-spawn path).

USER-origin sessions (interactive `cw start` / `cw resume`) always inherit
the operator's logged-in default model and ignore `worker_model`. See
`config/CONFIG_REFERENCE.md` for the cost-control example.

## Agent Spawning Decision Tree

**Default: Work directly unless clear reason to spawn.**

Don't spawn agents for:
- Single file reads, simple searches, finding files by name
- 1-3 related file reads (read in parallel directly)
- Single commands answerable from recent context

Do spawn agents for:
- Exploring unfamiliar codebase (1 Haiku Explore agent)
- Complex multi-file changes (5+ files)
- Parallel independent tasks (code review fixes)
- Long-running background work (tests, builds)

Agent count guidelines:
- Simple search: 0 (direct tools)
- Single area explore: 1 haiku
- Multi-area explore: 1-2 haiku max
- Planning/Implementation: 0-1 sonnet
- Code review (5+ items): 2-3 sonnet

## Pre-Flight Checklist

Before acting, ask:
1. Can Haiku do this instead of Sonnet?
2. Can I use direct tool calls instead of spawning agent?
3. Can 1 agent do what I planned for 2-3?
4. Do I need all these files or just 1-2?
5. Is there a more targeted search than exploration?

---

# Proactive Task Delegation

**Delegate aggressively.** Parallelize when possible.

## When to Delegate

- **Multiple independent subtasks** - Spawn parallel agents, one per subtask
- **Large feature with distinct parts** - Spawn agents in separate worktrees
- **Research + implementation** - Spawn research agent in background while planning
- **Tests + implementation** - Spawn test-writing agent in parallel with feature work
- **Code review feedback (5+ items)** - Spawn agents for independent feedback categories

### Code Review Implementation

**Don't use agents for:**
- 1-3 quick, straightforward fixes (typos, simple logic changes, single-file edits)
- Sequential/dependent feedback where each fix informs the next
- Simple refactoring in a single file or component

**Do use agents for:**
- 5+ independent feedback items that can be parallelized
- Feedback spanning multiple files/subsystems
- Large refactoring across the codebase
- Combined implementation + test + documentation changes

## How to Delegate

1. **Background agents**: Use `Task` tool with `run_in_background: true`
2. **Worktree isolation**: Use worktrees for branch-isolated work
3. **Track progress**: Use `TodoWrite` to track delegated tasks

## Parallel Execution Rules

When spawning parallel agents with `run_in_background: true`:
- **DO NOT run mypy in parallel** - Type caches conflict when multiple agents run simultaneously. Run mypy serially or after parallel work completes.
- **DO run ruff in parallel** - It's fast and thread-safe.
- **DO run pytest in parallel** - Test isolation handles it fine. Can safely run 3-4 test suites in parallel.
- **Coordinate file access** - If multiple agents modify overlapping files, they must coordinate sequentially instead of in parallel.

**Parallelization guidelines:**
- Up to **6 agents** can run simultaneously without resource contention
- For heavyweight operations (full pytest, large project builds): spawn 3-4 agents max
- For lightweight operations (linting, quick checks): can spawn up to 6 agents

## Agent File Operations

**Problem:** Background agents have limited Bash permissions and shell aliases (e.g., `cp -i`) block on prompts.

**Rule: Agents MUST use Read/Write tools for file operations, NOT Bash.**

| Operation | Use This | NOT This |
|-----------|----------|----------|
| Copy file | `Read` source then `Write` destination | `Bash(cp ...)` |
| Move file | `Read` then `Write` then `Bash(rm)` | `Bash(mv ...)` |
| Create file | `Write` | `Bash(echo >)` |
| Read file | `Read` | `Bash(cat)` |

---

# Code Writing Process

**Goal: Write clean code that passes ruff/mypy FIRST TIME, every time.**

## Before Writing Significant Code (>20 lines or multi-file)

### 1. Pattern Scan (5 min max)
- Read 2-3 similar files in the codebase
- Note the conventions (error handling, validators, type annotations, constants)
- Understand what patterns are expected in this context

### 2. Linting Pre-Check (in your head)
Before touching the editor, ask:
- Will this have magic numbers? Extract constant first
- Will this need error messages? Extract to variable first (EM101 rule)
- Are there 3+ similar code patterns? Plan extraction immediately
- Type annotations complete? (including `-> None` on all functions)
- Using `Any`? Replace with `object` or specific type

### 3. Read Relevant Documentation Section
Before writing, read the enforced rules that apply in [PYTHON-PATTERNS.md](PYTHON-PATTERNS.md):
- **Always:** "Python & Pydantic Conventions"
- **If testing:** "Test Architecture Principles"

### 4. Conservative Defaults
- When uncertain about a pattern, be MORE explicit, not less
- Better to extract a helper early than refactor it later
- Better to add full type annotations than minimal ones
- Better to use existing utilities than implement custom logic

### 5. State Approach BEFORE Writing

Explicitly communicate:

**Pattern scan results:**
- What conventions found in similar files
- Error handling patterns, validators, type annotations

**Linting guards:**
- Which ruff/mypy rules to follow
- Specific choices (object vs Any, constants vs magic numbers)

**Architectural approach:**
- Structure to use
- Where helpers/validators will live
- Test coverage approach

**Then:** Write clean code that passes ruff/mypy on first attempt

### Interpreter & Compiled-Dependency Isolation (test pitfall)

When writing or resolving a test that runs code under a "bare" or *different* interpreter — e.g. invoking `/usr/bin/python3` to prove a `sys.path` bootstrap works outside `uv run` — account for transitive **compiled** dependencies. C extensions (`pydantic_core`, etc.) are **ABI-bound to the interpreter that built them**: the venv's `.so` will not load under a foreign interpreter, even one of the same Python version. "Dependency not importable from this path" is NOT the same as "use a different interpreter."

- **Isolate via flags on the SAME interpreter, not by switching interpreters.** To exercise a bootstrap while keeping compiled deps loadable, use `sys.executable -S` (skips `site` processing so the editable `.pth` doesn't auto-add the package) plus `PYTHONPATH=<venv purelib>` (so deps stay importable) — NOT a foreign `python3`.
- If you catch yourself reaching for `/usr/bin/python3` (or any non-`sys.executable` interpreter) to "get a clean environment," that IS the cue — stop and ask whether a compiled dep will fail to load there. This pattern shipped a green-locally / red-in-CI test (#671) that the rule would have prevented.

## When to Use This Process

**Always:**
- Writing validators, models, or complex business logic
- Any multi-file changes
- CLI commands or session management methods
- Code that touches state files, multiplexer adapters, or handoff parsing

**Don't need to show thinking for:**
- Simple one-line fixes (typos, obvious bugs)
- Very small changes (<10 lines, single file)
- When patterns are already clear from context

## This Applies to Agents Too

When spawning agents to write code, this same process applies. Agents will:
1. Show pattern scan before writing
2. State linting guards and approach
3. Write code that passes linting first time
4. No ruff/mypy cleanup loops

---

# SysAdmin Principles (The Abigail Oath)

**"I will not mass-change this codebase in my eagerness to help."**

## Core Philosophy

- **Speed vs. Quality**: Fast is good, but broken is expensive. Measure twice, cut once.
- **Scope Discipline**: Do what was asked, not what seems helpful.
- **Incremental Changes**: Small commits, frequent reviews, easy rollbacks.
- **Explicit Over Implicit**: When in doubt, ask. When uncertain, pause.

## Stop-and-Ask Triggers

**STOP and ask the user when:**

1. **Debugging Depth 2+**: If you've tried 2+ different approaches without success
2. **Architectural Changes**: Before modifying shared infrastructure, patterns, or interfaces
3. **Scope Expansion**: When a "simple fix" turns into "we should also refactor X"
4. **Uncertainty**: When you're not sure if the approach is correct
5. **Breaking Changes**: Before any change that could break existing functionality

## Anti-Patterns to Avoid

### Kitchen-Sink Syndrome

**Problem**: "While I'm here, I'll also add X, Y, Z..."
**Why it hurts**: Scope creep, harder reviews, mixed concerns in commits
**Solution**: Do one thing well. Open separate issues for improvements.

### Rabbit-Holing

**Problem**: Going deeper into debugging without surfacing progress
**Why it hurts**: Wasted time, context exhaustion, frustration
**Solution**: After 2 attempts, stop and report findings. Ask for guidance.

### Late Escalation

**Problem**: Spending 30 minutes on something that needed user input
**Why it hurts**: Sunk cost, potentially wrong direction
**Solution**: Ask early. "I'm about to X, which will affect Y. Proceed?"

### DRY Violations (Configuration Duplication)

**Problem**: Copying the same config/value to multiple places
**Why it hurts**: One change requires N updates, drift becomes inevitable
**Solution**: Define once, reference everywhere

### Theorizing Before Grepping the Mechanism

**Problem**: When a system behaves unexpectedly (a queue ticket won't dispatch despite seemingly-free slots, a log stops updating, a counter looks wrong), constructing hypotheses — phantoms, buffering, caches, stale state — and acting on them (restarts, reaps) before reading the code that implements the behavior.
**Why it hurts**: Hypothesis-driven flailing burns time and can trigger destructive moves (an unnecessary restart, a reap) chasing a "bug" that is actually documented behavior. Real incident: ~12 tool calls plus an unneeded loop restart spent on a "lane-cap phantom" that one `grep` of `dispatch.py` resolved instantly — `running_in_lane = RUNNING + BLOCKED_ON_USER`, i.e. blocked tasks hold lane slots **by design**.
**Solution**: Grep the actual mechanism FIRST — the cheap, definitive code-read comes before the theory. If you catch yourself theorizing about *why* a system does X across more than one step without having read the code path that produces X, that IS the cue — stop and grep it. Sibling of **No Unverified Claims**.

## Scope and Commit Flow

### Review-Before-Commit Principle

**Small changes**: Review inline, commit when clean
**Medium changes**: Review per-file or per-feature, commit in logical chunks
**Large changes**: Review per-phase, commit after each phase passes review

### Commit Frequency Guidelines

| Change Size | Review Checkpoint | Commit Frequency |
|-------------|-------------------|------------------|
| 1-3 files | After all changes | Single commit |
| 4-10 files | Per logical unit | 2-3 commits |
| 10+ files | Per feature/phase | Multiple commits |

### Before Every Commit

1. Run linting (`ruff check .`)
2. Run type checking (`mypy .`)
3. Run relevant tests
4. Review your own diff
5. Write clear commit message

---

# Tool Usage Rules

**Always use Claude Code's dedicated tools instead of bash equivalents:**

| Task | Use This | NOT This |
|------|----------|----------|
| Search file contents | `Grep` tool | `bash grep`, `bash rg`, `bash git grep` |
| Find files by pattern | `Glob` tool | `bash find`, `bash ls` |
| Read files | `Read` tool | `bash cat`, `bash head`, `bash tail` |
| Edit files | `Edit` tool | `bash sed`, `bash awk` |
| Write files | `Write` tool | `bash echo >`, heredocs |

## Grep Tool Consistency

**Always use the Grep tool for content searches. No exceptions.**

- **ALWAYS use:** `Grep` tool with `pattern`, `path`, `glob`, `type` parameters
- **NEVER use:** `bash grep`, `bash rg`, `bash git grep` commands

**Why:**
- Grep tool is pre-approved and never requires user permission
- Bash grep commands may require approval, slowing down work
- Grep tool has structured output optimized for Claude Code

## Working Directory Guidelines

**Prefer absolute paths and avoid `cd` when possible** to maintain consistent working directory throughout the session.

**When cd is fine:**
- User explicitly requests it
- Command doesn't support `-C`, `--dir`, or path arguments
- Running multiple sequential commands that all need the same directory

---

# Context Management

Managing context effectively prevents session exhaustion and maintains quality work.

## Scope-Based Checkpoint Flow

| Checkpoint | Action |
|------------|--------|
| After each logical unit | Quick self-review of changes |
| Before committing | Run linting, type checks, tests |
| At 50% context | Assess progress, consider checkpoint |
| At 80% context | Prepare for handoff or wrap-up |
| At 90%+ context | Stop new work, generate handoff |

## Context Threshold Actions

### At 80% Context Usage

Options:
1. **Wrap up current work** - Complete immediate task, use `/session-done`
2. **Generate handoff** - Use `/handoff --reason context` if work incomplete
3. **Checkpoint and continue** - Summarize progress, continue carefully

### At 90%+ Context Usage

**Required action:** Stop starting new work. Focus on:
- Completing in-progress items
- Generating handoff document
- Writing clear resume prompt

## Review-Before-Commit Principle

**Small changes (1-3 files):** Review inline, commit when clean
**Medium changes (4-10 files):** Review per-file, commit in logical chunks
**Large changes (10+ files):** Review per-phase, multiple commits

---

# Knowledge Base Truth Standards

**Principle:** Domain knowledge is reference material, not infallible truth. Apply the same rigor to documentation as to any technical claim.

## Required Behaviors

### 1. Flag Conflicts

When domain docs contradict established technical practices or your knowledge:
- State the conflict clearly
- Explain the technical concern
- Offer to update the doc if correct

### 2. Ask for Clarification

When docs are ambiguous, potentially outdated, or reference undefined concepts:
- Ask rather than assume
- Note what's unclear and why

### 3. Project vs. General Authority

| Claim Type | Treatment |
|------------|-----------|
| **Project convention** ("we do X") | Authoritative - follow it |
| **General technical claim** ("X is best") | Skeptical - may push back |

Project-specific patterns are trusted as "how this codebase works." General technical assertions are subject to the same scrutiny as any claim.

---

This is free and unencumbered software released into the public domain.

For more information, please refer to <http://unlicense.org/>
