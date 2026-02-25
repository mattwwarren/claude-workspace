---
description: Wrap up work session with handoff generation and progress sync
argument-hint: [--plan <path>] [--issue <number>] [--sync] [--no-sync] [--completed <tasks>]
---

# Wrap Up Work Session

End your work session with automated progress tracking, handoff generation, and sync to GitHub Issues and/or Notion.

## Usage

```bash
# Auto-detect plan from marker
/session-done

# GitHub Issue integration
/session-done --issue 52                           # Post summary to GitHub Issue #52
/session-done --issue 52 --completed "Task 1,Task 2"  # Mark specific tasks complete

# Notion integration
/session-done --sync                               # Sync to Notion

# Combined
/session-done --issue 52 --sync                    # GitHub + Notion sync

# Legacy plan-based
/session-done --plan path/to/plan.md               # Specify plan explicitly
/session-done --no-sync                            # Skip all syncing
```

## What This Command Does

1. **Detects active plan** from todo marker, GitHub Issue context, or recent modifications
2. **Updates plan frontmatter** with session metadata:
   - Last session timestamp
   - Completed todos count
   - Current phase
   - Session summary
3. **Generates handoff document** in `.handoffs/session-YYYY-MM-DD-HHMM.md`
4. **Syncs progress** (optional):
   - **GitHub**: Posts session summary comment, updates task checkboxes, auto-closes if complete
   - **Notion**: Syncs plan metadata and progress to Notion database

## GitHub Issue Integration

When working on a GitHub Issue (via `/start-issue`):

```bash
/session-done --issue 52
```

**What happens:**
1. Posts a session summary comment to Issue #52
2. Updates task checkboxes based on completed todos
3. Auto-closes issue if all tasks complete
4. Updates plan frontmatter with GitHub sync metadata

**With explicit task completion:**
```bash
/session-done --issue 52 --completed "Task 1,Task 2,Task 3"
```

## Plan Marker System

Add a todo marker at session start for auto-detection:

```
📍 Working on: plan-name
```

**Supported formats:**
- `📍 Working on: plan-name`
- `Active plan: plan-name`
- `Plan: plan-name`

**Without marker:**
- Falls back to most recently modified plan (last 2 hours)
- May pick wrong plan if working on multiple

## Session Handoff

The command generates a handoff document containing:

- **Summary** - What was accomplished
- **Progress** - Percentage complete, todos done
- **Changes** - Git diff stats
- **Resumption prompt** - Copy-paste to resume work

## Options

| Option | Description |
|--------|-------------|
| `--issue <number>` | GitHub Issue number (posts summary, updates tasks, auto-closes) |
| `--completed <tasks>` | Comma-separated list of completed task names (with `--issue`) |
| `--sync` | Enable Notion sync (requires `NOTION_TOKEN`) |
| `--no-sync` | Disable all syncing (GitHub + Notion) |
| `--plan <path>` | Specify plan file explicitly (bypasses auto-detection) |

## Implementation

The command orchestrates:
- `$HOME/.claude/scripts/session_complete.py` - Update session metadata
- `$HOME/.claude/scripts/generate_handoff.py` - Create handoff document
- `$HOME/.claude/scripts/sync_github_issue.py` - Sync to GitHub Issues (optional)

## Related Commands

- `/start-issue` - Start work on a GitHub Issue
- `/start-phase` - Start work on a plan phase
- `/plan-to-issues` - Convert plan to GitHub Issues
