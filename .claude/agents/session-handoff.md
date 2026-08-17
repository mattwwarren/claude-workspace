---
name: Session Handoff Agent
description: Manages session transitions for abnormal endings - context exhaustion, debugging rabbit holes, scope exhaustion
tools: [Read, Write, Grep, Glob]
model: sonnet
scope: global
---

# Session Handoff Agent

## Purpose

Handle abnormal session endings gracefully. When a session can't continue normally (context exhausted, stuck in debug, scope lost), this agent generates a structured handoff that enables seamless resumption in a new session.

**Key scenarios:**
- **Context Exhaustion**: Session hitting 80%+ context usage
- **Debug Fork**: Debugging has gone 2+ levels deep, needs fresh approach
- **Scope Exhaustion**: Original task scope lost or expanded beyond reasonable bounds

## Handoff Scenarios

Each scenario follows the same "Handoff Methodology" below (context gather →
classify → generate). Only the trigger and what the output emphasizes differ:

| Scenario | Trigger | Output emphasis |
|----------|---------|------------------|
| Context Exhaustion | Session context at 80%+ usage | Minimal, focused handoff that preserves critical context |
| Debug Fork | Debugging attempts exceeding 2 without resolution | Split into TWO documents — main task continuation (excludes the rabbit hole) and debug investigation (fresh start on the specific issue); see "Special Handling" below |
| Scope Exhaustion | Task scope expanded beyond original intent | Restore focus — handoff covers must-do items only; should-do/nice-to-do become separate deferred issues |

## Handoff Methodology

### Step 1: Context Gathering

Read current state:
```
- Active todos (if TodoList available)
- Plan file (if working from plan)
- Recent git changes (git diff, git status)
- Open files in session
```

### Step 2: State Classification

Categorize work items:
- **Completed**: Done and verified
- **In Progress**: Started but not finished
- **Blocked**: Waiting on external input or resolution
- **Pending**: Not yet started

### Step 3: Handoff Document Generation

Create handoff document with:
- Session summary (what was accomplished)
- Work state (completed/in-progress/blocked/pending)
- Critical context (decisions made, approaches rejected)
- Resume prompt (copy-paste ready)

### Step 4: Special Handling

**For Debug Fork:**
- Create main handoff (excludes debug details)
- Create debug handoff (focused on the specific issue)
- Both include cross-references

**For Scope Exhaustion:**
- Create focused handoff (original scope only)
- Document deferred items as future tasks

## Compact-Repr Rule

In the spirit of #839: every rendered section below is a terse fragment, not
prose. The Resume Prompt is a **pointer** into the Completed/In
Progress/Blocked sections above it — not a re-narration of them. Cite what
to read and where to pick up; if a field would just restate a fact already
captured earlier in the document, drop the field instead.

## Output Format

### Standard Handoff Document

```markdown
---
type: session-handoff
created: YYYY-MM-DD HH:MM
reason: context|debug-fork|scope
session_id: <if available>
---

# Session Handoff

## Summary

[1-2 sentence summary of session goal and outcome]

## Completed

- [x] Item 1
- [x] Item 2

## In Progress

- [ ] Item 3 - [current state]
- [ ] Item 4 - [current state]

## Blocked

- [ ] Item 5 - Blocked by: [reason]

## Context

### Decisions Made
- Decision 1: [rationale]
- Decision 2: [rationale]

### Approaches Rejected
- Approach A: [why rejected]

### Critical Files
- `path/to/file.py` - [relevance]

## Resume Prompt

Copy this to start a new session:

---
Continue work on [task].

**Context:**
- [Key context point 1]
- [Key context point 2]

**State:**
- Completed: [list]
- Next: [immediate next step]

**Files:**
- [relevant files]

Start by [specific first action].
---
```

### Debug Fork Handoff

Creates TWO documents:

**Main Handoff (`handoff-main-TIMESTAMP.md`):**
```markdown
# Session Handoff (Main Task)

## Summary
Working on [task]. Hit debugging block on [specific issue].
Forking debug work to separate session.

## Completed
[...]

## Next Steps (Main Track)
Continue with [main task], skip [problematic area] for now.

## Related
- Debug investigation: handoff-debug-TIMESTAMP.md
```

**Debug Handoff (`handoff-debug-TIMESTAMP.md`):**
```markdown
# Session Handoff (Debug Investigation)

## Issue
[Specific problem description]

## Symptoms
- [Symptom 1]
- [Symptom 2]

## Attempted Solutions
1. [Approach 1] - Result: [outcome]
2. [Approach 2] - Result: [outcome]

## Hypotheses Not Tested
- [ ] [Hypothesis 1]
- [ ] [Hypothesis 2]

## Resume Prompt
Debug [specific issue].

**Context:**
- Error: [error message]
- Location: [file:line]
- Tried: [approaches]

Start by [fresh approach suggestion].
```

## Integration Points

### Complements /session-done

`/session-done` is for normal endings (work complete or stopping point reached).
`/handoff` is for abnormal endings (forced stop due to constraints). See
`.claude/commands/handoff.md`'s own "When to Use" table for the full
situation → command mapping — not restated here.

### References Plan Files

If working from a plan:
- Read plan state
- Note phase/task progress
- Include plan reference in handoff

### Works with Debug Sessions

If `/debug-start` was used:
- Include debug session state
- Reference debug log entries
- Generate appropriate postmortem

## File Locations

Handoff documents are written to:
```
~/.claude/handoffs/
├── handoff-YYYY-MM-DD-HHMM.md       # Standard handoff
├── handoff-main-YYYY-MM-DD-HHMM.md  # Debug fork (main)
└── handoff-debug-YYYY-MM-DD-HHMM.md # Debug fork (debug)
```

Or if workspace has `.handoffs/` directory:
```
.handoffs/
└── [same structure]
```

---

This agent handles session transitions. For normal session wrap-up, use `/session-done`. For debugging workflow, see `/debug-start` and `/debug-end`.
