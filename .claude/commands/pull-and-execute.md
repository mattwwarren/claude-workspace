---
description: Pull next queue item, spawn agent teams, review, and complete
argument-hint: [--id <item_id>] [--purpose impl|debt]
---

# Pull and Execute Queue Item

Claim the next work item from the queue, decompose it, spawn implementation agents, review the results, and complete the item. This is the execution engine for queued work.

## Arguments

- **--id <item_id>** (optional): Claim a specific item by ID instead of next in queue
- **--purpose impl|debt** (optional): Filter queue by purpose (default: auto-detect from session)

## Phase A: Claim

Determine the client from `$CW_CLIENT` environment variable.

Determine purpose: use `--purpose` if provided, otherwise use the current session's purpose (check `$CW_PURPOSE` or infer from session context — impl sessions pull impl items, debt sessions pull debt items).

Claim the item:

```bash
# Specific item
cw queue claim <client> --id <item_id> --json

# Next by purpose
cw queue claim <client> --purpose <purpose> --json
```

Parse the JSON output to get the full `QueueItem` with:
- `id` — item ID for completion
- `task.description` — what to do
- `task.prompt` — detailed instructions
- `task.context_files` — files to read first
- `task.success_criteria` — how to verify
- `task.priority` — urgency level

If no item is available, report "Queue is empty for purpose=<purpose>" and stop.

## Phase B: Plan

1. **Read context**: If `task.context_files` is non-empty, read each file to understand the scope.
2. **Read prompt**: The `task.prompt` contains the primary instructions. If it references a plan file, read that plan file.
3. **Assess scope**:
   - **Small**: Single file change, simple fix, < 50 lines. No decomposition needed.
   - **Medium**: 2-5 files, moderate complexity. May benefit from 1-2 parallel agents.
   - **Large**: 5+ files, complex feature, or multi-phase plan. Decompose into subtasks.
4. **Decompose** (medium/large only): Break into independent subtasks that can be parallelized.

## Phase C: Implementation Agent Team

For **small** scope: Do the work directly in this session. No agents needed.

For **medium/large** scope: Spawn Task agents (max 4 concurrent, `run_in_background: true`).

Each agent prompt should include:
- The subtask description and specific files
- Context from `task.context_files`
- Success criteria
- Instruction: "Run `ruff check` and `ruff format` on modified files. Run relevant pytest tests."
- Instruction: "For file operations, use Read/Write tools instead of Bash."

Wait for all agents to complete. Collect their results.

**Important**: Do NOT run mypy in parallel agents. It will be run serially in the review phase.

## Phase D: Review

After implementation completes, run quality gates serially:
```bash
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/ -x
```

Collect review findings categorized as HIGH, MEDIUM, or LOW severity.

## Phase E: Review Loop (max 2 iterations)

If there are **HIGH or MEDIUM** findings:
1. Fix the issues
2. Re-run quality gates
3. If fixes fail after 2 iterations, escalate to user

For **LOW** severity findings only:
- Queue them as debt items:
  ```bash
  cw queue add <client> "<finding description>" --purpose debt
  ```

## Phase F: Complete

1. **Final quality gates** (run serially)
2. **Git commit**: Stage and commit the changes
3. **Mark complete**:
   ```bash
   cw queue complete <client> <item_id> --result "<summary of what was done>"
   ```
4. **Report**: What was accomplished, files changed, quality gate results
5. **Offer to continue**: If more pending items exist, ask to continue

## Error Handling

If implementation fails catastrophically:
```bash
cw queue fail <client> <item_id> --error "<description of failure>"
```

## Examples

```bash
# Pull next impl item
/pull-and-execute --purpose impl

# Pull next debt item
/pull-and-execute --purpose debt

# Execute a specific item
/pull-and-execute --id abc12345

# Auto-detect purpose from session
/pull-and-execute
```
