---
description: "Constrained auto-dev for small-scope tech debt"
argument-hint: "<linear-issue-id or description>"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Edit", "Agent", "AskUserQuestion", "Skill"]
---

# Auto Debt Pipeline

Constrained mode of `/auto-dev` for low-risk tech debt tickets.
Rejects tickets that touch forbidden areas. Since v4 there is no size threshold; the debt alias widens the forbidden set via `--forbidden`, whose tokens (`migrations`, `auth`, `ci`, `shared-bases`) resolve to the detectors defined in `auto-dev-plan.md` Step 1d.5.

**Arguments:** "$ARGUMENTS"

---

## Constraints Applied

| Constraint | Value |
|-----------|-------|
| `--scope-limit` | `small` (reject tickets classified Large — a forbidden-area touch) |
| `--branch-prefix` | `debt` |
| `--forbidden` | `migrations,auth,ci,shared-bases` |

## Execution

Run `/auto-dev` with the constraints above:

```
/auto-dev $ARGUMENTS --scope-limit small --branch-prefix debt --forbidden migrations,auth,ci,shared-bases
```

All stages, guard matrix, friction protocol, merge gating, and error recovery are handled by `/auto-dev`. See that command for full documentation.
