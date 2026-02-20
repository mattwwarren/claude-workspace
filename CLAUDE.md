# claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

**For Python coding standards, see [PYTHON-PATTERNS.md](PYTHON-PATTERNS.md)**

## Project Structure

- `src/cw/` - Main package
  - `cli.py` - Click CLI dispatcher
  - `config.py` - Client config loading (~/.config/cw/clients.yaml)
  - `daemon.py` - Background daemon for session monitoring
  - `exceptions.py` - Custom exception hierarchy
  - `handoff.py` - Handoff document parsing
  - `history.py` - Event history tracking
  - `hooks.py` - Hook system for session lifecycle events
  - `models.py` - Pydantic models (Session, Client, State)
  - `notify.py` - Desktop notification integration
  - `plan.py` - Plan file parsing and management
  - `prompts.py` - Prompt generation for Claude sessions
  - `queue.py` - Task queue for inter-session messaging
  - `session.py` - Session lifecycle (start, bg, resume)
  - `tui.py` - Terminal UI components
  - `worktree.py` - Git worktree management for parallel work
  - `zellij.py` - Zellij CLI wrapper
- `layouts/` - Jinja2 templates for Zellij layouts (.kdl.j2)
- `config/` - Example configuration files
- `tests/` - Test suite

## Development

```bash
uv run cw --help                    # Run CLI
uv run pytest tests/ -v             # Run tests
uv run ruff check src/ tests/       # Lint
uv run mypy src/                    # Type check
uv run pytest tests/ --cov=cw      # Coverage report
```

## Quality Gates

Before committing, run all three checks:

```bash
uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v
```

Pre-commit hooks enforce this automatically (`uv run pre-commit install`).

**Requirements:**
- `ruff check` - **ZERO violations allowed**
- `mypy` - **ZERO type errors allowed**
- Test suite - **100% pass rate required**
- No suppressions (`# noqa`, `# type: ignore`) without explicit user approval

Report format: Only actionable problems. Zero praise, zero summaries.

## Testing

532 tests across 18 test files covering all modules.

| File | Tests | Covers |
|------|-------|--------|
| `test_cli.py` | CLI dispatch, Click integration | `cli.py` |
| `test_config.py` | Config load/save, client lookup | `config.py` |
| `test_daemon.py` | Daemon lifecycle, monitoring | `daemon.py` |
| `test_delegate.py` | Task delegation, agent spawning | delegation logic |
| `test_handoff.py` | Handoff parsing, mtime filtering | `handoff.py` |
| `test_history.py` | Event history tracking | `history.py` |
| `test_hooks.py` | Hook system, lifecycle events | `hooks.py` |
| `test_models.py` | Models, enums, state queries | `models.py` |
| `test_notify.py` | Desktop notifications | `notify.py` |
| `test_plan.py` | Plan file parsing | `plan.py` |
| `test_prompts.py` | Prompt generation | `prompts.py` |
| `test_queue.py` | Task queue, messaging | `queue.py` |
| `test_session_helpers.py` | `_relative_time` with freezegun | `session.py` helpers |
| `test_session.py` | Session lifecycle (start/bg/resume) | `session.py` |
| `test_structured_handoff.py` | Structured handoff format | `handoff.py` structured |
| `test_tui.py` | Terminal UI components | `tui.py` |
| `test_worktree.py` | Git worktree management | `worktree.py` |
| `test_zellij.py` | Zellij wrapper, layout generation | `zellij.py` |

**Patterns:**
- Monkeypatch `CONFIG_DIR`/`STATE_DIR` to `tmp_path` (see `conftest.py`)
- Mock `cw.zellij.*` via `mock_zellij` fixture for session tests
- Use `freezegun` for time-dependent assertions
- Use Click's `CliRunner` for CLI tests
- File-based locking for concurrent session state access

## Key Patterns

- State stored at `~/.local/share/cw/sessions.json`
- Client config at `~/.config/cw/clients.yaml`
- Generated layouts at `~/.config/zellij/layouts/cw-<client>.kdl`
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
- Client names for `start`, `switch`
- Session names for `resume` (filters out completed sessions)
- Purpose choices for `hand` (via Click.Choice, automatic)

## Common Workflows

### Full session lifecycle

```bash
# Start a new session (launches Zellij with impl/idea/debt panes)
cw start my-client

# Background when done (triggers /session-done, waits for handoff)
cw bg

# Resume later with handoff context injected
cw resume my-client/impl

# Check what's running
cw status
```

### Handing off between sessions

```bash
# Send a task from impl to debt session
cw hand debt "Fix the ruff violations in session.py" --from impl

# Messages are persisted to .cw/messages/ for audit
```

### Multi-client workflow

```bash
# Start sessions for different projects
cw start client-a
cw start client-b

# Switch between client tabs in Zellij
cw switch client-a
cw switch client-b

# List all active sessions
cw list
```

## Architecture Decisions

- **Keystroke injection**: `cw bg` injects `/session-done` into Zellij panes. Fragile but zero-coupling to Claude Code internals.
- **Flat JSON state**: Simple, human-readable. Single-user tool.
- **Jinja2 layouts**: KDL templates rendered per-client with workspace paths.
- **On-demand health checks**: `cw status` and `cw start` detect crashed Claude panes via Zellij's `dump-layout` output. No background daemon needed.
- **File-based locking**: Prevents concurrent state corruption from parallel session operations.
- **Event history**: Audit trail for session lifecycle transitions.
- **Background daemon**: Optional monitoring for session health and auto-recovery.

---

# Model Usage & Cost Optimization

**Cost per 1M tokens:** Haiku $0.25/$1.25 | Sonnet $3/$15 | Opus $15/$75

| Model | When to Use |
|-------|-------------|
| **haiku** | Quick searches, simple edits, file operations, status checks |
| **sonnet** | Default - implementation, reviews, planning, multi-file analysis |
| **opus** | Only when explicitly requested or Sonnet fails on complex reasoning |

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

## When to Use This Process

**Always:**
- Writing validators, models, or complex business logic
- Any multi-file changes
- CLI commands or session management methods
- Code that touches state files, Zellij integration, or handoff parsing

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
