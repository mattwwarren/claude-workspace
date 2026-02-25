---
description: Queue an approved plan for implementation via the cw task queue
argument-hint: [--plan <path>]
---

# Queue Plan for Implementation

Queue an approved plan to the `impl` session's task queue so it can be picked up by `/pull-and-execute`.

## Instructions

### Step 1: Identify the Active Plan

If `--plan <path>` was provided, use that path directly.

Otherwise, find the active plan:

1. Check your todo markers for `📍 Working on: <plan-name>`
2. If no marker, search for recently modified plan files:
   ```bash
   ls -lt ~/.claude/plans/*.md 2>/dev/null | head -5
   ```
   Also check the workspace:
   ```bash
   ls -lt .claude/plans/*.md 2>/dev/null | head -5
   ```
3. If multiple candidates exist, ask the user which plan to queue.

### Step 2: Read and Extract Plan Details

Read the plan file. Extract:

- **Title**: The H1 heading (e.g., `# My Feature Plan`)
- **First incomplete phase**: The first H2 section that has unchecked `- [ ]` tasks
- **Plan path**: Absolute path to the plan file

### Step 3: Determine Client

Read the client name from the `$CW_CLIENT` environment variable. If not set, ask the user.

### Step 4: Queue the Item

Run:

```bash
cw queue add <client> "<plan title>" --purpose impl --prompt "Execute plan at <absolute-path>. Start with phase: <first incomplete phase name>. Read the plan file for full context, then use /pull-and-execute to begin work."
```

### Step 5: Confirm

Report:
- The queued item ID
- The plan title
- Which phase will be executed first
- Remind the user: "The impl session can pick this up with `/pull-and-execute`"

## Examples

```bash
# Auto-detect plan
/queue-plan

# Specify plan explicitly
/queue-plan --plan ~/.claude/plans/auth-system/main.md
```
