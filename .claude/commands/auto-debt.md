---
description: "Constrained auto-dev for small-scope tech debt"
argument-hint: "<linear-issue-id or description>"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Edit", "Agent", "AskUserQuestion", "Skill"]
---

# Auto Debt Pipeline

Constrained mode of `/auto-dev` for small-scope tech debt tickets.
Rejects tickets that exceed small-scope thresholds or touch forbidden areas.

**Arguments:** "$ARGUMENTS"

---

## Constraints Applied

| Constraint | Value |
|-----------|-------|
| `--scope-limit` | `small` (reject tickets >10 files or >500 lines) |
| `--branch-prefix` | `debt` |
| `--forbidden` | `migrations,auth,ci,shared-bases` |

## Execution

Run `/auto-dev` with the constraints above:

```
/auto-dev $ARGUMENTS --scope-limit small --branch-prefix debt --forbidden migrations,auth,ci,shared-bases
```

All stages, guard matrix, friction protocol, merge gating, and error recovery are handled by `/auto-dev`. See that command for full documentation.
