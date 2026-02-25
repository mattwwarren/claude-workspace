---
description: Queue a tech debt item with optional priority
argument-hint: "<description>" [--priority N]
---

# Queue Tech Debt Item

Add a tech debt work item to the queue for the `debt` session to pick up.

## Instructions

### Step 1: Parse Arguments

- **description** (required): A clear, actionable description of the debt item
- **--priority N** (optional): Priority level, default 0

Priority guidance:
- `0` = Normal FIFO ordering (default)
- `1-5` = Elevated priority (important but not urgent)
- `10+` = Urgent (should be picked up next)

### Step 2: Determine Client

Read the client name from the `$CW_CLIENT` environment variable. If not set, ask the user.

### Step 3: Queue the Item

Run:

```bash
cw queue add <client> "<description>" --purpose debt --priority <N>
```

If no priority was specified, omit the `--priority` flag (defaults to 0).

### Step 4: Confirm

Report:
- The queued item ID
- The description
- The priority level
- Current queue depth: run `cw queue list <client> --purpose debt --status pending` and report count

## Examples

```bash
# Normal priority
/queue-debt "Fix ruff violations in session.py"

# Elevated priority
/queue-debt "Update type annotations in models.py" --priority 3

# Urgent
/queue-debt "Fix broken import in cli.py" --priority 10
```
