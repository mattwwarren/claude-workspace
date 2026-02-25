---
description: Generate session handoff for abnormal endings (context/scope exhaustion, debug fork)
argument-hint: [--reason <context|debug-fork|scope>]
---

# Session Handoff

Generate a structured handoff document when a session can't continue normally. Use this instead of `/session-done` when you're forced to stop due to constraints.

## Usage

```bash
/handoff                     # Normal handoff (reason: context by default)
/handoff --reason context    # Context exhaustion (80%+ usage)
/handoff --reason debug-fork # Fork work due to debug depth 2+
/handoff --reason scope      # Scope has expanded beyond original task
```

## When to Use

| Situation | Command |
|-----------|---------|
| Work complete | Use `/session-done` instead |
| Good stopping point | Use `/session-done` instead |
| Context at 80%+ | `/handoff --reason context` |
| Debug attempts >= 2 failed | `/handoff --reason debug-fork` |
| Scope exploded beyond task | `/handoff --reason scope` |

## Arguments

- **--reason <context\|debug-fork\|scope>**: Type of handoff
  - `context`: Context exhaustion (default)
  - `debug-fork`: Debugging rabbit hole - creates two handoffs
  - `scope`: Original scope lost to expansion

## What It Does

### 1. Gathers Context

Collects current session state:
- Active todos and their status
- Plan progress (if working from plan)
- Recent git changes
- Key decisions made
- Approaches tried/rejected

### 2. Generates Handoff Document

Creates structured handoff in `.handoffs/` or `~/.claude/handoffs/`:

```
.handoffs/
├── handoff-2026-01-30-1430.md       # Standard
├── handoff-main-2026-01-30-1430.md  # Debug fork (main track)
└── handoff-debug-2026-01-30-1430.md # Debug fork (investigation)
```

### 3. Provides Resume Prompt

Each handoff includes a copy-paste ready prompt for the next session.

## Handoff Types

### Context Exhaustion (`--reason context`)

**Output:**
- Summary of completed work
- In-progress items with current state
- Blocked items with reasons
- Compact resume prompt focused on continuation

### Debug Fork (`--reason debug-fork`)

**Output (TWO documents):**
1. **Main Handoff**: Continue main task, skip problematic area
2. **Debug Handoff**: Fresh investigation of specific issue

### Scope Exhaustion (`--reason scope`)

**Output:**
- Original scope (what was asked)
- Scope additions (what got added)
- Prioritized list: must-do, should-do, nice-to-do
- Focused handoff on must-do items only

## What To Do

When this skill is invoked:

1. **Parse arguments** for `--reason` flag (default: `context`)
2. **Gather session state:**
   - Read todos (if available)
   - Check git status/diff
   - Note key files worked on
   - Identify decisions made
3. **Generate handoff document(s):**
   - Use appropriate template for reason type
   - Write to `.handoffs/` (workspace) or `~/.claude/handoffs/` (global)
   - For debug-fork, create two documents
4. **Display summary:**
   - Show handoff location(s)
   - Display resume prompt

## Related Skills

- `/session-done` - Normal session wrap-up (work complete)
