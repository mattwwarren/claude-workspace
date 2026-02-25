# /orchestrate-phase

Orchestrates the automated implementation workflow for a single phase.

## Usage

```bash
/orchestrate-phase <issue-number> [options]
```

## Options

| Option | Description |
|--------|-------------|
| `--resume` | Resume from last checkpoint |
| `--status` | Show current orchestration status |
| `--pause` | Pause orchestration (can resume later) |
| `--abort` | Abort and cleanup state |
| `--dry-run` | Preview without making changes |
| `--repo` | Repository in owner/repo format |
| `--debug` | Enable debug logging |

## Examples

```bash
# Start orchestration for issue #52
/orchestrate-phase 52

# Resume from last checkpoint
/orchestrate-phase 52 --resume

# Check status
/orchestrate-phase 52 --status

# Pause for manual work
/orchestrate-phase 52 --pause

# Abort and cleanup
/orchestrate-phase 52 --abort
```

## Workflow Stages

### 1. Pre-Validation
- Checks previous phases are complete
- Runs project validation (ruff, mypy, pytest)
- **Blocks** if any check fails

### 2. Pre-Review
- Spawns architecture-reviewer agent
- Reviews phase plan for architectural fit
- **Blocks** if review rejects the plan

### 3. Implementation
- Loads tasks from GitHub issue checklist
- Adds tasks to TodoWrite
- Executes tasks sequentially
- Updates issue checkboxes as tasks complete
- **Blocks** if implementation incomplete

### 4. Post-Review
- Runs `/review-standard` on changes
- Validates against issue requirements
- Spawns specialized reviewers if needed
- **Proceeds to fix cycle** if findings exist

### 5. Fix Cycles
- Addresses review findings
- 1-3 issues: fixes directly
- 5+ issues: spawns parallel fix agents
- Re-reviews after fixes
- **Blocks** if cycle limit (3) reached

### 6. Complete
- Presents results summary
- Shows tasks completed, files changed, review cycles
- Creates commit with standard format

## State Persistence

State is persisted in two locations:
1. **GitHub Issue** - As a hidden comment (survives session boundaries)
2. **Local file** - `~/.claude/orchestration/{issue}.json` (faster access)

## Resuming After Blocks

When orchestration blocks, it saves state. To resume:
1. Address the blocking issue
2. Run: `/orchestrate-phase <issue> --resume`

## Safeguards

| Safeguard | Default | Purpose |
|-----------|---------|---------|
| Max review cycles | 3 | Prevent infinite fix loops |
| Stage timeouts | 5m-2h | Prevent runaway stages |
| Stall detection | 30m | Detect stuck states |

## Integration

Works with existing workflow:
- **`/plan-to-issues`** - Creates issues that orchestration consumes
- **`/start-issue`** - Detects orchestration state and prompts to resume
- **`/review-standard`** - Called during post-review stage
- **`/session-done`** - Updates orchestration checkpoints
